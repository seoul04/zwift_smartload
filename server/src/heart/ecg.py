"""
ECG side of the heart graphs: sample ring, scrolling strip, and the beats stack.

A Python port of the parts of the `my_heart` Android detector that need the raw
ECG — `EcgSamples`, `EcgTrace` (window and gain), `BeatTemplate`, `PeakFinder`,
`WaveAnchor` and `BeatOverlay`. The comments in that code carry the reasoning
behind each rule; this file keeps the same rules and points at the originals
where the detail matters.

The ECG is only used to draw: every number in the metrics and the RMSSD panel
comes from the sensor's own R–R stream (see `metrics.py`).
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Sequence, Tuple

from .qrs import QrsDetector

# Smallest peak-to-peak swing (uV) that counts as a beat in a search window.
MIN_EXCURSION_UV = 1e-3


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp `value` into [lo, hi]; `hi` wins when the bounds cross."""
    return max(lo, min(value, hi))

DEFAULT_FS_HZ = 130.0


class EcgRing:
    """
    Ring of recent ECG samples (uV) with millisecond timestamps.

    Timestamps are on the dongle's sample clock: the firmware stamps the first
    sample of a batch and advances by the nominal sample period, so arrival
    jitter never reaches the trace. Sample `i` of a batch sits at
    `t0_ms + i * ms_per_sample`.
    """

    def __init__(self, fs_hz: float = DEFAULT_FS_HZ, seconds: float = 20.0) -> None:
        self.fs_hz = float(fs_hz)
        self.ms_per_sample = 1000.0 / self.fs_hz
        self.capacity = max(2, int(self.fs_hz * seconds))
        self._times: Deque[int] = deque(maxlen=self.capacity)
        self._values: Deque[float] = deque(maxlen=self.capacity)
        # Cached list views, rebuilt after an append so searches can bisect.
        self._t_list: List[int] = []
        self._v_list: List[float] = []
        self._dirty = False
        # (value, newest_ms) of the smoothed full-scale gain.
        self._gain: Optional[Tuple[float, int]] = None
        self._init_playback()

    # -- playback clock --------------------------------------------------
    #
    # A port of the app's `EcgTrace`: the strip does not move because a new
    # sample arrived, it moves because a playback clock (`_playhead_ms`) is
    # advanced on every frame request and the window follows it. Packets land
    # in bursts (the dongle sends ~15-73 samples at a time), so anchoring on
    # the newest sample would hold still and then jump once per burst.

    #: How far the playhead may sit behind the newest sample, ms, regardless
    #: of the burst size. Also bounds the latency the strip introduces.
    _LATENCY_CEILING_MS = 1500.0
    #: Lead used before a packet span has been measured.
    _LATENCY_FALLBACK_MS = 140.0
    #: The lead is sized at this multiple of one burst, so a burst refills it.
    _LEAD_BATCH_FRACTION = 1.5
    _MIN_LEAD_MS = 25.0
    #: Smoothing on the measured burst span.
    _BATCH_EMA = 0.2
    #: Baseline over which the delivery rate is measured, ms. Long enough to
    #: average several bursts; a single arrival interval measures the gap
    #: *inside* a burst and is meaningless.
    _RATE_WINDOW_MS = 3000.0
    _FEED_EMA = 0.35
    #: Wall gap that separates one burst of lines from the next, ms. Lines
    #: inside a burst are <1 ms apart, bursts ~1000 ms apart.
    _BURST_GAP_MS = 50.0
    #: A single delivery cannot be longer than this, ms. Guards the first burst,
    #: which can swallow an unknown amount of warm-up.
    _MAX_BURST_SPAN_MS = 5000.0
    _MIN_FEED_RATE = 0.1
    _MAX_FEED_RATE = 4.0
    #: Smoothing and gain of the lead correction (applied as a rate, not a jump).
    _LEAD_EMA = 0.08
    _LEAD_GAIN = 0.35
    _MIN_RATE_FACTOR = 0.6
    _MAX_RATE_FACTOR = 1.6
    #: Longest wall step honoured in one frame, so a hiccup is not a leap.
    _MAX_DT_MS = 100.0
    #: The strip's width is held at the value it had when it came on screen,
    #: and only re-sized when the requested width drifts further than this.
    #:
    #: The width is `window_rr * mean_rr`, and mean_rr is a plain average over
    #: the last 30 intervals, so it moves with every beat - and violently after
    #: a noisy stretch. Re-sizing every frame moves BOTH edges: growing shows
    #: older data that was never on screen, shrinking erases data that was,
    #: while the playhead keeps advancing. On screen that reads as the left end
    #: "progressing in the past" and then being wiped, repeating for as many
    #: beats as it takes the old left edge to leave the view.
    #:
    #: 12% is loose enough that the normal beat-to-beat breathing of the mean
    #: (a few percent) never triggers a resize, and tight enough that every
    #: step between the width presets is followed: the closest pair, 8 and 10
    #: mean-RRs, is 25% apart.
    _WIDTH_TOLERANCE = 0.12

    def _init_playback(self) -> None:
        self._playhead_ms: Optional[float] = None
        self._last_wall_ms: Optional[float] = None
        self._last_arrival_wall_ms: Optional[float] = None
        self._last_arrival_data_ms: Optional[float] = None
        self._feed_rate = 1.0
        self._batch_span_ms = 0.0
        self._lead_ema_ms: Optional[float] = None
        self._width_ms: Optional[float] = None
        # Long baseline for the feed-rate estimate: (data_ms, wall_ms). See
        # `_learn_feed` for why one arrival interval is not enough.
        self._ref_data_ms: Optional[float] = None
        self._ref_wall_ms: Optional[float] = None
        # The newest data time when the current burst started, so the burst's
        # own span can be measured once the next gap reveals its end.
        self._burst_start_data_ms: Optional[float] = None
        self._burst_start_wall_ms: Optional[float] = None
        self._burst_end_data_ms: Optional[float] = None

    # -- writing ---------------------------------------------------------

    def push(self, samples_uv: Sequence[float], t0_ms: int, wall_ms: Optional[float] = None) -> None:
        """
        Append one batch. Cheap: no frame is built here.

        `wall_ms` is the wall clock at arrival. The wall clock against the data
        time it carries is the only unbiased way to learn the rate the sensor
        actually delivers at, because packet sizes vary.
        """
        now_wall = time.monotonic() * 1000.0 if wall_ms is None else float(wall_ms)
        prev_newest = self.newest_ms
        for i, uv in enumerate(samples_uv):
            self._times.append(int(t0_ms + i * self.ms_per_sample))
            self._values.append(float(uv))
        self._dirty = True

        self._learn_burst(now_wall, prev_newest)
        self._learn_feed(now_wall)
        self._last_arrival_wall_ms = now_wall
        self._last_arrival_data_ms = float(self.newest_ms or 0)

    def _learn_burst(self, now_wall: float, prev_newest: Optional[int]) -> None:
        """
        Learn how much data one *burst* carries.

        The dongle does not stream: it holds samples and then dumps ~1 s of them
        as a run of ~15-sample lines a fraction of a millisecond apart. The lead
        the playhead needs is the size of that burst, because that is what
        refills it — measuring a single line instead (112 ms here) sets a lead
        far too short to bridge the ~1.1 s gap, and the playhead then runs into
        the newest sample and sits there for most of every second.

        Bursts are told apart by wall gap, which is unambiguous on this stream:
        lines inside a burst are <1 ms apart and bursts are ~1000 ms apart.
        """
        last_wall = self._last_arrival_wall_ms
        if last_wall is None or prev_newest is None:
            self._burst_start_data_ms = float(prev_newest or 0)
            self._burst_start_wall_ms = now_wall
            return
        if now_wall - last_wall <= self._BURST_GAP_MS:
            self._burst_end_data_ms = float(prev_newest)
            return

        # A gap this long ends the current burst; its span is now known.
        self._burst_end_data_ms = float(prev_newest)
        start = self._burst_start_data_ms
        if start is not None and self._burst_end_data_ms is not None:
            span_ms = self._burst_end_data_ms - start
            # Reject a first "burst" that swallowed the warm-up, and any span
            # that could not plausibly be one delivery.
            if 0.0 < span_ms <= self._MAX_BURST_SPAN_MS:
                self._batch_span_ms = (
                    span_ms
                    if self._batch_span_ms <= 0.0
                    else self._batch_span_ms + (span_ms - self._batch_span_ms) * self._BATCH_EMA
                )
        # The next burst starts where this one left the data.
        self._burst_start_data_ms = float(prev_newest)
        self._burst_start_wall_ms = now_wall

    def _learn_feed(self, now_wall: float) -> None:
        """
        Learn data-ms per wall-ms from a baseline much longer than one arrival.

        Measuring arrival to arrival is wrong for a dongle that dumps a whole
        second of samples in one burst: the gaps between the lines inside the
        burst are ~0 ms, so the ratio explodes and saturates at the clamp. Over
        a window of ~`_RATE_WINDOW_MS` the individual bursts average out and the
        result is the real delivery rate the strip should drain at.
        """
        newest = float(self.newest_ms or 0)
        if self._ref_wall_ms is None:
            self._ref_data_ms, self._ref_wall_ms = newest, now_wall
            return
        wall_delta = now_wall - self._ref_wall_ms
        if wall_delta < self._RATE_WINDOW_MS:
            return
        data_delta = newest - (self._ref_data_ms or newest)
        if data_delta > 0 and wall_delta > 0:
            sample = _clamp(data_delta / wall_delta, self._MIN_FEED_RATE, self._MAX_FEED_RATE)
            self._feed_rate += (sample - self._feed_rate) * self._FEED_EMA
        self._ref_data_ms, self._ref_wall_ms = newest, now_wall

    def advance(self, wall_ms: float) -> None:
        """
        Move the playback clock without building a frame.

        Used while the strip is frozen: the pane keeps the frame it has, but the
        clock stays with the data so resuming shows the live signal instead of
        sliding to catch up.
        """
        if self.count() > 0:
            self._advance_playhead(float(wall_ms))

    def _advance_playhead(self, wall_ms: float) -> None:
        newest = self.newest_ms
        if newest is None:
            return
        avail = (self.count() - 1) * self.ms_per_sample

        if self._playhead_ms is None:
            # First frame: right edge on the newest sample, so a fresh strip
            # fills from the right instead of showing a blank lead.
            self._playhead_ms = float(newest)
            self._last_wall_ms = wall_ms
            self._lead_ema_ms = 0.0
            return

        dt = _clamp(wall_ms - (self._last_wall_ms or wall_ms), 0.0, self._MAX_DT_MS)
        self._last_wall_ms = wall_ms

        # The lead has to outlast the gap between bursts, which is measured
        # rather than assumed: a fixed lead drains away mid-interval and the
        # playhead stalls against the newest sample.
        cap_lead = min(avail * 0.5, self._LATENCY_CEILING_MS)
        target_lead = (
            self._batch_span_ms * self._LEAD_BATCH_FRACTION
            if self._batch_span_ms > 0.0
            else self._LATENCY_FALLBACK_MS
        )
        target_lead = _clamp(target_lead, min(self._MIN_LEAD_MS, cap_lead), max(cap_lead, 1.0))

        # A backlog beyond the ceiling is skipped, not scrolled through.
        #
        # The rate correction below only ever drains at _MAX_RATE_FACTOR times
        # real time, so a backlog of N seconds took N/(factor-1) seconds of
        # watching to clear - 50 s for a 30 s gap, and worse the longer the
        # strip went undrawn. The ceiling is documented as bounding the latency
        # the strip introduces, so enforce it rather than only steering with it.
        #
        # The allowance is the ceiling *plus one burst*: sitting a burst behind
        # the newest sample is how the strip keeps moving through the gap
        # between bursts, and a burst is legitimately up to a second of data.
        # Only the excess past that is stale enough to be worth dropping.
        #
        # A tab switch is not the only way to earn this: a frozen strip, a
        # stalled feed or a slow client all resume with a backlog.
        limit = self._LATENCY_CEILING_MS + self._batch_span_ms
        backlog = newest - self._playhead_ms
        if backlog > limit:
            self._playhead_ms = float(newest) - limit
            # Re-seat the smoothing, or the stale lead drags the rate for the
            # next half second and the strip visibly creeps.
            self._lead_ema_ms = limit

        lead = newest - self._playhead_ms
        self._lead_ema_ms = (
            lead
            if self._lead_ema_ms is None
            else self._lead_ema_ms + (lead - self._lead_ema_ms) * self._LEAD_EMA
        )

        # Drain at the measured feed rate, plus a correction from the lead
        # error. Applied as a *rate*, never a jump: a burst changes the speed
        # slightly instead of stepping a whole packet width.
        err = self._lead_ema_ms - target_lead
        rate = _clamp(
            self._feed_rate + self._LEAD_GAIN * err / max(target_lead, 1.0),
            self._feed_rate * self._MIN_RATE_FACTOR,
            self._feed_rate * self._MAX_RATE_FACTOR,
        )
        self._playhead_ms += dt * rate

        # Never draw past the newest sample; a stalled feed stops the clock.
        if self._playhead_ms > newest:
            self._playhead_ms = float(newest)

    def rewound(self, t0_ms: int) -> bool:
        """
        True when this batch starts before the newest sample already held.

        The firmware stamps `ts` from `k_uptime_get_32()`, so a dongle reboot
        (a USB reset is enough) restarts the clock near zero mid-stream. The
        searches here assume ascending time, so keeping both sides would make
        `bisect` return garbage and the strip would show nothing. Dropping the
        previous life of the ring is the only reading that makes sense: those
        samples are from a clock that no longer exists.
        """
        newest = self.newest_ms
        return newest is not None and int(t0_ms) < newest

    def clear(self) -> None:
        self._times.clear()
        self._values.clear()
        self._t_list = []
        self._v_list = []
        self._dirty = False
        self._gain = None
        self._init_playback()

    def _lists(self) -> Tuple[List[int], List[float]]:
        if self._dirty:
            self._t_list = list(self._times)
            self._v_list = list(self._values)
            self._dirty = False
        return self._t_list, self._v_list

    # -- EcgSource surface ----------------------------------------------

    def count(self) -> int:
        return len(self._times)

    def time_at(self, i: int) -> int:
        times, _ = self._lists()
        return times[i]

    def value_at_i(self, i: int) -> float:
        _, values = self._lists()
        return values[i]

    @property
    def newest_ms(self) -> Optional[int]:
        if not self._times:
            return None
        return int(self._times[-1])

    @property
    def oldest_ms(self) -> Optional[int]:
        if not self._times:
            return None
        return int(self._times[0])

    def value_at(self, t_ms: float) -> float:
        """Linear interpolation at `t_ms`; clamps to the oldest/newest sample."""
        n = self.count()
        if n == 0:
            return 0.0
        times, values = self._lists()
        if n == 1 or t_ms <= times[0]:
            return values[0]
        if t_ms >= times[-1]:
            return values[-1]
        lo = bisect_left(times, t_ms)
        if lo <= 0:
            return values[0]
        t0, t1 = times[lo - 1], times[lo]
        a = 0.0 if t1 == t0 else (t_ms - t0) / (t1 - t0)
        return values[lo - 1] * (1.0 - a) + values[lo] * a

    def band(self, lo_ms: float, hi_ms: float) -> Tuple[int, int]:
        """
        Half-open index range `[i, j)` of the samples inside `[lo_ms, hi_ms]`.

        The searches below all restrict to a band and never want to iterate the
        whole ring, so bounding by index is both faster and clearer.
        """
        times, _ = self._lists()
        i = bisect_left(times, lo_ms)
        j = bisect_right(times, hi_ms)
        return i, j

    # -- windows ---------------------------------------------------------

    def _stable_width_ms(self, requested_ms: float) -> float:
        """
        The width to draw, holding the previous value unless the request drifts.

        Both edges of the strip follow this number, so changing it every frame
        makes the left end move backwards (older data appears) or forwards
        (data already shown is erased) while the right end advances. See
        `_WIDTH_TOLERANCE` for why the request moves at all.

        Holding the width is not the same as hiding the change: once it does
        exceed the tolerance the strip resizes once, deliberately, instead of
        breathing every frame.
        """
        prev = self._width_ms
        if prev is None or abs(requested_ms - prev) > prev * self._WIDTH_TOLERANCE:
            self._width_ms = float(requested_ms)
        return self._width_ms

    def window(self, window_ms: float, mean_rr_ms: float, wall_ms: Optional[float] = None) -> Optional[dict]:
        """
        The scrolling strip: the `window_ms` of samples that ends at the playhead.

        Mirrors `EcgTrace.snapshot`. The window is a plain slice of the ring,
        never an interpolation: a QRS apex is one sample wide, and blending it
        with a neighbour by a phase that drifts frame to frame is what made R
        and S flicker between slightly different heights. The sub-sample
        remainder is returned separately as `left_fraction` so the pane can
        still place every sample at its true fractional x and scroll smoothly.
        """
        n = self.count()
        if n == 0:
            return None
        now_wall = time.monotonic() * 1000.0 if wall_ms is None else float(wall_ms)
        self._advance_playhead(now_wall)

        # Hold the width still while the strip is on screen. Using the raw
        # request would resize it on every beat, which drags the left edge
        # backwards whenever mean_rr rises. See `_stable_width_ms`.
        window_ms = self._stable_width_ms(window_ms)

        times, values = self._lists()
        newest = times[-1]
        playhead = self._playhead_ms if self._playhead_ms is not None else float(newest)

        want = max(1, int(round(window_ms / self.ms_per_sample)))
        want = min(want, n)

        # Where the right edge falls, in whole samples, and how far past that
        # boundary the playhead sits (0..1). Only ever clamped to the data held.
        end_f = min(
            (playhead - newest) / self.ms_per_sample + (n - 1),
            float(n - 1),
        )
        whole_end = int(math.floor(end_f))
        whole_end = max(0, min(whole_end, n - 1))
        left_fraction = end_f - math.floor(end_f)

        start = whole_end - (want - 1)
        first_real = 0
        if start < 0:
            first_real = -start
            start = 0
        window_values = values[start:whole_end + 1]
        if not window_values:
            return None

        return {
            "t0_ms": times[start],
            "ms_per_sample": self.ms_per_sample,
            "samples": window_values,
            "first_real": first_real,
            # Fraction of a sample the window's left edge sits past its sample.
            "left_fraction": left_fraction,
            "peak_uv": self._peak_ema(len(window_values)),
            "window_ms": want * self.ms_per_sample,
            "mean_rr_ms": mean_rr_ms,
            "newest_ms": newest,
            "playhead_ms": int(playhead),
        }

    # Gain smoothing: climb quickly so a real QRS is never clipped, fall back
    # slowly so the trace does not pump between beats (see EcgTrace.updateGain).
    _RISE = 0.5
    _FALL = 0.1
    _MIN_PEAK_UV = 100.0
    _GAIN_INTERVAL_MS = 200

    def _peak_ema(self, count: int) -> float:
        newest = self.newest_ms or 0
        if self._gain is not None and newest - self._gain[1] < self._GAIN_INTERVAL_MS:
            return max(self._gain[0], self._MIN_PEAK_UV)

        raw = self._peak_deviation(count)
        prev = 0.0 if self._gain is None else self._gain[0]
        if prev <= 0.0:
            value = raw
        elif raw > prev:
            value = prev + (raw - prev) * self._RISE
        else:
            value = prev + (raw - prev) * self._FALL
        self._gain = (value, newest)
        return max(value, self._MIN_PEAK_UV)

    def _peak_deviation(self, count: int) -> float:
        """Largest |sample - median| over the last `count` samples, uV."""
        if count <= 0:
            return 0.0
        values = self._lists()[1]
        tail = values[-count:]
        ordered = sorted(tail)
        base = ordered[len(ordered) // 2]
        return max((abs(v - base) for v in ordered), default=0.0)


@dataclass
class BeatWindow:
    """Pre/post span of one beat window, in ms, R at `r_at` of the grid."""

    pre: float
    post: float

    @property
    def total(self) -> float:
        return self.pre + self.post

    @property
    def r_at(self) -> int:
        """
        Grid index where R falls in a window of this shape.

        R is *not* at the middle of the window. A cardiac cycle is not symmetric
        about the R: the PR interval and P wave occupy ~`PRE_MS` before it, while
        the QRS, ST segment and T wave need ~`POST_MS` after it. A centred window
        put equal room on both sides, so the left half was mostly flat baseline
        and the right half ran out across the T wave — measured on a recording at
        68 bpm the T wave ended 388 ms after R against 375 ms of window, cutting
        it on 64% of beats. This index places R at `pre` of the way through, so
        the window follows the cycle instead.
        """
        total = self.total
        if total <= 0.0:
            return BeatTemplate.R_AT
        at = int((self.pre / total) * (BeatTemplate.GRID_LEN - 1))
        return min(max(at, 0), BeatTemplate.GRID_LEN - 1)

    @staticmethod
    def default() -> "BeatWindow":
        """
        A window of the shape the plot expects before an interval is known.

        Only the ratio matters: `r_at` depends on the pre/post proportion, which
        is fixed, so every window has the same shape whatever its width.
        """
        return BeatWindow(BeatTemplate.PRE_MS, BeatTemplate.POST_MS)


class BeatTemplate:
    """
    Beat windows built by warping the ECG around each known R time.

    Geometry: `GRID_LEN` samples with R at `BeatWindow.r_at`, which is a third of
    the way through the grid rather than the middle — the window is shaped like a
    cardiac cycle, with room for the T wave after R and only the PR interval
    before it. The warp is linear over the pre-R and post-R parts; both scale
    with the RR EMA so a short interval narrows the window instead of sliding the
    next R into view.
    """

    GRID_LEN = 256

    #: Grid index of R. Not the middle of the grid: the window around R is shaped
    #: like a cardiac cycle (see `BeatWindow.r_at`), so R sits a third of the way
    #: through it rather than half. One constant every consumer reads means the
    #: slicing, the geometry and the vertical anchor all agree on where R is.
    R_AT = 85

    PRE_MS = 250.0
    POST_MS = 500.0
    REST_MS = PRE_MS + POST_MS

    #: Where R is drawn across the plot, as a fraction of its width.
    #:
    #: Matches the window's shape: a third of the room before R, two thirds after.
    #: A wave drawn wider than the plot must lose its *tail* on the right (or its
    #: leading baseline on the left), never the T wave that follows R.
    R_FRAC = PRE_MS / REST_MS

    RR_DRAW_FRAC = 0.95

    _MIN_SAMPLING_MS = GRID_LEN - 1.0
    _MIN_DRAW_MS = 50.0
    _RR_WINDOW_FRAC = 0.90
    _MIN_WINDOW_MS = 360.0
    _RR_EMA_ALPHA = 0.2
    _RR_EMA_MIN_MS = 300.0
    _RR_EMA_MAX_MS = 2000.0
    _RR_AGREE_FRAC = 0.35
    _RR_RESEED_STREAK = 2

    def __init__(self) -> None:
        self.rr_ema_ms = 0.0
        self._rr_seed_ms = 0.0
        self._rr_challenger_ms = 0.0
        self._rr_challenger_count = 0

    def reset(self) -> None:
        self.rr_ema_ms = 0.0
        self._rr_seed_ms = 0.0
        self._rr_challenger_ms = 0.0
        self._rr_challenger_count = 0

    @staticmethod
    def draw_span(rr_ms: float) -> float:
        """
        Time one beat spans on the plot: 95% of its own interval.

        Not clamped to the sampling window on purpose: clamping would flatten
        every interval below ~72 bpm into the same width and hide exactly the
        rate change the plot exists to show.
        """
        if rr_ms <= 0.0:
            return 0.0
        return max(rr_ms * BeatTemplate.RR_DRAW_FRAC, BeatTemplate._MIN_DRAW_MS)

    def window_for_rr(self, rr_ms: float) -> BeatWindow:
        """
        The window sampled around a known R time: 90% of the interval, R at `R_AT`.

        It follows the interval with **no upper cap**. The old cap of 750 ms
        silently showed two-thirds of every slower beat: at 51 bpm the cycle is
        1143 ms, so 393 ms of each beat fell outside the window, and moving the
        pre/post split around only chose which end to lose. Removing the cap
        spends the grid's resolution instead: at 750 ms a sample is 2.9 ms, at
        1143 ms it is 4.5 ms, still finer than the 8 ms the sensor samples at.

        The lower bound stays, because it keeps one grid sample per
        millisecond-or-better at high rates, where the whole complex has to fit
        in a few hundred ms.
        """
        if rr_ms > 0.0:
            total = max(rr_ms * self._RR_WINDOW_FRAC, max(self._MIN_WINDOW_MS, self._MIN_SAMPLING_MS))
        else:
            total = self.REST_MS
        # Split in the shape of a cardiac cycle, never evenly: the T wave needs
        # far more room after R than the P wave needs before it.
        pre = total * (self.PRE_MS / self.REST_MS)
        return BeatWindow(pre, total - pre)

    def current_window(self) -> BeatWindow:
        return self.window_for_rr(self.rr_ema_ms)

    def from_ecg(
        self,
        ecg: EcgRing,
        r_ms: float,
        window: Optional[BeatWindow] = None,
    ) -> Optional[List[float]]:
        """
        One beat as a `GRID_LEN` window, R at `BeatWindow.r_at`, normalised by its
        own isoelectric level and amplitude.

        Normalising against the window's **median** and its largest excursion
        from that median — rather than against the R sample — is what makes the
        stack group vertically: every wave then shares a baseline of 0 and a
        reach of 1 whatever the R sample happened to be.
        """
        window = window or self.current_window()
        if ecg.count() < 2:
            return None
        t_first = float(ecg.time_at(0))
        t_last = float(ecg.time_at(ecg.count() - 1))
        if r_ms < t_first or r_ms > t_last:
            return None
        # The post window must be recorded, otherwise the look-ahead has no data.
        # Every term is a float; an integer addition here would quantise the
        # absolute uptime and let a beat through whose look-ahead is missing,
        # which clamps the tail of the wave onto the newest sample.
        if r_ms + window.post > t_last + 20.0:
            return None

        out: List[float] = []
        total = window.total
        # R is sampled exactly at the window's own index, so `g == r_at` lands on
        # `r_ms` whatever the window's shape. Mapping `g` straight onto
        # `r - pre + frac*total` instead left R about a millisecond off its index,
        # because `pre` is measured on a slightly different convention.
        frac_at_r = window.r_at / float(self.GRID_LEN - 1)
        for g in range(self.GRID_LEN):
            frac = g / float(self.GRID_LEN - 1)
            t = r_ms + (frac - frac_at_r) * total
            t = min(max(t, t_first), t_last)
            out.append(ecg.value_at(t))

        base = baseline(out)
        span = 1e-6
        for v in out:
            a = abs(v - base)
            if a > span:
                span = a
        return [(v - base) / span for v in out]

    def observe_rr(self, rr_ms: float) -> None:
        """Feed one observed interval into the plot-width EMA."""
        if rr_ms < self._RR_EMA_MIN_MS or rr_ms > self._RR_EMA_MAX_MS:
            return

        if self.rr_ema_ms <= 0.0:
            # Lock only after a second, agreeing interval (a bad first seed is common).
            if self._rr_seed_ms <= 0.0:
                self._rr_seed_ms = rr_ms
                return
            if self._agrees(rr_ms, self._rr_seed_ms):
                self.rr_ema_ms = 0.5 * (self._rr_seed_ms + rr_ms)
                self._rr_seed_ms = 0.0
                self._rr_challenger_count = 0
            else:
                self._rr_seed_ms = rr_ms
            return

        if self._agrees(rr_ms, self.rr_ema_ms):
            self.rr_ema_ms += self._RR_EMA_ALPHA * (rr_ms - self.rr_ema_ms)
            self._rr_challenger_count = 0
            return

        # Disagrees: either ectopy, or the EMA itself locked onto the wrong regime.
        if self._rr_challenger_count > 0 and self._agrees(rr_ms, self._rr_challenger_ms):
            self._rr_challenger_count += 1
            self._rr_challenger_ms += 0.5 * (rr_ms - self._rr_challenger_ms)
            if self._rr_challenger_count >= self._RR_RESEED_STREAK:
                self.rr_ema_ms = self._rr_challenger_ms
                self._rr_challenger_count = 0
        else:
            self._rr_challenger_ms = rr_ms
            self._rr_challenger_count = 1

    @staticmethod
    def _agrees(a: float, b: float) -> bool:
        return abs(a - b) / max(b, 1.0) <= BeatTemplate._RR_AGREE_FRAC


def baseline(samples: Sequence[float]) -> float:
    """Isoelectric level of one wave: the median, robust to P/QRS/T excursions."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return ordered[n // 2]
    return 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])


@dataclass
class WaveFrame:
    """
    Anchor of every wave in a stack, vertical and horizontal.

    A port of the app's `WaveAnchor.Frame`: `baselines` is the isoelectric
    level each wave sits on, `reach` one shared amplitude that draws them all
    at a comparable size, and `peaks` the grid index of each wave's own
    dominant stroke. The renderer draws `peaks[i]` on the shared centre line,
    which is what puts every R on the same vertical by construction.
    """

    baselines: List[float]
    reach: float
    peaks: List[int]


class WaveAnchor:
    """
    Anchor of a stored wave on the beats plot, vertical and horizontal.

    A port of the app's `WaveAnchor`. The drawn stack is meant to read as one
    group around a common height: every trace's flat baseline should sit on the
    same line, with the R spikes rising from it. That is what makes 30 beats
    look like one clean rhythm strip instead of a scribble.

    The obvious vertical anchor — the R sample, since R is what the waves were
    aligned on horizontally — does not do this. A wave is stored as
    `(value - base) / span`, with `base` the window's median, so its *baseline*
    is 0 by construction; anchoring on the R sample instead would leave that
    baseline carrying whatever the R-to-baseline distance happens to be. That
    distance changes with morphology (a deep S, a tall T, a quiet stretch) and
    with the signal's own DC offset, so every trace would be shifted up or down
    by a different amount and the stack would fan out. Anchoring on the
    isoelectric level removes exactly that per-wave shift, leaving shape
    differences to show as shape.

    Horizontally the same reasoning applies, for the opposite reason. Each wave
    is nominally sliced with R at `BeatTemplate.R_AT`, but the live beat times
    are *derived* (the sensor's R–R back-summed onto the newest ECG sample), so
    that anchor jitters beat to beat by more than the R is wide. Drawing every
    wave on the grid centre would therefore let each one's R land wherever the
    jitter left it. Pinning each wave on its own detected peak instead puts
    every R on the same line by construction.
    """

    #: Amplitude floor, so a flat or nearly-flat wave cannot scale to infinity.
    MIN_REACH = 1e-3

    #: How tall a wave's largest deviation must be before it is trusted as that
    #: wave's fiducial. Below it the wave is flat, so the middle of the grid is
    #: the safer anchor than a stray bump.
    MIN_PEAK_UV = 1e-3

    #: Largest shift, in grid samples, between the nominal centre and the peak.
    #:
    #: Measured, not guessed — `tools/eval_beat_alignment.py` is the measurement.
    #: Narrowing this to 25 samples makes the residual spread *worse* (3.6 -> 8.6
    #: samples), so the reach is not what fans the stack out. What does is the
    #: slice itself: on the 2026-11-08 recording the finder places the window a
    #: median 97 ms from the true R, with the errors split evenly either side, so
    #: no fixed bound can correct it. Fix the slice placement, not this.
    MAX_PEAK_SHIFT = 80

    @staticmethod
    def baseline(samples: Sequence[float]) -> float:
        """Isoelectric level of one stored wave: its median."""
        return baseline(samples)

    @staticmethod
    def apex(samples: Sequence[float]) -> int:
        """
        Grid index of the wave's dominant stroke, tie-broken toward the centre.

        Waves are normalised so the isoelectric level is `0` and the largest
        excursion reaches ±1 (see `BeatTemplate.from_ecg`). Polarity is left as
        recorded, so on one lead the dominant stroke may point up (a normal R)
        or down (a QS or a deep S) — which way is a property of the lead, not
        of the beat, and is the same for every wave in a stack.

        This is therefore the largest *deviation* from the baseline rather than
        the maximum: on a complex that points down the maximum is the T wave,
        and anchoring there would draw that beat skewed against the rest.
        """
        if not samples:
            return BeatTemplate.R_AT
        base = baseline(samples)
        best = 0
        best_dev = -1.0
        best_dist = abs(0 - BeatTemplate.R_AT)
        for g, v in enumerate(samples):
            d = abs(v - base)
            near = abs(g - BeatTemplate.R_AT)
            if d > best_dev or (d == best_dev and near < best_dist):
                best_dev = d
                best = g
                best_dist = near
        return best

    @classmethod
    def peak(cls, samples: Sequence[float], base: Optional[float] = None) -> int:
        """
        Grid index to draw `samples` centred on: its apex.

        A flat wave keeps the nominal centre rather than anchoring on noise,
        and the shift is capped at `MAX_PEAK_SHIFT` so a stray excursion far
        from the complex cannot slide the trace away from the others.
        """
        if not samples:
            return BeatTemplate.R_AT
        if base is None:
            base = baseline(samples)
        g = cls.apex(samples)
        if max((abs(v - base) for v in samples), default=0.0) < cls.MIN_PEAK_UV:
            return BeatTemplate.R_AT
        shift = _clamp(g - BeatTemplate.R_AT, -cls.MAX_PEAK_SHIFT, cls.MAX_PEAK_SHIFT)
        return BeatTemplate.R_AT + int(shift)

    @classmethod
    def frame(cls, waves: Sequence[Sequence[float]], percentile: float = 0.9) -> WaveFrame:
        """
        Frame for `waves`, all kept inside one band.

        The shared reach is a high percentile of the per-wave excursions rather
        than their maximum: one noisy or ectopic beat reaching twice as far
        should not squash every other trace into a thin strip in the middle of
        the plot. Individual waves are still clamped when drawn, so a taller
        one is clipped instead of escaping.
        """
        if not waves:
            return WaveFrame([], cls.MIN_REACH, [])

        bases: List[float] = []
        peaks: List[int] = []
        excursions: List[float] = []
        for w in waves:
            base = baseline(w)
            bases.append(base)
            peaks.append(cls.peak(w, base))
            excursions.append(max((abs(v - base) for v in w), default=0.0))

        ordered = sorted(excursions)
        idx = int((len(ordered) - 1) * _clamp(percentile, 0.0, 1.0))
        return WaveFrame(bases, max(ordered[idx], cls.MIN_REACH), peaks)


def wave_frame(waves: Sequence[Sequence[float]], percentile: float = 0.9) -> dict:
    """`WaveAnchor.frame` as the plain dict the frame builders serialise."""
    frame = WaveAnchor.frame(waves, percentile)
    return {
        "baselines": frame.baselines,
        "reach": frame.reach,
        "peaks": frame.peaks,
    }


@dataclass
class _Peak:
    """One picked sample: its time, its index, and the baseline it was measured from."""

    time_ms: int
    index: int
    base: float
    count: int


class QrsBeatSource:
    """
    Reference point of each beat, from the waveform itself.

    This replaces `PeakFinder` on streams the QRS detector can run on, which is
    every ECG at or above 200 Hz — the live dongle is at 256 Hz. The difference
    is not a better search window, it is a different question. `PeakFinder` took
    the sensor's beat announcement as truth and only asked *where inside this
    known beat is its R*, answering with the largest excursion from a local
    median. Pan–Tompkins asks *is this a beat at all*, from a slope-energy
    envelope with adaptive thresholds, so a T wave taller than a low R, a
    baseline swing or motion noise does not win a beat.

    Measured on `recording/zrelay_20261108_125551_qrs.edf` against EDFbrowser's
    own annotations, driving the beats stack through `HeartState`:

    =========================  =========  =========  ===============  ============
    slice centre               median     MAD        within 2 samples slice error
    =========================  =========  =========  ===============  ============
    `PeakFinder`               7.98       7.95       19 %             214 ms
    QRS peak + `PeakFinder`    1.66       1.02       54 %             145 ms
    QRS peak, used directly    1.18       0.59       79 %              26 ms
    =========================  =========  =========  ===============  ============

    The third row is why this class does no refinement and no clock
    calibration. Those exist in `PeakFinder` to correct a peak that came from a
    sensor timestamp; once the peak is measured from the waveform they have
    nothing left to correct, and running them anyway *moves a correct peak* —
    row two is row three with the old machinery put back on top.

    It also works without the sensor. The strap reports no beats at all across
    two 40 s stretches of that recording while the ECG keeps coming; a beat
    source anchored on the strap's announcements cannot draw them, and this one
    does (753 complexes against 719 announced).
    """

    #: The detector is only defined on 200 Hz and up (its refractory period and
    #: integration window are counted in 200 Hz samples). Below that the caller
    #: falls back to `PeakFinder`.
    MIN_FS_HZ = 200.0

    #: Measured R times retained. The stack only ever asks about recent beats,
    #: but `locate` searches this list, so it holds far more than one screen.
    HISTORY = 512

    def __init__(self, fs_hz: float) -> None:
        self.fs_hz = float(fs_hz)
        self.ms_per_sample = 1000.0 / self.fs_hz
        self._detector = QrsDetector(self.fs_hz, sense=1.0)
        #: Index, in pushed samples, of the next sample the detector will see.
        self._fed = 0
        #: Ring timestamp of each sample pushed, so a peak can be put on the
        #: ring's clock by *looking its time up* rather than computing it from
        #: the sample index.
        #:
        #: This is not a roundabout way of doing arithmetic. The index only maps
        #: to time linearly if every sample arrives, and live they do not: on a
        #: real session the dongle delivered 7 635 samples across 72 s of stamps,
        #: so 60 % of the ECG was missing in gaps between bursts. Timing a peak
        #: as `first_stamp + index * period` then places it progressively earlier
        #: than it really is — measured at 31 samples (121 ms) on that session,
        #: which pushes the R a third of a window right of centre and makes the
        #: stack look misaligned. A replay of a recorded file has no gaps, so it
        #: shows none of this and would hide the bug.
        #: Deep enough for the longest report lag (a search-back beat can arrive
        #: ~1.2 s late, 300 samples at 256 Hz); 2048 is ~8 s at 256 Hz.
        self._fed_times: Deque[int] = deque(maxlen=2048)
        self._found: Deque[float] = deque(maxlen=self.HISTORY)
        self._lags: Deque[int] = deque(maxlen=64)
        #: Peaks found while pushing, waiting to be handed out by `detect`.
        self._new: List[float] = []

    @classmethod
    def supports(cls, fs_hz: float) -> bool:
        return fs_hz >= cls.MIN_FS_HZ

    def reset(self) -> None:
        self._detector.reset()
        self._fed = 0
        self._fed_times.clear()
        self._found.clear()
        self._lags.clear()
        self._new.clear()

    #: Delay to assume before any beat has been measured, ms. The reference's
    #: own 21-sample compensation at 200 Hz, plus the peak search's walk-back,
    #: which is what the measured figure settles at on real data.
    NOMINAL_LAG_MS = 170

    def lag_ms(self) -> int:
        """
        How far behind the newest sample a peak is when it is reported.

        The detector's compensation (`del_comp`, 21 samples at 200 Hz) is only
        part of it: the peak search also walks back to the apex of the complex,
        which adds about another 11 samples at 200 Hz. Measured on the annotated
        capture the total is a flat 41 samples at 256 Hz (160 ms), so the figure
        is *measured* here rather than derived — deriving it from `del_comp`
        alone understates it by half, and a look-ahead that is too short lets a
        wave be sliced against a tail that has not arrived.

        The contract is deliberately one-sided: too short breaks waves, too long
        only delays them.
        """
        if self._lags:
            return int(math.ceil(self.median_lag_ms())) + 1
        return self.NOMINAL_LAG_MS

    def feed(self, samples_uv: Sequence[float], start_ms: Optional[int] = None) -> None:
        """
        Consume new ECG samples and record any beats found in them.

        Only samples never fed before are read, so this is safe to call with
        whatever the ring currently holds: the ring's own history is retained,
        so the caller does not have to track how much is new.

        `start_ms` is the ring time of the first sample in `samples_uv`. Each
        sample's own time is recorded as it is pushed, so a peak is dated from
        the sample it actually sits on. Timing it from the sample index instead
        would be wrong on any stream with gaps — see `_fed_times`.
        """
        times = self._sample_times(len(samples_uv), start_ms)
        for value, t_ms in zip(samples_uv, times):
            # Absolute sample index of the R just found, or None. Not an offset:
            # `QrsDetector` has already applied the detector's own delay.
            peak_index = self._detector.push(value)
            index = self._fed
            self._fed += 1
            self._fed_times.append(int(t_ms))
            if peak_index is None or peak_index < 0:
                continue
            self._lags.append(index - peak_index)
            peak_ms = float(self._time_of(peak_index))
            self._found.append(peak_ms)
            self._new.append(peak_ms)

    def _sample_times(self, count: int, start_ms: Optional[int]) -> List[int]:
        """
        Ring time of each of `count` samples starting at `start_ms`.

        `start_ms` is authoritative when given. Without it the times continue
        from the last sample fed — which keeps a caller that omits it working,
        at the cost of assuming contiguous arrival.
        """
        if start_ms is not None:
            base = int(start_ms)
        elif self._fed_times:
            base = self._fed_times[-1] + int(round(self.ms_per_sample))
        else:
            base = 0
        step = self.ms_per_sample
        return [base + int(round(i * step)) for i in range(count)]

    def _time_of(self, index: int) -> int:
        """
        Ring time of the sample at `index`, or the closest estimate if it has
        aged out of the retained times.

        The detector is now several seconds ahead of where it reports, so this
        looks back at most `lag_ms()`. The fallback extrapolates from the oldest
        retained sample rather than from sample 0, which keeps it right within
        one lag window even when history has expired.
        """
        offset = self._fed - 1 - index
        if 0 <= offset < len(self._fed_times):
            return self._fed_times[len(self._fed_times) - 1 - offset]
        if not self._fed_times:
            return int(round(index * self.ms_per_sample))
        oldest_index = self._fed - len(self._fed_times)
        return self._fed_times[0] + int(round((index - oldest_index) * self.ms_per_sample))

    def detect(self) -> List[float]:
        """R times recorded since the last call, oldest first."""
        if not self._new:
            return []
        out = self._new
        self._new = []
        return out

    def beats(self) -> List[float]:
        """Every R time still in history, oldest first."""
        return list(self._found)

    def median_lag_ms(self) -> float:
        """The measured report delay, ms. Falls back to the nominal `lag_ms`."""
        if not self._lags:
            return float(self.lag_ms())
        ordered = sorted(self._lags)
        return ordered[len(ordered) // 2] * self.ms_per_sample

    def locate(self, expected_ms: int) -> float:
        """
        The measured R time nearest `expected_ms`.

        Kept so this is a drop-in for `PeakFinder.locate`; the value is already
        exact, so this selects a measurement rather than making one. Nearest
        within half a beat, so a caller asking about a beat the detector placed
        slightly differently still gets that beat and not its neighbour. With
        nothing in range the expected time stands — a beat the detector has not
        reached yet must not be silently moved onto a different one.
        """
        if not self._found:
            return float(expected_ms)
        nearest = min(self._found, key=lambda t: abs(t - expected_ms))
        if abs(nearest - expected_ms) <= 0.5 * self._mean_rr_ms():
            return float(nearest)
        return float(expected_ms)

    def _mean_rr_ms(self) -> float:
        peaks = self._found
        if len(peaks) < 2:
            return 800.0
        return (peaks[-1] - peaks[0]) / float(len(peaks) - 1)


class PeakFinder:
    """
    Reference point of one beat for the beats plot, from the sensor's RR stream.

    The sensor tells us *when* each beat happens, not *where* its R peak sits in
    the ECG, so the two clocks agree only up to one offset. This measures that
    offset and keeps it, chaining from the last R actually found plus one
    interval — which is what keeps the stack centred even though the sensor's
    beat times are snapped to the ECG packet grid.
    """

    BEAT_MIN_MS = 250
    BEAT_MAX_MS = 3000

    _DEFAULT_CFG = {
        "acquire_interval_frac": 0.48,
        "track_interval_frac": 0.12,
        "min_track_reach_ms": 60,
        "acquire_near_max_frac": 0.55,
        "bootstrap_reach_ms": 450,
        "qrs_refine_ms": 55,
    }

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = dict(self._DEFAULT_CFG)
        if cfg:
            self.cfg.update(cfg)
        self._last_lag_ms = 0
        self._last_found_ms: Optional[int] = None   # the sample the last R was found on
        self._last_expected_ms: Optional[int] = None
        self._offset_history: Deque[int] = deque(maxlen=24)

        # -- ECG-clock calibration ------------------------------------------
        #
        # The tracking chain advances by one sensor R–R, but that interval is
        # measured in *real* time while the chain lives on the ECG sample clock.
        # The two are not the same clock: the dongle stamps the strap's own rate
        # (125 Hz on the recordings that exposed this), and on a
        # captured recording the R peaks came out 6.1% closer in stamped ms than
        # the sensor's own R–R (median 1088 ms against 1159 ms, per-beat spread
        # only 3%). Advancing by the raw interval therefore overshot the next R
        # by ~71 ms *every beat*, and the finder chased it: the offsets it
        # measured alternated between two values one sample step apart, the
        # pooled median carried a standing error, and the stack fanned out.
        #
        # The ratio is measured, not hardcoded: it is the ECG clock's own rate,
        # whatever this dongle actually runs at. `_CAL_RATIO` is R–R in ECG ms
        # over R–R in sensor ms.
        self._cal_ratio = 1.0
        self._cal_history: Deque[float] = deque(maxlen=self._CAL_SAMPLES)
        self._cal_prev_found: Optional[int] = None
        self._cal_prev_rr = 0

    #: Measured ratios retained. The estimate is their *median*, so one ectopic or
    #: mis-detected beat cannot move the clock, and the estimate keeps refining as
    #: beats arrive instead of freezing on whichever three came first.
    _CAL_SAMPLES = 15
    #: Ratios needed before the estimate is applied at all.
    _CAL_MIN = 3
    #: Ratios needed before the beats plot draws with it. See `timebase_ready`.
    _CAL_DRAW_MIN = 6
    #: How far the ratio may sit from 1 before it is treated as a mis-detection
    #: rather than a clock difference.
    _CAL_MIN_RATIO = 0.80
    _CAL_MAX_RATIO = 1.20
    #: How much the ratio may move before the tracking chain is reseated.
    _CAL_RESET_DELTA = 0.01

    def reset(self) -> None:
        self._last_lag_ms = 0
        self._last_found_ms = None
        self._last_expected_ms = None
        self._offset_history.clear()
        self._cal_ratio = 1.0
        self._cal_history.clear()
        self._cal_prev_found = None
        self._cal_prev_rr = 0

    def timebase(self) -> float:
        """
        R–R in ECG-clock ms over R–R in sensor ms: the ECG clock's rate relative
        to real time. 1.0 until enough intervals have agreed.
        """
        return self._cal_ratio

    @staticmethod
    def lookahead_ms(window: Optional[BeatWindow] = None) -> int:
        """
        ECG that must be recorded past a beat before its R can be located.

        The search band's far edge is the prediction plus `reach`; a band cut short
        by the newest sample holds a partial complex, so its pick slides toward that
        edge. For a beat stamped at the newest sample — which is what the dongle
        does — that is the difference between an aligned stack and one wave in
        thirty sitting left of the others.
        """
        # The widest tracking reach the tunables allow (see `locate`), plus the
        # refinement neighbourhood, so the whole band is real data.
        reach = max(int(BeatTemplate.REST_MS * 0.12), 60)
        return reach + 55

    def timebase_ready(self) -> bool:
        """
        True once the ECG clock has been measured well enough to draw from.

        Stricter than `_CAL_MIN`, which only makes the estimate *apply*: a median of
        three intervals is still within a sample step of the truth, and the first few
        waves drawn on it sit visibly off their neighbours. Waiting for a handful
        costs a few seconds after a connect and leaves the whole stack superposed.
        """
        return len(self._cal_history) >= self._CAL_DRAW_MIN

    def _observe_timebase(self, found_ms: int, sensor_rr_ms: int) -> None:
        """Measure the ECG clock against the sensor's own R–R interval."""
        prev = self._cal_prev_found
        prev_rr = self._cal_prev_rr
        self._cal_prev_found = found_ms
        self._cal_prev_rr = sensor_rr_ms
        if prev is None or prev_rr <= 0 or sensor_rr_ms <= 0:
            return
        if not (self.BEAT_MIN_MS <= sensor_rr_ms <= self.BEAT_MAX_MS):
            return
        # The chain advanced by `prev_rr` real ms; the ECG clock says the R moved
        # `found - prev` instead. That ratio is the clock's scale.
        ratio = (found_ms - prev) / float(prev_rr)
        if not (self._CAL_MIN_RATIO <= ratio <= self._CAL_MAX_RATIO):
            return
        self._cal_history.append(ratio)
        if len(self._cal_history) >= self._CAL_MIN:
            was = self._cal_ratio
            self._cal_ratio = median(list(self._cal_history))
            # A change in the clock makes the last found R stale — it was placed on
            # a chain that stepped by the old scale. Dropping it re-acquires from the
            # sensor's own time plus the pooled offset, which is measured per beat
            # and so does not inherit the stale chain. Without this the chain keeps
            # the error it accumulated before the clock settled.
            if abs(self._cal_ratio - was) > self._CAL_RESET_DELTA:
                self._last_found_ms = None

    def lag_ms(self) -> int:
        return self._last_lag_ms

    def locate(
        self,
        ecg: EcgRing,
        expected_ms: int,
        template: Optional[Sequence[float]] = None,
        template_window: Optional[BeatWindow] = None,
        sensor_rr_ms: int = 0,
    ) -> float:
        n = ecg.count()
        if n < 3:
            return float(expected_ms)

        t_first = ecg.time_at(0)
        t_last = ecg.time_at(n - 1)

        if sensor_rr_ms > 0 and self.BEAT_MIN_MS <= sensor_rr_ms <= self.BEAT_MAX_MS:
            rr = int(sensor_rr_ms)
        elif self._last_expected_ms is None:
            rr = 0
        else:
            d = expected_ms - self._last_expected_ms
            rr = d if self.BEAT_MIN_MS <= d <= self.BEAT_MAX_MS else 0
        self._last_expected_ms = expected_ms

        # Tracking chains from the previous *found* R, so the search never
        # depends on how the sensor happened to snap this beat to its packet.
        #
        # The step is `rr` converted onto the ECG clock: the interval is real
        # milliseconds, the chain is ECG milliseconds, and they differ by the
        # clock's own scale (measured above).
        tracking = rr > 0 and self._last_found_ms is not None
        step = int(round(rr * self._cal_ratio)) if tracking else 0
        predicted = (self._last_found_ms + step) if tracking else (expected_ms + self._pooled_offset())
        if tracking:
            reach = max(int(rr * self.cfg["track_interval_frac"]), self.cfg["min_track_reach_ms"])
        elif rr > 0:
            reach = int(rr * self.cfg["acquire_interval_frac"])
        else:
            reach = self.cfg["bootstrap_reach_ms"]

        lo = max(predicted - reach, t_first)
        hi = min(predicted + reach, t_last)

        # Nothing to measure until the window is fully recorded: a band clipped by
        # the start of the ring holds only later samples, and its pick would be wrong.
        if predicted - reach >= t_first and hi > lo:
            if tracking:
                # Tracking must *confirm* the complex, not just take whatever is
                # largest in its band: a T wave or a noisy stretch can be the
                # tallest thing near the prediction, and since the chain advances
                # from the found sample, one such pick would walk the whole stack
                # off the R.
                found = self._refine(ecg, predicted, lo, hi, require_strong=True)
            else:
                centre = self._nearest_strong_deviation(ecg, lo, hi, predicted)
                found = self._refine(ecg, centre, lo, hi) if centre is not None else None
            if found is not None:
                on_sample = int(round(found))
                sample = on_sample - expected_ms
                self._offset_history.append(sample)
                self._last_lag_ms = sample
                self._observe_timebase(on_sample, sensor_rr_ms)
                # The chain advances by a whole interval from the *sample* the peak
                # was found on, not from the sub-sample estimate: an interval in
                # milliseconds is exact, so adding it to a grid time keeps the chain
                # off the grid, and a fractional seed would only add jitter of its
                # own to the prediction band rather than cancel any.
                self._last_found_ms = on_sample
                return found

        # Tracking failed to confirm the complex: fall back to the sensor's time
        # and drop the chain, so the next beat re-acquires widely.
        self._last_found_ms = None
        return float(max(0, expected_ms + self._pooled_offset()))

    def _refine(
        self,
        ecg: EcgRing,
        around_ms: int,
        lo: int,
        hi: int,
        require_strong: bool = False,
    ) -> Optional[float]:
        near = self.cfg["qrs_refine_ms"]
        a = max(around_ms - near, lo)
        b = min(around_ms + near, hi)
        if require_strong:
            # The band must hold a real complex, and the pick must be the peak of
            # the complex rather than its edge: a T wave or a wander has no such
            # peak.
            if self._max_out(ecg, a, b) < MIN_EXCURSION_UV:
                return None
        peak = self._max_deviation(ecg, a, b)
        if peak is None:
            return None if require_strong else float(around_ms)
        return peak.time_ms + self._sub_sample_offset_ms(ecg, peak.index, peak.base, peak.count)

    @staticmethod
    def _sub_sample_offset_ms(ecg: EcgRing, index: int, base: float, count: int) -> float:
        """
        Where the peak lies between the samples, in ms.

        The pick is necessarily a whole sample, so on its own it can only be right
        to half a step — 2 ms at 256 Hz, 4 ms at 125 Hz — and that error lands
        directly in the drawn stack, shifting each wave against its neighbours.
        Fitting a parabola through the apex and the samples either side recovers
        where between them the true vertex sits, which takes the anchor off the
        sample grid.

        The apex triplet, not a fit to the two flanks: chords through the upstroke
        and the downstroke have nearly the same slope on a QRS, so their
        intersection is ill-conditioned and noise throws the crossing sideways. A
        three-point fit has no such near-cancellation.

        Deviation is taken as an absolute excursion from the band baseline, so a
        downward R gets the same treatment as an upward one. Any degenerate case —
        the apex on the band edge, a flat or single-sided top — returns zero and
        leaves the pick alone.
        """
        if index <= 0 or index >= count - 1:
            return 0.0
        y0 = abs(ecg.value_at_i(index - 1) - base)
        y1 = abs(ecg.value_at_i(index) - base)
        y2 = abs(ecg.value_at_i(index + 1) - base)
        # Second difference: negative at a maximum. Zero or positive means there is
        # no vertex between the neighbours (a flat top or a shoulder), so the
        # sample stands.
        curvature = y0 - 2.0 * y1 + y2
        if curvature > -1e-6:
            return 0.0
        offset = 0.5 * (y0 - y2) / curvature
        if not math.isfinite(offset) or offset < -0.5 or offset > 0.5:
            return 0.0
        return offset * ecg.ms_per_sample

    @staticmethod
    def _max_out(ecg: EcgRing, lo: int, hi: int) -> float:
        i, j = ecg.band(lo, hi)
        if i >= j:
            return 0.0
        values = ecg._lists()[1][i:j]
        return max(values) - min(values)

    def _nearest_strong_deviation(self, ecg: EcgRing, lo: int, hi: int, centre_ms: int) -> Optional[int]:
        """
        Time of a *strong* excursion closest to `centre_ms`.

        Taking simply the largest deviation is what made acquisition jump between
        neighbouring beats on a real recording: the next R can be taller than the
        one being looked for. Restricting to excursions within a fraction of the
        largest, then taking the one nearest where the beat is expected, keeps
        the pick on this beat.
        """
        if ecg.count() < 3 or hi <= lo:
            return None
        i, j = ecg.band(lo, hi)
        band = ecg._lists()[1][i:j]
        if len(band) < 3:
            return None
        ordered = sorted(band)
        if ordered[-1] - ordered[0] < MIN_EXCURSION_UV:
            return None
        base = ordered[len(ordered) // 2]
        deviations = [abs(v - base) for v in band]
        threshold = max(deviations) * self.cfg["acquire_near_max_frac"]

        times = ecg._lists()[0]
        best_t: Optional[int] = None
        best_dist = 1 << 62
        for k, d in enumerate(deviations):
            if d < threshold:
                continue
            t = times[i + k]
            dist = abs(t - centre_ms)
            if dist < best_dist:
                best_dist = dist
                best_t = t
        return best_t

    @staticmethod
    def _max_deviation(ecg: EcgRing, lo: int, hi: int) -> Optional["_Peak"]:
        """
        Largest deviation from the window's own median baseline: its time, its
        sample index, and the baseline it was measured against.

        Measuring from the level — not from the steepest slope — is what makes the
        pick land on the same point of the complex on every beat: a slope maximum
        wanders with the sample grid and with noise, an excursion maximum does not.
        """
        n = ecg.count()
        if n < 3 or hi <= lo:
            return None
        i, j = ecg.band(lo, hi)
        band = ecg._lists()[1][i:j]
        if len(band) < 3:
            return None
        ordered = sorted(band)
        # A flat window has no R to point at (uV units): without this, silence
        # would "find" the first sample and drag the pooled offset onto it.
        if ordered[-1] - ordered[0] < MIN_EXCURSION_UV:
            return None
        base = ordered[len(ordered) // 2]

        best_k = -1
        best_d = -1.0
        for k, v in enumerate(band):
            d = abs(v - base)
            if d > best_d:
                best_d = d
                best_k = k
        if best_k < 0:
            return None
        return _Peak(ecg.time_at(i + best_k), i + best_k, base, n)

    @staticmethod
    def _max_deviation_time(ecg: EcgRing, lo: int, hi: int) -> Optional[int]:
        """Integer pick only, for callers that do not want the sub-sample refinement."""
        peak = PeakFinder._max_deviation(ecg, lo, hi)
        return None if peak is None else peak.time_ms

    def _pooled_offset(self) -> int:
        if not self._offset_history:
            return 0
        return int(median(list(self._offset_history)))


class BeatOverlay:
    """
    Stack of drawn beats: one wave per beat, centred on where its R actually is.

    Two beat sources, and which one is in use is the difference between a stack
    that superposes and one that fans out:

    * `QrsBeatSource` (default on ECG at 200 Hz and up, so the live 256 Hz
      dongle): the beats are *detected* from the waveform, and the detected peak
      is the slice centre directly. This is the accurate path — see the table in
      `QrsBeatSource` for the measurement.
    * `PeakFinder` (below 200 Hz, where the QRS algorithm's own assumptions do
      not hold): the beats are the sensor's announcements, and the finder
      measures where inside each one its R sits.

    On the QRS path `on_beat` is not what creates waves — the ECG is. That
    matters beyond accuracy: the strap announces no beats at all across long
    stretches of a real recording while the ECG keeps coming, and those beats
    are simply not on the stack when the sensor is the trigger.

    A wave is committed one beat late, because its post-R part is only recorded
    once enough ECG has arrived after R. The model is the median of the last
    `TEMPLATE_BEATS` committed waves, so a single distorted beat cannot drag the
    reference point of the following ones.
    """

    BEAT_STEPS: Tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64)
    STACK_CAP = 64
    TEMPLATE_BEATS = 8
    TEMPLATE_MIN = 3

    # Waves withheld after a connect or reset so the model has something to
    # build the reference point from.
    WARMUP_BEATS = 3

    def __init__(
        self,
        template: Optional[BeatTemplate] = None,
        peak_finder: Optional[PeakFinder] = None,
        qrs_source: Optional[QrsBeatSource] = None,
    ) -> None:
        self._template = template or BeatTemplate()
        self._peak_finder = peak_finder or PeakFinder()
        self._qrs = qrs_source
        self._stack: Deque[Tuple[List[float], int]] = deque(maxlen=self.STACK_CAP)
        self._recent: Deque[List[float]] = deque(maxlen=self.TEMPLATE_BEATS)
        self._pending_ms: Optional[float] = None
        self._pending_wave: Optional[List[float]] = None
        #: Interval of the located, not-yet-committed beat.
        self._pending_rr = 0
        #: Beats detected but not committed yet, oldest first: (beat_ms, interval).
        self._queue: Deque[Tuple[int, int]] = deque()
        self._last_beat_ms: Optional[int] = None
        self._model: Optional[List[float]] = None
        self._commits = 0
        self._version = 0
        #: Time of the newest ECG sample already handed to the QRS detector, so
        #: each one is seen exactly once.
        self._fed_upto_ms: Optional[int] = None
        #: Time of the newest beat the detector reported, for measuring the next
        #: interval. Separate from `_last_beat_ms`, which the sensor path owns.
        self._last_detected_beat_ms: Optional[float] = None

    @property
    def version(self) -> int:
        return self._version

    def clear(self) -> None:
        self._stack.clear()
        self._recent.clear()
        self._pending_ms = None
        self._pending_wave = None
        self._pending_rr = 0
        self._queue.clear()
        self._last_beat_ms = None
        self._model = None
        self._commits = 0
        self._fed_upto_ms = None
        self._last_detected_beat_ms = None
        self._template.reset()
        self._peak_finder.reset()
        if self._qrs is not None:
            self._qrs.reset()
        self._version += 1

    def use_qrs(self, fs_hz: float) -> bool:
        """
        Switch to detecting beats from the waveform when the rate allows it.

        A session can start before the dongle's real rate is known (the fallback
        is 130 Hz) and settle at 256 Hz a packet later, so this is called on
        every ECG packet and is a no-op once the decision is stable.
        """
        if not QrsBeatSource.supports(fs_hz):
            return self._qrs is not None
        if self._qrs is not None:
            if abs(self._qrs.fs_hz - float(fs_hz)) < 1e-6:
                return True
            # A rate change means a different detector: its filters and its
            # refractory window are counted in samples at this rate.
            self._qrs = None
        self._qrs = QrsBeatSource(fs_hz)
        self._fed_upto_ms = None
        self._last_detected_beat_ms = None
        # The queue holds sensor-announced beats; the detector is the source now.
        self._queue.clear()
        self._pending_ms = None
        self._pending_wave = None
        self._pending_rr = 0
        self._template.reset()
        self._version += 1
        return True

    @property
    def detecting(self) -> bool:
        """True when beats come from the waveform rather than the sensor's."""
        return self._qrs is not None

    def disable_detector(self) -> None:
        """
        Fall back to the sensor-announced path, whatever the rate.

        Only useful for comparing the two on the same recording — the detector is
        the accurate path wherever it applies. Any beats already drawn stay as
        they are; new ones come from the sensor again.
        """
        if self._qrs is None:
            return
        self._qrs = None
        self._fed_upto_ms = None
        self._last_detected_beat_ms = None
        self._queue.clear()
        self._pending_ms = None
        self._pending_wave = None
        self._pending_rr = 0
        self._template.reset()
        self._version += 1

    def discard_pending(self) -> None:
        self._pending_ms = None
        self._pending_wave = None
        self._pending_rr = 0
        self._queue.clear()

    def beats(self, limit: Optional[int] = None) -> List[Tuple[List[float], int]]:
        """Oldest -> newest, each with the interval it was drawn for."""
        items = list(self._stack)
        if limit is None or limit >= len(items):
            return items
        return items[-limit:]

    def buffered_count(self) -> int:
        return len(self._stack)

    def detector_fed(self) -> Optional[int]:
        """
        Samples the QRS detector has consumed, or None when it is not in use.

        Exposed for the status line: it should track the session's own ECG sample
        count. A gap between the two means the feed is skipping, which shifts
        every detected peak against the ring's clock.
        """
        if self._qrs is None:
            return None
        return self._qrs._fed  # noqa: SLF001 - a diagnostic counter, not state

    def template(self) -> Optional[List[float]]:
        return self._model

    def lag_ms(self) -> int:
        if self._qrs is not None:
            return self._qrs.lag_ms()
        return self._peak_finder.lag_ms()

    def on_beat(self, ecg: EcgRing, beat_ms: int, sensor_rr_ms: int = 0) -> bool:
        """
        Record the sensor's beat at `beat_ms`.

        `sensor_rr_ms` is the interval the sensor measured for this beat: it is
        what sizes the window and what the reference chain advances by. Returns
        True when the *drawn* stack changed; the first `WARMUP_BEATS` completions
        feed the model but are not drawn.

        When the QRS detector is driving, this is only a hint: the waveform is
        what says a beat happened and where it is, so an announcement must not
        add a wave of its own or the same beat would be drawn twice. It still
        feeds the width EMA, which the sensor measures better than a
        peak-to-peak interval does.
        """
        if self._qrs is not None:
            if sensor_rr_ms > 0:
                self._template.observe_rr(float(sensor_rr_ms))
            return self.pump(ecg)

        prev = self._last_beat_ms
        self._last_beat_ms = beat_ms
        if sensor_rr_ms > 0:
            rr = sensor_rr_ms
        elif prev is not None:
            rr = beat_ms - prev
        else:
            rr = 0
        if rr > 0:
            self._template.observe_rr(float(rr))

        # Queue the beat rather than locating it now.
        #
        # `locate()` refuses a band clipped by the start of the ring, but the
        # dongle stamps a beat at the *newest* ECG sample it holds, so the tracking
        # band is clipped at its far edge on every beat and the pick slides toward
        # it — the wave lands several samples left of the others. Locating needs
        # look-ahead, and this is fed from `on_ecg` when more arrives.
        self._queue.append((int(beat_ms), rr))
        return self.pump(ecg)

    def pump(self, ecg: EcgRing) -> bool:
        """
        Advance the beat queue as far as the recorded ECG allows.

        Called when a beat is announced *and* when ECG arrives, so the queue never
        stalls: the look-ahead a beat needs is usually a packet away. On the QRS
        path this is also where the detector is fed, because the beats it finds
        are the queue.
        """
        if self._qrs is not None:
            self._feed_detector(ecg)

        changed = False
        while True:
            newest = ecg.newest_ms
            if newest is None:
                return changed

            # A located beat is committed once its post-R part is recorded, so the
            # wave is never clamped onto the newest sample.
            if self._pending_ms is not None:
                if newest < self._pending_ms + self._template.current_window().post:
                    return changed
                if self._commit(ecg):
                    changed = True
                continue

            if not self._queue:
                return changed
            beat_ms, rr = self._queue[0]
            # On the QRS path the peak is already measured from the waveform, so
            # there is nothing to locate — only the wave's tail has to exist,
            # which `_commit` checks. On the sensor path the finder still needs
            # its look-ahead before it can name an R.
            if self._qrs is None:
                lookahead = self._peak_finder.lookahead_ms(self._template.current_window())
                if newest < beat_ms + lookahead:
                    return changed
                self._queue.popleft()
                located = self._peak_finder.locate(
                    ecg,
                    beat_ms,
                    self._model,
                    self._template.current_window(),
                    rr,
                )
            else:
                # The detector could not have reported this peak before its whole
                # look-ahead had been consumed, so by now the samples are there.
                # The check is still made, because a beat can also be queued by a
                # caller that fed the detector less than it claims.
                if newest < beat_ms + self._qrs.lag_ms():
                    return changed
                self._queue.popleft()
                located = float(beat_ms)
            self._pending_ms = float(located)
            self._pending_rr = rr
            self._pending_wave = self._template.from_ecg(ecg, self._pending_ms)

    def _feed_detector(self, ecg: EcgRing) -> None:
        """
        Hand the QRS detector every ECG sample it has not seen, and queue the
        beats it reports.

        The ring keeps its own history, so the only bookkeeping needed is a mark
        of how far the detector has already read. Feeding it twice would make it
        see each complex twice and detection collapse; skipping samples would
        open a hole in the filter state.
        """
        assert self._qrs is not None
        newest = ecg.newest_ms
        if newest is None:
            return
        if self._fed_upto_ms is not None and newest <= self._fed_upto_ms:
            return

        start_ms = 0 if self._fed_upto_ms is None else self._fed_upto_ms + 1
        lo, hi = ecg.band(start_ms, newest)
        if hi <= lo:
            return
        _, values = ecg._lists()  # noqa: SLF001 - the ring is this module's own
        # The ring's own time for the first sample handed over, so the detector's
        # sample indices become comparable with `beat_ms` and with the ring.
        self._qrs.feed(values[lo:hi], start_ms=ecg.time_at(lo))
        self._fed_upto_ms = newest

        for beat_ms in self._qrs.detect():
            # The previous beat is the one this detector reported last, which is
            # not `_last_beat_ms`: that field is set by `on_beat`, and on this
            # path the sensor's announcements do not create beats at all. Using
            # it made every interval after the queue drained come out zero, so
            # the traces were drawn with no width.
            prev = self._last_detected_beat_ms
            rr = int(round(beat_ms - prev)) if prev is not None and beat_ms > prev else 0
            if rr > 0:
                # The drawn width follows the interval the beats *actually* have,
                # which on this path is measured rather than announced.
                self._template.observe_rr(float(rr))
            self._queue.append((int(round(beat_ms)), rr))
            self._last_detected_beat_ms = beat_ms

    def _commit(self, ecg: EcgRing) -> bool:
        """Commit the located beat whose entire window is now recorded."""
        wave = self._pending_wave or self._template.from_ecg(ecg, self._pending_ms)
        self._pending_ms = None
        self._pending_wave = None
        if wave is None:
            return False
        # Still feed the model while the early beats are withheld: the finder is
        # what settles the beat clock, so hiding them must not also stop them from
        # helping.
        self._recent.append(wave)
        if len(self._recent) >= self.TEMPLATE_MIN:
            self._model = median_wave(self._recent)
        changed = False
        # Withheld until the count passes AND the ECG clock has been measured. The
        # first beats are placed with the chain still running on the sensor's
        # real-ms intervals, which put them up to a third of a QRS off the ones
        # that follow — visible as a few strays at the left of an otherwise
        # superposed stack.
        if self._commits >= self.WARMUP_BEATS and self._clock_ready():
            self._stack.append((wave, self._pending_rr))
            changed = True
            self._version += 1
        self._commits += 1
        return changed

    def _clock_ready(self) -> bool:
        """
        Whether the beat reference point is trustworthy enough to draw.

        Only `PeakFinder` has a settling clock (it calibrates the ECG's own rate
        against the sensor's intervals), so only it can withhold waves. A
        detected peak IS the measurement — there is no clock to settle — so
        gating on one would just delay the first waves for no reason.
        """
        if self._qrs is not None:
            return True
        return self._peak_finder.timebase_ready()

def median_wave(waves: Sequence[Sequence[float]]) -> List[float]:
    n = len(waves[0])
    out: List[float] = []
    for g in range(n):
        out.append(median([w[g] for w in waves]))
    return out


def draw_axis(beats: Sequence[Tuple[Sequence[float], int]]) -> float:
    """
    The shared axis: the drawn span of the *median* interval on screen.

    Median rather than mean, because this single width is what every trace is
    scaled against; the mean would let the missed/extra detections that are
    supposed to be ignored widen the axis they are measured against.
    """
    rrs = sorted(rr for _, rr in beats if rr > 0)
    if not rrs:
        return 0.0
    mid = rrs[(len(rrs) - 1) // 2]
    return BeatTemplate.draw_span(float(mid))


def age_fraction(i: int, n: int) -> float:
    """
    Age of wave `i` in a stack, 0 (oldest) to 1 (newest).

    The beats pane ramps its colour by this, so the newest complex stands out.
    """
    if n <= 1:
        return 1.0
    return i / float(n - 1)
