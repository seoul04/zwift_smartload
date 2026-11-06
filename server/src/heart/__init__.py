"""
The heart graphs: ECG, beats, trends and the RMSSD panel.

A port of the four panels of the `my_heart` Android app onto the server, fed by
the same dongle the rest of the dashboard reads. `state.HeartState` owns the
accumulators; this package exposes nothing but that class.
"""

from .state import HeartState

__all__ = ["HeartState"]
