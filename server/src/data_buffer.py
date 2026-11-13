"""
Data buffer for storing and retrieving time-windowed sensor data.
"""
import logging
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DataBuffer:
    """Thread-safe rolling buffer for sensor data with time window support."""

    # A rewind larger than this means the dongle restarted its uptime clock.
    # Normal jitter and out-of-order points between device threads are far
    # smaller; the dongle's loop counter restarts at 0 on every boot.
    RESTART_THRESHOLD_MS = 5_000
    
    def __init__(self, max_minutes: int = 60):
        """
        Initialize the data buffer.
        
        Args:
            max_minutes: Maximum number of minutes of data to retain
        """
        self.max_minutes = max_minutes
        self.max_age_ms = max_minutes * 60 * 1000  # Convert to milliseconds
        self.lock = threading.Lock()
        
        # Track the most recent timestamp (this represents "now" in dongle time)
        self.latest_timestamp_ms: int = 0
        
        # Store data points as (timestamp_ms, value) tuples for each metric
        # Timestamps are in milliseconds (relative to dongle start)
        self.buffers: Dict[str, List[Tuple[int, float]]] = {
            'heart_rate': [],
            'power_meter_power': [],
            'power_meter_cadence': [],
            'trainer_speed': [],
            'trainer_power': [],
            'trainer_cadence': [],  # May not be available, but keep for consistency
            # Simulation data (from Zwift)
            'sim_grade': [],
            'sim_resistance': [],  # Trainer resistance (0-100)
        }
    
    def add_data_point(self, metric: str, timestamp_ms: int, value: float):
        """
        Add a data point to the buffer.
        
        Args:
            metric: Metric name (e.g., 'heart_rate', 'power_meter_power')
            timestamp_ms: Timestamp in milliseconds (relative to dongle start)
            value: Value of the metric
        """
        if metric not in self.buffers:
            return
        
        with self.lock:
            # Timestamps are the dongle's uptime, which restarts from zero when
            # it reboots. After that every new point lands far behind
            # latest_timestamp_ms, so the monotonic update below would pin the
            # clock at the old session's value forever (_cleanup_old_data is
            # anchored on it too, so nothing aged out either). The buffer then
            # reports "now" as a time no new point can reach, and every window
            # ends before the data: exactly the frozen dashboard, while serial
            # data kept arriving the whole time.
            #
            # Only a large rewind is treated as a reboot. Late points from the
            # current session arrive slightly behind the clock and must not
            # wipe the buffer.
            if (self.latest_timestamp_ms > 0
                    and timestamp_ms < self.latest_timestamp_ms
                    and self.latest_timestamp_ms - timestamp_ms > self.RESTART_THRESHOLD_MS):
                logger.warning(
                    "Dongle clock went backwards (%d -> %d ms): restarting the "
                    "buffer, the dongle most likely rebooted",
                    self.latest_timestamp_ms, timestamp_ms)
                self._restart_locked(timestamp_ms)

            # Update latest timestamp
            if timestamp_ms > self.latest_timestamp_ms:
                self.latest_timestamp_ms = timestamp_ms
            
            self.buffers[metric].append((timestamp_ms, value))
            self._cleanup_old_data()

    def _restart_locked(self, first_timestamp_ms: int):
        """Drop the previous session's data and re-anchor the clock."""
        for metric in self.buffers:
            self.buffers[metric] = []
        self.latest_timestamp_ms = first_timestamp_ms
    
    def get_data_for_window(self, window_minutes: int) -> Dict[str, List[Tuple[int, float]]]:
        """
        Get data points within the specified time window.

        Args:
            window_minutes: Time window in minutes (1, 5, or 60)

        Returns:
            Dictionary mapping metric names to lists of (timestamp_ms, value) tuples
        """
        window_ms = window_minutes * 60 * 1000
        cutoff_timestamp_ms = self.latest_timestamp_ms - window_ms
        return self._filter_buffers(cutoff_timestamp_ms, since_ms=None)

    def get_data_delta(
        self, window_minutes: int, since_ms: int
    ) -> Dict[str, List[Tuple[int, float]]]:
        """
        Get data points newer than since_ms that still fall within the time window.

        Args:
            window_minutes: Time window in minutes (1, 5, or 60)
            since_ms: Only return points with timestamp_ms strictly greater than this

        Returns:
            Dictionary mapping metric names to lists of (timestamp_ms, value) tuples
        """
        window_ms = window_minutes * 60 * 1000
        cutoff_timestamp_ms = self.latest_timestamp_ms - window_ms
        return self._filter_buffers(cutoff_timestamp_ms, since_ms=since_ms)

    def _filter_buffers(
        self, cutoff_timestamp_ms: int, since_ms: Optional[int]
    ) -> Dict[str, List[Tuple[int, float]]]:
        result = {}
        with self.lock:
            for metric, data_points in self.buffers.items():
                filtered = []
                for ts_ms, val in data_points:
                    if ts_ms < cutoff_timestamp_ms:
                        continue
                    if since_ms is not None and ts_ms <= since_ms:
                        continue
                    filtered.append((ts_ms, val))
                result[metric] = filtered
        return result
    
    def _cleanup_old_data(self):
        """Remove data points older than max_age."""
        cutoff_timestamp_ms = self.latest_timestamp_ms - self.max_age_ms
        
        for metric in self.buffers:
            self.buffers[metric] = [
                (ts_ms, val) for ts_ms, val in self.buffers[metric]
                if ts_ms >= cutoff_timestamp_ms
            ]
    
    def get_metrics_list(self) -> List[str]:
        """Get list of available metric names."""
        return list(self.buffers.keys())
