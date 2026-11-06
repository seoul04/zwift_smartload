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
