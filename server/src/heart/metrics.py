"""
R–R interval metrics for the heart graphs.

A Python port of the pure-Kotlin detector in the `my_heart` Android app
(`android/detector`), so the browser shows the same four panels from the same
numbers: heart rate, the two RMSSDs, the trends history and the RMSSD tile grid.

Everything here is fed by the sensor's **own** R–R intervals — the dongle's
`{"type":"hr","rr":[...]}` line — which is what the app uses too. No beat
detection: intervals outside [RR_MIN, RR_MAX] are ignored for HR, the RR
statistics and the tile grid (they are almost always a missed or extra sensor
beat) but still close a drawn beat; see `state.py`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

# Plausible interval band. The same gate the app applies to every statistic.
RR_MIN_MS = 300.0
RR_MAX_MS = 2000.0


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class HrFilter:
    """
    Smoothed heart rate from R–R intervals.

    Needs REACQUIRE_COUNT consistent intervals before it reports anything, then
    follows small changes. A larger jump (ectopy, a missed beat) is not applied
    until that many intervals agree on the new rate, so one odd interval cannot
    move it.
    """

    MIN_BPM = 30.0
    MAX_BPM = 220.0
    ALPHA = 0.2
    GATE = 0.18
    REACQUIRE_COUNT = 3
    REACQUIRE_SPREAD = 0.12

    def __init__(self) -> None:
        self._value = 0.0
        self._locked = False
        self._seed: List[float] = []
        self._challenger: List[float] = []

    def reset(self) -> None:
        self._value = 0.0
        self._locked = False
        self._seed.clear()
        self._challenger.clear()

    def bpm(self) -> float:
        return self._value if self._locked else 0.0

    def push(self, rr_ms: float) -> None:
        if rr_ms <= 0.0:
            return
        inst = 60_000.0 / rr_ms
        if inst < self.MIN_BPM or inst > self.MAX_BPM:
            return

        if not self._locked:
            self._seed = (self._seed + [inst])[-self.REACQUIRE_COUNT:]
            if len(self._seed) >= self.REACQUIRE_COUNT and self._consistent(self._seed):
                self._value = sum(self._seed) / len(self._seed)
                self._locked = True
                self._seed.clear()
                self._challenger.clear()
            return

        if self._value * (1.0 - self.GATE) <= inst <= self._value * (1.0 + self.GATE):
            self._value += self.ALPHA * (inst - self._value)
            self._challenger.clear()
            return

        self._challenger = (self._challenger + [inst])[-self.REACQUIRE_COUNT:]
        if len(self._challenger) >= self.REACQUIRE_COUNT and self._consistent(self._challenger):
            self._value = sum(self._challenger) / len(self._challenger)
            self._challenger.clear()

    @staticmethod
    def _consistent(vals: Sequence[float]) -> bool:
        lo = min(vals)
        hi = max(vals)
        return lo > 1e-6 and hi / lo <= 1.0 + HrFilter.REACQUIRE_SPREAD


class NnHrv:
    """
    Short/long RR statistics from the sensor's R–R stream.

    meanRr30Ms  — mean of the last <=30 intervals
    rmssd30Ms   — RMSSD over the last <=30 intervals (needs >=15)
    rmssd300Ms  — RMSSD over the last <=300 intervals (needs >=60)

    Returns 0 while the window is not yet filled enough — which is how the app
    reports "not known yet", and what makes the trends graph draw a gap.
    """

    FAST_N = 30
    SLOW_N = 300
    FAST_MIN = 15
    SLOW_MIN = 60

    def __init__(self) -> None:
        self._nn = [0] * self.SLOW_N
        self._count = 0
        self._head = 0

    def reset(self) -> None:
        self._count = 0
        self._head = 0
        self._nn = [0] * self.SLOW_N

    def count(self) -> int:
        return self._count

    def on_rr_ms(self, rr_ms: float) -> None:
        if rr_ms < RR_MIN_MS or rr_ms > RR_MAX_MS:
            return
        ms = int(rr_ms + 0.5)
        ms = max(300, min(2000, ms))
        self._nn[self._head] = ms
        self._head = (self._head + 1) % self.SLOW_N
        if self._count < self.SLOW_N:
            self._count += 1

    def _at(self, i: int, n: int) -> int:
        age_from_newest = n - 1 - i
        idx = self._head - 1 - age_from_newest
        while idx < 0:
            idx += self.SLOW_N
        return self._nn[idx % self.SLOW_N]

    def mean_rr30_ms(self) -> int:
        n = min(self._count, self.FAST_N)
        if n < 1:
            return 0
        total = sum(self._at(i, n) for i in range(n))
        return (total + n // 2) // n

    def rmssd30_ms(self) -> int:
        return self._rmssd_window(min(self._count, self.FAST_N), self.FAST_MIN)

    def rmssd300_ms(self) -> int:
        return self._rmssd_window(self._count, self.SLOW_MIN)

    def _rmssd_window(self, n: int, min_n: int) -> int:
        if n < min_n or n < 2:
            return 0
        sum_sq = 0.0
        for i in range(n - 1):
            d = self._at(i + 1, n) - self._at(i, n)
            sum_sq += d * d
        ms = math.sqrt(sum_sq / (n - 1))
        return max(0, min(65535, int(ms + 0.5)))


@dataclass
class TrendAxis:
    """A vertical axis: the range a series is drawn against, plus its label step."""

    min: float
    max: float
    step: float

    @property
    def span(self) -> float:
        return self.max - self.min

    def ticks(self) -> List[float]:
        if self.step <= 0.0 or self.span <= 0.0:
            return [self.min]
        n = int((self.max - self.min) / self.step)
        return [self.min + i * self.step for i in range(n + 1)]

    def to_dict(self) -> dict:
        return {"min": self.min, "max": self.max, "step": self.step}

    @staticmethod
    def empty() -> "TrendAxis":
        return TrendAxis(0.0, 1.0, 1.0)


@dataclass
class TrendFrame:
    """
    The three series of the trends graph on a common time base.

    Two axes, because the units differ: `hr_axis` in bpm for heart rate and
    `rmssd_axis` in ms for both RMSSD curves. Series hold NaN where the value is
    still unknown, so the graph draws a gap instead of a confident line at zero.
    """

    times_ms: List[float]
    hr_bpm: List[float]
    rmssd30_ms: List[float]
    rmssd300_ms: List[float]
    count: int
    window_ms: float
    now_ms: float
    hr_axis: TrendAxis
    rmssd_axis: TrendAxis

    @property
    def empty(self) -> bool:
        return self.count == 0


class TrendTrace:
    """
    Rolling history of per-beat trends: heart rate, RMSSD30 and RMSSD300.

    One point per accepted beat, so the X axis is beats placed at their real
    times. The history is kept regardless of which tab is open, and the window
    is sized in *time*, so changing the width re-windows what is already held
    rather than discarding it.
    """

    MIN_WIDTH_SECONDS = 60
    MAX_WIDTH_SECONDS = 3600
    DEFAULT_WIDTH_SECONDS = 600
    DEFAULT_CAPACITY = 16_384

    _HR_MIN_SPAN = 20.0
    _HR_FALLBACK = 140.0

    #: Top of the RMSSD axis, in ms. Pinned rather than auto-scaled so the
    #: trends curves are read against the same yardstick as the RMSSD tiles,
    #: which colour from 0 to 400 ms (`RmssdMapConfig.color_stops`). An
    #: auto-scaled axis re-fitted a quiet session into the same plot height as
    #: a stressed one, so the two panels disagreed about what "high" meant.
    RMSSD_AXIS_MAX = 400.0
    RMSSD_AXIS_STEP = 100.0

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = capacity
        # Stored outside the deque as parallel lists so a snapshot can slice the
        # window cheaply; [deque] enforces the capacity and the ordering.
        self._points: deque = deque(maxlen=capacity)
        self.width_seconds = self.DEFAULT_WIDTH_SECONDS

    def reset(self) -> None:
        self._points.clear()

    def point_count(self) -> int:
        return len(self._points)

    def on_beat(self, time_ms: float, hr_bpm: float, rmssd30_ms: float, rmssd300_ms: float) -> None:
        # Keep the series monotonic: a late beat must not reorder the history.
        if self._points and time_ms < self._points[-1][0]:
            return
        self._points.append(
            (
                float(time_ms),
                hr_bpm if hr_bpm > 0.0 else float("nan"),
                rmssd30_ms if rmssd30_ms > 0.0 else float("nan"),
                rmssd300_ms if rmssd300_ms > 0.0 else float("nan"),
            )
        )

    def snapshot(self, now_ms: float) -> TrendFrame:
        width_s = self.width_seconds
        window_ms = width_s * 1000.0
        if not self._points:
            return TrendFrame([], [], [], [], 0, window_ms, now_ms,
                              self._hr_axis([]), self._rmssd_axis())

        times: List[float] = []
        hr: List[float] = []
        r30: List[float] = []
        r300: List[float] = []

        newest = self._points[-1][0]
        # The right edge is "now" — a beat is simply pending at the right of a
        # live chart — but it never falls behind the newest beat, so a stalled
        # clock still shows the data that did arrive.
        end = max(now_ms, newest)
        start = end - window_ms
        for t, h, a, b in self._points:
            if t < start:
                continue
            if t > end:
                break
            times.append(t)
            hr.append(h)
            r30.append(a)
            r300.append(b)

        return TrendFrame(
            times_ms=times,
            hr_bpm=hr,
            rmssd30_ms=r30,
            rmssd300_ms=r300,
            count=len(times),
            window_ms=window_ms,
            now_ms=end,
            hr_axis=self._hr_axis(hr),
            rmssd_axis=self._rmssd_axis(),
        )

    def _hr_axis(self, hr: Sequence[float]) -> TrendAxis:
        """Auto-fit: heart rate ranges too widely for a useful fixed scale."""
        return _axis_for([hr], self._HR_MIN_SPAN, floor_at_zero=False,
                         fallback_max=self._HR_FALLBACK)

    @classmethod
    def _rmssd_axis(cls) -> TrendAxis:
        """Fixed 0..400 ms, matching the RMSSD tiles' colour range."""
        return TrendAxis(0.0, cls.RMSSD_AXIS_MAX, cls.RMSSD_AXIS_STEP)


def nice_step(raw: float) -> float:
    """Round steps: 1, 2, 5 x 10^k."""
    if raw <= 0.0:
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = 10.0 ** exponent
    m = raw / base
    if m <= 1.0:
        nice = 1.0
    elif m <= 2.0:
        nice = 2.0
    elif m <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * base


def _build_axis(lo_in: float, hi_in: float, min_span: float, floor_at_zero: bool) -> TrendAxis:
    lo, hi = lo_in, hi_in
    if hi - lo < min_span:
        mid = (hi + lo) / 2.0
        lo = mid - min_span / 2.0
        hi = mid + min_span / 2.0
    pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad
    if floor_at_zero and lo < 0.0:
        lo = 0.0

    step = nice_step((hi - lo) / 4.0)
    nlo = math.floor(lo / step) * step
    nhi = math.ceil(hi / step) * step
    if nhi <= nlo:
        return TrendAxis(nlo, nlo + step, step)
    return TrendAxis(nlo, nhi, step)


def _axis_for(
    series: Iterable[Sequence[float]],
    min_span: float,
    floor_at_zero: bool,
    fallback_max: Optional[float] = None,
) -> TrendAxis:
    """One axis over one or more series that share a scale."""
    lo = float("inf")
    hi = float("-inf")
    for values in series:
        for v in values:
            if not math.isfinite(v):
                continue
            if v < lo:
                lo = v
            if v > hi:
                hi = v
    if lo > hi:
        return _build_axis(0.0, fallback_max if fallback_max is not None else min_span, min_span, floor_at_zero)
    return _build_axis(lo, hi, min_span, floor_at_zero)


@dataclass
class RmssdMapConfig:
    """
    Every knob of the exponential RMSSD panel.

    The panel is `side` x `side` tiles on two geometric axes:

      - Time (X) runs oldest -> now. Tile widths double to the right, so the
        newest column is one fine slot of `slot_seconds` and every column to its
        left is twice as wide as the one after it. The `side` widths tile
        `width_seconds` exactly: slot_seconds * (2^side - 1) = width_seconds.
      - Window length (Y) runs longest N at the top -> shortest at the bottom,
        each row half the N of the row below it. The top row spans the full
        width in RRs, so both axes read the same number at their far end.
    """

    width_seconds: int = 480
    side: int = 4
    rr_min_ms: float = RR_MIN_MS
    rr_max_ms: float = RR_MAX_MS
    color_stops: Tuple[Tuple[float, str], ...] = (
        (10.0, "#2e7d32"),
        (205.0, "#fbc02d"),
        (400.0, "#c62828"),
    )

    MIN_WINDOW_RRS = 2

    def __post_init__(self) -> None:
        if self.side < 2:
            raise ValueError("need at least two tiles per side")
        if self.width_seconds <= 0:
            raise ValueError("width must be positive")

    @property
    def slots(self) -> int:
        """Fine time slots held in the ring; they tile the width exactly."""
        return (1 << self.side) - 1

    @property
    def slot_seconds(self) -> float:
        return self.width_seconds / self.slots

    def n_at(self, row: int) -> int:
        return max(self.MIN_WINDOW_RRS, self.width_seconds >> row)

    @property
    def n_longest(self) -> int:
        return self.n_at(0)

    @property
    def n_shortest(self) -> int:
        return self.n_at(self.side - 1)

    def column_slots(self, col: int) -> int:
        return 1 << (self.side - 1 - col)

    def column_start_seconds(self, col: int) -> float:
        ahead = sum(self.column_slots(c) for c in range(col + 1, self.side))
        return (self.column_slots(col) + ahead) * self.slot_seconds

    def bin_at(self, time_ms: float) -> int:
        return math.floor(time_ms * self.slots / (self.width_seconds * 1000.0))

    def to_dict(self) -> dict:
        return {
            "width_s": self.width_seconds,
            "side": self.side,
            "slots": self.slots,
            "slot_s": self.slot_seconds,
            "n_at": [self.n_at(r) for r in range(self.side)],
            "n_longest": self.n_longest,
            "n_shortest": self.n_shortest,
            "column_start_s": [self.column_start_seconds(c) for c in range(self.side)],
            "color_stops": [[ms, color] for ms, color in self.color_stops],
        }


class RmssdGrid:
    """
    The exponential RMSSD panel's accumulator.

    Each accepted RR closes one beat and updates **one fine slot**, the newest of
    `slots` that tile the whole width. A slot holds RMSSD over the last N RRs for
    every N a row asks for, computed in one pass over the RR history. A beat that
    lands in the slot already holding the newest one overwrites it, so the slot
    always describes the *latest* beat in its window — "how is my HRV now", not
    an average over stale beats.

    The panel's columns are then aggregations of the slot ring: column `side`-1
    ("now") is the newest slot, and each column one further left covers twice as
    many slots. A tile is the mean of the finite values in its column.
    """

    def __init__(self, cfg: Optional[RmssdMapConfig] = None) -> None:
        self.cfg = cfg or RmssdMapConfig()
        self._allocate()

    def _allocate(self) -> None:
        cfg = self.cfg
        self._rr_cap = cfg.n_longest
        self._rr = [0] * self._rr_cap
        self._rr_count = 0
        self._rr_head = 0
        self._slots = [None] * cfg.slots  # each: list[float|None] of length side
        self._slot_bin = [None] * cfg.slots
        self._head_bin: Optional[int] = None
        self._version = 0

    def reconfigure(self, cfg: RmssdMapConfig) -> None:
        """
        Switch to `cfg` and start over.

        Both knobs reshape the ring (a different side changes the slot count and
        the RR capacity, the width changes what each slot *means*), so the bins
        already stamped would no longer line up. The panel only ever shows the
        last few minutes, so the RRs are worth far less than the simplicity.
        """
        if cfg == self.cfg:
            return
        self.cfg = cfg
        self._allocate()

    def reset(self) -> None:
        self._rr_count = 0
        self._rr_head = 0
        self._slots = [None] * self.cfg.slots
        self._slot_bin = [None] * self.cfg.slots
        self._head_bin = None
        self._version += 1

    @property
    def version(self) -> int:
        return self._version

    def rr_count(self) -> int:
        return self._rr_count

    def on_rr(self, time_ms: float, rr_ms: float) -> None:
        cfg = self.cfg
        if rr_ms < cfg.rr_min_ms or rr_ms > cfg.rr_max_ms:
            return
        self._rr[self._rr_head] = int(rr_ms + 0.5)
        self._rr_head = (self._rr_head + 1) % self._rr_cap
        if self._rr_count < self._rr_cap:
            self._rr_count += 1

        b = cfg.bin_at(time_ms)
        slot = b % cfg.slots
        self._slots[slot] = self._write_slot()
        self._slot_bin[slot] = b
        if self._head_bin is None or b > self._head_bin:
            self._head_bin = b
        self._version += 1

    def _rr_at(self, k: int) -> int:
        """k = 0 is the newest RR."""
        idx = self._rr_head - 1 - k
        while idx < 0:
            idx += self._rr_cap
        return self._rr[idx]

    def _write_slot(self) -> List[Optional[float]]:
        cfg = self.cfg
        nn = cfg.n_longest
        # One pass over the history: the sum of squared successive differences
        # over the last `n` RRs, which is RMSSD(n) with the same N -> N-1
        # differences convention as NnHrv.
        sum_at = [0.0] * (nn + 1)
        newer = self._rr_at(0)
        for k in range(2, self._rr_count + 1):
            older = self._rr_at(k - 1)
            d = float(newer - older)
            sum_at[k] = sum_at[k - 1] + d * d
            newer = older

        out: List[Optional[float]] = []
        for row in range(cfg.side):
            n = cfg.n_at(row)
            if 2 <= n <= self._rr_count:
                out.append(math.sqrt(sum_at[n] / (n - 1)))
            else:
                out.append(None)
        return out

    def _slot_values(self, b: int) -> Optional[List[Optional[float]]]:
        slot = b % self.cfg.slots
        if self._slot_bin[slot] != b:
            return None
        return self._slots[slot]

    def snapshot(self) -> dict:
        """Panel frame: `side * side` tiles, row-major."""
        cfg = self.cfg
        frame = cfg.to_dict()
        frame["rr_count"] = self._rr_count

        tiles: List[dict] = []
        if self._head_bin is None:
            tiles = [{"v": None, "filled": False} for _ in range(cfg.side * cfg.side)]
            frame["tiles"] = tiles
            return frame

        head = self._head_bin
        for row in range(cfg.side):
            for col in range(cfg.side):
                span = cfg.column_slots(col)
                newest = head - (span - 1)
                oldest = head - 2 * (span - 1)
                total = 0.0
                n = 0
                b = newest
                while b >= oldest:
                    values = self._slot_values(b)
                    if values is not None:
                        v = values[row]
                        if v is not None and math.isfinite(v):
                            total += v
                            n += 1
                    b -= 1
                if n == 0:
                    tiles.append({"v": None, "filled": False})
                else:
                    tiles.append({"v": total / n, "filled": True})

        frame["tiles"] = tiles
        return frame
