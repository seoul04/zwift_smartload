#!/usr/bin/env python3
"""Watch the dongle's ECG stream and fail on the first imperfection.

This is the quick-turnover companion to the host-side tool: it does not try to
characterise a flaky stream, it decides pass/fail as fast as possible so the next
firmware iteration can start. A single stall, a single long gap or a stream that
never starts is enough to exit non-zero.

The counters it reads are the firmware's own (/ECG health lines and stall
messages on the CDC ACM console), so it measures what the dongle believes rather
than re-decoding the JSON records - which is what the relay server consumes.

Defaults are deliberately strict: 30 s of observation, no gap above 250 ms.
At 16 records/s a healthy stream has an 8 ms nominal period, and the best runs
measured on hardware had a 180-210 ms worst gap, so 250 ms still leaves room for
a slow USB handoff while catching every real stall.

Usage:
    python3 tools/dongle_watch.py                  # 30 s, fail on first stall
    python3 tools/dongle_watch.py --seconds 300
    python3 tools/dongle_watch.py --max-gap 200 --allow-restarts 0
"""

import argparse
import re
import sys
import time

try:
    import serial
except ImportError:
    sys.stderr.write("pyserial is required: python3 -m pip install pyserial\n")
    raise SystemExit(2)

# The firmware prints these. Matched leniently: the exact wording has changed
# between versions and the monitor should not break when a message is reworded.
RE_STALL = re.compile(r"no ECG for \d+ms while the link is up", re.I)
RE_STALLED_MARK = re.compile(r"<--\s*STALLED", re.I)
RE_HEALTH = re.compile(r"\[ECG\] slot (\d+):")
RE_FRAMES = re.compile(r"frames=(\d+)")
RE_MAXGAP = re.compile(r"maxgap=(\d+)")
RE_RESTARTS = re.compile(r"restarts=(\d+)")
RE_CONNECTED = re.compile(r"Connected: ([0-9A-Fa-f:]{17})")


class Watch:
    def __init__(self, args):
        self.args = args
        self.t0 = None
        self.stalls = 0
        self.max_gap = 0
        self.max_restarts = 0
        self.frames = 0
        self.connected = []
        self.first_frames_at = None
        self.last_health = None
        self.failure = None

    def note(self, line):
        now = time.monotonic()
        if self.t0 is None:
            self.t0 = now

        m = RE_CONNECTED.search(line)
        if m:
            addr = m.group(1).upper()
            if addr not in self.connected:
                self.connected.append(addr)
                print(f"  [{now - self.t0:6.1f}s] connected {addr}")

        if RE_STALL.search(line) or RE_STALLED_MARK.search(line):
            self.stalls += 1
            print(f"  [{now - self.t0:6.1f}s] STALL: {line.strip()[:120]}")
            if self.stalls > self.args.allow_restarts and self.failure is None:
                self.failure = (f"{self.stalls} stall(s) "
                                f"(allowed {self.args.allow_restarts})")

        if RE_HEALTH.search(line):
            self.last_health = line.strip()
            m = RE_FRAMES.search(line)
            if m:
                f = int(m.group(1))
                if f > self.frames and self.first_frames_at is None:
                    self.first_frames_at = now
                self.frames = max(self.frames, f)
            m = RE_MAXGAP.search(line)
            if m:
                gap = int(m.group(1))
                # maxgap is cumulative across the connection, so the largest
                # value seen is the worst gap on this link.
                if gap > self.max_gap:
                    self.max_gap = gap
                    if gap > self.args.max_gap and self.failure is None:
                        self.failure = (f"gap {gap}ms exceeds "
                                        f"{self.args.max_gap}ms")
            m = RE_RESTARTS.search(line)
            if m:
                self.max_restarts = max(self.max_restarts, int(m.group(1)))

    def verdict(self):
        print()
        print("=" * 60)
        print(f"connected sensors : {self.connected or 'none'}")
        print(f"frames (latest)   : {self.frames}")
        print(f"stalls            : {self.stalls}")
        print(f"max gap           : {self.max_gap} ms "
              f"(limit {self.args.max_gap} ms)")
        print(f"max restarts      : {self.max_restarts}")

        if self.failure:
            print(f"\nRESULT: FAIL - {self.failure}")
            print("  Expectation is a perfect stream; bailing early so the next")
            print("  firmware iteration can start.")
            return 1

        if self.frames == 0:
            print("\nRESULT: FAIL - no ECG frames at all")
            print("  The sensor never streamed. Check it is awake and that the")
            print("  dongle connected to it (see 'connected sensors' above).")
            return 1

        if self.t0 is not None:
            elapsed = time.monotonic() - self.t0
            print(f"\nelapsed           : {elapsed:.0f}s")
        print("\nRESULT: PASS - no stalls, gaps within limit")
        return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--seconds", type=float, default=30.0,
                   help="observation window (default 30)")
    p.add_argument("--max-gap", type=int, default=250,
                   help="largest tolerated inter-record gap in ms (default 250)")
    p.add_argument("--allow-restarts", type=int, default=0,
                   help="stalls tolerated before failing (default 0)")
    p.add_argument("--baud", type=int, default=115200)
    args = p.parse_args()

    try:
        port = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as exc:              # noqa: BLE001
        print(f"Cannot open {args.port}: {exc}")
        print("  In DFU the dongle is not on this port; after flashing it comes")
        print("  back as CDC ACM after a few seconds.")
        return 2

    print(f"Watching {args.port} for {args.seconds:.0f}s "
          f"(fail on stall, gap > {args.max_gap}ms) ...")
    watch = Watch(args)
    buf = b""
    end = time.monotonic() + args.seconds

    try:
        while time.monotonic() < end:
            n = port.in_waiting
            chunk = port.read(n if n else 1)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").replace("\x1b[0m", "")
                if not line.strip():
                    continue
                watch.note(line)
                if watch.failure is not None:
                    return watch.verdict()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        port.close()

    return watch.verdict()


if __name__ == "__main__":
    sys.exit(main())
