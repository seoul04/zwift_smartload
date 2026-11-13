#!/usr/bin/env python3
"""
Score the beats overlay's R alignment against hand-tagged peaks.

Why this exists
---------------
"Some waves are not R-synched" is a real defect but not a measurable one, and
the beats pane is the kind of thing where an obvious-looking fix can easily
trade one kind of fan for another. This tool turns the complaint into numbers:
for every beat in a stack it reports how far that beat's *true* R ends up from
the shared line the pane draws, in both grid samples and milliseconds.

How the measurement works
-------------------------
The pane pins each wave on its own detected `peak`, so what the eye sees is not
where the slice put R — it is `true_r_grid - peak`. A wave whose slice landed
the complex at grid 60 but whose `peak` was correctly measured as 60 still draws
its R on the shared line. Only when the two disagree by a different amount on
different beats does the stack fan out.

So the residual is computed in grid coordinates:

    r_grid   = R_AT + (true_r_ms - located_ms) / ms_per_grid_sample
    residual = r_grid - peak

`residual` is the error that survives anchoring. Reporting its spread (and not
just its mean) is the point: a constant offset is invisible once every trace is
pinned on its own peak, but a *varying* one is exactly the fan.

The replay is deterministic. The recording's own R-R marks and ECG are fed back
through `HeartState` in timestamp order, so the same file always produces the
same stack and two runs of two different algorithms are directly comparable.

Ground truth
------------
`--peaks` takes a file of hand-tagged R times, one per line, in sample indices by
default (what EDFbrowser's cursor reads out) or milliseconds with `--units ms`.
Blank lines and `#` comments are ignored. Tag as many beats as you like; the
report is per matched beat, so a partial tag over a messy stretch is fine.

Without `--peaks` the tool falls back to `--auto`, a crude derivative detector.
It is good enough to track the *relative* quality of two algorithms on the same
file, but it is not independent of the thing being tested, so do not read its
absolute numbers as truth.

Usage
-----
    python tools/eval_beat_alignment.py recording/zrelay_20261108_020954.edf
    python tools/eval_beat_alignment.py recording/x.edf --peaks peaks.txt
    python tools/eval_beat_alignment.py recording/x.edf --peaks peaks.txt --csv out.csv

The exit code is 0 when the median residual is within `--tolerance` samples and
1 otherwise, so it can be used as a check in a test or a loop.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.edf.reader import EdfRecording, read  # noqa: E402
from src.heart.ecg import BeatOverlay, BeatTemplate  # noqa: E402
from src.heart.state import HeartState  # noqa: E402


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


def load_tags(path: str, fs_hz: float, units: str) -> List[float]:
    """Hand-tagged R times from `path`, converted to milliseconds."""
    out: List[float] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            # Accept a bare number or a CSV row whose first field is the time.
            token = text.replace(",", " ").split()[0]
            try:
                value = float(token)
            except ValueError as exc:
                raise SystemExit(f"{path}:{lineno}: not a number: {token!r}") from exc
            out.append(value if units == "ms" else value * 1000.0 / fs_hz)
    if not out:
        raise SystemExit(f"{path}: no peaks found")
    return sorted(out)


def auto_peaks(rec: EdfRecording) -> List[float]:
    """
    Provisional R times, from the sensor's beat marks plus the ECG around them.

    This is deliberately *not* a competing peak detector. The recording's `RR_MS`
    channel already says, authoritatively, when each beat happened; the only open
    question is where inside the waveform its R sits. So each mark gives a search
    centre and the ECG gives the apex near it.

    That split is what makes the yardstick independent of the thing under test:
    `PeakFinder` chains from its own previous pick, so a bad pick propagates, and
    it re-measures the beat clock as it goes. This does neither — every mark is
    evaluated on the raw signal around it, so an error in one beat cannot move
    the next. It is still not hand-tagged truth (a T wave taller than a low R
    would fool it), which is why `--peaks` exists; but for comparing two
    algorithms over the same file it is stable, and a beat it gets wrong it gets
    wrong identically in both runs.
    """
    uv = rec.samples_uv
    fs = rec.fs_hz
    if fs <= 0 or len(uv) < 3 or rec.duration_ms <= 0:
        return []

    # A file tagged by an external annotator (EDFbrowser's QRS detector writes
    # `R-PEAK` annotations) already holds real truth, and it is better truth
    # than anything derived here: it is the output of a different algorithm on
    # the same signal, so scoring against it is not marking your own homework.
    tagged = [
        a.onset_ms for a in getattr(rec, "annotations", ())
        if a.text.upper().startswith("R-PEAK")
    ]
    if tagged:
        return sorted(float(t) for t in tagged)

    marks = rec.rr_marks()
    if not marks:
        return []

    # R sits inside the beat that *starts* at the mark, so the apex is normally a
    # little after it. Search a window that reaches back far enough for early
    # complexes without reaching the neighbouring beat.
    back_ms = 180.0
    ahead_ms = 320.0
    lo_off = int(round(back_ms * fs / 1000.0))
    hi_off = int(round(ahead_ms * fs / 1000.0))

    out: List[float] = []
    for index, _rr in marks:
        lo = max(0, index - lo_off)
        hi = min(len(uv), index + hi_off)
        if hi - lo < 3:
            continue
        window = uv[lo:hi]
        base = statistics.median(window)
        best = max(range(len(window)), key=lambda k: abs(window[k] - base))
        out.append((lo + best) * 1000.0 / fs)
    return out


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def slice_recording(rec: EdfRecording, start_s: float, end_s: float) -> EdfRecording:
    """
    A time window of `rec`, shifted so it starts at zero.

    The drawn stack holds only the last `BeatOverlay.STACK_CAP` beats of whatever
    it is fed, so without a window the only taggable stretch would be the end of
    the file. Replaying a slice makes any stretch reachable — which matters,
    because the interesting ones are usually where the tracing was messy.

    Mark indices, the ECG, and the tags all move together, so the timeline the
    scorer sees is the same shape as the original.
    """
    fs = rec.fs_hz
    if fs <= 0:
        return rec
    lo = max(0, int(round(start_s * fs)))
    hi = min(len(rec.samples_uv), int(round(end_s * fs))) if end_s > 0 else len(rec.samples_uv)
    if hi <= lo:
        raise SystemExit(f"window {start_s}..{end_s}s is empty for a "
                         f"{rec.duration_ms / 1000.0:.1f}s recording")
    # Annotations move with the slice too: `auto_peaks` prefers a file's own
    # `R-PEAK` tags, and dropping them here would silently send it back to the
    # weaker derived yardstick.
    lo_ms = lo * (1000.0 / fs)
    kept = [
        replace(ann, onset_ms=ann.onset_ms - lo_ms)
        for ann in rec.annotations
        if lo_ms <= ann.onset_ms < lo_ms + (hi - lo) * (1000.0 / fs)
    ]
    shifted = EdfRecording(
        label=f"{rec.label} [{start_s}..{end_s}s]",
        fs_hz=fs,
        samples_uv=list(rec.samples_uv[lo:hi]),
        rr_ms=list(rec.rr_ms[lo:hi]) if rec.rr_ms else [],
        metrics={},
        annotations=kept,
        start_iso=rec.start_iso,
    )
    return shifted


def replay(rec: EdfRecording) -> List[Tuple[float, List[float], int]]:
    """
    Feed the recording back through `HeartState`, capturing each drawn wave.

    Returns `(located_r_ms, samples, peak)` per committed beat, oldest first.
    `located_ms` is what `PeakFinder` decided the beat's R was, which is what
    the slice was centred on; the wave is `BeatTemplate.from_ecg`'s output.

    The capture works by wrapping `PeakFinder.locate` on the overlay's own
    instance. It is the one place the located time exists — the frame reports
    the wave and its peak, not the slice origin — and reading it here keeps the
    tool honest about what the algorithm actually did rather than re-deriving it.
    """
    state = HeartState(fs_hz=rec.fs_hz)
    overlay = state._overlay  # noqa: SLF001 - the benchmark needs the real seams
    finder = overlay._peak_finder  # noqa: SLF001

    located: List[float] = []

    # The two paths put the slice centre in different places, so the seam that
    # has to be read is different: `PeakFinder` computes it in `locate`, while
    # the QRS path takes the detected peak as-is and it only exists as
    # `_pending_ms` at commit time.
    original_locate = finder.locate
    original_commit = overlay._commit  # noqa: SLF001

    def capture_locate(ecg, expected_ms, template=None, template_window=None, sensor_rr_ms=0):
        found = original_locate(ecg, expected_ms, template, template_window, sensor_rr_ms)
        located.append(float(found))
        return found

    def capture_commit(ecg):
        if overlay._pending_ms is not None:  # noqa: SLF001
            located.append(float(overlay._pending_ms))  # noqa: SLF001
        return original_commit(ecg)

    finder.locate = capture_locate  # type: ignore[method-assign]
    overlay._commit = capture_commit  # type: ignore[method-assign]  # noqa: SLF001

    try:
        events = _events(rec)
        for t_ms, message in events:
            state.on_message(message)
    finally:
        finder.locate = original_locate  # type: ignore[method-assign]
        overlay._commit = original_commit  # type: ignore[method-assign]  # noqa: SLF001

    frame = state.beats_frame(limit=BeatOverlay.STACK_CAP)
    waves = frame["waves"]
    # `locate` is called once per queued beat, in order, but only some of those
    # become drawn waves (warm-up and timebase gating withhold the first few).
    # The drawn ones are the tail of `located`.
    tail = located[-len(waves):] if waves else []
    return [(tail[i], waves[i]["samples"], waves[i]["peak"]) for i in range(len(waves))]


def _events(rec: EdfRecording) -> List[Tuple[int, dict]]:
    """
    The recording as the serial lines the dongle would have sent, in order.

    ECG is grouped into the same ~15-sample batches the dongle emits, stamped so
    that the newest sample of the batch carries the batch time — the ordering
    `HeartState` and `BeatOverlay` are built around. R-R marks become `hr` lines
    at their own sample index, exactly as `recorder.py` recorded them.
    """
    events: List[Tuple[int, dict]] = []
    fs = rec.fs_hz
    uv = rec.samples_uv
    batch = 15
    i = 0
    while i < len(uv):
        chunk = uv[i:i + batch]
        # Stamped with the *first* sample's time, mirroring both the firmware
        # (`ecg_t0_ms`) and `playback_edf.py`, which is the convention
        # `EcgRing.push` expects.
        ts = int(round(i * 1000.0 / fs))
        events.append((ts, {"type": "ecg", "ts": ts, "fs": int(round(fs)),
                            "uv": [int(round(v)) for v in chunk]}))
        i += batch

    for index, rr in rec.rr_marks():
        ts = int(round(index * 1000.0 / fs))
        events.append((ts, {"type": "hr", "ts": ts, "bpm": int(round(60000.0 / rr)) if rr else 0,
                            "rr": [int(round(rr))]}))

    # Stable sort: at equal timestamps ECG goes first, which is the order the
    # dongle writes them and what lets a beat be located by that batch.
    events.sort(key=lambda item: (item[0], 0 if item[1]["type"] == "ecg" else 1))
    return events


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(
    waves: Sequence[Tuple[float, Sequence[float], int]],
    truth_ms: Sequence[float],
) -> Tuple[List[dict], int]:
    """
    One row per wave that a tagged peak can be attributed to, plus a count of
    waves that could not be.

    Attribution is **monotonic**: waves are committed oldest-first and the tags
    are sorted, so each wave must map to a later tag than the previous wave did.
    Matching each wave independently to its nearest tag instead let a wave whose
    slice landed half a cycle out claim the *neighbouring* beat's peak, which
    turned a misplaced beat into an imaginary 600 ms slice error and hid the
    real defect behind an attribution artefact.

    A wave is still dropped when nothing is within half a beat of where its
    slice claims R is: that is a tag gap, not an alignment error, and scoring it
    would mean inventing truth.
    """
    if not truth_ms:
        return [], len(waves)
    rows: List[dict] = []
    unmatched = 0
    cursor = 0
    for located_ms, _samples, peak in waves:
        best_i = -1
        best_dist = float("inf")
        for k in range(cursor, len(truth_ms)):
            dist = abs(truth_ms[k] - located_ms)
            if dist < best_dist:
                best_dist = dist
                best_i = k
            # Tags are sorted, so once we are past the query the rest is worse.
            elif truth_ms[k] > located_ms:
                break
        if best_i < 0:
            unmatched += 1
            continue

        true_ms = truth_ms[best_i]
        rr = BeatTemplate.REST_MS
        if best_i + 1 < len(truth_ms):
            rr = truth_ms[best_i + 1] - true_ms
        elif best_i > 0:
            rr = true_ms - truth_ms[best_i - 1]
        if not (200.0 <= rr <= 3000.0):
            rr = BeatTemplate.REST_MS

        # Outside half a beat the "nearest" tag is a different beat entirely.
        if best_dist > 0.5 * rr:
            unmatched += 1
            continue
        cursor = best_i + 1

        window = BeatTemplate().window_for_rr(rr)
        grid_ms = window.total / (BeatTemplate.GRID_LEN - 1)
        if grid_ms <= 0:
            continue
        r_grid = BeatTemplate.R_AT + (true_ms - located_ms) / grid_ms
        rows.append(
            {
                "located_ms": round(located_ms, 2),
                "true_ms": round(true_ms, 2),
                "slice_err_ms": round(true_ms - located_ms, 2),
                "r_grid": round(r_grid, 2),
                "peak": int(peak),
                "residual": round(r_grid - peak, 2),
                "rr_ms": round(rr, 1),
            }
        )
    return rows, unmatched


def summarise(rows: Sequence[dict]) -> dict:
    if not rows:
        return {"count": 0}
    residuals = [r["residual"] for r in rows]
    slice_errs = [r["slice_err_ms"] for r in rows]
    abs_res = [abs(v) for v in residuals]
    # Spread, not mean: a constant offset is hidden by the anchoring, so the
    # width of the distribution is what actually shows up as a fan.
    return {
        "count": len(rows),
        "median_abs_residual": round(statistics.median(abs_res), 2),
        "p90_abs_residual": round(sorted(abs_res)[int(0.9 * (len(abs_res) - 1))], 2),
        "max_abs_residual": round(max(abs_res), 2),
        "residual_mad": round(statistics.median([abs(v - statistics.median(residuals))
                                                 for v in residuals]), 2),
        "within_2_samples": round(100.0 * sum(1 for v in abs_res if v <= 2) / len(abs_res), 1),
        "within_5_samples": round(100.0 * sum(1 for v in abs_res if v <= 5) / len(abs_res), 1),
        "slice_median_abs_ms": round(statistics.median([abs(v) for v in slice_errs]), 2),
        "slice_p90_abs_ms": round(sorted([abs(v) for v in slice_errs])[
            int(0.9 * (len(slice_errs) - 1))], 2),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("edf", help="EDF recording to replay")
    parser.add_argument("--peaks", help="hand-tagged R times (sample index or ms)")
    parser.add_argument("--units", choices=("samples", "ms"), default="samples",
                        help="unit of --peaks (default: samples)")
    parser.add_argument("--start", type=float, default=0.0,
                        help="replay from this many seconds (default: start of file)")
    parser.add_argument("--end", type=float, default=0.0,
                        help="replay up to this many seconds (0 = end of file)")
    parser.add_argument("--csv", help="write the per-beat rows here")
    parser.add_argument("--tolerance", type=float, default=2.0,
                        help="fail if the median |residual| exceeds this many samples")
    args = parser.parse_args(argv)

    full = read(args.edf)
    rec = slice_recording(full, args.start, args.end)
    offset_ms = args.start * 1000.0
    if args.peaks:
        truth = [t - offset_ms for t in
                 load_tags(args.peaks, full.fs_hz, args.units) if t >= offset_ms]
        source = f"tagged ({len(truth)} peaks in window)"
    else:
        truth = auto_peaks(rec)
        if any(a.text.upper().startswith("R-PEAK") for a in rec.annotations):
            source = f"file annotations ({len(truth)} R-PEAK tags)"
        else:
            source = f"auto-detected ({len(truth)} peaks — provisional)"

    waves = replay(rec)
    rows, unmatched = score(waves, truth)
    stats = summarise(rows)

    print(f"file      {os.path.basename(args.edf)}")
    print(f"truth     {source}")
    print(f"beats     {stats.get('count', 0)} matched of {len(waves)} drawn"
          f"{f' ({unmatched} outside the tag coverage)' if unmatched else ''}")
    if not rows:
        print("no beats matched the truth peaks; nothing to score")
        return 1
    print()
    print(f"  median |residual|   {stats['median_abs_residual']:6.2f} samples")
    print(f"  p90    |residual|   {stats['p90_abs_residual']:6.2f} samples")
    print(f"  max    |residual|   {stats['max_abs_residual']:6.2f} samples")
    print(f"  residual MAD        {stats['residual_mad']:6.2f} samples")
    print(f"  within 2 samples    {stats['within_2_samples']:6.1f} %")
    print(f"  within 5 samples    {stats['within_5_samples']:6.1f} %")
    print()
    print(f"  slice error median  {stats['slice_median_abs_ms']:6.2f} ms")
    print(f"  slice error p90     {stats['slice_p90_abs_ms']:6.2f} ms")

    worst = sorted(rows, key=lambda r: -abs(r["residual"]))[:8]
    if worst:
        print()
        print("  worst beats (residual = where the true R is drawn, in samples)")
        print("    located_ms   true_ms  slice_err  r_grid  peak  residual")
        for r in worst:
            print(f"    {r['located_ms']:10.1f} {r['true_ms']:9.1f} "
                  f"{r['slice_err_ms']:9.1f} {r['r_grid']:7.1f} {r['peak']:5d} "
                  f"{r['residual']:8.1f}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {args.csv}")

    return 0 if stats["median_abs_residual"] <= args.tolerance else 1


if __name__ == "__main__":
    raise SystemExit(main())
