"""
Flask application for Zwift data visualization server.
"""
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional
import time


import tomllib

from flask import Flask, jsonify, render_template, request

from .data_buffer import DataBuffer
from .serial_reader import SerialReader


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Suppress werkzeug request logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Initialize Flask app with correct paths
app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(
    __name__,
    template_folder=os.path.join(app_dir, 'templates'),
    static_folder=os.path.join(app_dir, 'static')
)

# Global instances
data_buffer: DataBuffer = None
serial_reader: SerialReader = None

# Device status tracking (RSSI and last seen timestamp)
device_status = {
    'hr': {'rssi': None, 'last_seen_ms': None},
    'cp': {'rssi': None, 'last_seen_ms': None},
    'ftms': {'rssi': None, 'last_seen_ms': None}
}


def load_config() -> Dict:
    """Load configuration from config.conf (TOML format)."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(app_dir, 'config.conf')
    
    try:
        with open(config_path, 'rb') as f:
            config_dict = tomllib.load(f)
        return config_dict
    except FileNotFoundError:
        logger.warning(f"Config file not found at {config_path}, using defaults")
        return {}
    except Exception as e:
        logger.error(f"Error parsing config file: {e}, using defaults")
        return {}


def load_metrics_config() -> Dict:
    """Get metrics configuration from config file."""
    config = load_config()
    metrics_config = config.get('metrics', {})
    
    # Ensure internal_name matches the key for each metric
    result = {}
    for key, metric_config in metrics_config.items():
        result[key] = {
            'display_name': metric_config.get('display_name', key.replace('_', ' ').title()),
            'internal_name': metric_config.get('internal_name', key),
            'unit': metric_config.get('unit', ''),
            'show_in_current_values': metric_config.get('show_in_current_values', True),
            'show_in_plot': metric_config.get('show_in_plot', True),
            'color': metric_config.get('color', '#888888'),
            'line_width': metric_config.get('line_width', 2),
            'line_style': metric_config.get('line_style', 'solid'),  # solid, dash, dot, dashdot, longdash, longdashdot
            'yaxis': metric_config.get('yaxis', 'y'),
            'yaxis_range': metric_config.get('yaxis_range')  # Optional, can be None
        }
    
    return result


def init_app():
    """Initialize the application components."""
    global data_buffer, serial_reader
    
    # Prevent double initialization
    if serial_reader is not None and serial_reader.running:
        logger.warning("App already initialized, skipping re-initialization")
        return
    
    config = load_config()
    
    # Initialize data buffer
    max_minutes = config.get('buffer', {}).get('max_minutes', 60)
    data_buffer = DataBuffer(max_minutes=max_minutes)
    logger.info(f"Data buffer initialized with {max_minutes} minute capacity")
    
    # Initialize serial reader
    serial_port = config.get('dongle', {}).get('serial', '/dev/ttyACM0')
    log_file = config.get('logging', {}).get('log_file', '')
    # Only use log_file if it's not empty
    log_file = log_file if log_file else None
    serial_reader = SerialReader(port=serial_port, data_queue=None, log_file=log_file)
    
    # Start background thread to process serial data
    def process_serial_data():
        """Background thread to process data from serial reader."""
        while True:
            try:
                # Check if serial reader has data queue (we'll create one)
                if serial_reader.data_queue:
                    try:
                        data = serial_reader.data_queue.get(timeout=0.1)
                        if data:
                            # Check for device RSSI events
                            event = data.get('event')
                            if event == 'device_rssi':
                                device = data.get('device')
                                rssi = data.get('rssi')
                                timestamp_ms = data.get('timestamp_ms')
                                if device and rssi is not None:
                                    device_status[device]['rssi'] = rssi
                                    device_status[device]['last_seen_ms'] = timestamp_ms
                            else:
                                # Process metric data
                                metric = data.get('metric')
                                timestamp_ms = data.get('timestamp_ms')
                                value = data.get('value')
                                if metric and timestamp_ms is not None and value is not None:
                                    data_buffer.add_data_point(metric, timestamp_ms, value)
                    except:
                        pass
                else:
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"Error processing serial data: {e}")
                time.sleep(1)
    
    # Create data queue for serial reader
    import queue
    data_queue = queue.Queue()
    serial_reader.data_queue = data_queue
    
    # Start serial reader
    serial_reader.start()
    
    # Start data processing thread
    import threading
    processing_thread = threading.Thread(target=process_serial_data, daemon=True)
    processing_thread.start()
    
    logger.info("Application initialized")


@app.route('/')
def index():
    """Serve the main dashboard page."""
    return render_template('index.html')


@app.route('/api/data')
def get_data():
    """
    Get data for the specified time window.
    
    Query parameters:
        window: Time window in minutes (1, 5, or 60). Default: 5
    """
    window = request.args.get('window', '5', type=int)
    
    # Validate window
    if window not in [1, 5, 60]:
        window = 5
    
    if data_buffer is None:
        return jsonify({'error': 'Data buffer not initialized'}), 500
    
    # Get data for the window
    data = data_buffer.get_data_for_window(window)
    
    # Convert to format suitable for frontend
    result = {}
    for metric, points in data.items():
        result[metric] = {
            'timestamps': [ts.isoformat() for ts, _ in points],
            'values': [val for _, val in points]
        }

    return jsonify({
        'data': result,
        'window_minutes': window,
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/metrics')
def get_metrics():
    """Get list of available metrics."""
    if data_buffer is None:
        return jsonify({'error': 'Data buffer not initialized'}), 500
    
    metrics = data_buffer.get_metrics_list()
    return jsonify({'metrics': metrics})


@app.route('/api/metrics-config')
def get_metrics_config():
    """Get metrics configuration for the frontend."""
    config = load_metrics_config()
    return jsonify({'metrics_config': config})


@app.route('/api/status')
def get_status():
    """Get server and connection status."""
    # Get the latest timestamp from data buffer to use as "current time"
    # This is relative to dongle time, so we can compare with device timestamps
    if data_buffer:
        current_time_ms = data_buffer.latest_timestamp_ms
    else:
        current_time_ms = 0
    
    timeout_ms = 5000  # 5 seconds - devices are considered connected if seen in last 5 seconds
    devices = {}
    for device_name, status in device_status.items():
        last_seen = status['last_seen_ms']
        if last_seen is not None and current_time_ms > 0:
            is_connected = (current_time_ms - last_seen) < timeout_ms
        else:
            is_connected = False
        
        devices[device_name] = {
            'connected': is_connected,
            'rssi': status['rssi'],
            'last_seen_ms': last_seen
        }
    
    status = {
        'connected': serial_reader.connected if serial_reader else False,
        'running': serial_reader.running if serial_reader else False,
        'port': serial_reader.port if serial_reader else None,
        'devices': devices
    }
    return jsonify(status)


# Initialize app on import
init_app()
