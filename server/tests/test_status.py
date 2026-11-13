"""
Guards for the header's device LEDs.

The dashboard decides whether a sensor is "connected" from `/api/status`, and
that answer has to stay stable while the sensor is live. It did not: the header
LED for the heart-rate strap blinked between green and grey every few seconds
during a perfectly healthy session.

The cause was a mixed-clock comparison, and it is worth stating because nothing
about it looks wrong at a glance. `last_seen_ms` is the *dongle's* timestamp,
and liveness was `data_buffer.latest_timestamp_ms - last_seen < 5 s`. That is
only valid if every dongle packet shares one clock. They do not:

    ecg  ->  firmware's ECG sample clock (`ecg_t0_ms`), free-running
    hr   ->  `k_uptime_get_32()`, true uptime at arrival

On a live link those were measured ~21 minutes apart, and *both* feed the `hr`
device record (ECG counts as a heart-rate stream, since a Movesense may never
send a `hr` line). So `last_seen` alternated between "21 minutes ago" and "21
minutes in the future", and the LED followed whichever arrived last.

Liveness is now judged on `time.monotonic()` at arrival, which is
clock-agnostic: it cannot matter which dongle clock stamped a packet.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def _clean_devices():
    """Each test starts from "nothing seen"."""
    for status in app_module.device_status.values():
        status["rssi"] = None
        status["battery"] = None
        status["last_seen_ms"] = None
        status["seen_monotonic_ms"] = None
    yield


def _hr_connected(client) -> bool:
    resp = client.get("/api/status")
    assert resp.status_code == 200
    return resp.get_json()["devices"]["hr"]["connected"]


def test_a_freshly_seen_device_is_connected(client):
    app_module._mark_device_seen("hr", 1000)
    assert _hr_connected(client) is True


def test_an_unseen_device_is_not_connected(client):
    assert _hr_connected(client) is False


def test_liveness_ignores_which_dongle_clock_stamped_the_packet(client):
    """
    The regression. Two clocks ~21 minutes apart must not affect the LED.

    This is the exact shape of the live link: ECG stamped from the sample clock,
    R-R from uptime, both marking `hr` as seen. Under the old mixed-clock test
    one of them read as 21 minutes stale on every alternate packet.
    """
    ecg_clock_ms = 2_218_669
    uptime_clock_ms = 3_499_116
    assert uptime_clock_ms - ecg_clock_ms > 20 * 60 * 1000  # the real gap

    # Both report in, a moment apart, exactly as the live stream does.
    app_module._mark_device_seen("hr", ecg_clock_ms)
    app_module._mark_device_seen("hr", uptime_clock_ms)

    assert _hr_connected(client) is True

    # And still connected after the *next* ECG packet restamps it with the
    # lower-valued clock — the case that used to flip the LED back to grey.
    app_module._mark_device_seen("hr", ecg_clock_ms + 512)
    assert _hr_connected(client) is True


def test_the_dongle_stamp_is_still_reported_for_display(client):
    """`last_seen_ms` stays in the payload; only the age test moved."""
    app_module._mark_device_seen("hr", 123_456)
    body = client.get("/api/status").get_json()
    assert body["devices"]["hr"]["last_seen_ms"] == 123_456


def test_a_device_goes_stale_once_the_timeout_passes(client, monkeypatch):
    """
    Liveness still has to expire, or a dead sensor would read as connected.

    The age check is against arrival time, so shifting the monotonic reference
    forward is the same thing as time passing.
    """
    app_module._mark_device_seen("hr", 1000)
    assert _hr_connected(client) is True

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 6.0)
    assert _hr_connected(client) is False
