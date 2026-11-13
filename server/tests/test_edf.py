"""
Tests for EDF recording and playback.

These check the two properties that are easy to break and hard to notice: a
round trip must return the same values at the same times, and `RR_MS` must stay
a *step* channel. If either drifts, playback invents beats and the dashboard
shows an arrhythmia that was never in the recording.
"""
import math
import os

import pytest

from src.edf import EdfWriter, read
from src.edf.writer import RR_SAMPLE_HZ
from src.edf.reader import (
    EdfFileError,
    align_step,
    parse_tals,
    read_bytes,
)
from src.edf.recorder import RESTART_THRESHOLD_MS, EdfRecorder
from src.edf.writer import ANN_SPR, CHANNELS, DIG_MAX, DIG_MIN, NREC_OFFSET

FS = 130.0


def _write(path, seconds=2, fs=FS, fill=0.0):
    writer = EdfWriter(str(path), fs)
    for sec in range(seconds):
        writer.push_ecg([fill] * int(fs), int(sec * 1000))
    return writer


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_ecg_values_and_rate(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    signal = [int(300 * math.sin(2 * math.pi * 1.3 * k / FS)) for k in range(int(FS) * 3)]
    writer.push_ecg(signal[:130], 0)
    writer.push_ecg(signal[130:260], 1000)
    writer.push_ecg(signal[260:], 2000)
    writer.finish()

    rec = read(str(path))
    assert rec.fs_hz == pytest.approx(FS)
    assert len(rec.samples_uv) == len(signal)
    # The digital axis is int16 over +-20000 uV, so ~0.6 uV per step.
    for got, want in zip(rec.samples_uv, signal):
        assert got == pytest.approx(want, abs=1.0)


@pytest.mark.parametrize("fs", [125.0, 130.0, 256.0, 512.0])
def test_round_trip_holds_at_every_dongle_rate(tmp_path, fs):
    """
    The Movesense dongle can be built to subscribe at any of several rates, and
    256 Hz is the current one, so the writer's record geometry has to follow `fs`
    rather than assume the 130 Hz it was first written for.

    One record is one second, so samples-per-record *is* the rate: at 256 Hz a
    record holds 256 ECG samples and twice the bytes of a 130 Hz file. Getting
    that wrong does not raise — it silently decodes as time-shifted garbage.
    """
    path = tmp_path / f"session_{int(fs)}.edf"
    writer = EdfWriter(str(path), fs)
    seconds = 3
    # A ramp, so a record boundary can be detected from the values alone.
    signal = [int(600 * math.sin(2 * math.pi * 1.1 * k / fs)) for k in range(int(fs) * seconds)]
    step = int(fs)
    for start in range(0, len(signal), step):
        writer.push_ecg(signal[start:start + step], int(start * 1000 / fs))
    writer.finish()

    rec = read(str(path))
    assert rec.fs_hz == pytest.approx(fs)
    assert len(rec.samples_uv) == len(signal)
    assert rec.duration_ms == pytest.approx(seconds * 1000.0, abs=1.0)
    for got, want in zip(rec.samples_uv, signal):
        assert got == pytest.approx(want, abs=1.0)
    # The ECG channel's per-record width is the rate, and every record is full.
    assert writer.spr == int(round(fs))
    assert writer.record_count == seconds


def test_round_trip_preserves_the_one_hertz_channels(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    # Values must be set before the ECG that closes the record: a 1 Hz channel
    # holds its value for the second it was written in, and a record already
    # flushed cannot be edited afterwards.
    writer.set_channel("POWER", 210.0)
    writer.set_channel("CADENCE", 88.0)
    writer.set_channel("CMD_RESISTANCE", 42.0)
    writer.push_ecg([0] * 130, 0)
    writer.set_channel("POWER", 225.0)
    writer.push_ecg([0] * 130, 1000)
    writer.set_channel("POWER", 240.0)
    writer.push_ecg([0] * 130, 2000)
    writer.finish()

    rec = read(str(path))
    assert rec.metrics["CADENCE"][0] == pytest.approx(88.0, abs=0.5)
    assert rec.metrics["CMD_RESISTANCE"][0] == pytest.approx(42.0, abs=0.5)
    # POWER changed once per second; the values must appear in order.
    powers = rec.metrics["POWER"]
    assert powers[0] == pytest.approx(210.0, abs=0.5)
    assert powers[130] == pytest.approx(225.0, abs=0.5)
    assert powers[260] == pytest.approx(240.0, abs=0.5)


def test_header_records_the_signal_count_and_record_size(tmp_path):
    path = tmp_path / "session.edf"
    _write(path, seconds=2).finish()
    raw = path.read_bytes()

    n_sig = int(raw[252:256].decode("ascii").strip())
    assert n_sig == len(CHANNELS) + 1  # + annotations
    assert int(raw[236:244].decode("ascii").strip()) == 2  # one record per second


def test_finish_patches_the_record_count(tmp_path):
    path = tmp_path / "session.edf"
    writer = _write(path, seconds=3)
    assert path.read_bytes()[NREC_OFFSET:NREC_OFFSET + 8].strip() == b"-1"
    writer.finish()
    assert path.read_bytes()[NREC_OFFSET:NREC_OFFSET + 8].strip() == b"3"


def test_a_writer_killed_before_patching_still_reads(tmp_path):
    path = tmp_path / "session.edf"
    writer = _write(path, seconds=3)
    # Simulate a SIGKILL: flush the data but never patch the count.
    writer._flush_disk()
    raw = path.read_bytes()
    assert raw[NREC_OFFSET:NREC_OFFSET + 8].strip() == b"-1"

    rec = read_bytes(raw)
    assert len(rec.samples_uv) == 3 * 130, "the record count is recovered from the file length"


def test_an_empty_session_is_discarded(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    assert writer.finish() is None
    assert not path.exists(), "a session with no data must not leave a file behind"


# ---------------------------------------------------------------------------
# RR_MS is a step channel
# ---------------------------------------------------------------------------


def test_rr_marks_reports_one_beat_per_value_change(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.on_beat(800.0)
    writer.push_ecg([0] * 130, 1000)
    writer.on_beat(810.0)
    writer.push_ecg([0] * 130, 2000)
    writer.finish()

    rec = read(str(path))
    marks = rec.rr_marks()
    assert [round(rr) for _, rr in marks] == [800, 810]
    # The change lands on the sample after the beat was reported, not per sample.
    assert marks[0][0] == 130


def test_rr_marks_ignores_the_zero_prefix_before_the_first_beat(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)  # no beat reported yet
    writer.on_beat(750.0)
    writer.push_ecg([0] * 130, 1000)
    writer.finish()

    marks = read(str(path)).rr_marks()
    assert len(marks) == 1
    assert marks[0][1] == pytest.approx(750.0, abs=1.0)


def test_align_step_holds_a_value_and_does_not_repeat_it_as_a_beat():
    # 1 Hz step channel: 800 then 800 then 900.
    src = [800.0, 800.0, 900.0]
    out = align_step(src, 1.0, 6, 2.0)
    assert out == [800.0, 800.0, 800.0, 800.0, 900.0, 900.0]


def test_align_step_on_the_same_rate_is_a_passthrough():
    src = [800.0, 810.0, 900.0]
    assert align_step(src, FS, 3, FS) == src


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def test_annotation_round_trips_with_onset_and_duration(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.add_annotation(500.0, "GAP", 1000.0)
    writer.push_ecg([0] * 130, 1000)
    writer.finish()

    rec = read(str(path))
    assert len(rec.annotations) == 1
    ann = rec.annotations[0]
    assert ann.text == "GAP"
    assert ann.onset_ms == pytest.approx(500.0, abs=1.0)
    assert ann.duration_ms == pytest.approx(1000.0, abs=1.0)


def test_annotation_added_after_its_record_is_still_written(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.add_annotation(100.0, "LATE", 250.0)
    writer.push_ecg([0] * 130, 1000)  # flushes record 0 first
    writer.finish()

    rec = read(str(path))
    assert [a.text for a in rec.annotations] == ["LATE"]


def test_parse_tals_skips_the_time_keeping_marker():
    # "+12\x14\x14\x00" is a bare marker; the second TAL carries text.
    raw = b"+12\x14\x14\x00+13.5\x15\x00.2\x14GAP\x14\x00"
    anns = parse_tals(raw)
    assert [(a.text, round(a.onset_ms)) for a in anns] == [("GAP", 13500)]


def test_parse_tals_stops_on_a_damaged_tal_instead_of_raising():
    assert parse_tals(b"+12\x14\x14\x00+garbage") == []
    assert parse_tals(b"") == []


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def test_a_hole_shorter_than_the_gap_threshold_is_not_annotated(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([500] * 130, 0)
    writer.push_ecg([500] * 130, 1500)  # 500 ms hole, under GAP_MIN_MS
    writer.finish()

    rec = read(str(path))
    assert len(rec.samples_uv) > 2 * 130, "the hole is filled to keep the timeline honest"
    assert rec.gaps() == [], "half a second is not worth an annotation"


def test_a_long_hole_is_filled_and_annotated(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([500] * 130, 0)
    writer.push_ecg([500] * 130, 3000)  # 2 s hole
    writer.finish()

    rec = read(str(path))
    assert len(rec.samples_uv) == 4 * 130
    assert len(rec.gaps()) == 1
    assert rec.gaps()[0].onset_ms == pytest.approx(1000.0, abs=2.0)


def test_the_first_packet_does_not_produce_a_gap(tmp_path):
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([100] * 130, 50_000)  # first sample far from zero
    writer.finish()

    rec = read(str(path))
    assert rec.gaps() == []
    assert len(rec.samples_uv) == 130


# ---------------------------------------------------------------------------
# Recorder sessions and retention
# ---------------------------------------------------------------------------


def _ecg_message(ts_ms, fs=FS, uv=0):
    return {"type": "ecg", "ts": ts_ms, "fs": fs, "uv": [uv] * int(fs)}


def test_recorder_writes_the_heart_metrics_the_dashboard_shows(tmp_path):
    """HR_EMA and the RR statistics are derived from rr, not sent by the dongle."""
    from src.heart.metrics import HrFilter, NnHrv
    from src.heart.state import HeartState

    # A steady 72 bpm with a little jitter, so the EMA is not the sole witness.
    rrs = [830, 840, 825, 835, 845, 820, 838, 842, 828, 836,
           833, 841, 827, 839, 834, 843, 826, 837, 832, 840,
           835, 829, 844, 831, 838, 836, 830, 842, 833, 839]

    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "hr", "ts": 0, "rr": rrs})
    rec.on_message(_ecg_message(1000))
    rec.on_message(_ecg_message(2000))
    rec.close()

    out = read(str(tmp_path / os.listdir(tmp_path)[0]))

    # What the page would be showing after the same intervals.
    hr, nn = HrFilter(), NnHrv()
    for rr in rrs:
        hr.push(rr)
        nn.on_rr_ms(rr)
    at = out.metrics
    assert at["HR_EMA"][130] == pytest.approx(hr.bpm(), abs=0.05)
    assert at["MEAN_RR"][130] == pytest.approx(nn.mean_rr30_ms(), abs=0.5)
    assert at["HRV_RMSSD30"][130] == pytest.approx(nn.rmssd30_ms(), abs=0.5)
    assert at["HRV_RMSSD300"][130] == pytest.approx(nn.rmssd300_ms(), abs=0.5)
    assert at["HR_EMA"][130] > 60.0, "a real rate, not the 0 default"


def test_the_recorder_keeps_the_sensor_bpm_separate_from_the_derived_one(tmp_path):
    """A strap's bpm is not always 60000/rr; the file must show both, unfused."""
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    # 72 bpm claimed, while the interval implies 50 - a strap disagreeing with
    # itself, which is exactly the fault the pair of channels exists to show.
    rec.on_message({"type": "hr", "ts": 0, "bpm": 72, "rr": [1200, 1200, 1200]})
    rec.on_message(_ecg_message(1000))
    rec.close()

    out = read(str(tmp_path / os.listdir(tmp_path)[0]))
    assert out.metrics["SENSOR_HR"][130] == pytest.approx(72.0, abs=0.05)
    assert out.metrics["HR_EMA"][130] == pytest.approx(50.0, abs=1.0)


def test_recorder_records_what_the_trainer_reports_apart_from_the_setpoint(tmp_path):
    """RESISTANCE is the derived setpoint; TRAINER_RES is the report."""
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "sim", "ts": 0, "grade": 300, "resistance": 21})
    rec.on_message({"type": "ftms", "ts": 0, "power": 70, "speed": 1384, "resistance": 13})
    rec.on_message(_ecg_message(1000))
    rec.close()

    out = read(str(tmp_path / os.listdir(tmp_path)[0]))
    assert out.metrics["RESISTANCE"][130] == pytest.approx(21.0, abs=0.5), "the setpoint"
    assert out.metrics["TRAINER_RES"][130] == pytest.approx(13.0, abs=0.5), "the report"


def test_playback_replays_the_sensor_bpm_and_trainer_resistance(tmp_path):
    """Both new channels have to come back out, or recording them is pointless."""
    from playback_edf import SessionEmitter

    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "hr", "ts": 0, "bpm": 72, "rr": [1200]})
    rec.on_message({"type": "ftms", "ts": 0, "power": 70, "speed": 1384, "resistance": 13})
    rec.on_message(_ecg_message(1000))
    rec.close()

    out = read(str(tmp_path / os.listdir(tmp_path)[0]))
    events = SessionEmitter(out).events

    hrs = [p for _, p in events if p.get("type") == "hr"]
    assert hrs and hrs[0]["bpm"] == 72, "the strap's figure, not 60000/rr = 50"

    ftms = [p for _, p in events if p.get("type") == "ftms"]
    # The opening ECG packet already filled record 0, so a 1 Hz value that
    # arrives with it is held from record 1 onward - the same one-second shift
    # the GRADE test documents.
    assert ftms and ftms[-1]["resistance"] == 13
    assert ftms[-1]["power"] == 70 and ftms[-1]["speed"] == 1384
    """The file and the page must not disagree: same filters, same numbers."""
    from src.heart.state import HeartState

    rrs = [900, 910, 895, 905, 915, 890, 908, 912, 898, 906,
           902, 911, 897, 909, 904, 913, 896, 907, 900, 910]

    state = HeartState(fs_hz=FS)
    for rr in rrs:
        state.on_message({"type": "hr", "ts": 0, "rr": [rr]})

    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "hr", "ts": 0, "rr": rrs})
    rec.on_message(_ecg_message(1000))
    rec.close()
    out = read(str(tmp_path / os.listdir(tmp_path)[0]))

    status = state.status()
    assert out.metrics["HR_EMA"][130] == pytest.approx(status["hr_bpm"], abs=0.05)
    assert out.metrics["HRV_RMSSD30"][130] == pytest.approx(status["rmssd30_ms"], abs=0.5)
    assert out.metrics["HRV_RMSSD300"][130] == pytest.approx(status["rmssd300_ms"], abs=0.5)


def test_recorder_rolls_over_when_the_dongle_clock_restarts(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay", max_files=10)
    rec.on_message(_ecg_message(60_000))
    rec.on_message(_ecg_message(61_000))
    # A USB reset restarts the dongle's uptime clock.
    rec.on_message(_ecg_message(1_000))
    rec.on_message(_ecg_message(2_000))
    rec.close()

    files = sorted(os.listdir(tmp_path))
    assert len(files) == 2, "a clock restart must not be spliced into one file"


def test_recorder_keeps_one_file_for_normal_jitter(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay", max_files=10)
    rec.on_message(_ecg_message(10_000))
    rec.on_message(_ecg_message(11_000))
    rec.on_message(_ecg_message(10_800))  # late point, within the threshold
    rec.close()

    assert len(os.listdir(tmp_path)) == 1
    assert RESTART_THRESHOLD_MS >= 200, "the jitter above must stay below the threshold"


def test_recorder_retention_keeps_the_newest_files(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay", max_files=2)
    # Names are timestamped, so lexical order is chronological.
    for stamp in ("20260101_000001", "20260101_000002", "20260101_000003"):
        (tmp_path / f"zrelay_{stamp}.edf").write_bytes(b"x")
    rec._enforce_retention()

    left = sorted(os.listdir(tmp_path))
    assert left == ["zrelay_20260101_000002.edf", "zrelay_20260101_000003.edf"], \
        "the two newest are kept, the oldest removed"


def test_retention_never_deletes_a_foreign_file(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay", max_files=1)
    (tmp_path / "zrelay_20260101_000001.edf").write_bytes(b"x")
    (tmp_path / "hand_copied_recording.edf").write_bytes(b"important")
    (tmp_path / "notes.txt").write_text("do not delete")
    rec._enforce_retention()

    assert (tmp_path / "hand_copied_recording.edf").exists()
    assert (tmp_path / "notes.txt").exists()


def test_recorder_disabled_records_nothing(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay", enabled=False)
    rec.on_message(_ecg_message(1000))
    rec.close()
    assert os.listdir(tmp_path) == []


def test_recorder_drops_impossible_ecg_samples(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    # 50 000 uV is far outside the channel's +-20000; it would clip into a
    # plausible-looking 355 uV on the way back, so the packet is dropped.
    rec.on_message({"type": "ecg", "ts": 0, "fs": FS, "uv": [50_000] * 130})
    rec.on_message({"type": "ecg", "ts": 1000, "fs": FS, "uv": [100] * 130})
    rec.close()

    files = os.listdir(tmp_path)
    assert len(files) == 1
    rec2 = read(str(tmp_path / files[0]))
    assert max(abs(v) for v in rec2.samples_uv) < 1000, "the bad packet never reached the file"


def test_recorder_records_the_trainer_command_separately_from_the_report(tmp_path):
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    # Everything for a given second has to arrive before that second's record
    # is flushed by the next ECG packet.
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "sim", "ts": 0, "grade": 150, "resistance": 30})

    class Cmd:
        grade = 250       # 0.01 %
        target_resistance = 42

    rec.on_control(Cmd())
    rec.on_message(_ecg_message(1000))
    rec.on_message(_ecg_message(2000))
    rec.close()

    name = os.listdir(tmp_path)[0]
    out = read(str(tmp_path / name))
    # Record 0 was already flushed when the `sim` line and the command arrived,
    # so they land in record 1 — a 1 Hz channel holds its value forward.
    # The wire carries 0.01 %: 150 is a 1.5 % climb and must come back as 1.5.
    assert out.metrics["GRADE"][130] == pytest.approx(1.5, abs=0.05)
    assert out.metrics["CMD_GRADE"][130] == pytest.approx(2.5, abs=0.05)
    assert out.metrics["RESISTANCE"][130] == pytest.approx(30.0, abs=0.5)
    assert out.metrics["CMD_RESISTANCE"][130] == pytest.approx(42.0, abs=0.5)
    assert out.metrics["GRADE"][0] == pytest.approx(0.0, abs=0.05), "nothing was set yet"


def test_a_3_percent_climb_is_not_recorded_as_30(tmp_path):
    """The dongle prints grade in 0.01 %; the EDF channel is in %, so it is /100."""
    rec = EdfRecorder(folder=str(tmp_path), prefix="zrelay")
    rec.on_message(_ecg_message(0))
    rec.on_message({"type": "sim", "ts": 0, "grade": 300, "resistance": 21})
    rec.on_message(_ecg_message(1000))
    rec.on_message(_ecg_message(2000))
    rec.close()

    out = read(str(tmp_path / os.listdir(tmp_path)[0]))
    assert out.metrics["GRADE"][130] == pytest.approx(3.0, abs=0.05)
    # Resistance is already unitless 0..100 and must not be rescaled with it.
    assert out.metrics["RESISTANCE"][130] == pytest.approx(21.0, abs=0.5)


# ---------------------------------------------------------------------------
# Damage and edge cases
# ---------------------------------------------------------------------------


def test_a_truncated_file_is_refused():
    with pytest.raises(EdfFileError):
        read_bytes(b"")
    with pytest.raises(EdfFileError):
        read_bytes(b"0" * 300)


# ---------------------------------------------------------------------------
# Replay timeline
# ---------------------------------------------------------------------------


def test_replay_events_stay_inside_the_recording(tmp_path):
    """
    The event list must span the recording, not the ECG list length.

    The reader holds each 1 Hz value across the samples of its second, so the
    metric series are as long as the ECG. Indexing them on the second
    (`i * 1000`) replayed a 13 s session as 28 minutes — every metric line
    minutes late, and the whole timeline wrong.
    """
    from playback_edf import SessionEmitter

    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    for sec in range(3):
        writer.set_channel("POWER", 200.0 + sec)
        writer.push_ecg([0] * 130, sec * 1000)
    writer.finish()

    emitter = SessionEmitter(read(str(path)))
    assert emitter.duration_ms == pytest.approx(3000.0, abs=20.0)
    assert emitter.events, "a session must produce events"
    last = max(t for t, _ in emitter.events)
    assert last <= emitter.duration_ms + 50.0, "no event may sit past the recording"

    power = [p for t, p in emitter.events if p.get("type") == "cp"]
    # The int16 axis quantises, so compare to the resolution of the power channel.
    assert [round(p["power"]) for p in power] == [200, 201, 202]


def test_replay_emits_ecg_in_bursts_with_the_recording_rate(tmp_path):
    from playback_edf import LINE_SAMPLES, SessionEmitter

    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.finish()

    emitter = SessionEmitter(read(str(path)))
    ecg = [p for _, p in emitter.events if p.get("type") == "ecg"]
    assert len(ecg) == 130 // LINE_SAMPLES + 1
    assert all(len(p["uv"]) <= LINE_SAMPLES for p in ecg)
    assert sum(len(p["uv"]) for p in ecg) == 130
    assert ecg[0]["fs"] == pytest.approx(FS)


def test_replay_falls_back_to_beat_annotations_without_an_rr_channel(tmp_path):
    """
    An older app recording has no RR_MS channel, only beat annotations. Their
    gaps are the only interval source there is, so playback must use them
    instead of emitting no heart-rate data at all.
    """
    from playback_edf import SessionEmitter

    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.push_ecg([0] * 130, 1000)
    writer.push_ecg([0] * 130, 2000)
    writer.add_annotation(300.0, "N", 100.0)
    writer.add_annotation(1100.0, "N", 100.0)
    writer.add_annotation(1900.0, "PVC", 100.0)
    writer.finish()

    emitter = SessionEmitter(read(str(path)))
    hr = [p for _, p in emitter.events if p.get("type") == "hr"]
    assert [p["rr"] for p in hr] == [[800], [800]], \
        "the interval comes from the gap between consecutive beats"


def test_replay_prefers_the_rr_channel_over_annotations(tmp_path):
    from playback_edf import SessionEmitter

    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.on_beat(800.0)
    writer.push_ecg([0] * 130, 1000)
    writer.add_annotation(400.0, "N", 100.0)
    writer.add_annotation(900.0, "N", 100.0)
    writer.finish()

    emitter = SessionEmitter(read(str(path)))
    hr = [p for _, p in emitter.events if p.get("type") == "hr"]
    assert [p["rr"] for p in hr] == [[800]], \
        "the sensor's own interval wins over the detector's annotations"


def test_replay_emits_one_beat_line_per_interval(tmp_path):
    from playback_edf import SessionEmitter

    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0] * 130, 0)
    writer.on_beat(800.0)
    writer.push_ecg([0] * 130, 1000)
    writer.on_beat(820.0)
    writer.push_ecg([0] * 130, 2000)
    writer.finish()

    emitter = SessionEmitter(read(str(path)))
    hr = [p for _, p in emitter.events if p.get("type") == "hr"]
    assert [p["rr"] for p in hr] == [[800], [820]]
    assert hr[0]["bpm"] == 75


def test_record_layout_matches_the_header(tmp_path):
    path = tmp_path / "session.edf"
    _write(path, seconds=1).finish()
    raw = path.read_bytes()
    n_sig = int(raw[252:256].decode("ascii").strip())
    header = int(raw[184:192].decode("ascii").strip())
    # One record = every signal's samples for a second, 2 bytes each: a
    # full-rate channel carries `fs` samples, a 1 Hz one carries a single
    # sample, and the annotations channel a fixed ANN_SPR. Derived from the
    # channel table rather than spelled out, so moving a channel between rates
    # cannot silently leave this assertion describing the old layout — which is
    # exactly what happened when RR_MS became a 1 Hz channel.
    assert n_sig == len(CHANNELS) + 1
    per_record = sum(130 if chan.full_rate else chan.samples_per_record
                     for chan in CHANNELS)
    record_bytes = per_record * 2 + ANN_SPR * 2
    assert len(raw) - header == record_bytes
    assert DIG_MIN == -32768 and DIG_MAX == 32767


def test_rr_is_sub_sampled_and_every_beat_is_annotated(tmp_path):
    """
    `RR_MS` is stored `RR_SAMPLE_HZ` times a second, and each beat gets an event.

    Two separate contracts are pinned here, and both matter for replaying a file:

    * The sub-rate has to be fast enough that two beats never share a sample
      period. It is a step channel, so a shared period loses the earlier beat
      outright — at 1 Hz that was every beat above 60 bpm (37% of a real ride).
    * Every beat is also an annotation, because a step channel cannot represent
      a beat whose interval equals the previous one *at any rate*. Without the
      events, that beat is simply absent from the file.
    """
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0.0] * 130, 0)
    # Three beats inside the first second, 250 ms apart: at 5 Hz they fall in
    # separate sample periods, so all three survive.
    for rr in (700.0, 750.0, 800.0):
        writer.on_beat(rr)
        for _ in range(32):          # ~250 ms of ECG at 130 Hz
            writer._append_sample(0.0)
    writer.push_ecg([0.0] * 130, 1000)
    writer.finish()

    raw = path.read_bytes()
    n_sig = int(raw[252:256].decode("ascii").strip())
    base = 256
    labels = [raw[base + i * 16: base + (i + 1) * 16].decode("ascii").strip()
              for i in range(n_sig)]
    # Samples-per-record is the ninth per-signal field: label (16), transducer
    # (80), dimension (8), physical min/max (8 each), digital min/max (8 each),
    # prefilter (80) = 216 bytes before it.
    spr_off = base + n_sig * 216
    spr = [int(raw[spr_off + i * 8: spr_off + (i + 1) * 8].decode("ascii").strip())
           for i in range(n_sig)]
    assert spr[labels.index("RR_MS")] == int(RR_SAMPLE_HZ)
    assert spr[labels.index("ECG")] == 130

    rec = read(str(path))
    beats = [a for a in rec.annotations if a.text == "BEAT"]
    assert len(beats) == 3, f"one event per beat, got {len(beats)}"
    # A beat is an instant, so it carries no duration in the TAL.
    assert all(b.duration_ms == 0.0 for b in beats)
    # Reading the file back finds all three beats with their own intervals.
    assert [round(rr) for _, rr in rec.rr_marks()] == [700, 750, 800], \
        f"all three beats should survive at {RR_SAMPLE_HZ} Hz"


def test_beats_with_identical_intervals_are_still_recorded(tmp_path):
    """
    The case the annotations exist for: repeated intervals, inseparable by rate.

    A step channel marks a beat as a *change* in the held interval, so two
    consecutive beats reporting the same interval produce no change at all. No
    sample rate fixes that — it is a property of the encoding, not of the
    clock — and a real capture had 26 such pairs. The annotation is what keeps
    those beats in the file.

    This is the regression that removal caused: `RR_MS` alone recovered 158 of
    167 beats at 1, 5, 10 and 50 Hz alike, so the loss looked like a rate
    problem while being nothing of the sort.
    """
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0.0] * 130, 0)
    # Four beats, one per 250 ms, all with the *same* interval.
    for _ in range(4):
        writer.on_beat(900.0)
        for _ in range(32):
            writer._append_sample(0.0)
    writer.push_ecg([0.0] * 130, 1000)
    writer.finish()

    rec = read(str(path))
    # The annotations carry all four beats.
    beats = [a for a in rec.annotations if a.text == "BEAT"]
    assert len(beats) == 4, f"all four beats must be recorded, got {len(beats)}"
    gaps = [round(beats[i].onset_ms - beats[i - 1].onset_ms) for i in range(1, len(beats))]
    assert all(abs(g - 250) <= 12 for g in gaps), f"beat spacing drifted: {gaps}"

    # And `rr_marks()` must report four beats too. The step channel alone shows
    # one value for the whole stretch, so a reader that only watches for changes
    # returns one beat and silently drops three quarters of the session. That is
    # what `_annotated_marks` exists to prevent.
    marks = rec.rr_marks()
    assert len(marks) == 4, (
        f"every beat must survive reading, got {len(marks)} of 4")
    assert all(rr == pytest.approx(900.0, abs=2.0) for _, rr in marks), (
        f"each beat must carry its interval, got {[round(rr) for _, rr in marks]}")


def test_reading_a_file_recovers_every_annotated_beat(tmp_path):
    """
    `rr_marks()` must return one mark per beat, not one per distinct interval.

    A resting heart produces long runs of exactly equal intervals, and a step
    channel shows no change across them — so a reader that infers beats from
    value changes loses every repeat. A real 617 s capture had 719 beats that
    collapsed to 514 marks (28% lost), which silently shortened playback and
    inflated the derived HRV numbers. This pins the fix: the `BEAT` annotations
    are the beat log and reading must use them.
    """
    path = tmp_path / "session.edf"
    writer = EdfWriter(str(path), FS)
    writer.push_ecg([0.0] * 130, 0)
    # A run of identical intervals, which is the case a step channel cannot see.
    for _ in range(12):
        writer.on_beat(857.0)
        for _ in range(112):        # ~860 ms of ECG at 130 Hz
            writer._append_sample(0.0)
    writer.push_ecg([0.0] * 130, 10_400)
    writer.finish()

    rec = read(str(path))
    annotated = [a for a in rec.annotations if a.text == "BEAT"]
    marks = rec.rr_marks()

    assert len(annotated) == 12, f"{len(annotated)} beat events written"
    assert len(marks) == len(annotated), (
        f"reading lost beats: {len(marks)} marks for {len(annotated)} beats")
    # The interval each beat carries is its own, not whichever value happened to
    # be standing at that instant.
    assert all(rr == pytest.approx(857.0, abs=2.0) for _, rr in marks), (
        f"intervals read back as {[round(rr) for _, rr in marks]}")
