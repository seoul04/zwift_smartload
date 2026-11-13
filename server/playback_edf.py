#!/usr/bin/env python3
"""
Replay an EDF session through a virtual serial port.

The dongle's USB CDC stream is the server's only input, so replaying a recording
means synthesising that stream: this opens a pty, writes the same JSON lines the
firmware emits, and paces them on the recording's own clock. Point
`[dongle] serial` at the pty and the server — dashboard, DirCon, Zwift — cannot
tell the difference.

Why not just replay the JSON log
--------------------------------
`playback_serial.py` does exactly that, and it still works, but the log carries
no `ecg` line and no `rr` array, so it exercises neither the heart panels nor
anything that depends on intervals. An EDF session has both, which is the point:
the recording is the thing that has to be reproducible.

Line shapes mirrored from the firmware:
  {"type":"ecg","ts":..,"fs":..,"uv":[..]}     dongle/src/ecg_stream.c
  {"type":"hr","ts":..,"rr":[..],"bpm":..}     dongle/src/ecg_stream.c
  {"type":"cp","ts":..,"power":..,"cadence":..}
  {"type":"ftms","ts":..,"speed":..,"power":..}
  {"type":"sim","ts":..,"grade":..,"resistance":..}

ECG goes out in ~1 s bursts of 16-sample lines a fraction of a millisecond apart,
like the firmware, because the server's scrolling strip measures that cadence to
set its playback lead. A uniformly-paced stream would be replayed correctly but
would not exercise the burst handling at all.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.edf.reader import EdfRecording, read  # noqa: E402

#: Samples per ECG line, as the firmware emits them.
LINE_SAMPLES = 16

#: How far ahead of the recorded timeline to allow the writer to run, ms.
#: Small on purpose: a large buffer would hide the pacing bugs this exists to find.
AHEAD_MS = 5.0

#: Tolerance for the pty being full, so a slow reader cannot wedge the replayer.
WRITE_TIMEOUT_S = 2.0


def create_virtual_serial(link_path: Optional[str] = None):
    """Open a pty and return `(master_fd, slave_path)`; optionally symlink it."""
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.chmod(slave_path, 0o666)
    if link_path:
        try:
            if os.path.lexists(link_path):
                os.remove(link_path)
            os.symlink(slave_path, link_path)
            return master_fd, link_path
        except OSError as exc:
            print(f"Warning: cannot create {link_path}: {exc}", file=sys.stderr)
    return master_fd, slave_path


def _emit(master_fd: int, payload: Dict, deadline: float) -> bool:
    """Write one JSON line, drained so the pty never fills and blocks us."""
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        if time.monotonic() > deadline:
            print("\nWarning: the serial reader stopped draining; giving up on this line",
                  file=sys.stderr)
            return False
        _, writable, _ = select.select([], [master_fd], [], WRITE_TIMEOUT_S)
        if not writable:
            continue
        try:
            n = os.write(master_fd, view)
        except BrokenPipeError:
            return False
        except BlockingIOError:
            continue
        view = view[n:]
    return True


#: Beat annotation texts that mark a beat, as the app writes them.
_BEAT_TEXTS = {"N", "PVC", "R", "LEARN", "NOISE"}


class SessionEmitter:
    """
    The recorded session as a series of timed events, replayable from the start.

    Times are offsets in the recording's own clock (ms from the first sample),
    which is the clock the dongle stamps. An event list is built once and then
    walked; looping just walks it again.
    """

    def __init__(self, rec: EdfRecording) -> None:
        self.rec = rec
        self.fs = rec.fs_hz
        self.ms_per_sample = 1000.0 / self.fs
        self.events: List[tuple] = []
        self._build()

    def _build(self) -> None:
        rec = self.rec
        # Baseline: the recording's first sample is t=0. The dongle's own `ts`
        # is not recoverable from EDF, and does not need to be: the server keys
        # everything off relative time.
        n = len(rec.samples_uv)

        # ECG, in bursts of LINE_SAMPLES, stamped like the firmware stamps them.
        chunk = LINE_SAMPLES
        for start in range(0, n, chunk):
            end = min(start + chunk, n)
            t_ms = start * self.ms_per_sample
            self.events.append((
                t_ms,
                {
                    "type": "ecg",
                    "ts": int(round(t_ms)),
                    "fs": round(self.fs, 3),
                    "uv": [int(round(v)) for v in rec.samples_uv[start:end]],
                },
            ))

        # Sensor intervals: one `hr` line per changed RR value, which is where
        # the writer recorded a beat start. A recording with no RR_MS channel
        # (an older app file, or one written before the sensor stream existed)
        # still has beat annotations, so their gaps are the fallback.
        sensor_bpm = self._sensor_bpm_by_time()
        for t_ms, rr_ms in self._beat_stream():
            bpm = int(round(60000.0 / rr_ms)) if rr_ms > 0 else 0
            # Prefer the figure the strap itself reported at that moment: a
            # strap's bpm is not always exactly 60000/rr (median agreement, but
            # the tail runs several bpm apart), and replaying a derived number
            # would hide that rather than reproduce it.
            recorded = sensor_bpm.get(int(round(t_ms)))
            if recorded is not None:
                bpm = recorded
            self.events.append((
                t_ms,
                {
                    "type": "hr",
                    "ts": int(round(t_ms)),
                    "rr": [int(round(rr_ms))],
                    "bpm": bpm,
                },
            ))

        # 1 Hz metrics: one line per change, which keeps the replay faithful
        # without repeating an identical packet 60 times a minute.
        self._emit_steps(rec, "cp", ("POWER", "CADENCE"), ("power", "cadence"))
        self._emit_steps(rec, "ftms", ("TRAINER_POWER", "TRAINER_CADENCE", "SPEED",
                                       "TRAINER_RES"),
                         ("power", "cadence", "speed", "resistance"),
                         ints=("power", "speed", "resistance"))

        # The trainer's own report is its `sim` line. The commands Zwift sent are
        # not replayed: they are not sensor data, and the server regenerates them
        # from whatever control point gets written during the run. GRADE is put
        # back into hundredths of a percent, the unit the firmware prints (it
        # logs the FTMS value it decoded), so the line matches a real one.
        self._emit_steps(rec, "sim", ("GRADE", "RESISTANCE"), ("grade", "resistance"),
                         scales={"grade": 100.0}, ints=("grade", "resistance"))

        self.events.sort(key=lambda item: item[0])

    def _sensor_bpm_by_time(self) -> Dict[int, int]:
        """
        `{time_ms: bpm}` from the recorded SENSOR_HR channel, where it has one.

        The channel holds a value for the whole second, and a beat line lands at
        the same rounded millisecond as the second it belongs to, so the key is
        the second boundary on the ECG clock. An older file without the channel
        returns nothing and the caller falls back to 60000/rr.
        """
        values = self.rec.metrics.get("SENSOR_HR")
        if not values:
            return {}
        out: Dict[int, int] = {}
        for i, value in enumerate(values):
            if value:
                out[int(round(i * self.ms_per_sample))] = int(round(value))
        return out

    def _beat_stream(self) -> List[tuple]:
        """
        `(time_ms, rr_ms)` per beat, from the sensor stream or the annotations.

        The RR channel is the sensor's own measurement and is preferred. Beat
        annotations are a second, hand-inserted opinion — the app's detector
        marks where it *found* a complex — so they are only a fallback, and the
        interval is derived from consecutive annotations.
        """
        marks = self.rec.rr_marks()
        if marks:
            return [(index * self.ms_per_sample, rr_ms) for index, rr_ms in marks]

        beats = [a.onset_ms for a in self.rec.annotations
                 if a.text in _BEAT_TEXTS and a.text != "NOISE"]
        beats.sort()
        out: List[tuple] = []
        for i in range(1, len(beats)):
            rr = beats[i] - beats[i - 1]
            if 300.0 <= rr <= 2000.0:
                out.append((beats[i], rr))
        return out

    def _emit_steps(self, rec: EdfRecording, kind: str, labels, keys,
                    scales=None, ints=()) -> None:
        """
        Emit one line per change across the 1 Hz channels in `labels`.

        `scales` multiplies a channel on the way out, for a file unit that is not
        the wire unit (GRADE is stored in %, the dongle prints 0.01 %). `ints`
        names the channels the firmware prints as integers, so a replayed line
        looks like a real one instead of carrying the int16 quantisation of the
        EDF axis back out as 11.999694819562066.
        """
        scales = scales or {}
        series = [(label, key, rec.metrics.get(label)) for label, key in zip(labels, keys)]
        series = [(label, key, values) for label, key, values in series if values]
        if not series:
            return
        factor = {key: scales.get(key, 1.0) for _, key in zip(labels, keys)}
        length = min(len(values) for _, _, values in series)

        previous = None
        for i in range(length):
            # The reader holds each 1 Hz value across the samples of its second,
            # so these lists are as long as the ECG. Index them on the ECG clock,
            # not on the second: `i * 1000` would replay a 13 s session as 28
            # minutes and put every metric line at the wrong time.
            t_ms = i * self.ms_per_sample
            wanted = {key: values[i] for _, key, values in series}
            if previous is not None and all(
                    abs(wanted[k] - previous[k]) < 1e-9 for k in wanted):
                continue
            previous = wanted
            payload = {"type": kind, "ts": int(round(t_ms))}
            for _, key, _ in series:
                value = wanted[key] * factor[key]
                if key in ints:  # GRADE keeps its hundredths here: `int(300.0)`
                    value = int(round(value))
                elif factor[key] != 1.0:
                    value = round(value, 6)
                payload[key] = value
            self.events.append((t_ms, payload))

    @property
    def duration_ms(self) -> float:
        return self.rec.duration_ms


def play(master_fd: int, emitter: SessionEmitter, speed: float = 1.0,
         loop: bool = False, verbose: bool = False) -> None:
    """Walk the event list on the recording's clock and write it to the pty."""
    duration = emitter.duration_ms
    print(f"Recording: {len(emitter.rec.samples_uv)} samples at {emitter.fs:.1f} Hz "
          f"({duration / 1000.0:.1f} s), {len(emitter.events)} events")
    if emitter.rec.annotations:
        print(f"Annotations: {', '.join(sorted({a.text for a in emitter.rec.annotations}))}")
    if emitter.rec.gaps():
        print(f"Gaps in the recording: {len(emitter.rec.gaps())}")

    run = 0
    try:
        while True:
            run += 1
            if loop and run > 1:
                print(f"\n--- loop {run} ---")
            started = time.monotonic()
            sent = 0
            for t_ms, payload in emitter.events:
                target = started + (t_ms / speed) / 1000.0
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if not _emit(master_fd, payload, time.monotonic() + WRITE_TIMEOUT_S):
                    return
                sent += 1
                if verbose and sent % 500 == 0:
                    print(f"  {sent}/{len(emitter.events)} events "
                          f"({100.0 * sent / len(emitter.events):.0f}%)", end="\r")
            print(f"Playback complete: {sent} events over "
                  f"{time.monotonic() - started:.1f} s")
            if not loop:
                return
    except KeyboardInterrupt:
        print("\nPlayback interrupted.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay an EDF session on a virtual serial port",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Replay once on a pty, then point config.conf at it
  python playback_edf.py recordings/zrelay_20261107_181500.edf --output /tmp/ttyV0

  # Loop it, so the dashboard keeps showing the same session
  python playback_edf.py session.edf --loop

  # Confirm the recording is intact without touching a serial port
  python playback_edf.py session.edf --inspect
        """)
    parser.add_argument("file", help="EDF session to replay")
    parser.add_argument("--output", help="Stable path for the virtual port (a symlink)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Playback speed multiplier (default: 1.0)")
    parser.add_argument("--loop", action="store_true", help="Replay continuously")
    parser.add_argument("--verbose", action="store_true", help="Progress output")
    parser.add_argument("--inspect", action="store_true",
                        help="Report the recording's contents and exit")
    args = parser.parse_args()

    if args.speed <= 0:
        print("Error: --speed must be positive", file=sys.stderr)
        return 2
    if not os.path.exists(args.file):
        print(f"Error: no such file: {args.file}", file=sys.stderr)
        return 2

    try:
        rec = read(args.file)
    except Exception as exc:
        print(f"Error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1

    if args.inspect:
        print(f"{args.file}")
        print(f"  start      : {rec.start_iso or 'unknown'}")
        print(f"  ECG        : {rec.label}, {rec.fs_hz:.1f} Hz, "
              f"{len(rec.samples_uv)} samples, {rec.duration_ms / 1000.0:.1f} s")
        print(f"  R-R marks  : {len(rec.rr_marks())}")
        print(f"  metrics    : {', '.join(sorted(rec.metrics)) or 'none'}")
        print(f"  annotations: {[(a.text, round(a.onset_ms)) for a in rec.annotations]}")
        return 0

    emitter = SessionEmitter(rec)
    master_fd, port = create_virtual_serial(args.output)
    print(f"Virtual serial port: {port}")
    if args.output:
        print(f"  (real device: {os.path.realpath(port)})")
    print("Set this in config.conf and start the server:")
    print(f'  [dongle]\n  serial = "{port}"\n')

    try:
        play(master_fd, emitter, speed=args.speed, loop=args.loop, verbose=args.verbose)
    finally:
        os.close(master_fd)
        if args.output and os.path.islink(args.output):
            try:
                os.remove(args.output)
            except OSError:
                pass
        print("Virtual serial port closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
