"""
Heart state: the dongle's ECG and R–R lines turned into the four graph frames.

This is the equivalent of the app's `LiveEngine` plus the three strips that
publish it (`EcgStrip`, `TrendStrip`, `RmssdHeatmap`), collapsed into one object
because there is no render tick here — the browser polls and asks for a frame.

Timing. Beats are stamped on the **ECG sample clock**, exactly as the app does:
the dongle stamps the first sample of each ECG batch and advances by the sample
period, so its clock is smooth even though the batches arrive in bursts. A
heart-rate packet closes its last interval when it arrives, so that beat is
"now" and the earlier beats in the packet sit that many intervals back. Anchoring
per packet like this (rather than summing R–R from connect) keeps the beats plot
centred when delivery stalls.

Without ECG (a strap that never offers it) the same bookkeeping runs on the
dongle's own `ts`, so the intervals still drive the metrics, the trends graph and
the RMSSD panel — only the two plots that need a waveform stay empty.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from .ecg import BeatOverlay, BeatTemplate, EcgRing, age_fraction, draw_axis, wave_frame
from .metrics import HrFilter, NnHrv, RmssdGrid, RmssdMapConfig, TrendTrace

logger = logging.getLogger(__name__)

#: ECG `fs` used when a batch does not carry one (the Polar path is 130 Hz).
FALLBACK_FS_HZ = 130.0

#: How much ECG history the plots can reach back into, seconds.
ECG_RING_SECONDS = 20.0

#: Widths the ECG strip offers, in multiples of the mean RR.
WINDOW_PRESETS: Sequence[float] = (3.0, 5.0, 8.0, 10.0, 15.0, 20.0)

#: Widths the trends graph offers, minutes.
TREND_WIDTH_MINUTES: Sequence[int] = (1, 2, 5, 10, 20, 30, 45, 60)

#: Total time the RMSSD panel spans, seconds.
WIDTH_PRESETS: Sequence[int] = (120, 240, 480, 900, 1800, 3600)

#: Tile counts the panel offers per side.
MIN_SIDE = 2
MAX_SIDE = 8


class HeartState:
    """
    Everything the four heart panels show, fed by the dongle's serial lines.

    One lock guards the whole thing: the serial thread writes, the Flask threads
    read, and the frames are built inside the read. The work is small (a slice of
    the ECG ring and a pass over the beat ring), so serialising it is cheaper than
    the alternative and much easier to reason about.
    """

    def __init__(self, fs_hz: float = FALLBACK_FS_HZ) -> None:
        self._lock = threading.Lock()
        self.fs_hz = fs_hz

        self._ecg = EcgRing(fs_hz=fs_hz, seconds=ECG_RING_SECONDS)
        self._hr = HrFilter()
        self._overlay = BeatOverlay(BeatTemplate())
        # Beats are detected from the waveform when the rate allows it; below
        # 200 Hz the Pan–Tompkins port does not apply and the sensor's own
        # announcements drive the stack instead.
        self._overlay.use_qrs(fs_hz)
        self._nnh = NnHrv()
        self._trends = TrendTrace()
        self._grid = RmssdGrid(RmssdMapConfig())

        # Time at the right edge of the plots: the newest ECG sample, or the
        # dongle's own timestamp when there is no ECG.
        self._now_ms: Optional[int] = None
        self._beat_clock_ms: int = 0

        self._ecg_window_multiples: float = 10.0
        self._trend_width_seconds: int = 600
        self._beats_limit: int = 64

        # Bookkeeping for the status line.
        self._ecg_packets = 0
        self._rr_beats = 0
        self._last_rr_ms = 0
        self._last_ecg_fs: Optional[float] = None

        # Cached frames, invalidated by the counters below rather than on every
        # add: the browser polls faster than the tiles change, and rebuilding a
        # 64-tile grid per poll is pointless work.
        self._grid_cache: Optional[Dict[str, Any]] = None
        self._grid_cache_version = -1

    # -- ingest ----------------------------------------------------------

    def on_message(self, msg: Dict[str, Any]) -> bool:
        """
        Feed one parsed dongle line; returns True when it was heart-related.

        Only `ecg` and `hr` lines matter here. An `hr` line without `rr` (a strap
        reporting bpm only) is still accepted so the status line can show the
        heart rate, but it contributes no intervals.
        """
        kind = msg.get("type")
        if kind == "ecg":
            self._on_ecg(msg)
            return True
        if kind == "hr":
            self._on_hr(msg)
            return True
        return False

    def _on_ecg(self, msg: Dict[str, Any]) -> None:
        samples = msg.get("uv")
        if not samples:
            return
        fs = msg.get("fs")
        try:
            fs_hz = float(fs) if fs else self.fs_hz
        except (TypeError, ValueError):
            fs_hz = self.fs_hz

        t0_ms = int(msg.get("ts") or 0)
        with self._lock:
            # A sample rate change mid-session reallocates the ring, so the two
            # plots cannot mix timestamps from two grids.
            if fs_hz != self._ecg.fs_hz:
                self._ecg = EcgRing(fs_hz=fs_hz, seconds=ECG_RING_SECONDS)
                self.fs_hz = fs_hz
                # The beat detector is rate-specific, and the session may have
                # started on the 130 Hz fallback before the dongle said 256 Hz.
                self._overlay.use_qrs(fs_hz)
            # A dongle reboot restarts its uptime clock; the beats and trends
            # are anchored on it, so the whole session starts over rather than
            # mixing two clocks in one plot. Both the ring and the session
            # clock are consulted: R-R batches can push the session clock past
            # the last ECG sample, so the ring alone would miss the rewind.
            elif t0_ms < max(self._ecg.newest_ms or 0, self._now_ms or 0):
                logger.warning("ECG clock went backwards (dongle reboot?); restarting session")
                self._reset_locked()
            self._ecg.push(samples, t0_ms)
            self._last_ecg_fs = fs_hz
            self._ecg_packets += 1

            # Beats are queued on the R–R lines and located once enough ECG has
            # arrived past them, so both sides have to advance the queue or it
            # stalls between packets.
            self._overlay.pump(self._ecg)

            end = self._ecg.newest_ms
            if end is not None:
                self._now_ms = end
            if self._beat_clock_ms == 0:
                # Before the first beat, the ECG edge is the best clock there is.
                self._beat_clock_ms = self._now_ms or t0_ms

    def _on_hr(self, msg: Dict[str, Any]) -> None:
        rrs = msg.get("rr") or []
        ts = int(msg.get("ts") or 0)
        intervals: List[int] = []
        for rr in rrs:
            try:
                value = int(rr)
            except (TypeError, ValueError):
                continue
            if value > 0:
                intervals.append(value)

        with self._lock:
            if self._now_ms is None:
                self._now_ms = ts

            if not intervals:
                # No R–R in this packet: nothing to add, but the standing rate
                # still belongs on the status line.
                bpm = msg.get("bpm")
                if bpm is not None and self._hr.bpm() <= 0.0:
                    try:
                        self._hr.push(60_000.0 / float(bpm))
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                return

            # The packet closes its last interval when it arrives, so that beat is
            # "now" and the earlier ones sit that many intervals back.
            newest = self._now_ms if self._now_ms is not None else ts
            t = newest - sum(intervals)
            for rr in intervals:
                t += rr
                self._commit_beat(t, rr)
            self._beat_clock_ms = newest

    def _commit_beat(self, beat_ms: int, rr_ms: int) -> None:
        """One closed interval. Caller holds the lock."""
        self._rr_beats += 1
        self._last_rr_ms = rr_ms

        # The beats plot is drawn from the ECG, so it is skipped when the strap
        # does not provide one.
        if self._ecg.count() > 0:
            self._overlay.on_beat(self._ecg, beat_ms, rr_ms)

        if not (300 <= rr_ms <= 2000):
            # Almost always a missed or extra sensor beat: it closes the drawn
            # beat above but must not touch HR, the statistics or the grid.
            return

        self._nnh.on_rr_ms(float(rr_ms))
        self._grid.on_rr(float(beat_ms), float(rr_ms))
        self._hr.push(float(rr_ms))
        self._trends.on_beat(
            float(beat_ms),
            self._hr.bpm(),
            float(self._nnh.rmssd30_ms()),
            float(self._nnh.rmssd300_ms()),
        )

    # -- tuning ----------------------------------------------------------

    def set_ecg_window(self, multiples: float) -> None:
        with self._lock:
            self._ecg_window_multiples = min(max(float(multiples), 2.0), 20.0)

    def set_trend_width(self, seconds: int) -> None:
        with self._lock:
            self._trends.width_seconds = int(seconds)
            self._trend_width_seconds = self._trends.width_seconds

    def set_beats_limit(self, limit: int) -> None:
        steps = BeatOverlay.BEAT_STEPS
        nearest = min(steps, key=lambda s: abs(s - int(limit)))
        with self._lock:
            self._beats_limit = nearest

    def set_rmssd_config(self, width_seconds: Optional[int] = None, side: Optional[int] = None) -> None:
        with self._lock:
            cfg = self._grid.cfg
            new_cfg = RmssdMapConfig(
                width_seconds=int(width_seconds) if width_seconds is not None else cfg.width_seconds,
                side=min(max(int(side), MIN_SIDE), MAX_SIDE) if side is not None else cfg.side,
                rr_min_ms=cfg.rr_min_ms,
                rr_max_ms=cfg.rr_max_ms,
                color_stops=cfg.color_stops,
            )
            self._grid.reconfigure(new_cfg)

    def reset(self) -> None:
        """Drop every accumulated beat (a new session, or a manual clear)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """The body of `reset`; the ingest path calls it while holding the lock."""
        self._ecg.clear()
        self._hr.reset()
        self._nnh.reset()
        self._trends.reset()
        self._grid.reset()
        self._overlay.clear()
        self._now_ms = None
        self._beat_clock_ms = 0
        self._ecg_packets = 0
        self._rr_beats = 0
        self._last_rr_ms = 0
        self._last_ecg_fs = None
        self._grid_cache = None
        self._grid_cache_version = -1

    # -- frames ----------------------------------------------------------

    def _now(self) -> int:
        return int(self._now_ms or 0)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ecg_packets": self._ecg_packets,
                "ecg_fs_hz": self._last_ecg_fs,
                "ecg_samples": self._ecg.count(),
                "beats": self._rr_beats,
                "buffered_beats": self._overlay.buffered_count(),
                # Whether the beats stack is drawing from detected R peaks
                # (200 Hz and up) or from the sensor's announcements.
                "detecting": self._overlay.detecting,
                "beat_lag_ms": self._overlay.lag_ms(),
                "detector_fed": self._overlay.detector_fed(),
                "last_rr_ms": self._last_rr_ms,
                "now_ms": self._now(),
                "hr_bpm": round(self._hr.bpm(), 1),
                "mean_rr_ms": self._nnh.mean_rr30_ms(),
                "rmssd30_ms": self._nnh.rmssd30_ms(),
                "rmssd300_ms": self._nnh.rmssd300_ms(),
                "ecg_window_rr": self._ecg_window_multiples,
                "trend_width_s": self._trend_width_seconds,
                "beats_limit": self._beats_limit,
                "rmssd": self._grid.cfg.to_dict(),
            }

    def ecg_frame(
        self, window_multiples: Optional[float] = None, frozen: bool = False
    ) -> Dict[str, Any]:
        """
        The scrolling strip: the `window` mean-RRs of samples ending at the playhead.

        The window is sized in *beats* rather than seconds, so the trace looks the
        same whether the heart is fast or slow. While `frozen` the playback clock
        still advances (so resuming shows live data), but no frame is built — the
        caller keeps drawing the one it already has.
        """
        with self._lock:
            if frozen:
                self._ecg.advance(time.monotonic() * 1000.0)
                return None
            multiples = self._ecg_window_multiples if window_multiples is None else float(window_multiples)
            mean_rr = float(self._nnh.mean_rr30_ms() or 0) or 800.0
            window_ms = multiples * mean_rr
            frame = self._ecg.window(window_ms, mean_rr)
            if frame is None:
                return {
                    "empty": True,
                    "window_ms": window_ms,
                    "mean_rr_ms": mean_rr,
                    "window_rr": multiples,
                    "presets": list(WINDOW_PRESETS),
                    "now_ms": self._now(),
                }
            # Integers, not floats: the samples are whole microvolts already, and
            # this frame is fetched once per display refresh, so the encoding is
            # the difference between ~6 KB and ~16 KB per frame.
            frame["samples"] = [int(round(v)) for v in frame["samples"]]
            frame.update(
                {
                    "empty": False,
                    "window_rr": multiples,
                    "presets": list(WINDOW_PRESETS),
                    "now_ms": self._now(),
                }
            )
            return frame

    def beats_frame(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        The beats stack: one normalised wave per recent beat, all at one scale.

        Every wave holds the *same* slice of ECG — its sampling window — so every
        trace is drawn over the same width and they superpose into one readable
        stack. Sizing traces by each beat's own interval put the same grid sample
        at a different x on every trace, so at any given x each trace showed a
        different amount of ECG and the complexes stopped lining up.

        `r_frac` is where R sits across the plot: not the middle, because the
        window spends a third of its room before R (PR interval, P wave) and two
        thirds after it (QRS, ST segment, T wave).

        Each wave also carries its own `peak`: the grid index its dominant
        stroke was found on (see `WaveAnchor`). Slicing puts R at `r_at` by
        construction, but the beat clock that decides *where* to slice is derived
        from the sensor's packet-quantised R–R stream, so the complex can land a
        sample or two off. The pane draws every `peak` on the same vertical, which
        puts each R on the shared centre line whatever that jitter did.
        """
        with self._lock:
            limit_now = self._beats_limit if limit is None else int(limit)
            raw = self._overlay.beats(limit_now)
            axis_ms = draw_axis(raw)
            # Every wave is normalised against its own median, so one shared
            # amplitude is what draws them all at a comparable size.
            frame = wave_frame([samples for samples, _ in raw])
            waves = []
            for i, (samples, rr) in enumerate(raw):
                waves.append(
                    {
                        "samples": [round(s, 4) for s in samples],
                        # Kept for the header and for describing the stack; it no
                        # longer sizes the trace.
                        "span_ms": BeatTemplate.draw_span(float(rr)),
                        "rr_ms": rr,
                        "age": age_fraction(i, len(raw)),
                        "baseline": round(frame["baselines"][i], 4),
                        # Where this wave's own stroke sits in the grid, so the
                        # pane can pin every trace's R to one line.
                        "peak": int(frame["peaks"][i]),
                    }
                )
            return {
                "waves": waves,
                "axis_ms": axis_ms,
                # The window shape every trace shares, so the pane can place R
                # and scale the whole grid instead of one beat's interval.
                "r_at": BeatTemplate.R_AT,
                "r_frac": BeatTemplate.R_FRAC,
                "grid_len": BeatTemplate.GRID_LEN,
                "reach": frame["reach"],
                "shown": len(raw),
                "available": self._overlay.buffered_count(),
                "limit": limit_now,
                "steps": list(BeatOverlay.BEAT_STEPS),
                "stack_cap": BeatOverlay.STACK_CAP,
                "now_ms": self._now(),
            }

    def trend_frame(self, width_seconds: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            if width_seconds is not None:
                self._trends.width_seconds = int(width_seconds)
                self._trend_width_seconds = self._trends.width_seconds
            frame = self._trends.snapshot(float(self._now()))
            return {
                "times_ms": [round(t, 1) for t in frame.times_ms],
                "hr_bpm": [_finite_or_none(v) for v in frame.hr_bpm],
                "rmssd30_ms": [_finite_or_none(v) for v in frame.rmssd30_ms],
                "rmssd300_ms": [_finite_or_none(v) for v in frame.rmssd300_ms],
                "count": frame.count,
                "window_ms": frame.window_ms,
                "now_ms": frame.now_ms,
                "width_s": self._trend_width_seconds,
                "presets_min": list(TREND_WIDTH_MINUTES),
                "hr_axis": frame.hr_axis.to_dict(),
                "rmssd_axis": frame.rmssd_axis.to_dict(),
                # The tile grid's legend, so a filled RMSSD curve on this panel
                # means the same colour as a tile over there. One definition of
                # "green/yellow/red" for both graphs, from `RmssdMapConfig`.
                "color_stops": [[ms, color] for ms, color in self._grid.cfg.color_stops],
            }

    def rmssd_frame(self) -> Dict[str, Any]:
        with self._lock:
            if self._grid_cache is not None and self._grid_cache_version == self._grid.version:
                return self._grid_cache
            frame = self._grid.snapshot()
            frame["width_presets_s"] = list(WIDTH_PRESETS)
            frame["min_side"] = MIN_SIDE
            frame["max_side"] = MAX_SIDE
            self._grid_cache = frame
            self._grid_cache_version = self._grid.version
            return frame


def _finite_or_none(value: float) -> Optional[float]:
    """NaN is the "not known yet" marker; JSON has no NaN, so it becomes null."""
    if not math.isfinite(value):
        return None
    return round(value, 1)
