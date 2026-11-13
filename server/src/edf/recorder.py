"""
Recorder: turns the live serial stream into EDF sessions on disk.

Two decisions this file owns, both about surviving a real session:

**When a file ends.** The dongle timestamps with `k_uptime_get_32()`, so a USB
reset (or any reboot) restarts `ts` near zero mid-stream. Splicing that into one
EDF would put two clocks on one timeline: playback would jump, and EDFbrowser
would show the session shrinking. A rewind is therefore the end of a session and
the start of the next file, exactly as `DataBuffer` treats it.

**What the file is called.** `<prefix>_<yyyyMMdd_HHmmss>[_N].edf`, from the wall
clock at the first sample, so a directory listing sorts chronologically and the
name still means something in EDFbrowser. `_N` disambiguates two sessions inside
the same second.

Retention keeps the newest `max_files` sessions and removes the rest. Only files
matching this prefix and pattern are ever touched, so a hand-copied recording
sitting in the folder is not deleted by the rotation.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .writer import EdfWriter

#: The four heart metrics are computed here rather than read off a serial line:
#: the dongle sends intervals, not the smoothed numbers the dashboard derives
#: from them. These are the same filters `HeartState` feeds (`src/heart`), so a
#: file reports the values the page was showing at the time.
from ..heart.metrics import HrFilter, NnHrv

logger = logging.getLogger(__name__)

#: A rewind this large means the dongle restarted its uptime clock.
#: Matches `DataBuffer.RESTART_THRESHOLD_MS`: smaller shifts are jitter and
#: out-of-order points between device threads.
RESTART_THRESHOLD_MS = 5_000

#: Consecutive samples a full-rate value may sit outside its physical range
#: before it is dropped as bad data (the reader clamps, so a spike would become
#: a ramp). Three is longer than any real ECG excursion.
_MAX_OUT_OF_RANGE = 3

#: Threshold used for that check, as a fraction of the channel span.
_RANGE_SLACK = 0.02


class EdfRecorder:
    """
    Owns the current EDF session and rolls it over.

    Fed from `SerialReader.raw_listeners`, i.e. the same parsed lines the heart
    panels see. The calls come from the serial thread and the Flask threads
    (`resistance` comes from the DirCon event loop), so everything that touches
    writer state is under one lock.
    """

    def __init__(
        self,
        folder: str,
        prefix: str = "zrelay",
        max_files: int = 50,
        enabled: bool = True,
        fs_fallback: float = 130.0,
    ) -> None:
        self.folder = folder
        self.prefix = prefix
        self.max_files = max(0, int(max_files))
        self.enabled = bool(enabled)
        self.fs_fallback = float(fs_fallback)

        self._lock = threading.Lock()
        self._writer: Optional[EdfWriter] = None
        self._session_ts_ms: Optional[int] = None
        self._session_start: Optional[datetime] = None
        self._out_of_range: Dict[str, int] = {}
        #: Heart metrics run per session, not per process: an interval from the
        #: previous file would seed the new one with a rate it never measured.
        self._hr_filter = HrFilter()
        self._nn_hrv = NnHrv()
        #: Where the last finished session went, for /api/status.
        self.last_path: Optional[str] = None
        self.last_error: Optional[str] = None
        self.sessions = 0

        if self.enabled:
            try:
                os.makedirs(self.folder, exist_ok=True)
            except OSError as exc:
                self.enabled = False
                self.last_error = f"cannot create {self.folder}: {exc}"
                logger.error("EDF recording disabled: %s", self.last_error)

    # -- ingest ----------------------------------------------------------

    def on_message(self, msg: dict) -> None:
        """Feed one parsed dongle line. Called from the serial thread."""
        if not self.enabled:
            return
        kind = msg.get("type")
        if kind is None:
            return
        try:
            if kind == "ecg":
                self._on_ecg(msg)
            elif kind == "hr":
                self._on_hr(msg)
            elif kind == "cp":
                self._on_cp(msg)
            elif kind == "ftms":
                self._on_ftms(msg)
            elif kind == "sim":
                self._on_sim(msg)
        except Exception:
            # A malformed line must never take the serial thread (and with it
            # every sensor) down; log once per occurrence and keep going.
            logger.exception("EDF recorder failed on %r", kind)

    def _on_ecg(self, msg: dict) -> None:
        samples = msg.get("uv")
        if not samples:
            return
        ts = int(msg.get("ts") or 0)
        fs = msg.get("fs")
        try:
            rate = float(fs) if fs else self.fs_fallback
        except (TypeError, ValueError):
            rate = self.fs_fallback

        with self._lock:
            if not self._ensure_writer(ts, rate):
                return
            writer = self._writer
            if writer is None:
                return
            filtered = self._filter_ecg(samples)
            if not filtered:
                return
            writer.push_ecg(filtered, ts)

    def _filter_ecg(self, samples) -> List[float]:
        """
        Drop samples that are impossible for this channel.

        The writer scales into int16 and the reader reads the scale back, so a
        stray 50 000 uV value would not be clipped on the way in but would come
        back as a plausible 355 uV. Rejecting the sample outright is the only
        reading that does not invent a spike.
        """
        from .writer import CHANNELS  # local: avoids a cycle at import time

        chan = CHANNELS[0]
        span = chan.p_max - chan.p_min
        lo = chan.p_min - span * _RANGE_SLACK
        hi = chan.p_max + span * _RANGE_SLACK

        out: List[float] = []
        bad = 0
        for value in samples:
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if lo <= v <= hi:
                bad = 0
                out.append(v)
            else:
                bad += 1
                if bad >= _MAX_OUT_OF_RANGE:
                    return []
        return out

    def _on_hr(self, msg: dict) -> None:
        with self._lock:
            writer = self._writer
            if writer is None:
                return
            for rr in msg.get("rr") or []:
                try:
                    interval = float(rr)
                except (TypeError, ValueError):
                    continue
                writer.on_beat(interval)
                # Same filters, same order as HeartState._commit_beat. It skips
                # anything outside 300..2000 ms before feeding them; both of
                # these apply that band themselves (HR as 30..220 bpm), so the
                # values match the dashboard without repeating the gate.
                self._hr_filter.push(interval)
                self._nn_hrv.on_rr_ms(interval)
                self._publish_heart(writer)
            # The strap's own figure, kept as sent rather than derived from the
            # interval: if a strap's bpm and its intervals disagree, that is a
            # sensor fault, and overwriting one with the other would erase it.
            if msg.get("bpm") is not None:
                try:
                    writer.set_channel("SENSOR_HR", float(msg["bpm"]))
                except (TypeError, ValueError):
                    pass

    def _publish_heart(self, writer: EdfWriter) -> None:
        """
        Hold the four heart metrics at their current value. Caller holds the lock.

        Zero means "not known yet" in these filters, and it is also the
        channel default, so a strap that has only just produced a beat writes
        0 until the EMA locks - the same gap the dashboard draws.
        """
        writer.set_channel("HR_EMA", self._hr_filter.bpm())
        writer.set_channel("MEAN_RR", float(self._nn_hrv.mean_rr30_ms()))
        writer.set_channel("HRV_RMSSD30", float(self._nn_hrv.rmssd30_ms()))
        writer.set_channel("HRV_RMSSD300", float(self._nn_hrv.rmssd300_ms()))

    def _on_cp(self, msg: dict) -> None:
        with self._lock:
            writer = self._writer
            if writer is None:
                return
            if msg.get("power") is not None:
                writer.set_channel("POWER", float(msg["power"]))
            if msg.get("cadence") is not None:
                writer.set_channel("CADENCE", float(msg["cadence"]))

    def _on_ftms(self, msg: dict) -> None:
        with self._lock:
            writer = self._writer
            if writer is None:
                return
            if msg.get("speed") is not None:
                writer.set_channel("SPEED", float(msg["speed"]))
            # What the trainer says it is actually applying. Distinct from
            # `RESISTANCE`, which is the setpoint the dongle derived from the
            # grade: this one is the only evidence the trainer followed it.
            if msg.get("resistance") is not None:
                writer.set_channel("TRAINER_RES", float(msg["resistance"]))
            # Trainer power is the fallback, so it must not overwrite what the
            # power meter said: the meter is the reference when both report.
            if msg.get("power") is not None:
                writer.set_channel("TRAINER_POWER", float(msg["power"]))
            if msg.get("cadence") is not None:
                writer.set_channel("TRAINER_CADENCE", float(msg["cadence"]))

    def _on_sim(self, msg: dict) -> None:
        with self._lock:
            if self._writer is None:
                return
            if msg.get("grade") is not None:
                # The dongle prints the FTMS wire value it decoded, in 0.01 %
                # (`dongle/src/ftms_control_point.c`: `json_out(..."grade":%d...)`).
                # The EDF channel is in %, so it takes the same /100 as CMD_GRADE;
                # without it a 3 % climb is recorded as 30 %.
                self._writer.set_channel("GRADE", float(msg["grade"]) / 100.0)
            if msg.get("resistance") is not None:
                # Already unitless 0..100: the dongle converts grade to a target
                # resistance before logging, so this needs no rescaling.
                self._writer.set_channel("RESISTANCE", float(msg["resistance"]))

    # -- trainer commands -------------------------------------------------

    def on_control(self, cmd) -> None:
        """
        Zwift asked the trainer for a grade or a resistance.

        Recorded as its own step channel, not derived from the `sim` lines: the
        `sim` line is what the *trainer* reports back, which is not the same
        thing as what was requested, and the difference is exactly what is worth
        looking at when the resistance does not follow.
        """
        if not self.enabled:
            return
        with self._lock:
            writer = self._writer
            if writer is None:
                return
            grade = getattr(cmd, "grade", None)
            if grade is not None:
                writer.set_channel("CMD_GRADE", grade / 100.0)  # 0.01 % -> %
            resistance = getattr(cmd, "target_resistance", None)
            if resistance is not None:
                writer.set_channel("CMD_RESISTANCE", float(resistance))

    # -- session ---------------------------------------------------------

    def _ensure_writer(self, ts_ms: int, fs_hz: float) -> bool:
        """Open, roll over or keep the current writer. Caller holds the lock."""
        rewind = (
            self._session_ts_ms is not None
            and ts_ms < self._session_ts_ms
            and self._session_ts_ms - ts_ms > RESTART_THRESHOLD_MS
        )
        if self._writer is not None and (rewind or abs(self._writer.fs - fs_hz) > 0.01):
            logger.info(
                "EDF: closing session (%s)",
                "dongle clock restarted" if rewind else f"sample rate changed to {fs_hz} Hz")
            self._close_locked()
            self._session_ts_ms = None

        if self._writer is None:
            if not self._open_locked(fs_hz):
                return False

        self._session_ts_ms = ts_ms
        return True

    def _open_locked(self, fs_hz: float) -> bool:
        start = datetime.now()
        path = self._next_path(start)
        try:
            self._writer = EdfWriter(path, fs_hz, start_wall=start)
        except OSError as exc:
            self._writer = None
            self.last_error = f"cannot open {path}: {exc}"
            logger.error("EDF recording disabled: %s", self.last_error)
            self.enabled = False
            return False

        self._session_start = start
        self._out_of_range.clear()
        # A new session starts with no heart history: inheriting the previous
        # file's EMA would make the first minute of the new one look settled
        # when the strap has not produced a beat yet.
        self._hr_filter.reset()
        self._nn_hrv.reset()
        logger.info("EDF: recording to %s (%.1f Hz)", path, fs_hz)
        return True

    def _next_path(self, start: datetime) -> str:
        stem = f"{self.prefix}_{start.strftime('%Y%m%d_%H%M%S')}"
        path = os.path.join(self.folder, f"{stem}.edf")
        index = 1
        while os.path.exists(path):
            path = os.path.join(self.folder, f"{stem}_{index}.edf")
            index += 1
        return path

    def _close_locked(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            path = writer.finish()
        except Exception:
            logger.exception("EDF: failed to finish the session")
            writer.abort()
            self.last_error = "failed to finish the session"
            return
        if path is None:
            logger.info("EDF: session held no data, discarded")
            return
        self.last_path = path
        self.sessions += 1
        logger.info("EDF: session closed -> %s (%d records)", path, writer.record_count)
        self._enforce_retention()

    def close(self) -> None:
        """Finish the session in progress (shutdown path)."""
        with self._lock:
            self._close_locked()

    # -- retention -------------------------------------------------------

    def _enforce_retention(self) -> None:
        if self.max_files <= 0:
            return
        pattern = re.compile(
            rf"^{re.escape(self.prefix)}_\d{{8}}_\d{{6}}(_\d+)?\.edf$")
        try:
            names = [n for n in os.listdir(self.folder) if pattern.match(n)]
        except OSError as exc:
            logger.warning("EDF: cannot list %s: %s", self.folder, exc)
            return

        names.sort()  # timestamped, so lexical order is chronological
        excess = len(names) - self.max_files
        for name in names[:max(0, excess)]:
            path = os.path.join(self.folder, name)
            try:
                os.unlink(path)
                logger.info("EDF: removed old session %s", name)
            except OSError as exc:
                logger.warning("EDF: cannot remove %s: %s", name, exc)

    # -- status ----------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            writer = self._writer
            return {
                "enabled": self.enabled,
                "folder": self.folder,
                "max_files": self.max_files,
                "recording": writer is not None,
                "records": writer.record_count if writer is not None else 0,
                "current": writer.path if writer is not None else None,
                "started": self._session_start.isoformat() if (
                    writer is not None and self._session_start) else None,
                "sessions": self.sessions,
                "last": self.last_path,
                "error": self.last_error,
            }
