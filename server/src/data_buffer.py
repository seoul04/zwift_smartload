"""
Data buffer for storing and retrieving time-windowed sensor data.
"""
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


class DataBuffer:
    """Thread-safe rolling buffer for sensor data with time window support."""
    
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
            # Update latest timestamp
            if timestamp_ms > self.latest_timestamp_ms:
                self.latest_timestamp_ms = timestamp_ms
            
            self.buffers[metric].append((timestamp_ms, value))
            self._cleanup_old_data()
    
    def get_data_for_window(self, window_minutes: int) -> Dict[str, List[Tuple[datetime, float]]]:
        """
        Get data points within the specified time window.
        
        Args:
            window_minutes: Time window in minutes (1, 5, or 60)
            
        Returns:
            Dictionary mapping metric names to lists of (datetime, value) tuples
            (timestamps converted to datetime for frontend compatibility)
        """
        window_ms = window_minutes * 60 * 1000  # Convert to milliseconds
        cutoff_timestamp_ms = self.latest_timestamp_ms - window_ms
        
        result = {}
        
        with self.lock:
            for metric, data_points in self.buffers.items():
                # Filter data points within the window
                filtered = [
                    (self._ms_to_datetime(ts_ms), val)
                    for ts_ms, val in data_points
                    if ts_ms >= cutoff_timestamp_ms
                ]
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
    
    def _ms_to_datetime(self, timestamp_ms: int) -> datetime:
        """
        Convert relative timestamp (ms) to datetime for frontend.
        Uses a base datetime and adds the relative time.
        
        Args:
            timestamp_ms: Timestamp in milliseconds (relative)
            
        Returns:
            datetime object (for frontend compatibility)
        """
        # Use a fixed base datetime (e.g., epoch start) and add relative time
        # This is just for display purposes - the actual time doesn't matter
        base_datetime = datetime(2024, 1, 1, 0, 0, 0)
        return base_datetime + timedelta(milliseconds=timestamp_ms)
    
    def get_metrics_list(self) -> List[str]:
        """Get list of available metric names."""
        return list(self.buffers.keys())
