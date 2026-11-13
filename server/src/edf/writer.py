"""
EDF+ recording of a live session.

Why EDF+
--------
`playback_serial.py` can only replay what was captured as JSON, and
`sample.json` carries no ECG and no R-R at all, so nothing in it exercises the
four heart panels. EDF+ is the format EDFbrowser reads, so one file serves both
purposes: open it to analyse a session by hand, or feed it to `playback_edf.py`
to replay the session through the server exactly as the dongle produced it.

Layout
------
One data-record per second, the same convention as the Android app's
`EdfSessionWriter`, so files from either producer read alike:

  ECG                     at the full sample rate (`fs`)
  everything else         1 sample per record (1 Hz), sample-and-hold

`RR_MS` used to be carried at the full rate as a step channel, on the argument
that one beat is a *change* in the held value and resampling would move the beat
edges. That part is still true — but it does not require spending one int16 per
ECG sample: it cost 512 B/s at 256 Hz for about 1.5 B/s of information, and the
overhead grew with the sample rate rather than with the number of beats. It is
now carried at `RR_SAMPLE_HZ` (5 Hz), which is the point worth thinking about:
a step channel cannot report two beats inside one sample period, so the rate is
a *fidelity* choice, not just a size one. At 1 Hz every second above 60 bpm lost
its earlier beat, and replaying a file reads the beats back out of this channel
— so the loss showed up as wrong RMSSD. At 5 Hz the shortest gap that can hide is
200 ms (300 bpm), and a 582-beat ride round-trips with all 582 intervals and an
identical RMSSD.

Rate is not the whole story, though. A step channel also cannot report a beat
whose interval equals the previous one, at *any* rate — the held value does not
change, so there is nothing to see. Every beat is therefore also written as a
`BEAT` annotation, which is what makes the beat log complete; the two channels
answer different questions and neither replaces the other. The reader still
accepts full-rate files, so old sessions stay readable (`EdfReader.align_step`
mirrors the old rule).

The digital range is the full int16 span on every channel, so a value is a
straight scaling and nothing clips a legitimate sample.

One file per session. The dongle stamps `ts` from its uptime clock, so a reboot
restarts it near zero; the recorder treats that as the end of a session rather
than splicing two clocks into one timeline (see `recorder.py`).
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Full int16 span: every channel is scaled into this, so none can clip.
DIG_MIN = -32768
DIG_MAX = 32767

#: Annotations data-record size, in 2-byte samples (240 bytes/second).
ANN_SPR = 120

#: Data records coalesced before hitting the disk (~1 min at 1 record/s).
FLUSH_RECORDS = 60

#: Offset of the 8-byte "number of data records" header field.
NREC_OFFSET = 236

#: Flatline longer than this is annotated as a gap, so EDFbrowser shows it.
GAP_MIN_MS = 900

#: Rate `RR_MS` is stored at, in samples per second.
#:
#: 5 Hz, because the channel is a *step* channel: a beat is a change in the held
#: value, so two beats landing in the same sample period are indistinguishable
#: from one — the earlier interval is simply gone. At 1 Hz that needs two beats
#: within 1000 ms, which is anything above 60 bpm; a 582-beat ride lost 37% of
#: its intervals. 5 Hz needs two beats within 200 ms (300 bpm), which does not
#: happen: the same ride lost none, and it is also what the Android app stores,
#: so files from either producer read alike.
#:
#: The cost is 10 B/s, against 512 B/s for the full-rate layout this replaced.
RR_SAMPLE_HZ = 5.0

#: Slack when comparing a beat's onset against a sub-sample instant, in ms.
#: `_next_ts_ms` accumulates a sample period per sample, so a beat landing on a
#: record boundary is stored a few ulps late; without slack it would report one
#: bucket later and shift the beat by a sample when the file is read back.
_ONSET_EPSILON_MS = 1e-6

#: Cap on one synthetic fill; a longer hole is a dropout, not a stretch of ECG.
MAX_FILL_MS = 30_000


@dataclass(frozen=True)
class Channel:
    """One EDF signal: its label, unit, physical range and its rate."""

    label: str
    dim: str
    p_min: float
    p_max: float
    #: True for the channels carried at `fs`, False for the slow ones. How slow
    #: is `samples_per_record`; this flag only means "runs at the ECG rate".
    full_rate: bool
    #: Samples this channel stores per 1-second record. 1 for a plain 1 Hz hold.
    #: A channel that needs to catch every event between records needs more —
    #: `RR_MS` is the case in point (see `RR_SAMPLE_HZ`).
    samples_per_record: int = 1

    def to_digital(self, physical: float) -> int:
        """Map a physical value onto the int16 axis, clamped to the range."""
        span = self.p_max - self.p_min
        if span <= 0.0:
            return DIG_MIN
        value = physical
        if value != value:  # NaN has no representation: fall back to the floor
            value = self.p_min
        dig = (value - self.p_min) * (DIG_MAX - DIG_MIN) / span + DIG_MIN
        return max(DIG_MIN, min(DIG_MAX, int(round(dig))))


#: Channels written at the ECG's own rate.
_FULL_RATE: Tuple[Channel, ...] = (
    # uV, not mV: the dongle streams whole microvolts (`{"type":"ecg","uv":..}`)
    # and the reader hands them back unchanged. +-20000 uV is +-20 mV, far more
    # than a prep-less strap produces, and still resolves to 0.6 uV.
    Channel("ECG", "uV", -20_000.0, 20_000.0, True),
)

#: Channels written once per second. Order is the order in the file.
_SLOW_RATE: Tuple[Channel, ...] = (
    # Sensor R-R, one sample per second: the interval in force at the end of the
    # second. It used to be a full-rate step channel, which cost one int16 per
    # ECG sample — 512 B/s at 256 Hz, 40% of the file, for ~1.5 bytes/s of actual
    # information (a 60 s capture held 138 240 samples for 420 beats, 329x
    # redundant). Dropping it to 1 Hz makes the record smaller *and* stops the
    # cost growing when the sample rate goes up.
    #
    # Stored at `RR_SAMPLE_HZ`, not 1 Hz. A 1 Hz hold keeps only the *last* beat
    # of each second, and replaying a session reads the beats back out of this
    # channel — so every collision became a missing interval in the metrics.
    # Measured on a real ride, 1 Hz recovered 366 of 582 beats: 37% of the
    # intervals were lost, which silently corrupts RMSSD (it is a function of
    # successive *differences* between intervals, so dropped beats are not a
    # rounding error, they are wrong numbers). At 5 Hz all 582 came back.
    Channel("RR_MS", "ms", 0.0, 2_000.0, False,
            samples_per_record=int(RR_SAMPLE_HZ)),
    # The sensor's *own* bpm, straight off the `hr` line. The four metrics below
    # are derived from `RR_MS`; this is what the strap itself reported, so the
    # two can be compared instead of taken on trust. Zero on a strap that sends
    # intervals without a bpm (a Movesense exposes R-R as a per-beat resource).
    Channel("SENSOR_HR", "bpm", 0.0, 220.0, False),
    Channel("HR_EMA", "bpm", 0.0, 220.0, False),
    Channel("MEAN_RR", "ms", 0.0, 2_000.0, False),
    Channel("HRV_RMSSD30", "ms", 0.0, 300.0, False),
    Channel("HRV_RMSSD300", "ms", 0.0, 300.0, False),
    # The power meter and the trainer are kept apart rather than fused into one
    # "power" series: which of them a value came from is usually the question
    # being asked when a session is opened in EDFbrowser.
    Channel("POWER", "W", -500.0, 2_500.0, False),
    Channel("CADENCE", "rpm", 0.0, 255.0, False),
    Channel("TRAINER_POWER", "W", -500.0, 2_500.0, False),
    Channel("TRAINER_CADENCE", "rpm", 0.0, 255.0, False),
    Channel("SPEED", "m/h", 0.0, 100_000.0, False),
    # `RESISTANCE` is the setpoint the dongle derived from the commanded grade
    # (see CMD_GRADE); `TRAINER_RES` is what the trainer says it is
    # actually applying, off its Indoor Bike Data. The second is the only
    # evidence of whether the trainer followed, and it lags by seconds.
    Channel("RESISTANCE", "%", 0.0, 100.0, False),
    Channel("TRAINER_RES", "%", 0.0, 100.0, False),
    # What the trainer reported back, and — separately — what Zwift asked for.
    # The gap between the two is the point of recording the pair.
    Channel("GRADE", "%", -30.0, 30.0, False),
    Channel("CMD_GRADE", "%", -30.0, 30.0, False),
    Channel("CMD_RESISTANCE", "%", 0.0, 100.0, False),
)

CHANNELS: Tuple[Channel, ...] = _FULL_RATE + _SLOW_RATE

#: Channel labels the recorder is expected to feed; the rest hold their last value.
SLOW_LABELS: Tuple[str, ...] = tuple(c.label for c in _SLOW_RATE)

#: EDF gives a signal label a 16-character header field and `_put` truncates to
#: it. A longer label would be written short and then never match `SLOW_LABELS`
#: on the way back, leaving a channel that reads as all zeros and looks like a
#: sensor fault. `TRAINER_RESISTANCE` (18) is exactly how that happened once.
MAX_LABEL = 16
_TOO_LONG = [c.label for c in CHANNELS if len(c.label) > MAX_LABEL]
if _TOO_LONG:
    raise ValueError(
        f"EDF signal labels are {MAX_LABEL} bytes; too long: {_TOO_LONG}")



def _put(buf: bytearray, text: str, width: int) -> None:
    """Append one fixed-width ASCII header field, space padded."""
    raw = text.encode("ascii", errors="replace")[:width]
    buf.extend(raw)
    buf.extend(b" " * (width - len(raw)))


def _fmt8(value: float) -> str:
    """A physical bound in EDF's 8-character field (`%.4g`, as the app writes it)."""
    return f"{value:.4g}"[:8]


class EdfWriter:
    """
    Streams one session into an EDF+C file.

    Samples arrive in batches on the dongle's clock; the writer lays them out on
    a fixed `fs` grid starting at the first sample and closes a record every
    second. A hole in delivery is filled with flatline so the timeline stays
    glued to real time, which is what keeps playback pacing honest.
    """

    def __init__(self, path: str, fs: float, start_wall: Optional[datetime] = None) -> None:
        if fs <= 1.0:
            raise ValueError(f"implausible sample rate {fs}")
        self.path = path
        self.fs = float(fs)
        self.ms_per_sample = 1000.0 / self.fs
        self.spr = max(1, int(round(self.fs)))  # samples per record, per full-rate channel

        self.n_sig = len(CHANNELS) + 1  # + the annotations channel
        self.header_bytes = 256 + self.n_sig * 256

        self._start = start_wall or datetime.now()
        self._origin_ms: Optional[int] = None
        self._next_ts_ms: Optional[float] = None
        self._sample_in_rec = 0
        self._n_rec = 0
        self._pending_records = 0
        self._buf = bytearray()

        # Full-rate channel values for the record being filled.
        self._ecg_vals: List[int] = []
        self._held_rr_ms = 0.0
        #: `(onset_ms, rr_ms)` per beat, for sub-sampling `RR_MS` within a record.
        #: Only the current and previous record are ever consulted, so it is
        #: trimmed as it grows rather than kept for the session.
        self._rr_history: List[Tuple[float, float]] = []

        # 1 Hz channels: the last value seen, repeated for each record.
        self._slow: Dict[str, float] = {label: 0.0 for label in SLOW_LABELS}
        self._slow_dirty = False

        self._anns: List[Tuple[float, float, str]] = []
        self._closed = False
        #: Set once if a TAL block ever overflows, so the warning is logged once.
        self._tal_overflowed = 0

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "wb")
        self._write_header()

    # -- ingest ----------------------------------------------------------

    def push_ecg(self, samples_uv: Sequence[float], t0_ms: int) -> None:
        """Append one batch of ECG samples whose first sample sits at `t0_ms`."""
        if self._closed:
            return
        if self._origin_ms is None:
            self._origin_ms = int(t0_ms)
            self._next_ts_ms = float(t0_ms)

        # A hole means the strap stalled or the link dropped. Fill it so the
        # file's timeline keeps matching wall time; without this, playback would
        # run fast for the rest of the session.
        gap_ms = t0_ms - (self._next_ts_ms or float(t0_ms))
        if gap_ms >= self.ms_per_sample * 0.5:
            count = int(round(gap_ms / self.ms_per_sample))
            count = min(count, int(MAX_FILL_MS / self.ms_per_sample))
            if gap_ms >= GAP_MIN_MS:
                self._anns.append(((self._next_ts_ms or 0.0) - self._origin_ms, gap_ms, "GAP"))
            for _ in range(max(0, count)):
                self._append_sample(0.0)

        for uv in samples_uv:
            self._append_sample(float(uv))

    def on_beat(self, rr_ms: float) -> None:
        """
        The sensor reported an interval. It is recorded two ways, on purpose.

        `RR_MS` is the plottable channel: sub-sampled at `RR_SAMPLE_HZ`, so it
        draws as a staircase in EDFbrowser and answers "what interval was in
        force at this instant". Its rate is a fidelity knob for *collisions* —
        two beats inside one sample period collapse to one.

        `BEAT` annotations are the beat *log*: one event per beat. They exist
        because a step channel cannot, at any rate, report a beat whose interval
        equals the previous one — the held value does not change, so there is
        nothing to detect. On a capture with 26 such pairs, a step channel
        recovered 158 of 167 beats at 1, 5, 10 and 50 Hz alike. Only an event
        stream closes that, so the annotations are not redundant with the
        channel and removing them silently truncates the beat history.

        The annotation carries only the onset; the interval stays in `RR_MS`,
        so the two do not disagree about what the strap measured.
        """
        if rr_ms <= 0 or self._origin_ms is None or self._next_ts_ms is None:
            return
        self._held_rr_ms = float(rr_ms)
        # `RR_MS` is sub-sampled across each record, so it needs to know *when*
        # an interval took effect, not only what the latest one was. Without this
        # every sub-sample in a record would report the same end-of-record value.
        #
        onset_ms = max(0.0, self._next_ts_ms - self._origin_ms)
        self._rr_history.append((onset_ms, self._held_rr_ms))
        # Duration 0 is carried through as the TAL's own "no duration" form
        # rather than `add_annotation`'s default span: a beat is an instant, and
        # a 20 ms block on every one of them makes the annotations pane read like
        # the QRS complexes are 20 ms wide.
        self._anns.append((onset_ms, 0.0, "BEAT"))

    def set_channel(self, label: str, value: float) -> None:
        """Set a 1 Hz channel's held value. Unknown labels are ignored."""
        if label in self._slow:
            new = float(value)
            if new != self._slow[label]:
                self._slow[label] = new
                self._slow_dirty = True

    def add_annotation(self, onset_ms: float, text: str, duration_ms: float = 100.0) -> None:
        if not text or self._closed:
            return
        self._anns.append((max(0.0, onset_ms), max(20.0, duration_ms), text.upper()[:16]))

    # -- records ---------------------------------------------------------

    def _append_sample(self, uv: float) -> None:
        self._ecg_vals.append(CHANNELS[0].to_digital(uv))
        self._sample_in_rec += 1
        if self._next_ts_ms is not None:
            self._next_ts_ms += self.ms_per_sample
        if self._sample_in_rec >= self.spr:
            self._flush_record()

    def _rr_record_values(self, buckets: int) -> List[float]:
        """
        The R-R value held at each of `buckets` instants across the record.

        Sub-sampling means each sample answers "what interval was in force at
        this moment", so a beat that arrived between two of them still changes
        the value the next one reads. Repeating the end-of-record value instead
        would store the same number `buckets` times and catch nothing.

        The instants are the record's own sub-sample times, matching the grid
        `align_step` reads them back on. A moment before the first beat of the
        session has no interval yet, so it reports 0, as the held value does.
        """
        if buckets <= 1:
            return [self._held_rr_ms]
        # The record spans [n_rec, n_rec+1) seconds and its samples sit at
        # n_rec + k/buckets for k in 0..buckets-1 — the same phase `align_step`
        # reads them back on, so both sides agree on which moment a value means.
        base = float(self._n_rec) * 1000.0
        step = 1000.0 / float(buckets)
        return [self._rr_at(base + k * step) for k in range(buckets)]

    def _rr_at(self, at_ms: float) -> float:
        """
        The R-R interval in force at `at_ms`, in session-relative milliseconds.

        A beat takes effect at its own onset, so the interval in force at a
        moment is the most recent beat at or before it. Before the session's
        first beat there is nothing to report, which is 0 — the same "no
        interval yet" the held value starts as.
        """
        value = 0.0
        for onset_ms, rr_ms in self._rr_history:
            # `_next_ts_ms` accumulates one sample period at a time, so a beat
            # that lands exactly on a bucket boundary arrives as 1000.0000000001
            # and would compare as *after* it — the interval then reports one
            # bucket late and the reader puts the beat on the wrong sample. A
            # tiny tolerance pins the boundary cases to the bucket they belong
            # to; nothing real resolves at 1 us, so this cannot merge two beats.
            if onset_ms > at_ms + _ONSET_EPSILON_MS:
                break
            value = rr_ms
        return value

    def _flush_record(self, pad: bool = False) -> None:
        if self._sample_in_rec == 0:
            return
        if pad and self._sample_in_rec < self.spr:
            # Repeat the last sample so the final record is full width; EDF has
            # no notion of a short record.
            n = self.spr - self._sample_in_rec
            self._ecg_vals.extend([self._ecg_vals[-1]] * n)
            self._sample_in_rec = self.spr

        for value in self._ecg_vals:
            self._buf.extend(struct.pack("<h", value))
        # The slow channels, in file order (_SLOW_RATE order). Most contribute
        # one held value; `RR_MS` contributes `RR_SAMPLE_HZ` of them, sampled
        # across the record so a beat cannot hide between two of them.
        for chan in _SLOW_RATE:
            if chan.samples_per_record > 1:
                for value in self._rr_record_values(chan.samples_per_record):
                    self._buf.extend(struct.pack("<h", chan.to_digital(value)))
            else:
                self._buf.extend(
                    struct.pack("<h", chan.to_digital(self._slow[chan.label])))
        self._buf.extend(self._build_tal(self._n_rec))

        self._n_rec += 1
        self._pending_records += 1
        self._slow_dirty = False
        self._ecg_vals.clear()
        self._sample_in_rec = 0

        if self._pending_records >= FLUSH_RECORDS:
            self._flush_disk()

    def _build_tal(self, record_index: int) -> bytes:
        """
        One annotations data-record: the time-keeping TAL, then any events.

        Format is EDF+'s TAL: `+onset[\\x15duration]\\x14text\\x14\\x00`, with the
        bare `+record` marker keeping empty records time-stamped.
        """
        out = bytearray()
        out.extend(f"+{record_index}".encode("ascii"))
        out.extend(b"\x14\x14\x00")

        rec_end_ms = (record_index + 1) * 1000.0
        keep: List[Tuple[float, float, str]] = []
        for onset, duration, text in self._anns:
            if onset < rec_end_ms:
                out.extend(f"+{onset / 1000.0:.4f}".encode("ascii"))
                # TAL allows the duration to be omitted entirely; writing a
                # literal 0 instead would claim a zero-width span, which some
                # readers display as a 0 ms annotation. A beat has no duration,
                # so leave the field out rather than lie about it.
                if duration > 0.0:
                    out.extend(b"\x15")
                    out.extend(f"{duration / 1000.0:.4f}".encode("ascii"))
                out.extend(b"\x14")
                out.extend(text.encode("ascii", errors="replace"))
                out.extend(b"\x14\x00")
            else:
                keep.append((onset, duration, text))
        self._anns = keep

        block = bytes(out[: ANN_SPR * 2])
        if len(out) > ANN_SPR * 2:
            # The block is a fixed width, so anything past it is lost. With one
            # `BEAT` per beat this needs ~12 beats inside a single second (~720
            # bpm) before it can bite, so it should not fire — but a dropped
            # beat is a silently incomplete rhythm, so say so once per session
            # rather than let events disappear with nothing in the log.
            if self._tal_overflowed == 0:
                self._tal_overflowed = 1
                logger.warning(
                    "EDF: annotations overflowed the %d-byte block; some events "
                    "in this record were dropped", ANN_SPR * 2)
        return block.ljust(ANN_SPR * 2, b"\x00")

    def _flush_disk(self) -> None:
        if self._pending_records <= 0 and not self._buf:
            return
        if self._buf:
            self._fh.write(bytes(self._buf))
            self._buf.clear()
        self._pending_records = 0
        self._fh.flush()

    # -- header ----------------------------------------------------------

    def _write_header(self) -> None:
        h = bytearray()
        _put(h, "0", 8)                       # version
        _put(h, "X X X X", 80)                # patient identification
        start_label = self._start.strftime("%d-%b-%Y").upper()
        _put(h, f"Startdate {start_label} X z-relay-server EDF", 80)
        _put(h, self._start.strftime("%d.%m.%y"), 8)
        _put(h, self._start.strftime("%H.%M.%S"), 8)
        _put(h, f"{self.header_bytes}", 8)
        _put(h, "EDF+C", 44)
        _put(h, "-1", 8)                      # number of records, patched on close
        _put(h, "1", 8)                       # seconds per data record
        _put(h, f"{self.n_sig}", 4)

        for chan in CHANNELS:
            _put(h, chan.label, 16)
        _put(h, "EDF Annotations", 16)

        for _ in CHANNELS:                    # transducers
            _put(h, "", 80)
        _put(h, "", 80)

        for chan in CHANNELS:
            _put(h, chan.dim, 8)
        _put(h, "", 8)

        for chan in CHANNELS:
            _put(h, _fmt8(chan.p_min), 8)
        _put(h, "-1", 8)

        for chan in CHANNELS:
            _put(h, _fmt8(chan.p_max), 8)
        _put(h, "1", 8)

        for _ in CHANNELS:
            _put(h, f"{DIG_MIN}", 8)
        _put(h, f"{DIG_MIN}", 8)

        for _ in CHANNELS:
            _put(h, f"{DIG_MAX}", 8)
        _put(h, f"{DIG_MAX}", 8)

        for _ in CHANNELS:                    # prefiltering
            _put(h, "", 80)
        _put(h, "", 80)

        for chan in CHANNELS:
            width = self.spr if chan.full_rate else chan.samples_per_record
            _put(h, f"{width}", 8)
        _put(h, f"{ANN_SPR}", 8)

        for _ in CHANNELS:                    # reserved
            _put(h, "", 32)
        _put(h, "", 32)

        assert len(h) == self.header_bytes, (len(h), self.header_bytes)
        self._fh.write(h)
        # Flush immediately: until the first data record lands, the header is the
        # whole file, and a process killed in between would leave zero bytes
        # behind — not even identifiable as a truncated EDF.
        self._fh.flush()

    def _patch_n_rec(self) -> None:
        self._fh.seek(NREC_OFFSET)
        _buf = bytearray()
        _put(_buf, f"{self._n_rec}", 8)
        self._fh.write(bytes(_buf))
        self._fh.seek(0, os.SEEK_END)

    # -- closing ---------------------------------------------------------

    def finish(self) -> Optional[str]:
        """Pad the last record, patch the record count, close. Returns the path."""
        if self._closed:
            return self.path
        try:
            if self._sample_in_rec > 0:
                self._flush_record(pad=True)
            # An annotation whose record has already been written has nowhere to
            # go: EDF attaches annotations to a record, never to a time. Give it
            # one more second of flatline rather than dropping it, so a GAP that
            # ends a session is still in the file. A 1 Hz value that changed
            # after the last record was flushed (a final trainer command) needs
            # the same treatment, or the session's last state is lost.
            if self._anns or self._slow_dirty:
                self._write_tail_record()
            self._flush_disk()
            if self._n_rec <= 0:
                self._closed = True
                self.abort()
                return None
            self._patch_n_rec()
            self._fh.flush()
            os.fsync(self._fh.fileno())
        finally:
            self._closed = True
            try:
                self._fh.close()
            except OSError:
                pass
        return self.path

    def _write_tail_record(self) -> None:
        """
        One full-width record of held values, carrying the pending annotations.

        The buffer holds *digital* int16 values, so the held value has to be
        carried over as-is; writing a literal 0 here decodes as the channel's
        midpoint (0 uV), which would put a flat line at the middle of the axis.
        """
        ecg_last = self._ecg_vals[-1] if self._ecg_vals else CHANNELS[0].to_digital(0.0)
        self._ecg_vals = [ecg_last] * self.spr
        self._sample_in_rec = self.spr
        self._flush_record()

    def abort(self) -> None:
        """Close and delete, for a session that produced nothing usable."""
        self._closed = True
        try:
            self._fh.close()
        except (OSError, ValueError):
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass

    @property
    def record_count(self) -> int:
        return self._n_rec
