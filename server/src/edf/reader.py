"""
Minimal EDF / EDF+ reader for the sessions `writer.py` records.

A port of `my_heart/android/edf/src/main/kotlin/com/myheart/playback/EdfReader.kt`,
cut down to what playback needs: the ECG, the step-held R-R channel, the 1 Hz
metrics and the TAL annotations. The rules that matter are kept identical, and
they are the non-obvious ones:

* `RR_MS` is a **step** channel. A value equal to the previous one is
  indistinguishable from the hold, so it means "no new beat" — not "the same
  interval measured again". `align_step` is the mirror of the writer's
  `held_rr_ms`, and getting it wrong fabricates beats at every sample.
* Physical values are reconstructed from the digital range by the same
  interpolation the writer inverted. No unit guessing: the writer writes uV,
  so `ECG` comes back in uV.
* A truncated file is refused rather than half-read, but `n_rec == -1` (a
  writer killed before it could patch the count) is accepted, since that is a
  real recording whose header simply never got finalised.

Annotations are parsed only for `GAP`; the beat *positions* for playback come
from `RR_MS`, because that is the sensor's own measurement. Beat annotations
would be a second, hand-inserted opinion.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Label of the step channel the writer emits.
RR_CHANNEL = "RR_MS"

#: Labels recognised as the 1 Hz metrics (see `writer._SLOW_RATE`).
SLOW_LABELS = (
    "SENSOR_HR",
    "HR_EMA",
    "MEAN_RR",
    "HRV_RMSSD30",
    "HRV_RMSSD300",
    "POWER",
    "CADENCE",
    "TRAINER_POWER",
    "TRAINER_CADENCE",
    "SPEED",
    "RESISTANCE",
    "TRAINER_RES",
    "GRADE",
    "CMD_GRADE",
    "CMD_RESISTANCE",
)


@dataclass
class Annotation:
    onset_ms: float
    duration_ms: float
    text: str


@dataclass
class EdfRecording:
    """One decoded session, in the writer's own units."""

    label: str
    fs_hz: float
    #: ECG in uV, one entry per sample at `fs_hz`.
    samples_uv: List[float] = field(default_factory=list)
    #: R-R in ms, step-held at `fs_hz`; 0 where no beat had been reported yet.
    rr_ms: List[float] = field(default_factory=list)
    #: 1 Hz channels, already aligned onto the ECG timeline length.
    metrics: Dict[str, List[float]] = field(default_factory=dict)
    annotations: List[Annotation] = field(default_factory=list)
    #: EDF header start time, ISO-8601, for naming and display.
    start_iso: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        if self.fs_hz <= 0.0:
            return 0.0
        return len(self.samples_uv) * 1000.0 / self.fs_hz

    def rr_marks(self) -> List[Tuple[int, float]]:
        """
        Every beat the sensor reported, as `(sample index, interval ms)`.

        The step channel alone is not enough. A step value only changes when the
        interval changes, so two consecutive beats with an *identical* interval
        collapse into one — and a resting heart produces runs of exactly equal
        milliseconds. On `recording/zrelay_20261108_125551.edf` that lost 205 of
        719 beats (28%). That is why the writer also emits a `BEAT` annotation
        per beat: the annotations carry the beats the step channel cannot.

        So beats come from the annotations when the file has them, and fall back
        to step changes for older recordings written before they existed. The
        interval for an annotation beat is read from the step channel *at* that
        beat's onset, which is the value the writer had holding there.
        """
        if self.annotations:
            beats = self._annotated_marks()
            if beats:
                return beats
        return self._step_marks()

    def _step_marks(self) -> List[Tuple[int, float]]:
        """Beats inferred from step changes: one per distinct interval value."""
        out: List[Tuple[int, float]] = []
        prev = math.nan
        for i, value in enumerate(self.rr_ms):
            if value != value:  # NaN
                continue
            if value != prev:
                if value > 0.0:
                    out.append((i, value))
                prev = value
        return out

    def _annotated_marks(self) -> List[Tuple[int, float]]:
        """
        One mark per `BEAT` annotation, with the interval that beat closed.

        A beat's onset is stamped where the beat *is*, but the step channel only
        stores `RR_SAMPLE_HZ` value changes a second, so the new interval becomes
        visible at the next sub-sample boundary at or after the onset — not at
        the onset itself. So the interval is "the first value after the onset
        that differs from the one in force just before it", which is this beat's
        own. When nothing differs (consecutive identical intervals, the whole
        reason the annotations exist) the standing value is the answer.
        """
        out: List[Tuple[int, float]] = []
        n = len(self.rr_ms)
        if n == 0:
            return out
        #: How far past an onset the interval it wrote may appear. One sub-sample
        #: step would do; a second is slack for a reader on a different grid.
        window = max(1, int(round(self.fs_hz)))
        for ann in self.annotations:
            if ann.text.upper() != "BEAT":
                continue
            index = int(round(ann.onset_ms * self.fs_hz / 1000.0))
            index = min(max(index, 0), n - 1)

            before = self.rr_ms[max(0, index - 1)]
            if before != before or before < 0.0:
                before = 0.0

            value = 0.0
            standing = 0.0
            for k in range(index, min(n, index + window + 1)):
                candidate = self.rr_ms[k]
                if candidate != candidate or candidate <= 0.0:
                    continue
                if not standing:
                    standing = candidate
                if candidate != before:
                    value = candidate
                    break
            if not value:
                value = standing
            out.append((index, value))
        out.sort(key=lambda item: item[0])
        return out

    def gaps(self) -> List[Annotation]:
        return [a for a in self.annotations if a.text == "GAP"]


class EdfFileError(ValueError):
    """Raised when the file cannot be decoded."""


def _ascii_int(raw: bytes, offset: int, length: int) -> int:
    try:
        return int(raw[offset:offset + length].decode("ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise EdfFileError(
            f"bad integer at {offset}: {raw[offset:offset + length]!r}") from exc


def _ascii_float(raw: bytes, offset: int, length: int) -> float:
    try:
        return float(raw[offset:offset + length].decode("ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise EdfFileError(
            f"bad number at {offset}: {raw[offset:offset + length]!r}") from exc


def _field(raw: bytes, offset: int, length: int) -> str:
    return raw[offset:offset + length].decode("ascii", errors="replace").strip()


def read(path: str) -> EdfRecording:
    """Decode an EDF/C file. Raises `EdfFileError` if it is not readable."""
    with open(path, "rb") as fh:
        raw = fh.read()
    return read_bytes(raw)


def read_bytes(raw: bytes) -> EdfRecording:
    if len(raw) < 256:
        raise EdfFileError("file too short to hold an EDF header")

    header_bytes = _ascii_int(raw, 184, 8)
    n_rec = _ascii_int(raw, 236, 8)
    record_s = _ascii_float(raw, 244, 8)
    n_sig = _ascii_int(raw, 252, 4)

    if record_s <= 0.0:
        raise EdfFileError(f"bad record duration {record_s}")
    if n_sig < 1:
        raise EdfFileError(f"bad signal count {n_sig}")
    if header_bytes < 256 + n_sig * 256:
        raise EdfFileError(f"header of {header_bytes} B cannot hold {n_sig} signals")

    labels = [_field(raw, 256 + i * 16, 16) for i in range(n_sig)]
    # `base` is where the per-signal dimension block starts (256 + 16n + 80n);
    # each block below is 8 bytes per signal, and prefiltering (80) and the
    # sample counts that follow it sit past five of them.
    base = 256 + n_sig * (16 + 80)
    dims = [_field(raw, base + i * 8, 8) for i in range(n_sig)]
    p_min = [_ascii_float(raw, base + n_sig * 8 + i * 8, 8) for i in range(n_sig)]
    p_max = [_ascii_float(raw, base + n_sig * 16 + i * 8, 8) for i in range(n_sig)]
    d_min = [_ascii_int(raw, base + n_sig * 24 + i * 8, 8) for i in range(n_sig)]
    d_max = [_ascii_int(raw, base + n_sig * 32 + i * 8, 8) for i in range(n_sig)]
    spr = [_ascii_int(raw, base + n_sig * 120 + i * 8, 8) for i in range(n_sig)]

    # The writer leaves -1 until it patches on close; a killed recorder still
    # leaves a usable file, so the count is recovered from the file length.
    data_start = header_bytes
    bytes_per_rec = sum(spr) * 2
    if bytes_per_rec <= 0:
        raise EdfFileError("data records hold no samples")
    available = (len(raw) - data_start) // bytes_per_rec
    if n_rec < 0:
        n_rec = available
    elif available < n_rec:
        raise EdfFileError(
            f"truncated: header claims {n_rec} records, file holds {available}")
    if n_rec <= 0:
        raise EdfFileError("file holds no data records")

    ecg_ch = next(
        (i for i, label in enumerate(labels)
         if "ECG" in label.upper() or "EKG" in label.upper()), None)
    if ecg_ch is None:
        raise EdfFileError(f"no ECG channel among {labels}")
    rr_ch = next((i for i, label in enumerate(labels)
                  if label.upper() == RR_CHANNEL), None)
    ann_ch = next((i for i, label in enumerate(labels)
                   if "ANNOTATION" in label.upper()), None)

    fs_hz = spr[ecg_ch] / record_s
    if fs_hz <= 0.0:
        raise EdfFileError(f"bad sample rate {fs_hz}")

    slow_ch = {
        i: label for i, label in enumerate(labels)
        if label.upper() in SLOW_LABELS
    }

    n_ecg = n_rec * spr[ecg_ch]
    samples: List[float] = []
    rr: List[float] = []
    slow: Dict[str, List[float]] = {label: [] for label in slow_ch.values()}
    tal = bytearray()

    offset = data_start
    for _ in range(n_rec):
        for ch in range(n_sig):
            count = spr[ch]
            if ch == ecg_ch:
                for k in range(count):
                    dig = struct.unpack_from("<h", raw, offset + k * 2)[0]
                    samples.append(_dig_to_phys(
                        dig, d_min[ch], d_max[ch], p_min[ch], p_max[ch]))
                offset += count * 2
            elif ch == rr_ch:
                for k in range(count):
                    dig = struct.unpack_from("<h", raw, offset + k * 2)[0]
                    rr.append(_dig_to_phys(
                        dig, d_min[ch], d_max[ch], p_min[ch], p_max[ch]))
                offset += count * 2
            elif ch == ann_ch:
                tal.extend(raw[offset:offset + count * 2])
                offset += count * 2
            elif ch in slow_ch:
                for k in range(count):
                    dig = struct.unpack_from("<h", raw, offset + k * 2)[0]
                    slow[slow_ch[ch]].append(_dig_to_phys(
                        dig, d_min[ch], d_max[ch], p_min[ch], p_max[ch]))
                offset += count * 2
            else:
                offset += count * 2

    # The R-R channel is a step channel: carry each value forward at its own
    # rate. When the file has no such channel (an app recording without sensor
    # R-R), the beats are recovered from the annotations instead.
    if rr_ch is not None and len(rr) == n_ecg:
        rr_aligned = rr
    elif rr_ch is not None:
        rr_aligned = align_step(rr, spr[rr_ch] / record_s, n_ecg, fs_hz)
    else:
        rr_aligned = []

    annotations = parse_tals(bytes(tal))
    start_iso = _start_iso(raw)

    return EdfRecording(
        label=labels[ecg_ch],
        fs_hz=fs_hz,
        samples_uv=samples,
        rr_ms=rr_aligned,
        metrics={label: _align_hold(values, n_ecg) for label, values in slow.items()},
        annotations=annotations,
        start_iso=start_iso,
    )


def _start_iso(raw: bytes) -> Optional[str]:
    """The header's start date and time, as ISO-8601, or None if unparseable."""
    date = _field(raw, 168, 8)
    clock = _field(raw, 176, 8).replace(".", ":")
    try:
        day, month, year = date.split(".")
        yy = int(year)
        year_full = 2000 + yy if yy < 85 else 1900 + yy
        return f"{year_full:04d}-{int(month):02d}-{int(day):02d}T{clock}"
    except ValueError:
        return None


def _dig_to_phys(dig: int, d_min: int, d_max: int, p_min: float, p_max: float) -> float:
    if d_max == d_min:
        return p_min
    return p_min + (dig - d_min) * (p_max - p_min) / float(d_max - d_min)


def _align_hold(values: Sequence[float], n: int) -> List[float]:
    """Repeat each 1 Hz value for the samples of its second."""
    if not values:
        return []
    out: List[float] = []
    for value in values:
        out.extend([value] * max(1, n // len(values)))
    if len(out) < n:
        out.extend([values[-1]] * (n - len(out)))
    return out[:n]


def align_step(src: Sequence[float], src_fs: float, n_ecg: int, ecg_fs: float) -> List[float]:
    """
    Sample-and-hold a step channel onto the ECG timeline.

    A value equal to the previous one is a *hold*, not a repeated measurement:
    this is what keeps one beat from becoming a beat per sample.
    """
    if not src:
        return []
    out: List[float] = []
    held = src[0]
    j = 0
    last = len(src) - 1
    for i in range(n_ecg):
        upto = int(i * src_fs / ecg_fs)
        upto = min(max(upto, 0), last)
        while j < upto:
            j += 1
            if src[j] != src[j - 1]:
                held = src[j]
        out.append(held)
    return out


def parse_tals(raw: bytes) -> List[Annotation]:
    """
    Parse EDF+ Time-stamped Annotation Lists.

    One TAL is `+onset[\\x15duration]\\x14[\\x14]text\\x14...\\x00`; the onset with
    no text is a time-keeping marker and is skipped. Anything that does not
    parse ends the scan rather than raising: a file with a damaged tail still
    replays from the start.
    """
    out: List[Annotation] = []
    i = 0
    n = len(raw)
    while i < n:
        while i < n and raw[i] == 0:
            i += 1
        if i >= n:
            break

        start = i
        while i < n and raw[i] not in (0x14, 0x15):
            i += 1
        if i >= n:
            break
        try:
            onset_s = float(raw[start:i].decode("ascii").strip().lstrip("+"))
        except (ValueError, UnicodeDecodeError):
            break

        duration_s = 0.0
        if raw[i] == 0x15:
            i += 1
            dur_start = i
            while i < n and raw[i] != 0x14:
                i += 1
            try:
                duration_s = float(raw[dur_start:i].decode("ascii").strip())
            except (ValueError, UnicodeDecodeError):
                duration_s = 0.0
        if i >= n or raw[i] != 0x14:
            break
        i += 1

        if i < n and raw[i] == 0x14:  # time-keeping marker, no text
            i += 1
            if i < n and raw[i] == 0:
                i += 1
            continue

        while i < n and raw[i] != 0:
            text_start = i
            while i < n and raw[i] != 0x14:
                i += 1
            text = raw[text_start:i].decode("ascii", errors="replace").strip()
            if text:
                out.append(Annotation(onset_s * 1000.0, duration_s * 1000.0, text.upper()))
            if i < n and raw[i] == 0x14:
                i += 1
        if i < n and raw[i] == 0:
            i += 1
    return out
