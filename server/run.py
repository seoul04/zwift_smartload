#!/usr/bin/env python3
"""
Main entry point for the Zwift Visualization Server.
"""
import signal
import sys

from src.app import app, shutdown_edf


def _install_shutdown_hook():
    """
    Finish the EDF session on the way out.

    The recorder buffers data records for a minute at a time, so a hard stop
    loses up to a minute of the recording. A SIGTERM from a process manager, or
    a Ctrl-C, is the normal way this server stops, so both are handled; SIGKILL
    cannot be caught and is accepted as data loss.
    """
    def handler(signum, _frame):
        signame = signal.Signals(signum).name
        print(f"\n{signame}: closing the EDF session before exiting...", flush=True)
        shutdown_edf()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


if __name__ == '__main__':
    _install_shutdown_hook()
    # use_reloader=False is critical! Flask's reloader starts the app twice,
    # which causes multiple access to the serial port and data corruption.
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
