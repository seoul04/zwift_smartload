from src.data_buffer import DataBuffer


def test_get_data_for_window_returns_ms_timestamps():
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('heart_rate', 1000, 120.0)
    buf.add_data_point('heart_rate', 2000, 121.0)
    buf.add_data_point('heart_rate', 120_000, 130.0)

    # 1-minute window anchored at latest (120s): cutoff at 60s
    data = buf.get_data_for_window(1)
    assert data['heart_rate'] == [(120_000, 130.0)]


def test_get_data_delta_only_new_points_in_window():
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('heart_rate', 1000, 120.0)
    buf.add_data_point('heart_rate', 2000, 121.0)
    buf.add_data_point('heart_rate', 3000, 122.0)
    buf.add_data_point('power_meter_power', 2500, 200.0)

    delta = buf.get_data_delta(5, since_ms=2000)
    assert delta['heart_rate'] == [(3000, 122.0)]
    assert delta['power_meter_power'] == [(2500, 200.0)]


def test_get_data_delta_excludes_points_outside_window():
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('heart_rate', 1000, 120.0)
    buf.add_data_point('heart_rate', 5000, 125.0)
    buf.add_data_point('heart_rate', 70_000, 130.0)

    delta = buf.get_data_delta(1, since_ms=5000)
    assert delta['heart_rate'] == [(70_000, 130.0)]


def test_dongle_reboot_restarts_the_clock():
    """A reboot rewinds the uptime clock; the buffer must follow it.

    Regression: latest_timestamp_ms only ever moved forward, so after a dongle
    reboot it stayed at the old session's value. Every window then ended before
    the new (smaller) timestamps and the dashboard froze with live data still
    arriving on the serial port.
    """
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('trainer_power', 500_000, 200.0)
    buf.add_data_point('trainer_power', 824_948, 210.0)

    # Dongle rebooted: uptime restarts near zero.
    buf.add_data_point('trainer_power', 3_000, 0.0)

    assert buf.latest_timestamp_ms == 3_000
    data = buf.get_data_for_window(1)
    assert data['trainer_power'] == [(3_000, 0.0)]


def test_small_rewind_does_not_wipe_the_buffer():
    """Out-of-order points within a session must not be mistaken for a reboot."""
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('trainer_power', 10_000, 200.0)
    buf.add_data_point('trainer_power', 10_500, 210.0)
    # Slightly late point, well under RESTART_THRESHOLD_MS.
    buf.add_data_point('trainer_power', 10_200, 205.0)

    assert buf.latest_timestamp_ms == 10_500
    data = buf.get_data_for_window(1)
    assert len(data['trainer_power']) == 3


def test_clock_advances_after_reboot():
    """After the restart the clock must track the new session upwards again."""
    buf = DataBuffer(max_minutes=60)
    buf.add_data_point('trainer_power', 900_000, 200.0)
    buf.add_data_point('trainer_power', 2_000, 1.0)
    buf.add_data_point('trainer_power', 9_000, 2.0)

    assert buf.latest_timestamp_ms == 9_000
    # The pre-reboot point is gone; both new-session points are within a minute.
    assert buf.get_data_for_window(1)['trainer_power'] == [(2_000, 1.0), (9_000, 2.0)]
