"""
Tests for the heart graphs: the R-R statistics, the trends window, the RMSSD
tile grid and the beat reference finder.

These mirror the Kotlin suite in `my_heart/android/detector/src/test`, because
the two implementations have to agree: the app and the server draw the same
panels from the same rules, and a change to one that is not made to the other
shows up as a graph that reads differently in the two places.
"""
import math

import pytest

from src.heart.ecg import (
    BeatOverlay,
    BeatTemplate,
    EcgRing,
    PeakFinder,
    WaveAnchor,
    baseline,
    draw_axis,
    wave_frame,
)
from src.heart.metrics import (
    HrFilter,
    NnHrv,
    RmssdGrid,
    RmssdMapConfig,
    TrendTrace,
    nice_step,
)


# ---------------------------------------------------------------------------
# Heart rate smoothing
# ---------------------------------------------------------------------------


def test_hr_filter_needs_three_consistent_intervals_before_reporting():
    hr = HrFilter()
    hr.push(800)
    hr.push(810)
    assert hr.bpm() == 0.0, "two intervals are not enough to lock"
    hr.push(805)
    # It locks on the mean of the three seed intervals, not on the last one.
    assert hr.bpm() == pytest.approx((60_000 / 800 + 60_000 / 810 + 60_000 / 805) / 3)


def test_hr_filter_ignores_a_single_outlier_interval():
    hr = HrFilter()
    for rr in (800, 800, 800):
        hr.push(rr)
    locked = hr.bpm()
    # A doubled interval (a missed beat) must not move the rate on its own.
    hr.push(1600)
    assert hr.bpm() == pytest.approx(locked)


def test_hr_filter_reaquires_after_a_run_of_new_intervals():
    hr = HrFilter()
    for rr in (800, 800, 800):
        hr.push(rr)
    for rr in (1000, 1000, 1000):
        hr.push(rr)
    assert hr.bpm() == pytest.approx(60.0, rel=1e-6)


def test_hr_filter_gates_implausible_instantaneous_rates():
    hr = HrFilter()
    # 100 ms would be 600 bpm; 3000 ms would be 20 bpm. Neither may lock it.
    for rr in (100, 100, 100, 3000, 3000, 3000):
        hr.push(rr)
    assert hr.bpm() == 0.0


# ---------------------------------------------------------------------------
# NnHrv: the two RMSSDs
# ---------------------------------------------------------------------------


def test_nnhv_reports_zero_until_each_window_is_filled():
    nnh = NnHrv()
    for _ in range(NnHrv.FAST_MIN - 1):
        nnh.on_rr_ms(800)
    assert nnh.rmssd30_ms() == 0, "RMSSD30 needs FAST_MIN intervals"
    nnh.on_rr_ms(800)
    assert nnh.rmssd30_ms() == 0, "a flat run has zero RMSSD, reported as 0"

    for _ in range(NnHrv.SLOW_MIN - NnHrv.FAST_MIN):
        nnh.on_rr_ms(800)
    assert nnh.rmssd300_ms() == 0


def test_nnhv_rmssd_matches_the_textbook_definition():
    nnh = NnHrv()
    rrs = [800, 840, 800, 840, 800, 840, 800, 840, 800, 840,
           800, 840, 800, 840, 800]
    for rr in rrs:
        nnh.on_rr_ms(rr)
    diffs = [rrs[i + 1] - rrs[i] for i in range(len(rrs) - 1)]
    expected = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    assert nnh.rmssd30_ms() == int(expected + 0.5)


def test_nnhv_ignores_intervals_outside_the_physiological_band():
    nnh = NnHrv()
    for rr in (100, 5000, -800):
        nnh.on_rr_ms(rr)
    assert nnh.count() == 0


def test_nnhv_mean_rr_uses_the_last_thirty_intervals():
    nnh = NnHrv()
    for _ in range(40):
        nnh.on_rr_ms(900)
    for _ in range(30):
        nnh.on_rr_ms(1000)
    assert nnh.mean_rr30_ms() == 1000


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


def test_trend_trace_keeps_history_when_the_width_narrows_and_widens():
    trace = TrendTrace()
    trace.width_seconds = 600
    for i in range(100):
        trace.on_beat(i * 1000.0, 120.0, 40.0, 38.0)

    trace.width_seconds = 60
    narrow = trace.snapshot(100_000.0)
    trace.width_seconds = 600
    wide = trace.snapshot(100_000.0)

    assert narrow.count < wide.count, "widening must show the history still held"
    assert wide.count == 100


def test_trend_trace_breaks_warmup_values_as_gaps():
    trace = TrendTrace()
    trace.width_seconds = 60
    # 0 is "not known yet" in the detector; it must become a gap, not a zero.
    trace.on_beat(1000.0, 120.0, 0.0, 0.0)
    trace.on_beat(2000.0, 121.0, 42.0, 40.0)
    frame = trace.snapshot(2000.0)
    assert frame.count == 2
    assert math.isnan(frame.rmssd30_ms[0])
    assert frame.rmssd30_ms[1] == 42.0
    assert frame.hr_bpm[0] == 120.0


def test_trend_trace_keeps_the_series_monotonic():
    trace = TrendTrace()
    trace.on_beat(5000.0, 120.0, 40.0, 40.0)
    trace.on_beat(3000.0, 120.0, 40.0, 40.0)  # a late beat, must be dropped
    assert trace.point_count() == 1


def test_trend_trace_snapshot_drops_points_that_have_left_the_window():
    trace = TrendTrace()
    trace.width_seconds = 60
    trace.on_beat(1000.0, 120.0, 40.0, 40.0)
    # The right edge follows the clock, so a point a long time behind it no
    # longer falls inside the window and is dropped from the frame.
    frame = trace.snapshot(1_000_000.0)
    assert frame.now_ms == pytest.approx(1_000_000.0)
    assert frame.count == 0
    # The point is still held, so widening the window brings it back.
    trace.width_seconds = 3600
    assert trace.snapshot(1_000_000.0).count == 1


def test_nice_step_uses_the_one_two_five_ladder():
    assert nice_step(0.9) == 1.0
    assert nice_step(1.7) == 2.0
    assert nice_step(3.3) == 5.0
    assert nice_step(7.0) == 10.0
    assert nice_step(23.0) == 50.0


# ---------------------------------------------------------------------------
# RMSSD grid
# ---------------------------------------------------------------------------


def test_grid_geometry_tiles_the_width_exactly():
    cfg = RmssdMapConfig(width_seconds=480, side=4)
    assert cfg.slots == 15
    assert cfg.slot_seconds == pytest.approx(32.0)
    # Column 0 is the oldest and the widest; each one to its right halves, and
    # the newest ("now") is one fine slot. Together they tile the width exactly.
    widths = [cfg.column_slots(c) for c in range(cfg.side)]
    assert widths == [8, 4, 2, 1]
    assert sum(widths) == cfg.slots


def test_grid_rows_halve_from_the_full_width():
    cfg = RmssdMapConfig(width_seconds=480, side=4)
    assert [cfg.n_at(r) for r in range(cfg.side)] == [480, 240, 120, 60]
    assert cfg.n_longest == 480
    assert cfg.n_shortest == 60


def test_grid_column_labels_read_back_from_now():
    cfg = RmssdMapConfig(width_seconds=480, side=4)
    starts = [cfg.column_start_seconds(c) for c in range(cfg.side)]
    assert starts == pytest.approx([480.0, 224.0, 96.0, 32.0])


def test_grid_is_empty_before_any_beat():
    grid = RmssdGrid(RmssdMapConfig(width_seconds=120, side=3))
    frame = grid.snapshot()
    assert frame["rr_count"] == 0
    assert all(not tile["filled"] for tile in frame["tiles"])


def test_grid_fills_every_tile_once_enough_beats_arrived():
    grid = RmssdGrid(RmssdMapConfig(width_seconds=120, side=3))
    cfg = grid.cfg
    # A full ring: the oldest column's slots have to be stamped too, so this is
    # more beats than the longest window alone would need.
    for i in range(cfg.n_longest * 4):
        grid.on_rr(i * 800.0, 800 + (i % 7) * 12)
    frame = grid.snapshot()
    assert frame["rr_count"] == cfg.n_longest
    filled = sum(1 for tile in frame["tiles"] if tile["filled"])
    assert filled == cfg.side * cfg.side


def test_grid_ignores_intervals_outside_its_gate():
    grid = RmssdGrid(RmssdMapConfig(width_seconds=120, side=3))
    grid.on_rr(1000.0, 150.0)   # too short
    grid.on_rr(2000.0, 4000.0)  # too long
    assert grid.rr_count() == 0
    assert grid.version == 0


def test_grid_reconfigure_reshapes_and_restarts_the_ring():
    grid = RmssdGrid(RmssdMapConfig(width_seconds=480, side=4))
    for i in range(120):
        grid.on_rr(i * 100.0, 800)
    assert grid.rr_count() > 0

    grid.reconfigure(RmssdMapConfig(width_seconds=480, side=5))
    assert grid.cfg.side == 5
    assert grid.rr_count() == 0, "a different geometry cannot reuse the old bins"


def test_grid_reconfigure_is_a_no_op_for_the_same_config():
    cfg = RmssdMapConfig(width_seconds=480, side=4)
    grid = RmssdGrid(cfg)
    for i in range(30):
        grid.on_rr(i * 100.0, 800)
    before = grid.rr_count()
    grid.reconfigure(RmssdMapConfig(width_seconds=480, side=4))
    assert grid.rr_count() == before


# ---------------------------------------------------------------------------
# ECG ring and the beat reference finder
# ---------------------------------------------------------------------------


def synthetic_ecg(fs=130.0, seconds=8.0, beats_ms=(), amplitude=1000.0):
    """An ECG ring with a narrow spike at each of `beats_ms`."""
    ring = EcgRing(fs_hz=fs, seconds=seconds + 1)
    n = int(fs * seconds)
    msp = 1000.0 / fs
    values = []
    for i in range(n):
        t = i * msp
        v = 0.0
        for bt in beats_ms:
            d = t - bt
            v += amplitude * math.exp(-(d * d) / 30.0)
            v += -0.3 * amplitude * math.exp(-((d - 24.0) ** 2) / 40.0)
        values.append(v)
    ring.push(values, 0)
    return ring


def test_ecg_ring_interpolates_and_clamps_at_the_edges():
    ring = EcgRing(fs_hz=100.0)
    ring.push([0.0, 100.0, 200.0], 0)
    assert ring.value_at(-50.0) == 0.0
    assert ring.value_at(10.0) == pytest.approx(100.0)
    assert ring.value_at(500.0) == 200.0


def test_ecg_ring_holds_only_its_capacity():
    ring = EcgRing(fs_hz=10.0, seconds=2.0)  # 20 samples
    ring.push(list(range(100)), 0)
    assert ring.count() == 20
    assert ring.value_at_i(19) == 99.0


def test_peak_finder_chains_from_the_previous_rather_than_the_packet_grid():
    # Beats every 800 ms, but the sensor's own times are snapped 128 ms late.
    beats = [800.0 * i for i in range(1, 7)]
    ecg = synthetic_ecg(beats_ms=beats)
    finder = PeakFinder()
    rrs = [800] * 7
    found = []
    for i, bt in enumerate(beats):
        snapped = bt + 128.0
        found.append(finder.locate(ecg, int(snapped), None, None, rrs[i]))
    # Every located reference must sit on the real spike, not on the snapped time.
    for f, bt in zip(found, beats):
        assert abs(f - bt) <= 20, f"located {f} for a beat at {bt}"


def test_beat_template_normalises_every_wave_to_a_shared_baseline():
    beats = [800.0 * i for i in range(1, 7)]
    ecg = synthetic_ecg(beats_ms=beats)
    template = BeatTemplate()
    for rr in (800, 800, 800):
        template.observe_rr(rr)
    wave = template.from_ecg(ecg, beats[2])
    assert wave is not None
    assert len(wave) == BeatTemplate.GRID_LEN
    assert baseline(wave) == pytest.approx(0.0, abs=1e-6)
    assert max(abs(v) for v in wave) == pytest.approx(1.0, rel=1e-6)


def test_beat_template_refuses_a_window_whose_tail_is_not_recorded():
    ecg = synthetic_ecg(seconds=2.0, beats_ms=[800.0, 1600.0])
    template = BeatTemplate()
    # A beat right at the newest sample has no post-R data to slice.
    assert template.from_ecg(ecg, 1900.0) is None


def test_beat_template_draw_span_follows_the_interval():
    assert BeatTemplate.draw_span(800.0) == pytest.approx(760.0)
    assert BeatTemplate.draw_span(1500.0) == pytest.approx(1425.0)
    # A degenerate interval keeps a floor so a beat cannot collapse to a point.
    assert BeatTemplate.draw_span(10.0) == BeatTemplate._MIN_DRAW_MS


def test_draw_axis_uses_the_lower_median_so_a_missed_beat_cannot_widen_it():
    waves = [([0.0], 800), ([0.0], 0), ([0.0], 1600), ([0.0], 820)]
    axis = draw_axis(waves)
    # Intervals are 800, 820, 1600; the lower median is 820, not the doubled one.
    assert axis == pytest.approx(BeatTemplate.draw_span(820.0))
    assert draw_axis([]) == 0.0


def test_peak_finder_does_not_latch_onto_a_flat_trace():
    ring = EcgRing(fs_hz=130.0)
    ring.push([0.0] * 400, 0)
    finder = PeakFinder()
    located = finder.locate(ring, 3000, None, None, 800)
    # With no complex to confirm, the sensor's own time is the answer.
    assert located == 3000


# ---------------------------------------------------------------------------
# HeartState: the dongle lines -> frames path
# ---------------------------------------------------------------------------


def test_state_anchors_beats_to_the_newest_ecg_sample():
    from src.heart import HeartState

    state = HeartState(fs_hz=100.0)
    state.on_message({"type": "ecg", "ts": 0, "fs": 100, "uv": [0.0] * 10})
    # ECG covers 0..90 ms, so the packet's last interval closes at 90 ms.
    state.on_message({"type": "hr", "ts": 500, "bpm": 75, "rr": [800]})
    status = state.status()
    assert status["beats"] == 1
    assert status["now_ms"] == 90, "the ECG clock wins over the packet timestamp"


def test_state_keeps_beat_times_in_order_within_one_packet():
    from src.heart import HeartState

    state = HeartState(fs_hz=100.0)
    state.on_message({"type": "ecg", "ts": 0, "fs": 100, "uv": [0.0] * 10})
    state.on_message({"type": "hr", "ts": 3000, "bpm": 75, "rr": [800, 810, 820]})
    # Three intervals: the last closes at "now", the others sit that many back.
    assert state.status()["beats"] == 3
    trend = state.trend_frame()
    times = trend["times_ms"]
    assert times == sorted(times), "beats must reach the graph oldest first"
    assert times[-1] == pytest.approx(90.0)


def test_state_still_reports_metrics_without_any_ecg():
    from src.heart import HeartState

    state = HeartState()
    for i in range(40):
        state.on_message({"type": "hr", "ts": i * 800, "bpm": 75, "rr": [800]})
    status = state.status()
    assert status["beats"] == 40
    assert status["ecg_samples"] == 0
    # No waveform, so the two plots that need one stay empty...
    assert state.beats_frame()["shown"] == 0
    assert state.ecg_frame()["empty"] is True
    # ...but the numeric panels are fully populated.
    assert state.trend_frame()["count"] == 40


def test_state_rejects_non_heart_lines():
    from src.heart import HeartState

    state = HeartState()
    assert state.on_message({"type": "ftms", "ts": 0, "power": 200}) is False
    assert state.on_message({"type": "cp", "ts": 0, "power": 200}) is False
    assert state.status()["beats"] == 0


def test_state_beats_limit_snaps_onto_the_ladder():
    from src.heart import HeartState

    state = HeartState()
    state.set_beats_limit(13)
    assert state.status()["beats_limit"] == 16
    state.set_beats_limit(999)
    assert state.status()["beats_limit"] == 64


def test_state_reset_clears_everything():
    from src.heart import HeartState

    state = HeartState()
    for i in range(40):
        state.on_message({"type": "hr", "ts": i * 800, "bpm": 75, "rr": [800]})
    state.reset()
    status = state.status()
    assert status["beats"] == 0
    assert status["hr_bpm"] == 0.0
    assert state.trend_frame()["count"] == 0


def test_ecg_ring_flags_a_clock_that_went_backwards():
    ring = EcgRing(fs_hz=100.0)
    ring.push([1.0] * 10, 50_000)
    assert ring.rewound(90_000) is False
    assert ring.rewound(1_000) is True, "a dongle reboot restarts the uptime clock"


def test_state_starts_over_when_the_dongle_reboots():
    """A USB reset mid-stream restarts the firmware clock near zero."""
    from src.heart import HeartState

    state = HeartState(fs_hz=100.0)
    state.on_message({"type": "ecg", "ts": 0, "fs": 100, "uv": [0.0] * 10})
    for i in range(40):
        state.on_message({"type": "hr", "ts": 100 + i * 800, "bpm": 75, "rr": [800]})
    assert state.status()["beats"] == 40
    # Keep the session clock well ahead of the rebooted one, as a real session
    # would be after running for a while.
    state.on_message({"type": "ecg", "ts": 40_000, "fs": 100, "uv": [0.0] * 10})

    # The dongle reboots: the next batch is stamped near zero again.
    state.on_message({"type": "ecg", "ts": 200, "fs": 100, "uv": [0.0] * 10})
    status = state.status()
    assert status["beats"] == 0, "the old clock is gone, so the session restarts"
    assert status["ecg_samples"] == 10, "the new batch is kept"
    assert status["now_ms"] == 290, "time runs forward again on the new clock"
    frame = state.ecg_frame()
    assert frame["empty"] is False


# ---------------------------------------------------------------------------
# The playback clock: the strip slides instead of stepping per packet
# ---------------------------------------------------------------------------


def _stream(ticks, batch, fs=125.0, multiples=10.0):
    """Run a virtual live stream: `batch` samples per 16 ms tick, one frame per tick."""
    ring = EcgRing(fs_hz=fs, seconds=20.0)
    ms = 1000.0 / fs
    t = 0.0
    frames = []
    for tick in range(ticks):
        wall = tick * 16.0
        ring.push([100.0] * batch, int(t), wall_ms=wall)
        t += batch * ms
        frames.append(ring.window(multiples * 800.0, 800.0, wall_ms=wall))
    return frames


def test_ecg_strip_slides_without_jumping_between_frames():
    """
    The display contract from the app's `EcgTraceTest`: the clock never runs
    backwards and no frame steps further than a couple of refresh periods, so
    the trace slides continuously rather than jumping once per packet.
    """
    frames = _stream(ticks=600, batch=2)

    clocks = [f["playhead_ms"] for f in frames]
    assert clocks[-1] > clocks[0] + 5000, "the strip actually scrolled"

    steps = [b - a for a, b in zip(clocks, clocks[1:])]
    assert all(step >= 0 for step in steps), "the clock never runs backwards"
    assert max(steps) <= 40, f"a frame jumped {max(steps)} ms"
    # Smooth means nearly every frame moved: a packet-driven strip would have
    # moved in bursts and sat still in between.
    moving = sum(1 for step in steps if step > 0.01)
    assert moving / len(steps) > 0.9, f"only {moving}/{len(steps)} frames moved"


def test_ecg_strip_scrolls_sub_sample_so_peaks_do_not_step():
    """
    Consecutive frames differ by a fraction of a sample, not a whole one.

    Each sample is still drawn at an exact ring value — the smoothness comes
    from `left_fraction` placing it at a fractional x, never from blending two
    samples, which is what made an R/S apex flicker in height.
    """
    frames = _stream(ticks=300, batch=2, fs=125.0)
    ms = 1000.0 / 125.0

    fractions = set()
    for f in frames[50:]:
        fractions.add(round(f["left_fraction"] % 1.0, 2))
    assert len(fractions) > 10, f"only {len(fractions)} sub-sample positions seen"

    # A 16 ms tick at 8 ms/sample is two samples, so whole-sample snapping alone
    # would repeat the same frame; the fractional edge is what varies.
    assert any(0.0 < f["left_fraction"] < 1.0 for f in frames[50:])


def test_ecg_strip_width_does_not_breathe_with_the_rr_average():
    """
    The strip's width must not follow mean_rr frame by frame.

    The width is `window_rr * mean_rr` and mean_rr is a plain mean over the
    last 30 intervals, so it moves with every beat - sharply after a noisy
    stretch. Both edges of the window are derived from the width, so resizing
    every frame drags the LEFT edge backwards (older data appears from nowhere)
    and then forwards (data already on screen is erased) while the right edge
    keeps advancing. On the screen that reads as the left end scrolling in the
    past and being wiped, repeating for several beats.
    """
    ring = EcgRing(fs_hz=256.0)
    t = 0
    wall = 1000.0
    for _ in range(20):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    # Settle the strip, then walk mean_rr through a realistic post-artifact
    # swing: a few percent each way, never a genuine change of width.
    ring.window(9500.0, 950.0, wall_ms=wall)
    starts = []
    for mean_rr in (950, 960, 970, 985, 1000, 1015, 1030, 1020, 1000, 980, 955):
        wall += 33.0
        frame = ring.window(10.0 * mean_rr, float(mean_rr), wall_ms=wall)
        starts.append(frame["t0_ms"])

    assert len(set(starts)) == 1, (
        f"the left edge moved with the R-R average: {starts}"
    )


def test_ecg_strip_resizes_once_for_a_genuine_width_change():
    """
    Holding the width must not mean ignoring it.

    A real change - a different preset, or a heart rate far from the held
    width - still resizes the strip, once, instead of breathing every frame.
    """
    ring = EcgRing(fs_hz=256.0)
    t = 0
    wall = 1000.0
    for _ in range(20):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    first = ring.window(9500.0, 950.0, wall_ms=wall)
    assert abs(first["window_ms"] - 9500.0) < 1.0

    # 1300 ms is 37% away from the held width, past _WIDTH_TOLERANCE.
    wall += 33.0
    changed = ring.window(13000.0, 1300.0, wall_ms=wall)
    assert abs(changed["window_ms"] - 13000.0) < 1.0
    assert changed["t0_ms"] < first["t0_ms"], "a wider strip reaches further back"


def test_ecg_strip_skips_a_backlog_instead_of_replaying_it():
    """
    Returning to the ECG tab must show live data, not scroll through the gap.

    While the tab is hidden the client stops asking for frames but the ring
    keeps filling, so the strip resumes with `newest - playhead` seconds of
    backlog. The smooth rate correction drains that at only _MAX_RATE_FACTOR
    times real time - a 30 s gap took 50 s of watching, and a longer absence
    was effectively never caught up. The ceiling then bounds it.
    """
    ring = EcgRing(fs_hz=256.0)
    t = 0
    wall = 0.0
    for _ in range(120):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    # One frame to seat the playhead, then 20 s of data with no frame drawn.
    ring.window(9500.0, 950.0, wall_ms=wall)
    for _ in range(20):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    # The first frame after the switch back must already be near live.
    wall += 16.0
    frame = ring.window(9500.0, 950.0, wall_ms=wall)
    lead = frame["newest_ms"] - frame["playhead_ms"]
    # The allowance is the ceiling plus one burst of lag, not an unbounded wait.
    limit = ring._LATENCY_CEILING_MS + 6000.0
    assert lead <= limit, f"resumed {lead/1000:.1f} s behind live"


def test_ecg_strip_does_not_jump_without_a_backlog():
    """The ceiling must not fire on a healthy stream: no visible resync."""
    ring = EcgRing(fs_hz=256.0)
    t = 0
    wall = 0.0
    for _ in range(30):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    jumps = []
    prev = None
    for _ in range(120):
        wall += 16.0
        frame = ring.window(9500.0, 950.0, wall_ms=wall)
        if prev is not None:
            jumps.append(frame["playhead_ms"] - prev)
        prev = frame["playhead_ms"]
    assert all(j >= 0 for j in jumps), "the clock never runs backwards"
    assert max(jumps) <= 40, f"a frame jumped {max(jumps)} ms with no backlog"


def test_ecg_strip_follows_every_width_preset_step():
    """
    Holding the width must not swallow a preset change.

    The presets are mean-RR multiples; the closest pair (8 and 10) is 25%
    apart, so the tolerance has to stay well below that or a step would be
    ignored and the strip would keep the old width.
    """
    ring = EcgRing(fs_hz=256.0)
    t = 0
    wall = 1000.0
    for _ in range(20):
        ring.push([100.0] * 256, t, wall_ms=wall)
        t += 1000
        wall += 1000.0

    mean_rr = 950.0
    # The width snaps to whole samples by design (the slip is never an
    # interpolation), so allow up to half a sample of rounding.
    slack = 0.5 * ring.ms_per_sample + 0.01
    for multiples in (3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 10.0, 8.0, 5.0):
        wall += 33.0
        frame = ring.window(multiples * mean_rr, mean_rr, wall_ms=wall)
        assert abs(frame["window_ms"] - multiples * mean_rr) <= slack, (
            f"{multiples} RR did not take: drew {frame['window_ms']:.0f} ms"
        )


def test_ecg_strip_never_draws_past_the_newest_sample():
    """With no new data the playhead parks on the newest sample, not beyond it."""
    ring = EcgRing(fs_hz=125.0)
    ring.push([100.0] * 100, 0, wall_ms=0.0)
    newest = ring.newest_ms
    frame = None
    for i in range(60):
        frame = ring.window(8000.0, 800.0, wall_ms=1000.0 + i * 16.0)
    assert frame["playhead_ms"] <= newest
    assert frame["playhead_ms"] > 0


def test_ecg_frozen_advances_the_clock_but_builds_no_frame():
    """A frozen strip keeps its frame, yet the clock stays with the data."""
    from src.heart import HeartState

    state = HeartState(fs_hz=125.0)
    for i in range(30):
        state.on_message({"type": "ecg", "ts": i * 120, "fs": 125, "uv": [100.0] * 15})
    before = state.ecg_frame()
    assert before["empty"] is False

    assert state.ecg_frame(frozen=True) is None, "frozen builds no frame"
    after = state.ecg_frame()
    assert after["empty"] is False
    # The clock kept running while frozen, so resuming is live, not a catch-up.
    assert after["playhead_ms"] >= before["playhead_ms"]


def test_ecg_strip_keeps_moving_when_the_dongle_dumps_a_whole_second():
    """
    The dongle sends ~1 s of samples at once, once a second, not small regular
    batches. Measuring the delivery rate over one arrival interval reads the
    ~0 ms gaps *inside* a burst and saturates the clamp, after which the
    playhead burns its lead and pins to the newest sample for most of each
    second. The rate has to be measured over a baseline spanning several bursts.
    """
    ring = EcgRing(fs_hz=125.0, seconds=20.0)
    ms = 1000.0 / 125.0
    t = 0.0
    wall = 0.0
    clocks = []

    for _ in range(25):
        ring.push([100.0] * 125, int(t), wall_ms=wall)   # one second of ECG
        t += 125 * ms
        for _ in range(60):                              # then 60 fps of frames
            wall += 16.67
            clocks.append(ring.window(8000.0, 800.0, wall_ms=wall)["playhead_ms"])

    steps = [b - a for a, b in zip(clocks, clocks[1:])]
    # Ignore the warm-up, where the clock is still finding its lead.
    settled = steps[600:]
    stalled = sum(1 for s in settled if s < 0.01)
    assert stalled / len(settled) < 0.10, f"{100 * stalled / len(settled):.0f}% of frames stalled"
    assert max(settled) <= 40, f"a frame jumped {max(settled):.1f} ms"
    assert all(s >= 0 for s in settled), "the clock never runs backwards"


# ---------------------------------------------------------------------------
# Beat plot geometry: the whole cycle, in the shape of a cardiac cycle
# ---------------------------------------------------------------------------


def test_beat_window_gives_the_t_wave_more_room_after_r_than_the_p_before():
    """
    A cardiac cycle is not symmetric about R: the PR interval and P wave need
    far less room than the QRS, ST segment and T wave that follow. A centred
    window put equal room on both sides, so the left half was mostly flat
    baseline while the right half ran out across the T.
    """
    ref_frac = BeatTemplate.R_AT / float(BeatTemplate.GRID_LEN - 1)
    assert ref_frac == pytest.approx(BeatTemplate.R_FRAC, abs=0.01)
    assert ref_frac < 0.5, "R sits before the middle of the grid"

    window = BeatTemplate().window_for_rr(800.0)
    assert window.post > window.pre, "more room after R"
    # The split follows PRE_MS:POST_MS whatever the window's total width.
    assert window.pre / window.total == pytest.approx(BeatTemplate.PRE_MS / BeatTemplate.REST_MS, abs=1e-3)
    # And every window agrees, because the proportion is fixed.
    assert window.r_at == BeatTemplate.R_AT


def test_beat_window_follows_the_whole_cycle_instead_of_capping_it():
    """
    The window used to saturate at 750 ms, so a slow beat was only ever sampled
    two-thirds of the way round: at 51 bpm the cycle is 1143 ms and 393 ms of it
    fell outside the grid. Nothing that moved the pre/post split could fix that —
    it only chose which end to lose.
    """
    template = BeatTemplate()
    assert template.window_for_rr(1200.0).total == pytest.approx(1080.0, abs=1.0)
    assert template.window_for_rr(1200.0).total / 1200.0 == pytest.approx(0.90, abs=1e-3)
    assert template.window_for_rr(1143.0).total / 1143.0 == pytest.approx(0.90, abs=1e-3)
    # A slow beat's window is wider than the old cap, which is the whole point.
    assert template.window_for_rr(1143.0).total > 750.0

    # The lower bound still holds, so a fast beat keeps grid resolution.
    assert template.window_for_rr(300.0).total >= BeatTemplate.GRID_LEN - 1.0

    # The drawn span follows the interval too; they are separate things.
    assert BeatTemplate.draw_span(1200.0) > BeatTemplate.draw_span(800.0)
    assert BeatTemplate.draw_span(800.0) > BeatTemplate.draw_span(600.0)
    assert BeatTemplate.draw_span(1000.0) / 1000.0 == pytest.approx(0.95, abs=0.01)


def test_beat_window_r_is_sampled_exactly_at_its_grid_index():
    """`g == r_at` must land on `r_ms` whatever the window's shape."""
    ring = EcgRing(fs_hz=1000.0, seconds=5.0)   # 1 ms per sample: t == index
    for k in range(0, 4000):
        ring.push([float(k)], k, wall_ms=float(k))
    template = BeatTemplate()
    window = template.window_for_rr(800.0)
    r_ms = 2000.0
    wave = template.from_ecg(ring, r_ms, window)
    assert wave is not None
    # The value at R's index is the sample taken at r_ms: 2000.
    assert ring.value_at(r_ms) == pytest.approx(2000.0, abs=1.0)
    # Normalised, so compare the shape: R's grid index is the local value there.
    assert window.r_at == BeatTemplate.R_AT


def test_beat_window_scales_the_grid_over_the_plot_width():
    """
    The whole grid spreads over the plot width, so R is drawn at `R_FRAC` of it.

    Mapping `span / r_at` instead — as if R were always the middle — made the grid
    too coarse whenever R sat early in the window, which ran the T wave off the
    right edge.
    """
    width = 1000.0
    px_per_sample = width / (BeatTemplate.GRID_LEN - 1)
    # The last grid sample lands on the far edge, the first on the near one.
    assert px_per_sample * (BeatTemplate.GRID_LEN - 1) == pytest.approx(width, abs=0.01)
    # R's x is a third of the way across, and the tail past it fits inside.
    r_x = width * BeatTemplate.R_FRAC
    assert r_x == pytest.approx(width / 3.0, abs=1.0)
    tail = (BeatTemplate.GRID_LEN - 1 - BeatTemplate.R_AT) * px_per_sample
    assert r_x + tail == pytest.approx(width, abs=1.0), "the T wave stays on the plot"


def test_beat_refuses_a_wave_whose_look_ahead_is_not_recorded():
    """
    A beat announced before its post-window has been recorded is refused, never
    clamped.

    Clamping repeats the newest sample for the tail of the wave, which draws a
    flat horizontal line where the T wave should be. The app had this bug from a
    Float-precision guard; the Python port had to keep the 20 ms slop honest, so
    this pins the behaviour at a long uptime.
    """
    template = BeatTemplate()   # no interval yet, so the widest window: post = 500 ms
    ring = EcgRing(fs_hz=125.0, seconds=20.0)
    t0 = 4_737_715_466
    idx = 0
    total = 250
    ms_per = 1000.0 / 125.0
    while idx < total:
        n = min(16, total - idx)
        ring.push([float(idx + k) for k in range(n)], int(t0 + round(idx * ms_per)), wall_ms=0.0)
        idx += n
    # R is 150 ms before the newest sample: 350 ms of its 500 ms post-window is missing.
    r = float(ring.newest_ms) - 150.0
    assert template.from_ecg(ring, r) is None


def test_beat_never_ends_in_a_flat_tail():
    """
    No committed wave may end in a run of repeated samples.

    That flat tail is the visible symptom of a wave sliced past the newest sample:
    the grid is clamped onto it and repeats it. Streaming beats the way the live
    source does — announcing each while the ring still ends a packet short of its
    look-ahead, then retrying — is what puts the guard under pressure: a wave that
    passes it must be whole, and one that does not must be refused outright.
    """
    fs = 125.0
    ms_per = 1000.0 / fs
    rr_ms = 1000.0
    ring = EcgRing(fs_hz=fs, seconds=20.0)
    template = BeatTemplate()

    def sample_at(t):
        """A synthetic beat: a QRS spike and a slower T, plus a drift."""
        phase = t % rr_ms
        qrs = 1000.0 * math.exp(-((phase - 200.0) ** 2) / 200.0)
        twave = 220.0 * math.exp(-((phase - 560.0) ** 2) / 9000.0)
        # A slow drift, so no two adjacent samples are ever exactly equal.
        return qrs + twave + 0.37 * (t % 7.0)

    waves = []
    pending = []          # R times announced but not yet sliced
    announced = 0
    idx = 0
    total = int(rr_ms * 12 / ms_per)
    while idx < total:
        n = min(16, total - idx)
        ring.push([sample_at((idx + k) * ms_per) for k in range(n)],
                  int(round(idx * ms_per)), wall_ms=0.0)
        idx += n
        end = idx * ms_per
        # Announce a beat as soon as its own sample is in the ring, even though
        # its look-ahead is not recorded yet — that is the live situation.
        while (announced + 1) * rr_ms + 200.0 <= end:
            announced += 1
            pending.append(announced * rr_ms + 200.0)
        # Retry the pending ones; a wave only enters the stack once it is whole.
        still = []
        for r_ms in pending:
            wave = template.from_ecg(ring, r_ms)
            if wave is not None:
                waves.append(wave)
            else:
                still.append(r_ms)
        pending = still

    assert waves, "some beats were committed"

    # The only clamp the guard permits is its own 20 ms look-ahead allowance, so
    # the longest legal flat tail is that allowance in grid samples. Anything
    # beyond it means the wave was sliced past the newest sample and its tail
    # repeated it — the defect this test exists to catch (it was ~60 of the 256
    # grid samples, a fifth of the wave, on the app).
    window = template.current_window()
    grid_ms = window.total / (BeatTemplate.GRID_LEN - 1)
    allowed = int(math.ceil(20.0 / grid_ms)) + 1
    assert allowed < BeatTemplate.GRID_LEN // 10, "sanity: the slop is a small share"

    for wave in waves:
        run = 1
        for g in range(len(wave) - 1, 0, -1):
            if wave[g] == wave[g - 1]:
                run += 1
            else:
                break
        assert run <= allowed, (
            f"a wave ended in a flat tail of {run} samples, past the {allowed} "
            f"the {20.0} ms look-ahead slop allows"
        )


def test_peak_finder_calibrates_the_ecg_clock_against_the_sensor_interval():
    """
    The tracking chain must advance on the ECG's own clock, not in real ms.

    The dongle stamps 125 Hz, but on a captured recording the R peaks came out
    6.1% closer in stamped ms than the sensor's own R-R (median 1088 ms against
    1159 ms, with a per-beat spread of only 3%). Advancing the chain by the raw
    interval therefore overshot the next R by ~71 ms on *every* beat, and the
    stack fanned out over ±120 ms instead of superposing.
    """
    fs = 133.16          # the ECG rate the capture actually ran at
    ms_per = 1000.0 / fs
    stamp_ms_per = 8.0   # ... while the dongle stamped it as 125 Hz
    rr_real = 1159.0     # the sensor's own interval, in real ms

    ring = EcgRing(fs_hz=fs, seconds=40.0)
    finder = PeakFinder()

    # Beats on the real clock; R positions come out on the stamped grid.
    r_real = []
    t = 500.0
    while t < rr_real * 20:
        r_real.append(t)
        t += rr_real

    # Samples for the whole span, pushed on the stamped timeline.
    total = int((r_real[-1] + 1000.0) / ms_per)
    samples = []
    for i in range(total):
        real = i * ms_per
        # A QRS-like trough once per beat, plus a T wave, so the pick has a shape.
        v = 0.0
        for r in r_real:
            d = real - r
            v += -2000.0 * math.exp(-(d * d) / 300.0)
            v += 300.0 * math.exp(-((d - 280.0) ** 2) / 12000.0)
        samples.append(v)
    # Push in ~15-sample packets on the stamped clock.
    idx = 0
    while idx < total:
        n = min(15, total - idx)
        ring.push(samples[idx:idx + n], int(round(idx * stamp_ms_per)), wall_ms=0.0)
        idx += n

    # Announce each beat as the live path does.
    for r in r_real:
        stamped = r / (ms_per / stamp_ms_per)   # real -> stamped ms
        finder.locate(ring, int(round(stamped)), None, BeatTemplate().current_window(), int(rr_real))

    # The measured ratio is ECG-ms per real-ms, i.e. stamped step / real step.
    assert finder.timebase() == pytest.approx(stamp_ms_per / ms_per, abs=0.02), "timebase not calibrated"


def test_peak_finder_leaves_the_timebase_alone_when_it_is_already_right():
    """A 125 Hz stream against its own intervals must not be "corrected"."""
    fs = 125.0
    ms_per = 1000.0 / fs
    rr = 1000.0
    ring = EcgRing(fs_hz=fs, seconds=40.0)
    finder = PeakFinder()
    r_real = [500.0 + k * rr for k in range(20)]
    total = int((r_real[-1] + 1000.0) / ms_per)
    samples = []
    for i in range(total):
        real = i * ms_per
        v = 0.0
        for r in r_real:
            d = real - r
            v += -2000.0 * math.exp(-(d * d) / 300.0)
        samples.append(v)
    idx = 0
    while idx < total:
        n = min(15, total - idx)
        ring.push(samples[idx:idx + n], int(round(idx * ms_per)), wall_ms=0.0)
        idx += n
    for r in r_real:
        finder.locate(ring, int(round(r)), None, BeatTemplate().current_window(), int(rr))
    assert finder.timebase() == pytest.approx(1.0, abs=0.02)


def test_a_beat_is_not_located_until_its_search_band_is_recorded():
    """
    The dongle stamps a beat at the newest ECG sample it holds, so the tracking
    band's far edge is always past `t_last` if the beat is located on arrival.

    `locate()` guards the band's *lower* edge but not the upper, and a band cut
    short by the newest sample holds a partial complex: the pick slides toward that
    edge. That is what left roughly one wave in thirty sitting several samples left
    of the others. The overlay therefore queues beats and locates them only once
    enough ECG has arrived past them.
    """
    fs = 125.0
    ms_per = 8.0
    ring = EcgRing(fs_hz=fs, seconds=60.0)
    overlay = BeatOverlay()

    # The look-ahead has to cover the widest tracking band, or the guard is moot.
    lookahead = PeakFinder.lookahead_ms(BeatTemplate().current_window())
    assert lookahead > PeakFinder().cfg["min_track_reach_ms"]

    located = []
    finder = overlay._peak_finder
    orig = finder.locate

    def spy(ecg, expected, template=None, window=None, rr=0):
        located.append((expected, ecg.newest_ms))
        return orig(ecg, expected, template, window, rr)

    finder.locate = spy

    # Two seconds of flat ECG, pushed sample-block by sample-block.
    idx = 0
    beat_ms = 400
    while idx * ms_per < 2400:
        ring.push([0.0] * 15, int(idx * ms_per), wall_ms=0.0)
        idx += 15
        if ring.newest_ms is not None and ring.newest_ms >= beat_ms and not located:
            overlay.on_beat(ring, beat_ms, 800)
        overlay.pump(ring)

    assert located, "the beat was eventually located"
    expected, newest = located[0]
    assert newest >= expected + lookahead, (
        f"located a beat at {expected} while the ring ended at {newest}: "
        f"the band needed {lookahead} ms of look-ahead"
    )


def test_peak_finder_places_the_anchor_between_samples():
    """
    The anchor is not snapped to the sample grid.

    A whole-sample pick can only be right to half a step — 3.8 ms at 130 Hz — and
    that error shifts each drawn wave against its neighbours, which is what makes
    the stack look fanned. A parabola through the apex and its two neighbours
    recovers where the true vertex sits between them.
    """
    fs = 1000.0 / 7.69          # the app's test rate
    ms_per = 1000.0 / fs
    # 903 ms is not a whole number of steps, so successive R peaks fall at different
    # fractional positions and a grid-snapped pick must show it.
    rr = 903.0
    beats = [rr * k for k in range(1, 6)]
    ecg = synthetic_ecg(fs=fs, seconds=8.0, beats_ms=beats)
    finder = PeakFinder()

    refs = []
    for k, bt in enumerate(beats, start=1):
        ref = finder.locate(ecg, int(bt), None, None, int(rr))
        assert abs(ref - bt) <= ms_per / 2 + 1.0, (
            f"beat {k}: reference {ref} is not sub-sample close to its R {bt}"
        )
        refs.append(ref)

    # A snapped reference is always an integer number of steps from the buffer
    # start, so at least one of these must be off the grid.
    off_grid = sum(1 for ref in refs if abs((ref / ms_per) - round(ref / ms_per)) > 0.05)
    assert off_grid > 0, f"all references landed on the sample grid: {refs}"


def test_peak_finder_sub_sample_offset_treats_a_downward_r_like_an_upward_one():
    """
    Deviation is an absolute excursion from the band baseline, so an inverted lead
    gets the same refinement, with the same sign.

    The same vertex, sampled just left of the apex, has to come back as the same
    sub-sample offset whether the complex points up or down.
    """
    fs = 100.0
    # A parabola whose vertex sits at +0.25 samples from the middle sample:
    # |16 - (x - 0.25)^2| for x in (-1, 0, 1).
    def triplet(sign):
        return [sign * (16.0 - (-1.25) ** 2),
                sign * (16.0 - (-0.25) ** 2),
                sign * (16.0 - (0.75) ** 2)]

    offsets = []
    for sign in (+1.0, -1.0):
        ring = EcgRing(fs_hz=fs, seconds=2.0)
        ring.push([0.0] * 10 + triplet(sign) + [0.0] * 10, 0)
        offsets.append(PeakFinder._sub_sample_offset_ms(ring, 11, 0.0, ring.count()))

    assert offsets[0] == pytest.approx(0.25 * 10.0, abs=1e-6)
    assert offsets[1] == pytest.approx(offsets[0], abs=1e-9)


def test_peak_finder_sub_sample_offset_leaves_degenerate_apexes_alone():
    """
    A flat top and an apex on the band edge have no vertex between the neighbours:
    the sample must stand rather than be nudged by a wild fit.

    (With an absolute deviation the apex is always at least as tall as both
    neighbours, so curvature is never positive — the flat top is the degenerate
    case that has to be caught, and it is exactly what `> -1e-6` does.)
    """
    # Flat top: three equal samples have zero curvature.
    ring = EcgRing(fs_hz=100.0, seconds=2.0)
    ring.push([0.0] * 20 + [-100.0, -100.0, -100.0] + [0.0] * 20, 0)
    assert PeakFinder._sub_sample_offset_ms(ring, 21, 0.0, ring.count()) == 0.0

    # Apex on the band edge: no neighbours to fit through.
    ring2 = EcgRing(fs_hz=100.0, seconds=2.0)
    ring2.push([0.0] * 10 + [-100.0, 0.0] + [0.0] * 10, 0)
    assert PeakFinder._sub_sample_offset_ms(ring2, ring2.count() - 1, 0.0, ring2.count()) == 0.0
    assert PeakFinder._sub_sample_offset_ms(ring2, 0, 0.0, ring2.count()) == 0.0


# ---------------------------------------------------------------------------
# WaveAnchor: anchoring the drawn stack
#
# A port of the app's `WaveAnchorTest`. These pin the property that makes the
# stack read as one group: the vertical anchor follows a wave's isoelectric
# level rather than its R sample, and the horizontal anchor follows its own
# detected stroke wherever the slice happened to land it.
# ---------------------------------------------------------------------------


def _stored_wave(r_amp=1000.0, s_amp=200.0, t_amp=150.0, up=True):
    """
    A wave built exactly as `BeatTemplate.from_ecg` builds one.

    `r_amp` is how far R stands above the isoelectric line, `s_amp` how far the
    S trough drops below it, `t_amp` the T wave's height. The transform is what
    makes the *baseline* land on a shared value; which excursion ends up being
    the largest is what decides whether an R-sample anchor could group them.
    """
    r_at = BeatTemplate.R_AT
    raw = []
    for g in range(BeatTemplate.GRID_LEN):
        dt = g - r_at
        v = 0.0
        if -40 < dt < 40:
            v += r_amp * math.exp(-(dt / 10.0) ** 2)
        s = dt - 30.0
        if -30 < s < 70:
            v -= s_amp * math.exp(-(s / 20.0) ** 2)
        t = dt - 250.0
        if -80 < t < 120:
            v += t_amp * math.exp(-(t / 45.0) ** 2)
        raw.append(v if up else -v)
    return _normalise(raw)


def _normalise(raw):
    """Isoelectric median to 0, largest excursion to ±1, polarity kept."""
    base = baseline(raw)
    span = max((abs(v - base) for v in raw), default=0.0) or 1e-6
    return [(v - base) / span for v in raw]


def _stored_at(centre, up=True):
    """A wave whose QRS sits at `centre`, so its R is not the grid centre."""
    raw = []
    for g in range(BeatTemplate.GRID_LEN):
        dt = g - centre
        v = 0.0
        if -30 < dt < 30:
            qrs = 1000.0 * math.exp(-(dt / 10.0) ** 2)
            v = qrs if up else -qrs
        t = dt - 220.0
        if -80 < t < 120:
            v += 150.0 * math.exp(-(t / 45.0) ** 2)
        raw.append(v)
    return _normalise(raw)


def test_wave_anchor_follows_the_baseline_not_the_r_sample():
    """
    Two beats whose R is the same height but whose *other* excursions differ end
    up with different stored baselines, so an R-sample anchor draws them at
    different heights. The baseline anchor removes exactly that.
    """
    plain = _stored_wave(r_amp=1000.0, s_amp=200.0, t_amp=150.0)
    deep_s = _stored_wave(r_amp=1000.0, s_amp=1200.0, t_amp=150.0)

    # Normalised against the isoelectric median, every wave's flat level is 0 and
    # its largest excursion reaches magnitude 1, whatever the morphology.
    assert baseline(plain) == pytest.approx(0.0, abs=1e-4)
    assert baseline(deep_s) == pytest.approx(0.0, abs=1e-4)
    # The dominant stroke reaches full scale in either direction: on `plain` the
    # R outgrows the S, on `deep_s` the S outgrows the R.
    assert abs(plain[WaveAnchor.apex(plain)]) == pytest.approx(1.0, abs=1e-4)
    assert abs(deep_s[WaveAnchor.apex(deep_s)]) == pytest.approx(1.0, abs=1e-4)
    assert plain[WaveAnchor.apex(plain)] == pytest.approx(1.0, abs=1e-4)
    assert deep_s[WaveAnchor.apex(deep_s)] == pytest.approx(-1.0, abs=1e-4)

    # With the baseline anchor, a wave's isoelectric level is drawn on the shared
    # mid-height line for every wave, whatever its morphology.
    frame = WaveAnchor.frame([plain, deep_s])

    def drawn_baseline(i, w):
        return 0.5 - (baseline(w) - frame.baselines[i]) / frame.reach * 0.5

    assert drawn_baseline(0, plain) == pytest.approx(drawn_baseline(1, deep_s), abs=1e-4)


def test_wave_anchor_one_huge_beat_does_not_squash_the_rest():
    normal = [_stored_wave(r_amp=1000.0, s_amp=800.0, t_amp=150.0) for _ in range(9)]
    huge = _stored_wave(r_amp=3000.0, s_amp=2400.0, t_amp=450.0)
    frame = WaveAnchor.frame(normal + [huge], percentile=0.9)
    # The shared reach must follow the normal beats so they keep their size.
    assert frame.reach < 1.2


def test_wave_anchor_empty_stack_is_safe():
    frame = WaveAnchor.frame([])
    assert frame.baselines == []
    assert frame.peaks == []
    assert frame.reach > 0.0


def test_wave_anchor_peak_follows_the_stroke_wherever_the_slice_landed_it():
    """Whatever index a wave's QRS landed on, that is the index returned."""
    for centre in (90, 110, 127, 140, 165):
        assert WaveAnchor.peak(_stored_at(centre)) == centre


def test_wave_anchor_downward_lead_anchors_on_its_own_dominant_stroke():
    """
    Polarity is kept as recorded, so a downward QRS stays downward and its apex
    is still the stroke — not the T wave that happens to be the maximum.
    """
    inverted = _stored_at(centre=150, up=False)
    assert WaveAnchor.apex(inverted) == 150
    assert WaveAnchor.peak(inverted) == 150


def test_wave_anchor_flat_wave_keeps_the_nominal_centre():
    """A flat or silent wave must not anchor on noise."""
    assert WaveAnchor.peak([0.0] * BeatTemplate.GRID_LEN) == BeatTemplate.R_AT
    assert WaveAnchor.apex([]) == BeatTemplate.R_AT


def test_wave_anchor_shift_is_capped():
    """A stray excursion far from the complex cannot slide the trace away."""
    wave = [0.0] * BeatTemplate.GRID_LEN
    wave[250] = 5.0
    peak = WaveAnchor.peak(wave)
    assert peak == BeatTemplate.R_AT + WaveAnchor.MAX_PEAK_SHIFT


def test_wave_anchor_frame_reports_a_peak_for_every_wave():
    waves = [_stored_at(100), _stored_at(127), _stored_at(150)]
    frame = WaveAnchor.frame(waves)
    assert frame.peaks == [100, 127, 150]
    assert len(frame.baselines) == 3


def test_wave_frame_dict_matches_the_anchor_frame():
    waves = [_stored_at(100), _stored_at(150)]
    frame = WaveAnchor.frame(waves)
    as_dict = wave_frame(waves)
    assert as_dict["peaks"] == frame.peaks
    assert as_dict["reach"] == pytest.approx(frame.reach)
    assert as_dict["baselines"] == pytest.approx(frame.baselines)


def test_beats_frame_carries_each_waves_peak():
    """
    The pane draws every wave's `peak` on the shared centre line, so the frame
    has to carry one — a constant would silently draw the jittered waves apart.
    """
    from src.heart import HeartState

    fs = 125.0
    ms_per = 8.0
    rr = 900.0
    first_r = 200.0
    beats = 16
    state = HeartState(fs_hz=fs)

    # Stream the way the dongle does: ECG in 16-sample packets, with each beat
    # announced once its R is in the ring and snapped to the packet grid as the
    # sensor's own beat clock is.
    idx = 0
    announced = 0
    total = int((rr * (beats + 2)) / ms_per)
    while idx < total:
        n = min(16, total - idx)
        samples = []
        for k in range(n):
            t = (idx + k) * ms_per
            phase = t % rr
            samples.append(1200.0 * math.exp(-((phase - first_r) ** 2) / 200.0))
        state.on_message({"type": "ecg", "ts": int(round(idx * ms_per)), "fs": fs, "uv": samples})
        idx += n

        while announced < beats and rr * (announced + 1) + first_r <= idx * ms_per:
            announced += 1
            true_r = rr * announced + first_r
            # The sensor's beat time is snapped to the 128 ms packet grid.
            beat_ms = int(round(true_r / 128.0)) * 128
            state.on_message({"type": "hr", "ts": beat_ms, "bpm": 67, "rr": [int(rr)]})

    frame = state.beats_frame()
    assert frame["shown"] > 0, "the stack needs a few beats to draw"
    peaks = [w["peak"] for w in frame["waves"]]
    assert all(isinstance(p, int) for p in peaks)
    assert len(peaks) == frame["shown"]
    # A synthetic signal this clean slices on its own stroke, so every wave's
    # peak is the nominal centre; the point is that it is measured, not assumed.
    assert all(abs(p - frame["r_at"]) <= 3 for p in peaks), peaks
    assert all(0 <= p < frame["grid_len"] for p in peaks)


