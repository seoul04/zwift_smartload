"""
Guards for the beat-alignment benchmark itself.

The benchmark in `tools/eval_beat_alignment.py` is the thing that makes "the
stack is fanned" a measurable claim, so the parts of it that would be easy to
get subtly wrong are pinned here: the synthetic replay has to reproduce the
frame the server really builds, and the scoring has to attribute each wave to
the right beat.

These do not assert how *good* the alignment currently is — that number is
meant to move as the algorithm improves. They assert that the measuring stick
still measures the right thing.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.edf.reader import Annotation  # noqa: E402
from src.heart.ecg import BeatTemplate, WaveAnchor  # noqa: E402
from tools import eval_beat_alignment as bench  # noqa: E402


# ---------------------------------------------------------------------------
# A synthetic recording: clean beats at a known place
# ---------------------------------------------------------------------------


class _Rec:
    """Just the attributes the benchmark reads off an `EdfRecording`."""

    def __init__(self, fs_hz: float, samples_uv, rr_marks):
        self.fs_hz = fs_hz
        self.samples_uv = samples_uv
        self._rr_marks = rr_marks
        self.metrics = {}

    @property
    def duration_ms(self) -> float:
        return len(self.samples_uv) * 1000.0 / self.fs_hz

    def rr_marks(self):
        return list(self._rr_marks)


def _synthetic(fs_hz=125.0, beats=12, rr_ms=850.0, r_amp=-9000.0, t_amp=1200.0):
    """
    A rhythm of narrow upward R spikes with a T wave behind each.

    The R is made the largest excursion so a correct detector has an unambiguous
    answer, and beats are spaced well beyond the search windows so attribution
    cannot be ambiguous either.
    """
    total_ms = 2000.0 + beats * rr_ms
    n = int(total_ms * fs_hz / 1000.0)
    uv = [0.0] * n
    marks = []
    first_ms = 2000.0
    for b in range(beats):
        r_ms = first_ms + b * rr_ms
        r_idx = int(round(r_ms * fs_hz / 1000.0))
        marks.append((r_idx, rr_ms))
        for d in range(-12, 13):
            i = r_idx + d
            if 0 <= i < n:
                uv[i] += r_amp * math.exp(-((d / 2.2) ** 2))
        # T wave, ~250 ms after R and much smaller.
        t_idx = r_idx + int(round(250.0 * fs_hz / 1000.0))
        for d in range(-25, 26):
            i = t_idx + d
            if 0 <= i < n:
                uv[i] += t_amp * math.exp(-((d / 10.0) ** 2))
    return _Rec(fs_hz, uv, marks)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


def test_auto_peaks_finds_the_r_not_the_t_wave():
    rec = _synthetic()
    peaks = bench.auto_peaks(rec)
    assert len(peaks) == len(rec.rr_marks())
    # Each pick must be within a couple of samples of the R it belongs to.
    for (idx, _rr), found in zip(rec.rr_marks(), peaks):
        r_ms = idx * 1000.0 / rec.fs_hz
        assert abs(found - r_ms) <= 30.0, f"picked {found} for an R at {r_ms}"


def test_auto_peaks_is_empty_without_rr_marks():
    rec = _synthetic()
    rec._rr_marks = []
    assert bench.auto_peaks(rec) == []


def test_load_tags_reads_samples_comments_and_ms(tmp_path):
    path = tmp_path / "peaks.txt"
    path.write_text("# hand tagged\n100\n\n 200  # trailing\n300\n", encoding="utf-8")
    got = bench.load_tags(str(path), fs_hz=100.0, units="samples")
    assert got == [1000.0, 2000.0, 3000.0]

    got_ms = bench.load_tags(str(path), fs_hz=100.0, units="ms")
    assert got_ms == [100.0, 200.0, 300.0]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_events_stamp_ecg_with_the_batch_start():
    """
    `ts` is the first sample's time, as the firmware and `playback_edf.py` write it.

    Getting this wrong by one batch period shifts every located beat and quietly
    poisons everything downstream, so it is worth its own assertion.
    """
    rec = _synthetic(beats=3)
    events = bench._events(rec)
    first = [m for _t, m in events if m["type"] == "ecg"][0]
    assert first["ts"] == 0
    second = [m for _t, m in events if m["type"] == "ecg"][1]
    assert second["ts"] == pytest.approx(round(15 * 1000.0 / rec.fs_hz))


def test_replay_draws_the_synthetic_beats():
    rec = _synthetic()
    waves = bench.replay(rec)
    assert waves, "a clean rhythm should produce drawn waves"
    # The clean beats are all well inside the jitter allowance, so the anchor
    # must land near the nominal R index.
    for _located, _samples, peak in waves:
        assert abs(peak - BeatTemplate.R_AT) <= WaveAnchor.MAX_PEAK_SHIFT


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_reports_no_residual_for_a_perfect_beat():
    """A wave sliced on the truth, and anchored on the truth, has zero error."""
    rec = _synthetic()
    window = BeatTemplate().window_for_rr(850.0)
    grid_ms = window.total / (BeatTemplate.GRID_LEN - 1)
    truth_ms = 10_000.0
    # Put R exactly on R_AT's grid position, and report that as the anchor.
    located = truth_ms - (0) * grid_ms
    waves = [(located, [0.0] * BeatTemplate.GRID_LEN, BeatTemplate.R_AT)]
    rows, unmatched = bench.score(waves, [truth_ms])
    assert unmatched == 0
    assert len(rows) == 1
    assert rows[0]["residual"] == pytest.approx(0.0, abs=0.01)


def test_score_turns_a_slice_error_into_a_residual():
    """One grid sample of slice error is one grid sample of residual."""
    rec = _synthetic()
    truth_ms = 10_000.0
    # Two tags 850 ms apart, so the interval the scorer derives is the real one
    # rather than its fallback; the gap is wide enough not to clip the wave.
    truth = [truth_ms, truth_ms + 850.0]
    grid_ms = BeatTemplate().window_for_rr(850.0).total / (BeatTemplate.GRID_LEN - 1)
    located = truth_ms + 3 * grid_ms          # slice put R three samples early
    waves = [(located, [0.0] * BeatTemplate.GRID_LEN, BeatTemplate.R_AT)]
    rows, _ = bench.score(waves, truth)
    assert rows[0]["r_grid"] == pytest.approx(BeatTemplate.R_AT - 3.0, abs=0.05)
    assert rows[0]["residual"] == pytest.approx(-3.0, abs=0.05)


def test_score_drops_a_wave_that_is_outside_the_tag_coverage():
    rec = _synthetic()
    waves = [(10_000.0, [0.0] * BeatTemplate.GRID_LEN, BeatTemplate.R_AT)]
    rows, unmatched = bench.score(waves, [])
    assert rows == []
    assert unmatched == 1


def test_score_will_not_reach_past_half_a_beat_for_a_tag():
    """
    A wave more than half an interval from every tag is unmatched, not snapped.

    Nearest-tag matching (without the half-beat guard) would attribute a badly
    placed slice to whichever R happened to be closest and report a plausible
    residual for a beat it never actually matched, hiding the defect.
    """
    rec = _synthetic()
    truth = [10_000.0]
    # One tag, so the scorer falls back to the default interval for the guard:
    # 500 ms out is beyond half of it.
    waves = [(10_500.0, [0.0] * BeatTemplate.GRID_LEN, BeatTemplate.R_AT)]
    rows, unmatched = bench.score(waves, truth)
    assert rows == []
    assert unmatched == 1


def test_summarise_uses_spread_not_just_the_mean():
    """The residual MAD is what exposes a fan; a constant offset must not look bad."""
    constant = [{"residual": 7.0, "slice_err_ms": 30.0} for _ in range(10)]
    assert bench.summarise(constant)["residual_mad"] == pytest.approx(0.0)
    assert bench.summarise(constant)["median_abs_residual"] == pytest.approx(7.0)

    fanned = [{"residual": (-8.0 if i % 2 else 8.0), "slice_err_ms": 30.0} for i in range(10)]
    assert bench.summarise(fanned)["residual_mad"] == pytest.approx(8.0)


def test_annotated_r_peaks_are_preferred_over_a_derived_apex():
    """
    A file tagged by an external annotator already holds truth.

    Scoring a detector against the apex this tool re-derives is marking its own
    homework: both would make the same mistakes. EDFbrowser's `R-PEAK` tags come
    from a different algorithm on the same signal, which is what makes them a
    yardstick rather than a mirror.
    """
    rec = _synthetic()
    # Tag peaks that are deliberately nowhere near the signal's real apexes: if
    # the tags are used at all, the result tracks them and not the waveform.
    rec.annotations = [
        Annotation(onset_ms=111.5, duration_ms=0.0, text="R-PEAK"),
        Annotation(onset_ms=999.0, duration_ms=0.0, text="R-PEAK"),
    ]
    assert bench.auto_peaks(rec) == [111.5, 999.0]
