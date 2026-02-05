#!/usr/bin/env python3
"""
Main entry point for the Zwift Visualization Server.
"""
from src.app import app

if __name__ == '__main__':
    # use_reloader=False is critical! Flask's reloader starts the app twice,
    # which causes multiple access to the serial port and data corruption.
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
