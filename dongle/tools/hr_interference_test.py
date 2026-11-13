#!/usr/bin/env python3
"""Test whether enabling the sensor's Heart Rate service stops the ECG stream.

The sensor's HrBridge module subscribes to /Component/MAX3000x/HeartRateData
whenever a client enables 0x2A37 notifications, and its own comment says "every
subscription restarts the AFE beat detector". The MAX3000x is the single AFE
that also produces /Meas/ECG - so the HR subscription and the ECG stream share
one front end.

That makes HR a suspect rather than a neutral observer: the previous experiment
used 0x2A37 as a link-liveness control, which means the control may have been
causing the loss it was there to measure.

This script tests that directly. It streams ECG undisturbed, then enables HR
notifications at a known instant. If frames stop at that instant, the cause is
established rather than inferred from a failure rate.

Usage:
    python3 tools/hr_interference_test.py --before 15 --after 30
"""

import argparse
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

CMD = "34800001-7185-4d5d-b431-630e7050e8f0"
DATA = "34800002-7185-4d5d-b431-630e7050e8f0"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"


class Probe:
    def __init__(self):
        self.frames = 0
        self.last_frame = None
        self.hr = 0
        self.per_second = {}
        self.t0 = None

    def sec(self):
        return int(time.monotonic() - self.t0)

    def on_data(self, _h, d):
        if not d:
            return
        if d[0] == 0x02:                     # DATA frame start
            self.frames += 1
            self.last_frame = time.monotonic()
            s = self.sec()
            self.per_second[s] = self.per_second.get(s, 0) + 1

    def on_hr(self, _h, d):
        self.hr += 1


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--before", type=float, default=15.0,
                   help="seconds of undisturbed ECG first")
    p.add_argument("--after", type=float, default=30.0,
                   help="seconds to watch after enabling HR")
    p.add_argument("--address")
    args = p.parse_args()

    addr = args.address
    if not addr:
        found = await BleakScanner.discover(timeout=8.0, return_adv=True)
        cands = [(a, adv.rssi) for a, (dev, adv) in found.items()
                 if "movesense" in ((adv.local_name or dev.name or "").lower())]
        if not cands:
            print("No Movesense found.")
            return 1
        addr, rssi = max(cands, key=lambda c: c[1])
        print(f"Using {addr} (RSSI {rssi})")

    client = BleakClient(addr, timeout=45.0)
    await client.connect()
    pr = Probe()
    pr.t0 = time.monotonic()

    try:
        bat = await client.read_gatt_char(BATTERY)
        print(f"Battery: {bat[0]}%")
    except Exception:                      # noqa: BLE001
        pass

    await client.start_notify(DATA, pr.on_data)
    await client.write_gatt_char(CMD, bytes([0x00, 0x01]), response=True)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(CMD, bytes([0x01, 0x02]) + b"/Meas/ECG/256",
                                response=True)

    print(f"\nPhase 1: ECG alone for {args.before:g}s ...")
    await asyncio.sleep(args.before)
    f_before = pr.frames
    print(f"  frames before HR: {f_before} "
          f"({f_before / args.before:.2f}/s, ideal 16.00)")

    if f_before < 10:
        print("  ECG never started; test inconclusive.")
        await client.disconnect()
        return 1

    mark = time.monotonic()
    print(f"\nPhase 2: enabling HR notifications NOW (t={pr.sec()}s) ...")
    await client.start_notify(HR_MEASUREMENT, pr.on_hr)

    await asyncio.sleep(args.after)
    f_after = pr.frames - f_before
    print(f"  frames after HR:  {f_after} "
          f"({f_after / args.after:.2f}/s, ideal 16.00)")
    print(f"  HR notifications: {pr.hr}")

    if pr.last_frame:
        quiet = time.monotonic() - pr.last_frame
    else:
        quiet = float("inf")

    print("\nPer-second frame counts (HR enabled at "
          f"{int(mark - pr.t0)}s):")
    total = int(pr.sec()) + 1
    for s in range(total):
        n = pr.per_second.get(s, 0)
        bar = "#" * n
        tag = " <-- HR on" if s == int(mark - pr.t0) else ""
        print(f"  {s:3d}s {n:2d} {bar}{tag}")

    print()
    if f_after == 0 or quiet > 5.0:
        print("RESULT: ECG stopped after HR notifications were enabled.")
        print("  The sensor's HR subscription shares the MAX3000x with the ECG")
        print("  measurement, so enabling 0x2A37 is what kills the stream.")
        print("  'HR notifications still arriving while ECG is silent' is")
        print("  therefore not evidence of a healthy sensor - it is the cause.")
    else:
        print("RESULT: ECG kept streaming with HR enabled; no interference "
              "observed in this run.")
        print("  The failure is intermittent, so repeat this a few times "
              "before concluding.")

    try:
        await client.write_gatt_char(CMD, bytes([0x02, 0x02]), response=True)
    except Exception:                      # noqa: BLE001
        pass
    await client.disconnect()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
