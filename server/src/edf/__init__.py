"""
EDF+ recording and playback for z-relay-server.

`writer` streams a live session to an EDF+C file, `reader` decodes one back, and
`recorder` decides where the sessions live and how many are kept. The CLI
replayer is `playback_edf.py` at the project root, next to `playback_serial.py`.
"""

from .reader import Annotation, EdfFileError, EdfRecording, read
from .recorder import EdfRecorder
from .writer import CHANNELS, EdfWriter

__all__ = [
    "Annotation",
    "CHANNELS",
    "EdfFileError",
    "EdfRecorder",
    "EdfRecording",
    "EdfWriter",
    "read",
]
