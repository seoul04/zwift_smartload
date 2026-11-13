#!/usr/bin/env python3
"""Host-side reader for the Movesense EcgUplink characteristic - direct 256 Hz ECG.

This bypasses GSP entirely. There is no HELLO, no SUBSCRIBE command, no client
reference and no continuation packets: enabling notifications on the
characteristic is what starts the stream. The point is to find out *where* the
ECG stream stops.

The diagnostic rests on a field GSP does not have. Every frame carries a
sequence number, so for each frame the client can tell:

    seq contiguous, no data     -> the sensor sent nothing: the measurement
                                   (/Meas/ECG) itself stopped. Not a transport
                                   fault, not something app code can fix.
    seq jumped forward          -> the sensor did send frames but the client
                                   never received them: link or notification
                                   delivery. That *is* fixable here.
    seq contiguous, data flows  -> healthy.

Without a sequence number both failures look identical from the host: silence.
That ambiguity is why this characteristic exists.

Wire format of one notification (little endian, one frame per notification):

    [0..3]   uint32  timestamp, ms, from ECGData.timestamp
    [4..7]   uint32  sequence, increments by 1 per frame sent
    [8..]    int32   samples, 16 per frame at 256 Hz, 0.38147 uV per count

Nominal frame size is 72 bytes at 256 Hz (8 + 16*4), one notification every
62.5 ms. The sample count is read from the notification length rather than
assumed, so a provider that batches differently is decoded rather than misread.

Usage:
    python3 host_ecg_uplink.py --duration 120
    python3 host_ecg_uplink.py --address 0C:8C:DC:1B:64:CD --duration 300

The dongle must not be connected to the same sensor: a Movesense accepts one
connection at a time, and the failure would be a connect error rather than
anything about the stream.

Exit code is 0 if the stream was healthy for the whole window, 1 otherwise, so
this can drive a soak loop.
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

# Must match EcgUplink.cpp. The firmware stores UUIDs byte-reversed; the arrays
# there decode to these textual forms.
ECG_SERVICE = "00ec0001-7185-4d5d-b431-630e7050e8f0"
ECG_CHAR = "00ec0002-7185-4d5d-b431-630e7050e8f0"

# Used only to find the sensor: although the sensor does advertise its services,
# matching on the name plus the Movesense FDF3 service is what works reliably
# here, and that avoids depending on the ECG service being advertised.
MOVESENSE_SERVICE = "0000fdf3-0000-1000-8000-00805f9b34fb"

HEADER_BYTES = 8
UUV_PER_COUNT = 0.38147

# A frame is 8 + 4*n bytes. Anything wildly larger means this is not one of our
# frames, and 89 bytes is the largest payload MTU 92 can carry in one
# notification, so anything above that could not have arrived intact anyway.
MAX_FRAME = 89


class Stats:
    """Everything needed to decide whether the stream was healthy, and if not,
    which of the two failure modes it was."""

    def __init__(self, rate):
        self.rate = rate
        self.frames = 0
        self.samples = 0
        self.malformed = 0
        self.oversize = 0

        self.first_frame_t = None
        self.last_frame_t = None
        self.max_gap_ms = 0.0
        self.gaps = []              # (elapsed_s, gap_ms)

        self.per_second = {}        # second -> frames
        self.per_second_samples = {}

        # Sequence tracking - the reason this stream is diagnosable. A jump means
        # frames were sent and lost; no jump over a silence means nothing was
        # sent at all.
        self.first_seq = None
        self.last_seq = None
        self.seq_jumps = []         # (elapsed_s, missing_frames)
        self.seq_lost_frames = 0
        self.seq_reorders = 0

        # Device clock, to detect skipped samples within an uninterrupted frame
        # sequence.
        self.first_ts = None
        self.last_ts = None
        self.ts_regressions = 0
        self.ts_unexpected = 0

    def note_frame(self, count, seq, ts, now):
        self.frames += 1
        self.samples += count

        if self.first_frame_t is None:
            self.first_frame_t = now
        else:
            gap_ms = (now - self.last_frame_t) * 1000.0
            if gap_ms > self.max_gap_ms:
                self.max_gap_ms = gap_ms
            if gap_ms >= 250.0:
                self.gaps.append((now - self.first_frame_t, gap_ms))
        self.last_frame_t = now

        sec = int(now - self.first_frame_t)
        self.per_second[sec] = self.per_second.get(sec, 0) + 1
        self.per_second_samples[sec] = self.per_second_samples.get(sec, 0) + count

        if seq is not None:
            if self.first_seq is None:
                self.first_seq = seq
            elif self.last_seq is not None:
                delta = (seq - self.last_seq) & 0xFFFFFFFF
                if delta == 1:
                    pass
                elif delta == 0:
                    self.seq_reorders += 1
                elif delta > 0x7FFFFFFF:
                    # Went backwards: not a loss, a reordering or a reset.
                    self.seq_reorders += 1
                else:
                    missing = delta - 1
                    self.seq_lost_frames += missing
                    self.seq_jumps.append((now - self.first_frame_t, missing))
            self.last_seq = seq

        if ts is not None:
            if self.first_ts is None:
                self.first_ts = ts
            elif self.last_ts is not None:
                delta = (ts - self.last_ts) & 0xFFFFFFFF
                # A frame spans ~62.5 ms at 256 Hz; allow generous slack before
                # calling it a skip, since the provider timestamp is quantised.
                expected = 1000.0 / self.rate * 16.0
                if delta > 0x7FFFFFFF:
                    self.ts_regressions += 1
                elif delta > expected * 3:
                    self.ts_unexpected += 1
            self.last_ts = ts


class EcgUplinkClient:
    def __init__(self, args):
        self.args = args
        self.stats = Stats(args.rate)
        self.monitor_start = None
        self.monitor_end = None
        self.first_payload_len = None
        self.notif_count = 0
        self.mtu = None
        self.subscribe_t = None
        self.enable_error = None

    def on_notify(self, _handle, data):
        now = time.monotonic()
        self.notif_count += 1

        if len(data) < HEADER_BYTES:
            self.stats.malformed += 1
            return

        if len(data) > MAX_FRAME:
            self.stats.oversize += 1
            return

        body = len(data) - HEADER_BYTES
        if body % 4 != 0:
            self.stats.malformed += 1
            return

        if self.first_payload_len is None:
            self.first_payload_len = len(data)
            print(f"  [first frame] {len(data)} bytes, "
                  f"{body // 4} samples")

        ts = int.from_bytes(data[0:4], "little")
        seq = int.from_bytes(data[4:8], "little")
        count = body // 4
        self.stats.note_frame(count, seq, ts, now)

        if self.args.verbose and self.stats.frames <= 3:
            uv = [
                int.from_bytes(data[8 + i * 4:12 + i * 4], "little", signed=True)
                for i in range(min(count, 8))
            ]
            print(f"  [frame] seq={seq} ts={ts}ms count={count} "
                  f"first_uV≈{[round(v * UUV_PER_COUNT, 1) for v in uv]}")

    async def run(self, client):
        svcs = client.services
        svc = svcs.get_service(ECG_SERVICE)
        if svc is None:
            print(f"Service {ECG_SERVICE} NOT found on the sensor.")
            print("  Services present:")
            for s in svcs:
                print(f"    {s.uuid}  {s.description or ''}")
            return False

        print(f"Service found: {ECG_SERVICE}")
        char = svcs.get_characteristic(ECG_CHAR)
        if char is None:
            print(f"  characteristic {ECG_CHAR} NOT found in the service.")
            print("  Characteristics present:")
            for c in svc.characteristics:
                print(f"    {c.uuid}  props={c.properties}")
            return False

        print(f"  characteristic: {ECG_CHAR}")
        print(f"  properties    : {char.properties}")
        if "notify" not in [p.lower() for p in char.properties]:
            print("  ERROR: characteristic does not expose notify.")
            return False

        # MTU is reported because the frame is designed to fit one notification,
        # and a smaller-than-expected MTU is the one way that assumption breaks.
        try:
            self.mtu = client.mtu_size
        except Exception:  # noqa: BLE001
            self.mtu = None
        if self.mtu:
            print(f"  ATT MTU       : {self.mtu} "
                  f"(payload {self.mtu - 3} bytes, need {HEADER_BYTES + 16 * 4})")
            if self.mtu - 3 < HEADER_BYTES + 16 * 4:
                print("  WARNING: MTU payload is smaller than one frame; the "
                      "frame may be truncated or rejected.")

        print(f"\nEnabling notifications (this starts the stream) ...")
        self.subscribe_t = time.monotonic()
        try:
            await client.start_notify(ECG_CHAR, self.on_notify)
        except Exception as exc:  # noqa: BLE001
            print(f"start_notify failed: {exc}")
            return False

        print(f"Streaming for {self.args.duration:g}s "
              f"(requested {self.args.rate} Hz)\n")

        self.monitor_start = time.monotonic()
        last_report = 0.0
        last_frames = 0
        last_t = self.monitor_start
        silent_since = None
        next_silence_report = 0.0

        while True:
            elapsed = time.monotonic() - self.monitor_start
            if elapsed >= self.args.duration:
                break

            await asyncio.sleep(0.1)

            if not client.is_connected:
                print(f"  [{elapsed:6.1f}s] LINK DROPPED by the remote")
                break

            now = time.monotonic()
            if self.stats.frames != last_frames:
                last_frames = self.stats.frames
                last_t = now
                if silent_since is not None:
                    print(f"  [{elapsed:6.1f}s] stream resumed after "
                          f"{now - silent_since:.1f}s")
                    silent_since = None
                    next_silence_report = elapsed + 10.0
            else:
                if silent_since is None and now - last_t > 1.0:
                    silent_since = last_t
                    next_silence_report = elapsed + 5.0
                    print(f"  [{elapsed:6.1f}s] no frames for "
                          f"{now - last_t:.1f}s "
                          f"(notifs={self.notif_count}, "
                          f"seq={self.stats.last_seq}, "
                          f"frames={self.stats.frames})")
                elif silent_since is not None and elapsed >= next_silence_report:
                    next_silence_report = elapsed + 10.0
                    print(f"  [{elapsed:6.1f}s] still silent after "
                          f"{now - silent_since:.0f}s "
                          f"(notifs={self.notif_count})")

            if elapsed - last_report >= 5.0:
                last_report = elapsed
                fps = self.stats.frames / elapsed if elapsed > 0 else 0.0
                # The MTU is re-read on every report rather than captured once:
                # if the link renegotiates partway through, the throughput
                # profile changes with it and a single reading at connect would
                # hide that.
                cur_mtu = getattr(client, "mtu_size", None)
                if cur_mtu and cur_mtu != self.mtu:
                    print(f"  [{elapsed:6.1f}s] MTU changed "
                          f"{self.mtu} -> {cur_mtu}")
                    self.mtu = cur_mtu
                print(f"  [{elapsed:6.1f}s] frames={self.stats.frames} "
                      f"fps={fps:.2f} "
                      f"seq_lost={self.stats.seq_lost_frames} "
                      f"maxgap={self.stats.max_gap_ms:.0f}ms "
                      f"mtu={self.mtu}")

        self.monitor_end = time.monotonic()

        try:
            await client.stop_notify(ECG_CHAR)
        except Exception:  # noqa: BLE001
            pass

        return self.report()

    def report(self):
        st = self.stats
        print()
        print("=" * 68)

        if st.frames == 0:
            print("RESULT: no ECG frames at all.")
            print(f"  notifications  : {self.notif_count}")
            print(f"  malformed      : {st.malformed}  oversize: {st.oversize}")
            if self.notif_count == 0:
                print("\n  No notifications arrived, so the sensor never started "
                      "the measurement.")
                print("  Enabling notify on the characteristic is what requests "
                      "it; if the service and characteristic were found but")
                print("  nothing came, the /Meas/ECG subscribe inside EcgUplink "
                      "was refused. Check the sensor log for the")
                print("  'EcgUplink: ECG subscribe rejected' line.")
            else:
                print("\n  Notifications arrived but none decoded. The payload "
                      "layout does not match EcgUplink.cpp.")
            return False

        observed = None
        if self.monitor_start and self.monitor_end:
            observed = self.monitor_end - self.monitor_start
        span = (st.last_frame_t - st.first_frame_t) if st.first_frame_t else 0.0
        if span <= 0:
            print("RESULT: only one frame received; cannot measure.")
            return False

        expected_fps = self.args.rate / 16.0
        span_fps = st.frames / span
        rate = st.samples / span

        # Duty is measured over the whole observation window, not the frame span.
        # Over the span alone, a stream that stopped after 8 s looks perfectly
        # regular and reports ~100%, which is precisely the failure being hunted.
        duty = (st.frames / (expected_fps * observed) * 100.0) if observed else \
               (span_fps / expected_fps * 100.0)

        print(f"RESULT over {span:.1f}s of delivered stream "
              f"(requested {self.args.rate} Hz)")
        if observed:
            print(f"  observation window : {observed:.1f}s")
        print(f"  frames             : {st.frames}")
        print(f"  samples            : {st.samples}")
        print(f"  frames/s           : {span_fps:.2f}  "
              f"(ideal {expected_fps:.2f})")
        print(f"  effective rate     : {rate:.2f} Hz  "
              f"(ideal {self.args.rate})")
        print(f"  DUTY CYCLE         : {duty:.1f}%"
              f"{'  (of whole run)' if observed else ''}")
        print(f"  max gap            : {st.max_gap_ms:.0f} ms")
        print(f"  malformed frames   : {st.malformed}  oversize: {st.oversize}")

        print()
        print("  sequence analysis (the part GSP cannot show):")
        print(f"    first .. last seq : {st.first_seq} .. {st.last_seq}")
        print(f"    frames lost       : {st.seq_lost_frames}")
        print(f"    reorders/resets   : {st.seq_reorders}")
        if st.first_ts is not None and st.last_ts is not None:
            tspan = (st.last_ts - st.first_ts) & 0xFFFFFFFF
            print(f"    device clock span : {tspan} ms")
            print(f"    clock anomalies   : regressions={st.ts_regressions} "
                  f"gaps>3x_frame={st.ts_unexpected}")

        if st.gaps:
            print(f"\n  gaps >= 250 ms ({len(st.gaps)}):")
            for when, gap in st.gaps[:25]:
                print(f"    +{when:7.1f}s  {gap:7.0f} ms")
            if len(st.gaps) > 25:
                print(f"    ... and {len(st.gaps) - 25} more")

        if st.seq_jumps:
            print(f"\n  sequence jumps ({len(st.seq_jumps)}):")
            for when, missing in st.seq_jumps[:25]:
                print(f"    +{when:7.1f}s  {missing} frame(s) never delivered")
            if len(st.seq_jumps) > 25:
                print(f"    ... and {len(st.seq_jumps) - 25} more")

        print("\n  per-10s windows (frames/s):")
        windows = {}
        for sec, n in st.per_second.items():
            windows.setdefault(sec // 10 * 10, 0)
            windows[sec // 10 * 10] += n
        for w in sorted(windows):
            fps = windows[w] / 10.0
            bar = "#" * int(min(fps / expected_fps, 1.0) * 40)
            mark = "  <-- degraded" if fps < expected_fps * 0.5 else ""
            print(f"    {w:4d}-{w + 10:4d}s  {fps:5.2f}  {bar}{mark}")

        reasons = []
        if duty < 98.0:
            reasons.append(f"duty {duty:.1f}% < 98%")
        if st.max_gap_ms >= 500:
            reasons.append(f"max gap {st.max_gap_ms:.0f} ms >= 500 ms")
        if st.seq_lost_frames:
            reasons.append(f"{st.seq_lost_frames} frame(s) lost on the link")
        healthy = not reasons

        print()
        if healthy:
            print("VERDICT: the direct stream is healthy for the whole window.")
            print("  Nothing was dropped in the transport and nothing was "
                  "skipped by the measurement.")
            return True

        print(f"VERDICT: NOT healthy ({'; '.join(reasons)}).")

        # This is the whole point of the sequence number: the two failures have
        # different owners, and only one of them is fixable in app code.
        if st.seq_lost_frames > 0 and duty >= 90.0:
            print("\n  Mode: TRANSPORT LOSS. Frames were sent and the sequence "
                  "records them, but the client never received them.")
            print("  The measurement kept running the whole time. This is a "
                  "link/notification delivery fault - the place to look is")
            print("  the connection parameters, the MTU, the radio, or the "
                  "notify path. It is fixable on this side.")
        elif st.seq_lost_frames == 0 and duty < 98.0:
            print("\n  Mode: MEASUREMENT STALL. The sequence is unbroken, which "
                  "means no frame was ever lost in the")
            print("  transport: the sensor simply stopped producing samples and "
                  "then resumed. /Meas/ECG itself paused.")
            print("  This is inside the sensor's measurement engine (AFE or its "
                  "driver). No change to the BLE path, the GATT layout or")
            print("  the host client can fix it - the same stall would appear on "
                  "any transport.")
            if st.ts_unexpected:
                print(f"  Note: {st.ts_unexpected} timestamp gap(s) over 3x a "
                      f"frame, consistent with skipped sample blocks.")
        else:
            print("\n  Mode: mixed. Both lost frames and an unbroken sequence "
                  "appeared, so the window contains more than one failure.")
            print("  Re-run with a longer --duration to separate them.")

        return False


async def find_device(args):
    if args.address:
        return args.address

    print(f"Scanning for {args.scan:g}s ...")

    # discover(..., return_adv=True) rather than the detect_callback form: with
    # bleak 3.x on BlueZ the callback variant returned nothing at all here.
    found = await BleakScanner.discover(timeout=args.scan, return_adv=True)
    if not found:
        return None

    matches = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        services = [s.lower() for s in (adv.service_uuids or [])]
        if ("movesense" in name.lower()
                or args.name.lower() in name.lower()
                or MOVESENSE_SERVICE in services):
            matches.append((addr, name, adv.rssi))
            print(f"  found {addr}  {name or '(no name)'}  RSSI {adv.rssi}")

    if not matches:
        print(f"  no device matched {args.name!r} in {len(found)} seen")
        for addr, (dev, adv) in found.items():
            print(f"    {addr}  {adv.local_name or dev.name or '(no name)'}  "
                  f"{adv.rssi} dBm")
        return None

    addr, name, rssi = max(matches, key=lambda m: m[2] if m[2] is not None else -999)
    print(f"  using {addr}  {name}  RSSI {rssi}")
    return addr


async def run_trial(address, args):
    print("\n--- trial ---")
    client = BleakClient(address, timeout=args.timeout)
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Connection failed: {exc}")
        print("  If the dongle is streaming to this sensor, stop it first: "
              "a Movesense accepts one connection.")
        return None, None

    reader = EcgUplinkClient(args)
    try:
        print("Connected.")
        healthy = await reader.run(client)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"\nStream interrupted: {exc}")
        healthy = False
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    return healthy, reader.stats


async def soak(address, args):
    """Repeat trials. The stall is intermittent: identical conditions produced a
    perfect stream, then a stall 24 s in, then a perfect stream, so one run says
    nothing about the failure rate."""
    results = []
    for i in range(1, args.trials + 1):
        print("\n=================================================")
        print(f"TRIAL {i}/{args.trials}")
        print("=================================================")
        healthy, st = await run_trial(address, args)
        if healthy is None:
            results.append({"ok": False, "reason": "connect failed"})
        else:
            results.append({
                "ok": healthy,
                "frames": st.frames,
                "duty": (st.frames / (args.rate / 16.0 * args.duration)) * 100.0,
                "lost": st.seq_lost_frames,
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
            print(f"  T{i}: FAILED  frames={r['frames']} "
                  f"duty~{r['duty']:.0f}% seq_lost={r['lost']}")
    return 0 if ok == len(results) else 1


async def main():
    args = parse_args()
    address = await find_device(args)
    if not address:
        print("\nNo Movesense found.")
        print("  - is the sensor on and awake?")
        print("  - is the dongle holding the connection? the sensor accepts "
              "one connection at a time")
        return 1

    if args.trials > 1:
        return await soak(address, args)

    healthy, _ = await run_trial(address, args)
    return 0 if healthy else 1


def parse_args():
    p = argparse.ArgumentParser(
        description="Host-side reader for the Movesense EcgUplink "
                    "characteristic (direct 256 Hz ECG).")
    p.add_argument("--address", help="connect to this address, skip scanning")
    p.add_argument("--name", default="Movesense",
                   help="substring to match during scanning")
    p.add_argument("--scan", type=float, default=8.0,
                   help="scan duration in seconds (default 8)")
    p.add_argument("--rate", type=int, default=256,
                   help="sample rate EcgUplink requests (default 256); must "
                        "match the firmware")
    p.add_argument("--duration", type=float, default=120.0,
                   help="measurement window in seconds (default 120)")
    p.add_argument("--timeout", type=float, default=45.0,
                   help="connect timeout in seconds (default 45)")
    p.add_argument("--trials", type=int, default=1,
                   help="run N trials back to back and report a failure rate")
    p.add_argument("--gap", type=float, default=40.0,
                   help="seconds to settle between trials (default 40)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="dump the first frames decoded")
    return p.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
