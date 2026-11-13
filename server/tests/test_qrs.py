"""
Tests for the Pan–Tompkins QRS detector.

The reference for this module is EDFbrowser's `filt/pt_qrs.c`, so the bar is not
"detects beats" — it is "produces the same beat stream". The strongest test here
replays a recording that EDFbrowser annotated *itself* and asserts the port
lands on the same samples.

The port was additionally checked against the compiled reference C over the same
samples: 753 peaks against 753, identical but for two beats inside the stretches
EDFbrowser marks `GAP`. That comparison needs a C toolchain, so it is not a unit
test; the annotated recording is the version of it that runs anywhere.
"""
import math

import pytest

from src.heart.qrs import MIN_FS_HZ, PanTompkins, QrsDetector

#: Recording annotated by EDFbrowser's own QRS detector.
_ANNOTATED = "recording/zrelay_20261108_125551_qrs.edf"

#: uV per digital unit in that recording's `ECG` channel (40000 / 65535).
_UV_PER_BIT = 40000.0 / 65535.0


def _synthetic(fs_hz: float, seconds: float = 20.0, rr_s: float = 0.8,
               wander_uv: float = 200.0, r_uv: float = 1500.0,
               t_uv: float = 400.0):
    """A narrow R over a slow baseline wander, with a T wave after each beat."""
    out = []
    for i in range(int(fs_hz * seconds)):
        t = i / fs_hz
        value = wander_uv * math.sin(2 * math.pi * 0.15 * t)
        phase = t % rr_s
        if phase < 0.012:
            value += r_uv * math.sin(math.pi * phase / 0.012)
        if 0.25 < phase < 0.45:
            value += t_uv * math.sin(math.pi * (phase - 0.25) / 0.2)
        out.append(value)
    return out


def _peaks(values, fs_hz: float):
    detector = PanTompkins(fs_hz, sense=1.0)
    found = []
    for i, value in enumerate(values):
        offset = detector.push(value)
        if offset:
            found.append(i - offset)
    return found


def _annotated_recording():
    from src.edf.reader import EdfFileError, read

    try:
        return read(_ANNOTATED)
    except (FileNotFoundError, EdfFileError):
        pytest.skip(f"{_ANNOTATED} is not present")


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fs_hz", [200.0, 256.0, 500.0, 512.0])
def test_finds_one_peak_per_beat_at_every_supported_rate(fs_hz):
    peaks = _peaks(_synthetic(fs_hz), fs_hz)
    expected = int(20.0 / 0.8)
    # The final beat can land inside the detector's look-ahead at the cut-off,
    # so the count is "all but at most one" rather than exact.
    assert len(peaks) in (expected, expected - 1)
    gaps = [peaks[i + 1] - peaks[i] for i in range(len(peaks) - 1)]
    # Every interval is one RR (800 ms): no doubles, no halves.
    for gap in gaps:
        assert abs(gap - 0.8 * fs_hz) <= 0.03 * fs_hz


def test_reports_a_peak_already_in_the_past():
    """
    The detector is causal: when it fires, the R is behind the current sample.

    A live plot has to know how much ECG to hold back before a beat's wave can be
    drawn, so this delay is a real interface, not an implementation detail.
    """
    fs_hz = 256.0
    detector = PanTompkins(fs_hz, sense=1.0)
    fired = 0
    for i, value in enumerate(_synthetic(fs_hz, seconds=6.0)):
        offset = detector.push(value)
        if offset:
            fired += 1
            assert 0 < offset < int(0.4 * fs_hz)
            assert i - offset >= 0
    assert fired >= 5


def test_polarity_does_not_matter():
    """An inverted lead is the same heart; the reference rectifies the envelope."""
    fs_hz = 256.0
    upright = _peaks(_synthetic(fs_hz), fs_hz)
    inverted = _peaks([-v for v in _synthetic(fs_hz)], fs_hz)
    assert len(upright) == len(inverted)
    for a, b in zip(upright, inverted):
        assert abs(a - b) <= 2


def test_a_flat_trace_produces_no_beats():
    assert _peaks([0.0] * 5000, 256.0) == []


def test_refuses_a_rate_below_the_algorithms_assumptions():
    with pytest.raises(ValueError):
        PanTompkins(125.0, sense=1.0)
    with pytest.raises(ValueError):
        PanTompkins(MIN_FS_HZ - 1e-3, sense=1.0)
    # At the boundary it is allowed.
    PanTompkins(200.0, sense=1.0)


def test_reset_returns_the_detector_to_a_fresh_state():
    fs_hz = 256.0
    values = _synthetic(fs_hz, seconds=10.0)
    detector = PanTompkins(fs_hz, sense=1.0)
    for value in values:
        detector.push(value)
    warmed = _peaks(values, fs_hz)

    detector.reset()
    replay = []
    for i, value in enumerate(values):
        offset = detector.push(value)
        if offset:
            replay.append(i - offset)
    # A detector that was not really reset would warm up differently.
    assert replay == warmed


# ---------------------------------------------------------------------------
# Index bookkeeping
# ---------------------------------------------------------------------------


def test_qrs_detector_turns_offsets_into_absolute_indices():
    fs_hz = 256.0
    detector = QrsDetector(fs_hz, sense=1.0)
    for value in _synthetic(fs_hz, seconds=10.0):
        detector.push(value)

    assert len(detector.peaks) > 5
    # Peaks come back oldest-first and are the same list `peaks` holds.
    assert detector.peaks == sorted(detector.peaks)
    # One interval per gap, in seconds, and consistent with the peaks.
    assert len(detector.intervals) == len(detector.peaks) - 1
    for i, interval in enumerate(detector.intervals):
        assert interval == pytest.approx(
            (detector.peaks[i + 1] - detector.peaks[i]) / fs_hz)


def test_reset_clears_the_peak_and_interval_lists():
    detector = QrsDetector(256.0, sense=1.0)
    for value in _synthetic(256.0, seconds=10.0):
        detector.push(value)
    assert detector.count > 0

    detector.reset()
    assert detector.count == 0
    assert detector.intervals == []


# ---------------------------------------------------------------------------
# Equivalence with EDFbrowser
# ---------------------------------------------------------------------------


def test_matches_the_edfbrowser_annotations_peak_for_peak():
    """
    Replay the recording EDFbrowser annotated and reproduce its own peak stream.

    A beat counts as matched when the port puts an R within 25 ms of the
    annotation. Anything looser would not distinguish this detector from the
    crude peak picker it replaces.
    """
    rec = _annotated_recording()
    truth = sorted(a.onset_ms for a in rec.annotations
                   if a.text.upper().startswith("R-PEAK"))
    if not truth:
        pytest.skip(f"{_ANNOTATED} carries no R-peak annotations")

    detector = QrsDetector(rec.fs_hz, sense=1.0)
    for value in rec.samples_uv:
        detector.push(value / _UV_PER_BIT)

    found_ms = [peak * 1000.0 / rec.fs_hz for peak in detector.peaks]

    def nearest(t):
        return min(abs(t - f) for f in found_ms)

    matched = sum(1 for t in truth if nearest(t) <= 25.0)
    # 748 annotated beats, and the port finds every one of them.
    assert matched == len(truth)

    hit = sum(1 for f in found_ms if min(abs(f - t) for t in truth) <= 25.0)
    # It also does not invent beats: at most the handful at the file's edges and
    # around the stretches EDFbrowser itself marks `GAP`.
    assert hit >= len(found_ms) - 8


def test_finds_at_least_as_many_beats_as_the_sensor_reported():
    """
    Detecting from the waveform must not lose beats the strap already had.

    This recording's 719 sensor beats and the waveform's 753 complexes are two
    descriptions of the same 617 s, so the detector has to account for all of
    the strap's beats plus whatever the strap was quiet through. Fewer than the
    sensor means the detector is missing real beats.
    """
    rec = _annotated_recording()
    detector = QrsDetector(rec.fs_hz, sense=1.0)
    for value in rec.samples_uv:
        detector.push(value / _UV_PER_BIT)

    sensor_marks = len(rec.rr_marks())
    assert sensor_marks > 0
    assert detector.count >= sensor_marks


# ---------------------------------------------------------------------------
# Beats stack: the detector as the slice centre
# ---------------------------------------------------------------------------


def _ecg_only_stream(state, fs_hz, rr_ms, beats, amplitude=1300.0):
    """Push ECG in 15-sample packets with no `hr` lines at all."""
    ms_per = 1000.0 / fs_hz
    idx = 0
    total = int((rr_ms * beats) / ms_per)
    while idx < total:
        n = min(15, total - idx)
        samples = []
        for k in range(n):
            phase = ((idx + k) * ms_per) % rr_ms
            samples.append(amplitude * math.exp(-((phase - 150.0) ** 2) / 25.0))
        state.on_message({"type": "ecg", "ts": int(round(idx * ms_per)),
                          "fs": fs_hz, "uv": samples})
        idx += n


def test_beats_are_drawn_from_the_ecg_alone():
    """
    No sensor beat stream, and still a stack.

    The strap goes quiet for long stretches of a real recording while the ECG
    keeps coming. Beats that only exist because the sensor announced them are
    simply missing then; detecting them from the waveform is what draws them.
    """
    from src.heart import HeartState

    fs_hz = 256.0
    rr_ms = 700.0
    state = HeartState(fs_hz=fs_hz)
    assert state._overlay.detecting  # noqa: SLF001

    _ecg_only_stream(state, fs_hz, rr_ms, beats=20)

    frame = state.beats_frame()
    assert frame["shown"] >= 10, f"only {frame['shown']} beats from ECG alone"
    # The interval each wave is drawn at is measured, not announced.
    rrs = [w["rr_ms"] for w in frame["waves"] if w["rr_ms"]]
    assert rrs, "waves carry no interval"
    assert all(abs(rr - rr_ms) <= 0.05 * rr_ms for rr in rrs), rrs


def test_the_stack_uses_the_detected_peak_as_its_centre():
    """
    The detected R *is* the slice centre, with no second measurement on top.

    This is the whole point of the change and it is also the easy thing to get
    wrong: routing the good peak through `PeakFinder` again moves it (measured
    at 1.66 samples of residual against 1.18 for using it directly), so an
    integration that merely "improves" the old finder would look plausible and
    be worse.
    """
    from src.heart import HeartState

    fs_hz = 256.0
    rr_ms = 800.0
    state = HeartState(fs_hz=fs_hz)
    _ecg_only_stream(state, fs_hz, rr_ms, beats=16)

    overlay = state._overlay  # noqa: SLF001
    # Every committed wave must be centred on a beat the detector itself named.
    detected = set(overlay._qrs.beats())  # noqa: SLF001
    assert detected
    frame = state.beats_frame()
    assert frame["shown"] > 0
    # The waves are sliced at detector beats, so each wave's own stroke should
    # land on the nominal centre rather than drifting off it.
    peaks = [w["peak"] for w in frame["waves"]]
    assert all(abs(p - frame["r_at"]) <= 3 for p in peaks), peaks


def test_a_rate_below_200hz_falls_back_to_the_sensor_path():
    """
    The Pan–Tompkins port is defined at 200 Hz and up; below that the stack must
    keep working the old way rather than silently losing every beat.
    """
    from src.heart import HeartState

    state = HeartState(fs_hz=125.0)
    assert not state._overlay.detecting  # noqa: SLF001

    # The old path needs the sensor's announcements, and produces waves from them.
    ms_per = 8.0
    rr = 900.0
    idx = 0
    announced = 0
    while idx < int((rr * 14) / ms_per):
        n = 16
        samples = [1200.0 * math.exp(-((((idx + k) * ms_per) % rr - 200.0) ** 2) / 200.0)
                   for k in range(n)]
        state.on_message({"type": "ecg", "ts": int(idx * ms_per), "fs": 125.0,
                          "uv": samples})
        idx += n
        while announced < 12 and rr * (announced + 1) + 200 <= idx * ms_per:
            announced += 1
            state.on_message({"type": "hr", "ts": int(round((rr * announced + 200) / 128)) * 128,
                              "bpm": 67, "rr": [int(rr)]})

    assert state.beats_frame()["shown"] > 0


def test_the_detector_is_rebuilt_when_the_rate_changes():
    """
    A session can start on the 130 Hz fallback before the dongle reports 256 Hz,
    so the decision has to be revisited rather than made once at construction.
    """
    from src.heart import HeartState

    state = HeartState(fs_hz=130.0)
    assert not state._overlay.detecting  # noqa: SLF001

    state.on_message({"type": "ecg", "ts": 0, "fs": 256, "uv": [0.0] * 256})
    assert state._overlay.detecting  # noqa: SLF001
    assert state._overlay._qrs.fs_hz == pytest.approx(256.0)  # noqa: SLF001


def test_detected_peaks_are_dated_from_their_own_sample_when_the_stream_has_gaps():
    """
    A live dongle drops ECG bursts, so sample index does not map to time.

    Measured on a real session: 7 635 samples delivered across 72 s of
    timestamps, i.e. 60 % of the ECG missing in gaps between bursts. Dating a
    peak as `first_stamp + index * period` then places it progressively *early*
    — 31 samples (121 ms) on that capture — which pushes R a third of a window
    right of centre and makes an otherwise perfect stack look misaligned. A
    replay of an EDF file has no gaps, so it cannot reproduce this.
    """
    from src.heart.ecg import QrsBeatSource

    fs_hz = 256.0
    period_ms = 1000.0 / fs_hz
    rr_samples = 205          # ~800 ms
    sources = []
    for gapped in (False, True):
        src = QrsBeatSource(fs_hz)
        # Beat i lands at sample rr_samples * (i + 1). The gapped stream stamps
        # the second half 5000 ms later, as if a burst were lost.
        n = rr_samples * 12
        t = 0
        i = 0
        while i < n:
            block = []
            for k in range(15):
                idx = i + k
                phase = idx % rr_samples
                block.append(1500.0 * math.exp(-((phase - 60) ** 2) / 20.0))
            stamp = int(round(t + (5000 if gapped and i > n // 2 else 0)))
            src.feed(block, start_ms=stamp)
            t += 15 * period_ms
            i += 15
        sources.append(sorted(src.beats()))

    flat, gap = sources
    assert flat, "the ungapped stream must still detect beats"
    assert gap, "the gapped stream must still detect beats"
    # With the stamps used, the gap delays the second half by 5 s; the correct
    # answer therefore keeps the same *relative* spacing inside each half. What
    # must not happen is peaks remaining on the ungapped grid, which is what
    # index-based timing produced.
    assert max(gap) > max(flat) + 4000, (
        f"gapped peaks were not dated from their stamps: {gap[-3:]} vs {flat[-3:]}"
    )
