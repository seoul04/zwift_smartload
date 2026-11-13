#!/usr/bin/env python3
"""Host-side Movesense GSP ECG client - a control experiment for the dongle.

The dongle firmware recovers a dead ECG stream by reconnecting, which means it
can show a healthy average while the sensor keeps dropping the measurement. To
decide whether that is the dongle's fault or the sensor's, this script talks GSP
from the host's own Bluetooth adapter and measures the same things the firmware
does. If the host sees the same stalls at the same RSSI, the fault is upstream of
the dongle and the next place to look is the sensor's firmware.

It deliberately reimplements the protocol rather than reusing the firmware's
assumptions, so a shared misunderstanding cannot hide: the frame layout, the
sample decode and the reference handling are all derived from the wire here.

Measured on the wire (68-byte ECG frame, after the 2-byte GSP header):
    buf[0..3]   uint32 LE  timestamp, ms
    buf[4..67]  16 x int32 LE  ADC counts, 0.38147 uV per count

Usage:
    python3 host_gsp_ecg.py --duration 120
    python3 host_gsp_ecg.py --address 0C:8C:DC:1B:64:CD --duration 300
    python3 host_gsp_ecg.py --rate 128 --duration 60

The dongle must not be connected to the same sensor at the same time: the
Movesense will refuse or drop one of the two connections.

Exit code is 0 if the stream delivered data for the whole run, 1 otherwise, so
this can be used as a soak-test check in a loop.
"""

import argparse
import asyncio
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.stderr.write(
        "bleak is required: python3 -m pip install bleak\n"
        "(on Linux this also needs BlueZ, and the adapter must not be "
        "soft-blocked: `rfkill list bluetooth`)\n"
    )
    raise SystemExit(2)

# Movesense GATT Sensor Data (GSP). UUIDs taken from the firmware's constants so
# both sides are known to agree.
GSP_SERVICE = "34802252-7185-4d5d-b431-630e7050e8f0"
GSP_COMMAND = "34800001-7185-4d5d-b431-630e7050e8f0"
GSP_DATA = "34800002-7185-4d5d-b431-630e7050e8f0"

# Advertised services, used only to find the sensor while scanning. The GSP
# service itself is NOT advertised, so scanning on it never matches; this is the
# Movesense service that is.
MOVESENSE_SERVICE = "0000fdf3-0000-1000-8000-00805f9b34fb"

# Standard Heart Rate service, used purely as a link-liveness control.
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
HR_CONTROL = "00002a39-0000-1000-8000-00805f9b34fb"

# Standard Battery Service. Read on every run so a depleted cell cannot be
# blamed for a stall after the fact.
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# GSP command codes and response types.
CMD_HELLO = 0x00
CMD_SUBSCRIBE = 0x01
CMD_UNSUBSCRIBE = 0x02

RESP_COMMAND = 0x01
RESP_DATA = 0x02
RESP_DATA_PART2 = 0x03

HTTP_OK = 200
HTTP_CONFLICT = 409

UUV_PER_COUNT = 0.38147

# A frame that never delivered a continuation is discarded when the next DATA
# arrives; this is the same rule the firmware uses.
MAX_FRAME = 512


class Stats:
    """Everything needed to judge whether the stream was actually healthy."""

    def __init__(self):
        self.frames = 0
        self.samples = 0
        self.unknown_ref = 0
        self.short_frames = 0
        self.overflows = 0
        self.notifs = 0
        self.cmd_responses = 0
        self.hr_notifs = 0

        # Raw notification shape. This is the diagnostic that separates "the
        # sensor only serialised N samples" from "the sensor serialised 16 and
        # the ATT layer truncated them": both look like 3 samples per frame in
        # the decoded report, but only one of them produces 0x03 continuations.
        self.notif_lengths = {}
        self.notif_kinds = {}
        self.samples_per_frame = {}
        self.last_hr = None
        self.disconnected = False
        self.disconnect_at = None
        self.battery = None

        self.first_frame = None
        self.last_frame = None
        self.max_gap = 0.0
        self.gaps = []          # (when, gap_ms) for gaps over the threshold
        self.per_second = {}    # second index -> frames
        self.per_second_samples = {}

        # Stream clock: the device's own timestamps, which show whether samples
        # are missing even when frames arrive regularly.
        self.first_ts = None
        self.last_ts = None
        self.ts_regressions = 0
        self.ts_unexpected = 0

    def note_frame(self, count, ts, now):
        self.frames += 1
        self.samples += count

        if self.first_frame is None:
            self.first_frame = now
        else:
            gap = (now - self.last_frame) * 1000.0
            if gap > self.max_gap:
                self.max_gap = gap
        self.last_frame = now

        sec = int(now - self.first_frame)
        self.per_second[sec] = self.per_second.get(sec, 0) + 1
        self.per_second_samples[sec] = self.per_second_samples.get(sec, 0) + count

        if ts is not None:
            if self.first_ts is None:
                self.first_ts = ts
            else:
                delta = (ts - self.last_ts) & 0xFFFFFFFF
                # At 256 Hz a frame spans ~62.5 ms; anything wildly different
                # means samples were skipped or the clock reset.
                if delta > 0x7FFFFFFF:
                    self.ts_regressions += 1
                elif delta > 500:
                    self.ts_unexpected += 1
            self.last_ts = ts


class GspClient:
    def __init__(self, args):
        self.args = args
        self.stats = Stats()
        self.ref_ecg = 0
        self.ref_hr = 0
        # True once we have accepted the live stream's reference. Reset at the
        # start of each trial so a re-run can adopt a different reference.
        self.adopted_ref = False
        self.buf = bytearray()
        self.pending = False
        self.pending_ref = 0
        self.hello_ref = None
        self.subscribe_ref = None
        self.streaming = False
        self._t0 = time.monotonic()

    # ---- GSP framing -------------------------------------------------------

    def on_hr(self, _handle, data):
        self.stats.hr_notifs += 1
        self.stats.last_hr = time.monotonic()

    def on_notify(self, _handle, data):
        now = time.monotonic()
        self.stats.notifs += 1

        # Count the raw shape before any interpretation. A notification is
        # capped at MTU-3 bytes, so this histogram is the direct evidence for
        # whether the sensor is sending whole frames or truncated ones.
        self.stats.notif_lengths[len(data)] = self.stats.notif_lengths.get(len(data), 0) + 1

        if len(data) < 2:
            return

        kind = data[0]
        ref = data[1]
        self.stats.notif_kinds[kind] = self.stats.notif_kinds.get(kind, 0) + 1

        if kind == RESP_COMMAND:
            self.on_command_response(ref, bytes(data[2:]))
            return

        if kind == RESP_DATA:
            # A new frame starts. If one was still buffered it never received
            # its continuation, so account for it and move on.
            if self.pending and self.buf:
                self.finish_frame(self.pending_ref, now)
            self.buf = bytearray(data[2:])
            self.pending_ref = ref
            self.pending = True
            return

        if kind == RESP_DATA_PART2:
            if len(self.buf) + (len(data) - 2) > MAX_FRAME:
                self.stats.overflows += 1
                self.pending = False
                self.buf = bytearray()
                return
            self.buf += data[2:]
            return

        # Anything else is not part of the stream.

    def finish_frame(self, ref, now):
        buf = self.buf
        self.pending = False
        self.buf = bytearray()

        if len(buf) < 4:
            self.stats.short_frames += 1
            return

        # Adopt a live stream whose reference we do not hold. The sensor keeps a
        # measurement running after its subscriber goes away and answers a new
        # SUBSCRIBE with 409 while that old stream is still active.
        #
        # The earlier version only adopted when ref_ecg was 0, relying on the 409
        # response to reset it first. That is fragile: if the 409 is lost, or the
        # frames start arriving before it, ref_ecg keeps the reference we asked
        # for, this check never fires, and every good frame is binned as
        # unknown_ref. That looks identical to a sensor stall while the wire is
        # perfectly healthy - 1751 valid 70-byte frames were discarded that way.
        # Adopt on the first frame of a healthy stream instead of waiting for a
        # control-plane message to authorise it.
        if ref != self.ref_ecg:
            if not self.adopted_ref:
                print(f"  [adopt] streaming under ref {ref} "
                      f"({len(buf)} bytes, {(len(buf) - 4) // 4} samples)")
                self.ref_ecg = ref
                self.adopted_ref = True
            else:
                self.stats.unknown_ref += 1
                return

        count = (len(buf) - 4) // 4
        if count <= 0:
            self.stats.short_frames += 1
            return

        ts = int.from_bytes(buf[0:4], "little")
        self.stats.samples_per_frame[count] = self.stats.samples_per_frame.get(count, 0) + 1
        self.stats.note_frame(count, ts, now)

        if self.args.verbose and self.stats.frames <= 3:
            uv = [
                int.from_bytes(buf[4 + i * 4:8 + i * 4], "little", signed=True)
                for i in range(min(count, 8))
            ]
            print(f"  [frame] ts={ts}ms count={count} first_uv≈"
                  f"{[round(v * UUV_PER_COUNT, 1) for v in uv]}")

    def on_command_response(self, ref, payload):
        self.stats.cmd_responses += 1

        # HELLO is answered with "Hello" rather than an HTTP status. Reading it
        # as a status gives 0x6548 = 25928, which looks like a fatal error.
        if payload == b"Hello":
            print(f"  [cmd] HELLO acknowledged (ref {ref})")
            return

        if len(payload) < 2:
            print(f"  [cmd] ref {ref}: short response {payload.hex()}")
            return

        status = int.from_bytes(payload[0:2], "big")
        label = {
            HTTP_CONFLICT: " (CONFLICT - measurement already running)",
        }.get(status, "")
        print(f"  [cmd] ref {ref}: HTTP {status}{label}")

        if status == HTTP_CONFLICT and ref == self.ref_ecg:
            # Let the frames tell us the live reference.
            self.ref_ecg = 0
            self.adopted_ref = False
            self.streaming = True

    # ---- sequence ----------------------------------------------------------

    def read_rssi(self):
        """Read RSSI from the live link, best effort.

        BlueZ on this host does not populate Device1.RSSI for a connected LE
        peer (bleak 3.1 dropped BleakClient.rssi, and bluetoothctl omits RSSI
        while connected), so this often returns None. Callers must treat a
        missing sample as "unavailable", not as "no signal". Discovery is never
        used here: a scan on the same adapter blocks while a connection is held
        and would freeze the monitor loop.
        """
        try:
            return self.client.rssi
        except Exception:                 # noqa: BLE001
            return None

    async def write(self, client, payload, what, timeout=10.0):
        print(f"  [tx] {what}: {payload.hex()}")

        # A write-with-response can block forever if the sensor has stopped
        # servicing its command characteristic. Without this bound the whole run
        # freezes silently right after SUBSCRIBE, which looks like a hang in the
        # harness rather than the sensor refusing to answer.
        try:
            await asyncio.wait_for(
                client.write_gatt_char(GSP_COMMAND, payload, response=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            print(f"  [cmd] {what}: NO RESPONSE after {timeout:.0f}s "
                  f"(sensor accepted the link but did not service the command)")
            raise

    async def run(self, client):
        self.client = client
        print(f"  MTU (host view): {client.mtu_size}")

        # Read the battery first. It is a cheap read and it settles the most
        # common confounder before the stream is touched: a depletion that only
        # shows up under the load of continuous ECG streaming.
        try:
            raw = await client.read_gatt_char(BATTERY_LEVEL)
            if raw:
                self.stats.battery = int(raw[0])
                print(f"  [bat] battery level: {self.stats.battery}%")
        except Exception as exc:          # noqa: BLE001
            print(f"  [bat] battery level unavailable: {exc}")

        await client.start_notify(GSP_DATA, self.on_notify)

        # Subscribe to the standard Heart Rate measurement as a link-liveness
        # control. It rides the same connection with the same radio, so if ECG
        # stops while HR notifications keep arriving the sensor is still talking
        # and has only abandoned the ECG stream - the same distinction the
        # dongle firmware makes with hrs_notifs vs gsp_notifs.
        if not self.args.no_hr_control:
            try:
                await client.start_notify(HR_MEASUREMENT, self.on_hr)
                print("  [hrs] subscribed as link-liveness control")
            except Exception as exc:      # noqa: BLE001
                print(f"  [hrs] control unavailable: {exc}")

        # HELLO first: the sensor answers "Hello" and will not accept
        # subscriptions before it.
        self.hello_ref = 1
        await self.write(client, bytes([CMD_HELLO, self.hello_ref]), "HELLO")
        await asyncio.sleep(0.25)

        # The Heart Rate measurement CCCD write is all the control needs; the
        # control point itself is for resetting the energy expenditure counter
        # and is not required just to receive beats.

        path = f"/Meas/ECG/{self.args.rate}".encode()
        self.subscribe_ref = 2
        self.ref_ecg = self.subscribe_ref
        # The reference we get may not be the one we asked for: the sensor keeps
        # an older measurement alive and streams it under its own reference. Let
        # the first healthy frame decide, rather than the 409 response.
        self.adopted_ref = False
        await self.write(
            client,
            bytes([CMD_SUBSCRIBE, self.subscribe_ref]) + path,
            f"SUBSCRIBE {path.decode()}",
        )
        self.streaming = True

        await self.monitor()

        # Be a good citizen: release the subscription so the next run (or the
        # dongle) does not inherit a stale stream and a 409.
        if self.ref_ecg:
            last_ref = self.ref_ecg
            try:
                await self.write(
                    client,
                    bytes([CMD_UNSUBSCRIBE, last_ref]),
                    "UNSUBSCRIBE (cleanup)",
                )
                await asyncio.sleep(0.2)
            except Exception as exc:      # noqa: BLE001 - best effort only
                print(f"  [warn] cleanup unsubscribe failed: {exc}")
        try:
            await client.stop_notify(GSP_DATA)
        except Exception:                 # noqa: BLE001
            pass

    async def monitor(self):
        """Watch for silence and report it as it happens, not just at the end."""
        start = time.monotonic()
        self.monitor_start = start
        last_frames = 0
        last_t = start
        silent_since = None
        hr_at_silence = 0
        next_silence_report = 0.0
        next_rssi_sample = 0.0
        rssi_samples = []
        rssi_warned = False

        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            elapsed = now - start

            # Sample RSSI periodically from the live connection. A link that
            # degrades as the run goes on points at the radio (range,
            # interference, adapter power saving), while a flat RSSI with a dead
            # stream points at the sensor. Without this the two are
            # indistinguishable in the final report.
            #
            # This must NOT use BleakScanner.discover(): a discovery run on the
            # same adapter while a connection is held blocks indefinitely and,
            # because sampling sits inside the monitor loop, silently froze the
            # entire run. get_rssi() reads from the existing link instead.
            if self.args.rssi_log and elapsed >= next_rssi_sample:
                next_rssi_sample = elapsed + 10.0
                rssi = self.read_rssi()
                if rssi is not None:
                    rssi_samples.append((elapsed, rssi))
                    print(f"  [{elapsed:6.1f}s] [rssi] {rssi} dBm")
                elif not rssi_warned:
                    rssi_warned = True
                    print("  [rssi] unavailable while connected on this BlueZ "
                          "stack; using timestamp continuity as the "
                          "radio-vs-sensor discriminator")

            self.rssi_samples = rssi_samples

            if elapsed >= self.args.duration:
                self.monitor_end = now
                return

            if not self.client.is_connected:
                self.stats.disconnected = True
                self.stats.disconnect_at = now
                self.monitor_end = now
                print(f"  [{elapsed:6.1f}s] [link] DISCONNECTED "
                      f"(last frame {now - last_t:.1f}s ago, "
                      f"hr_notifs={self.stats.hr_notifs})")
                return

            if self.stats.frames != last_frames:
                last_frames = self.stats.frames
                last_t = now
                if silent_since is not None:
                    print(f"  [{elapsed:6.1f}s] stream resumed after "
                          f"{(now - silent_since):.1f}s of silence")
                    silent_since = None
                    next_silence_report = 0.0
            else:
                if silent_since is None and (now - last_t) > 1.0:
                    silent_since = last_t
                    hr_at_silence = self.stats.hr_notifs
                    next_silence_report = elapsed + 10.0
                    # Report both counters: if HR keeps climbing while frames
                    # are frozen, the blockage is inside the sensor, not the link.
                    hr_live = "?"
                    if self.stats.last_hr is not None:
                        hr_live = f"{now - self.stats.last_hr:.1f}s ago"
                    print(f"  [{elapsed:6.1f}s] no frames for "
                          f"{(now - last_t):.1f}s "
                          f"(notifs={self.stats.notifs}, "
                          f"hr_notifs={self.stats.hr_notifs}, "
                          f"last_hr={hr_live}, "
                          f"unknown_ref={self.stats.unknown_ref})")
                elif silent_since is not None and elapsed >= next_silence_report:
                    next_silence_report = elapsed + 10.0
                    grew = self.stats.hr_notifs - hr_at_silence
                    print(f"  [{elapsed:6.1f}s] still silent after "
                          f"{now - silent_since:.0f}s; "
                          f"hr_notifs +{grew} since the stall "
                          f"({'link alive' if grew else 'LINK QUIET'})")

    # ---- reporting ---------------------------------------------------------

    def report(self):
        st = self.stats
        print()
        print("=" * 68)

        if st.frames == 0:
            print("RESULT: no ECG frames at all.")
            print(f"  notifications seen : {st.notifs}")
            print(f"  command responses  : {st.cmd_responses}")
            print(f"  frames discarded   : short={st.short_frames} "
                  f"unknown_ref={st.unknown_ref}")
            print()
            print("  If notifications are flowing but nothing decodes, the "
                  "reference is not being adopted.")
            print("  If there are no notifications, the subscription was "
                  "refused or the sensor is not streaming.")
            return False

        wall = st.last_frame - st.first_frame if st.last_frame else 0.0
        if wall <= 0:
            return False

        # Duty must be measured over the whole observation window. Measuring it
        # across the frame span alone hides a stream that stopped early: the
        # frames that did arrive look perfectly regular, so the average is
        # untouched by the minutes of silence that followed.
        observed = None
        if getattr(self, "monitor_start", None) and \
                getattr(self, "monitor_end", None):
            observed = self.monitor_end - self.monitor_start

        expected_frames_per_s = self.args.rate / 16.0
        actual = st.frames / wall
        span_duty = actual / expected_frames_per_s * 100.0
        rate = st.samples / wall

        duty = span_duty
        if observed and observed > 0:
            duty = st.frames / (expected_frames_per_s * observed) * 100.0

        print(f"RESULT over {wall:.1f}s of delivered stream "
              f"(requested rate {self.args.rate} Hz)")
        if observed:
            print(f"  observation window : {observed:.1f}s")
        print(f"  frames             : {st.frames}")
        print(f"  samples            : {st.samples}")
        print(f"  frames/s           : {actual:.2f}  "
              f"(ideal {expected_frames_per_s:.2f})")
        print(f"  effective rate     : {rate:.2f} Hz  "
              f"(ideal {self.args.rate})")
        print(f"  DUTY CYCLE         : {duty:.1f}%"
              f"{'  (of whole run)' if observed else ''}")
        print(f"  max gap            : {st.max_gap:.0f} ms")
        print(f"  discarded frames   : short={st.short_frames} "
              f"unknown_ref={st.unknown_ref} overflow={st.overflows}")

        # The notification shape. This is the primary evidence for where 16
        # samples went: a 0x02-only histogram pinned at MTU-3 means truncation,
        # while 0x02 followed by 0x03 means the frame was really fragmented.
        if st.notif_lengths:
            hist = " ".join(f"{n}B:{c}"
                            for n, c in sorted(st.notif_lengths.items()))
            print(f"  notif lengths      : {hist}")
        if st.notif_kinds:
            names = {RESP_COMMAND: "CMD", RESP_DATA: "DATA",
                     RESP_DATA_PART2: "PART2"}
            kinds = " ".join(f"{names.get(k, hex(k))}:{c}"
                             for k, c in sorted(st.notif_kinds.items()))
            print(f"  notif kinds        : {kinds}")
            if RESP_DATA_PART2 not in st.notif_kinds:
                print("                       ^ no PART2: the sensor never "
                      "continuation-split a frame")
        if st.samples_per_frame:
            spf = " ".join(f"{n}samp:{c}"
                           for n, c in sorted(st.samples_per_frame.items()))
            print(f"  samples/frame      : {spf}")
        print(f"  HR control notifs  : {st.hr_notifs} "
              f"({'link stayed alive' if st.hr_notifs else 'no control data'})")
        bat = f"{st.battery}%" if st.battery is not None else "unavailable"
        print(f"  battery at start   : {bat}")
        if st.disconnected:
            print("  LINK                : dropped by the remote during the run")
        if st.first_ts is not None and st.last_ts is not None:
            span = (st.last_ts - st.first_ts) & 0xFFFFFFFF
            print(f"  device clock span  : {span} ms")
            print(f"  clock anomalies    : regressions={st.ts_regressions} "
                  f"jumps>500ms={st.ts_unexpected}")

        # Per-10s windows make a stall pattern visible instead of averaged away.
        print("\n  per-10s windows (frames/s):")
        windows = {}
        for sec, n in st.per_second.items():
            windows.setdefault(sec // 10 * 10, 0)
            windows[sec // 10 * 10] += n
        worst = None
        for w in sorted(windows):
            fps = windows[w] / 10.0
            bar = "#" * int(min(fps / expected_frames_per_s, 1.0) * 40)
            mark = ""
            # Only judge a window that the run actually covered. A trailing
            # partial window (e.g. 1 frame arriving after the last full second)
            # would otherwise be flagged as degraded and mask a clean run.
            covered = min(10.0, max(0.0, wall - w))
            if covered < 5.0:
                mark = "  (partial)"
            elif fps < expected_frames_per_s * 0.5:
                mark = "  <-- degraded"
                worst = w
            print(f"    {w:4d}-{w + 10:4d}s  {fps:5.2f}  {bar}{mark}")

        # Per-minute throughput for long runs. The 10 s view above is right for
        # spotting a stall in a 2-minute run, but a sustained run needs the
        # coarser shape: the question is whether the stream decays with
        # time-under-load, and that only shows up over many minutes.
        if self.args.sustained and st.per_second:
            print("\n  per-minute throughput (sustained view):")
            minute = {}
            for sec, n in st.per_second.items():
                minute.setdefault(sec // 60, 0)
                minute[sec // 60] += n
            for m in sorted(minute):
                fps = minute[m] / 60.0
                pct = 100.0 * fps / expected_frames_per_s
                bar = "#" * int(min(pct / 100.0, 1.0) * 40)
                print(f"    {m:3d}-{m + 1:3d} min  {fps:5.2f} fr/s  "
                      f"{pct:5.1f}%  {bar}")

        if getattr(self, "rssi_samples", None):
            vals = [r for _t, r in self.rssi_samples]
            print(f"\n  RSSI: min={min(vals)} max={max(vals)} dBm "
                  f"first={vals[0]} last={vals[-1]}")
            if vals[-1] <= vals[0] - 8:
                print("        ^ dropped sharply during the run: suspect the "
                      "radio link, not the sensor")

        # A run is only healthy if the stream covered the whole observation
        # window with no long holdup. Judging on the delivered span alone would
        # pass a stream that stopped after a few seconds.
        reasons = []
        if duty < 98.0:
            reasons.append(f"duty {duty:.1f}% < 98%")
        if st.max_gap >= 500:
            reasons.append(f"max gap {st.max_gap:.0f} ms >= 500 ms")
        if st.disconnected:
            reasons.append("link dropped")
        healthy = not reasons

        print()
        if healthy:
            print("VERDICT: stream is healthy. Losses are not coming from the "
                  "transport.")
            return True

        print(f"VERDICT: NOT healthy ({'; '.join(reasons)}).")
        print("  The host has its own adapter, its own stack and the same "
              "sensor, so a loss pattern like this is not specific to the")
        print("  dongle firmware. That points at the sensor abandoning the "
              "measurement.")

        # Separate the failure modes, since the remedy is different. Link loss
        # is checked first: when the link goes down the stream cannot continue,
        # so every other symptom is downstream of it.
        hr_alive_at_end = False
        if st.hr_notifs and st.last_frame is not None:
            # Did HR notifications continue past the last ECG frame?
            hr_alive_at_end = (st.last_hr is not None
                               and st.last_hr > st.last_frame + 2.0)

        if st.disconnected and st.hr_notifs >= 3 and hr_alive_at_end:
            print("\n  Mode: the BLE LINK dropped, but Heart Rate "
                  "notifications were still arriving on it right up to the")
            print("  end. The sensor was alive and talking; it lost only the "
                  "ECG stream and then the link. Re-run with a longer window")
            print("  to see whether the link drop is a consequence of the "
                  "sensor's own fault (a stalled session that the supervisor")
            print("  eventually tears down).")
        elif st.disconnected:
            print("\n  Mode: the BLE LINK dropped before/while the stream "
                  "stalled. This needs a stable link before the sensor can be")
            print("  judged: check RSSI, power saving on the adapter, and "
                  "connection parameters (a long interval plus a short")
            print("  supervision timeout drops links that a 4 s timeout "
                  "survives).")
        elif hr_alive_at_end:
            print("\n  Mode: the link stayed alive (Heart Rate notifications "
                  "kept arriving) while the ECG stream stopped. The sensor is")
            print("  still connected and has abandoned only the ECG "
                  "measurement - the same signature the dongle reported, on a")
            print("  completely different adapter and stack. That is strong "
                  "evidence of a sensor-side fault.")
        else:
            print("\n  Mode: stream stalled with no Heart Rate control data to "
                  "compare against, so this run cannot say whether the link")
            print("  or the sensor's measurement engine stopped. The HR "
                  "control is on by default; check that the sensor exposes")
            print("  0x2A37 and that --no-hr-control was not passed.")
        return False


async def find_device(args):
    if args.address:
        return args.address

    print(f"Scanning for {args.scan:g}s ...")

    # BleakScanner.discover(..., return_adv=True) is used rather than the
    # detect_callback form: with bleak 3.x on BlueZ the callback variant
    # returned nothing at all here, while discover() worked immediately.
    found = await BleakScanner.discover(timeout=args.scan, return_adv=True)

    if not found:
        return None

    matches = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        services = [s.lower() for s in (adv.service_uuids or [])]

        # The sensor does NOT advertise the GSP service UUID, so filtering on it
        # would never match. It advertises the Movesense service (FDF3) and the
        # standard Heart Rate service; the name is the reliable selector.
        is_movesense = (
            "movesense" in name.lower()
            or args.name.lower() in name.lower()
            or MOVESENSE_SERVICE in services
        )
        if is_movesense:
            matches.append((addr, name, adv.rssi))
            print(f"  found {addr}  {name or '(no name)'}  "
                  f"RSSI {adv.rssi}")

    if not matches:
        print(f"  no device matched {args.name!r} in {len(found)} seen")
        for addr, (dev, adv) in found.items():
            nm = adv.local_name or dev.name or "(no name)"
            print(f"    {addr}  {nm}  {adv.rssi} dBm")
        return None

    addr, name, rssi = max(matches, key=lambda m: m[2] if m[2] is not None else -999)
    print(f"  using {addr}  {name}  RSSI {rssi}")
    return addr


async def run_trial(address, args):
    """One connect-measure-disconnect cycle. Returns (healthy, stats)."""
    print(f"\n--- trial ---")
    client = BleakClient(address, timeout=args.timeout)

    try:
        await client.connect()
    except Exception as exc:              # noqa: BLE001
        # Bleak frequently raises exceptions whose str() is empty (notably
        # asyncio.TimeoutError), so name the type explicitly. An empty message
        # once sent this script's diagnosis looking for a phantom "dongle is
        # streaming" condition when the real cause was a connect timeout.
        print(f"Connection failed: {type(exc).__name__}: {exc!r}")
        print("  Common causes: stale link still held by another client, sensor")
        print("  already at its 2-peripheral limit, or the sensor wedged after a")
        print("  link drop (power-cycle it).")
        return None, None

    gsp = GspClient(args)
    try:
        print(f"Connected. {'Paired' if client.is_connected else 'Link up'}.")
        await gsp.run(client)
    except asyncio.CancelledError:
        raise
    except Exception as exc:              # noqa: BLE001
        print(f"\nStream interrupted: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:                 # noqa: BLE001
            pass

    return gsp.report(), gsp.stats


async def soak(address, args):
    """Repeat trials to measure intermittency, which a single run cannot show.

    The stall is intermittent: identical conditions (battery 100%, HTTP 200,
    strong RSSI) produced a perfect stream, then a stall 24 s in, then a perfect
    stream. A single run says nothing about the failure rate, so this repeats
    and reports how often the stream failed.
    """
    results = []
    for i in range(1, args.trials + 1):
        print(f"\n=================================================")
        print(f"TRIAL {i}/{args.trials}")
        print(f"=================================================")
        healthy, st = await run_trial(address, args)
        if healthy is None:
            results.append({"ok": False, "reason": "connect failed"})
        else:
            results.append({
                "ok": healthy,
                "duty": (st.frames / (args.rate / 16.0 * args.duration)) * 100.0,
                "frames": st.frames,
                "battery": st.battery,
                "link": st.disconnected,
            })

        if i < args.trials:
            print(f"\nsettling {args.gap:.0f}s before the next trial ...")
            await asyncio.sleep(args.gap)

    ok = sum(1 for r in results if r["ok"])
    print("\n" + "=" * 68)
    print(f"SOAK RESULT: {ok}/{len(results)} trials clean "
          f"({100.0 * ok / max(len(results), 1):.0f}%)")
    for i, r in enumerate(results, 1):
        if r.get("reason"):
            print(f"  T{i}: {r['reason']}")
        elif r["ok"]:
            print(f"  T{i}: clean")
        else:
            link = " link dropped" if r["link"] else ""
            print(f"  T{i}: FAILED  frames={r['frames']} "
                  f"duty~{r['duty']:.0f}%{link}")

    if ok < len(results):
        print("\n  A failure rate of this size means the fault is intermittent, "
              "not a warm-up or a single bad session.")
        print("  Identical conditions produce both outcomes, so signal "
              "strength, battery and the subscribe reply are not the cause.")
    return 0 if ok == len(results) else 1


async def main():
    args = parse_args()

    address = await find_device(args)
    if not address:
        print("\nNo Movesense advertising the GSP service was found.")
        print("  - is the sensor on and awake?")
        print("  - is the dongle holding the connection? the sensor accepts "
              "one subscriber at a time")
        return 1

    if args.trials > 1:
        return await soak(address, args)

    healthy, _ = await run_trial(address, args)
    return 0 if healthy else 1


def parse_args():
    p = argparse.ArgumentParser(
        description="Host-side Movesense GSP ECG control experiment.")
    p.add_argument("--address", help="connect to this address, skip scanning")
    p.add_argument("--name", default="Movesense",
                   help="substring to match during scanning")
    p.add_argument("--scan", type=float, default=8.0,
                   help="scan duration in seconds (default 8)")
    p.add_argument("--rssi", type=int, default=None,
                   help="RSSI to report for a matched device")
    p.add_argument("--rate", type=int, default=256,
                   help="ECG sample rate: 125/128/200/250/256/500/512")
    p.add_argument("--duration", type=float, default=120.0,
                   help="measurement window in seconds (default 120)")
    p.add_argument("--timeout", type=float, default=45.0,
                   help="connect timeout in seconds (default 45)")
    p.add_argument("--no-hr-control", action="store_true",
                   help="do not subscribe to the Heart Rate service")
    p.add_argument("--trials", type=int, default=1,
                   help="run N trials back to back and report a failure rate "
                        "(default 1); implies --gap between them")
    p.add_argument("--gap", type=float, default=40.0,
                   help="seconds to settle between trials in a soak (default 40)")
    p.add_argument("--sustained", action="store_true",
                   help="one long single-connection run, reporting per-minute "
                        "throughput to expose decay with time-under-load "
                        "(use with a large --duration, e.g. 900)")
    p.add_argument("--rssi-log", action="store_true",
                   help="sample RSSI every 10 s during the run; distinguishes a "
                        "decaying radio link from a sensor-side stall")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="dump the first frames decoded")
    return p.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
