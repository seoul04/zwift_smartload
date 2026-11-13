#!/usr/bin/env python3
"""Dual-access test: 256 Hz ECG over GSP on one link, standard HRS on another.

The sensor advertises two peripheral links, so a bike computer (or Zwift) can
read heart rate from the SIG Heart Rate service (0x180D / 0x2A37) while a
separate central streams raw ECG over the Movesense GSP service. This script
holds both connections at once from the same adapter and reports each
independently, so a slow ECG stream cannot be hidden by a healthy HR one.

Measured over the wire (68-byte ECG frame, after the 2-byte GSP header):
    buf[0..3]   uint32 LE  timestamp, ms
    buf[4..67]  16 x int32 LE  ADC counts, 0.38147 uV per count

Why the notification length matters more than anything else here:

    GATTSensorDataClient only splits a payload above a hardcoded 150 bytes, so a
    70-byte ECG frame is submitted as ONE notification and never produces a
    DATA_PART2 continuation. The ATT MTU alone decides how many bytes survive:
    the notification is truncated to MTU-3. At MTU 23 that is 20 bytes = 2
    header + 4 timestamp + 14 payload = 3 samples, which reads as "48 Hz" and is
    pure truncation. So the length histogram is reported explicitly: 70 B means
    the whole frame arrived, anything smaller means the peer's MTU is too low.

Usage:
    python3 host_dual_access.py --duration 60
    python3 host_dual_access.py --address 0C:8C:DC:1B:64:CD --duration 300

The dongle firmware must not be connected to the same sensor at the same time:
the Movesense advertises two links and a third central will be refused.
"""

import argparse
import asyncio
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.stderr.write("bleak is required: python3 -m pip install bleak\n")
    raise SystemExit(2)

GSP_SERVICE = "34802252-7185-4d5d-b431-630e7050e8f0"
GSP_COMMAND = "34800001-7185-4d5d-b431-630e7050e8f0"
GSP_DATA = "34800002-7185-4d5d-b431-630e7050e8f0"

HRS_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"

RESP_COMMAND = 0x01
RESP_DATA = 0x02
RESP_DATA_PART2 = 0x03

# 2-byte GSP header + 4-byte timestamp + 16 samples * 4 bytes
FULL_FRAME_LEN = 70
SAMPLES_PER_FRAME = 16
UUV_PER_COUNT = 0.38147

MAX_FRAME = 512


class Stats:
    def __init__(self):
        self.frames = 0
        self.samples = 0
        self.short_frames = 0
        self.max_gap = 0.0
        self.first_frame = None
        self.last_frame = None
        self.frame_lengths = {}
        self.samples_per_frame = {}
        self.kinds = {}
        self.notif_lengths = {}

        self.first_ts = None
        self.last_ts = None
        self.ts_regressions = 0
        self.ts_unexpected = 0

        # HR side
        self.hr_notifs = 0
        self.hr_first = None
        self.hr_last = None
        self.hr_max_gap = 0.0
        self.hr_values = []
        self.hr_rr_count = 0

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

        if self.first_ts is None:
            self.first_ts = ts
        else:
            delta = (ts - self.last_ts) & 0xFFFFFFFF
            if delta == 0:
                self.ts_regressions += 1
            elif delta > 500:
                self.ts_unexpected += 1
        self.last_ts = ts

    def note_hr(self, bpm, rr_count, now):
        self.hr_notifs += 1
        if self.hr_first is None:
            self.hr_first = now
        else:
            gap = (now - self.hr_last) * 1000.0
            if gap > self.hr_max_gap:
                self.hr_max_gap = gap
        self.hr_last = now
        self.hr_values.append(bpm)
        self.hr_rr_count += rr_count


def parse_hr(data):
    """Decode a 0x2A37 notification: flags, hr (u8/u16), energy, rr[]."""
    if len(data) < 2:
        return None, 0
    flags = data[0]
    idx = 1
    if flags & 0x01:  # uint16 heart rate
        if len(data) < idx + 2:
            return None, 0
        bpm = int.from_bytes(data[idx:idx + 2], "little")
        idx += 2
    else:
        bpm = data[idx]
        idx += 1
    if flags & 0x08:  # energy expended present
        idx += 2
    rr_count = 0
    if flags & 0x10:  # RR intervals present
        rr_count = (len(data) - idx) // 2
    return bpm, rr_count


class EcgLink:
    def __init__(self, args, stats):
        self.args = args
        self.stats = stats
        self.buf = bytearray()
        self.pending = False
        self.ref = 0
        self.streaming = False

    def on_notify(self, _handle, data):
        now = time.monotonic()
        self.stats.notif_lengths[len(data)] = \
            self.stats.notif_lengths.get(len(data), 0) + 1
        if len(data) < 2:
            return
        kind = data[0]
        self.stats.kinds[kind] = self.stats.kinds.get(kind, 0) + 1

        if kind == RESP_COMMAND:
            payload = bytes(data[2:])
            if payload == b"Hello":
                print("  [ecg] HELLO acknowledged")
            else:
                status = int.from_bytes(payload[0:2], "big") if len(payload) >= 2 else -1
                print(f"  [ecg] cmd resp: HTTP {status}")
            return

        if kind == RESP_DATA:
            if self.pending and self.buf:
                self.finish(now)
            self.buf = bytearray(data[2:])
            self.ref = data[1]
            self.pending = True
            self.streaming = True
            return

        if kind == RESP_DATA_PART2:
            if len(self.buf) + (len(data) - 2) > MAX_FRAME:
                self.buf = bytearray()
                self.pending = False
                return
            self.buf += data[2:]
            return

    def finish(self, now):
        self.pending = False
        buf, self.buf = self.buf, bytearray()
        if len(buf) < 4:
            self.stats.short_frames += 1
            return
        n = (len(buf) - 4) // 4
        if n <= 0:
            self.stats.short_frames += 1
            return
        self.stats.frame_lengths[len(buf) + 2] = \
            self.stats.frame_lengths.get(len(buf) + 2, 0) + 1
        self.stats.samples_per_frame[n] = \
            self.stats.samples_per_frame.get(n, 0) + 1
        ts = int.from_bytes(buf[0:4], "little")
        self.stats.note_frame(n, ts, now)


class HrLink:
    def __init__(self, stats):
        self.stats = stats
        self.last_logged = None

    def on_notify(self, _handle, data):
        bpm, rr = parse_hr(bytes(data))
        if bpm is None:
            return
        self.stats.note_hr(bpm, rr, time.monotonic())
        if self.last_logged is None or abs((bpm or 0) - self.last_logged) >= 3:
            print(f"  [hrs] {bpm} bpm (rr={rr})")
            self.last_logged = bpm


async def find_sensor(args):
    if args.address:
        return args.address
    print(f"scanning for {args.name!r} ...")
    devices = await BleakScanner.discover(timeout=args.scan, return_adv=True)
    best = None
    for addr, (dev, adv) in devices.items():
        if dev.name and args.name.lower() in dev.name.lower():
            if best is None or adv.rssi > best[1]:
                best = (addr, adv.rssi, dev.name)
    if best is None:
        raise SystemExit(f"no device matching {args.name!r} found")
    print(f"found {best[2]} at {best[0]} (rssi {best[1]})")
    return best[0]


async def main():
    args = parse_args()
    address = await find_sensor(args)
    stats = Stats()
    ecg = EcgLink(args, stats)
    hr = HrLink(stats)

    # Two connections from one adapter. The order matters only in that ECG is
    # the thing being measured, so it is established first and the HR link is
    # the newcomer competing for airtime.
    print("\n--- opening ECG link (GSP) ---")
    ecg_client = BleakClient(address, timeout=args.timeout)
    await ecg_client.connect()
    print(f"  connected: {ecg_client.is_connected}")
    await ecg_client.start_notify(GSP_DATA, ecg.on_notify)
    await ecg_client.write_gatt_char(GSP_COMMAND, bytes([0x00, 0x01]), response=True)
    await asyncio.sleep(0.3)
    await ecg_client.write_gatt_char(
        GSP_COMMAND, bytes([0x01, 0x02]) + b"/Meas/ECG/256", response=True)
    print("  subscribed /Meas/ECG/256")

    print("\n--- opening HR link (HRS 0x180D) ---")
    hr_client = None
    try:
        hr_client = BleakClient(address, timeout=args.timeout)
        await hr_client.connect()
        chars = []
        for svc in hr_client.services:
            if svc.uuid.lower() == HRS_SERVICE:
                chars = [c.uuid for c in svc.characteristics]
        if HR_MEASUREMENT.lower() not in [c.lower() for c in chars]:
            print("  HR service not found on this firmware - "
                  "the ECG link is being tested alone")
            print(f"  (characteristics present: {chars})")
            await hr_client.disconnect()
            hr_client = None
        else:
            await hr_client.start_notify(HR_MEASUREMENT, hr.on_notify)
            print("  connected and subscribed 0x2A37 - BOTH links are live")
    except Exception as exc:  # noqa: BLE001 - report, do not kill the ECG run
        print(f"  second link failed: {type(exc).__name__}: {exc}")
        print("  the sensor may only accept one central in this firmware")
        hr_client = None

    # Wait for the first ECG frame before starting the clock, otherwise the
    # measurement window is padded with connection setup time.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not ecg.streaming:
        await asyncio.sleep(0.1)
    if not ecg.streaming:
        print("  no ECG frames after 8s (stale measurement on the sensor?)")
        try:
            await ecg_client.write_gatt_char(
                GSP_COMMAND, bytes([0x02, 0x02]), response=True)
            await asyncio.sleep(0.5)
            await ecg_client.write_gatt_char(
                GSP_COMMAND, bytes([0x01, 0x03]) + b"/Meas/ECG/256", response=True)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not ecg.streaming:
                await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001
            pass

    t0 = time.monotonic()
    print(f"\n--- measuring for {args.duration:.0f}s ---")
    await asyncio.sleep(args.duration)
    elapsed = time.monotonic() - t0

    for label, client in (("ECG", ecg_client), ("HR", hr_client)):
        if client is None:
            continue
        try:
            await client.disconnect()
            print(f"  {label} link closed")
        except Exception:  # noqa: BLE001
            pass

    report(stats, elapsed, args.duration, hr_client is not None)
    return 0 if stats.frames else 1


def report(st, elapsed, requested, dual):
    print("\n" + "=" * 68)
    print(f"RESULT over {elapsed:.1f}s with {'TWO links' if dual else 'ONE link'}"
          f" (ECG requested {requested:.0f}s)")

    print("\n  -- ECG (GSP /Meas/ECG/256) --")
    if st.frames == 0:
        print("    NO FRAMES")
    else:
        span = (st.last_frame - st.first_frame) if st.first_frame else 0.0
        fps = st.frames / span if span else 0.0
        rate = st.samples / span if span else 0.0
        duty = 100.0 * span / elapsed if elapsed else 0.0
        print(f"    frames            : {st.frames}")
        print(f"    samples           : {st.samples}")
        print(f"    frames/s          : {fps:.2f}  (ideal 16.00)")
        print(f"    effective rate    : {rate:.2f} Hz  (ideal 256)")
        print(f"    DUTY CYCLE        : {duty:.1f}%")
        print(f"    max gap           : {st.max_gap:.0f} ms")
        print(f"    short frames      : {st.short_frames}")
        print(f"    notif lengths     : " +
              " ".join(f"{n}B:{c}" for n, c in sorted(st.notif_lengths.items())))
        print(f"    notif kinds       : " +
              " ".join(f"{hex(k)}:{c}" for k, c in sorted(st.kinds.items())))
        print(f"    assembled frames  : " +
              " ".join(f"{n}B:{c}" for n, c in sorted(st.frame_lengths.items())))
        print(f"    samples/frame     : " +
              " ".join(f"{n}:{c}" for n, c in sorted(st.samples_per_frame.items())))
        if st.first_ts is not None:
            print(f"    device clock span : "
                  f"{(st.last_ts - st.first_ts) & 0xFFFFFFFF} ms")
            print(f"    clock anomalies   : regressions={st.ts_regressions} "
                  f"jumps>500ms={st.ts_unexpected}")

        full = sum(c for n, c in st.frame_lengths.items() if n >= FULL_FRAME_LEN)
        if full == 0 and st.frames:
            print("    NOTE: no full 70-byte frames - the peer's MTU is "
                  "truncating them (MTU 23 gives 3 samples)")

    print("\n  -- HR (standard HRS 0x180D / 0x2A37) --")
    if not dual:
        print("    not tested (no second link)")
    elif st.hr_notifs == 0:
        print("    link was up but no HR notifications arrived")
    else:
        span = (st.hr_last - st.hr_first) if st.hr_first else 0.0
        rate = st.hr_notifs / span if span else 0.0
        lo, hi = min(st.hr_values), max(st.hr_values)
        avg = sum(st.hr_values) / len(st.hr_values)
        print(f"    notifications     : {st.hr_notifs}")
        print(f"    rate              : {rate:.2f} /s")
        print(f"    bpm min/avg/max   : {lo}/{avg:.0f}/{hi}")
        print(f"    max gap           : {st.hr_max_gap:.0f} ms")
        print(f"    rr pairs delivered: {st.hr_rr_count}")

    print()
    if dual and st.frames and st.hr_notifs:
        print("VERDICT: BOTH links delivered data simultaneously.")
    elif st.frames and not dual:
        print("VERDICT: ECG only - the second link was not established.")
    elif st.frames:
        print("VERDICT: ECG ok, HR silent.")
    else:
        print("VERDICT: no ECG data.")


def parse_args():
    p = argparse.ArgumentParser(
        description="Concurrent ECG (GSP) + heart rate (HRS) test")
    p.add_argument("--address", help="connect to this address, skip scanning")
    p.add_argument("--name", default="Movesense",
                   help="substring to match while scanning (default Movesense)")
    p.add_argument("--scan", type=float, default=8.0,
                   help="scan time in seconds (default 8)")
    p.add_argument("--duration", type=float, default=60.0,
                   help="measurement time in seconds (default 60)")
    p.add_argument("--timeout", type=float, default=45.0,
                   help="connect timeout in seconds (default 45)")
    return p.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
