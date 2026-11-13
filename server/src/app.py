"""
Flask application for Zwift data visualization server.
"""
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional
import time


import tomllib

from flask import Flask, jsonify, render_template, request

from .data_buffer import DataBuffer
from .dircon import DirconServer, DongleIdentity, MdnsAdvertiser, SensorState, build_device
from .edf import EdfRecorder
from .heart import HeartState
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
heart_state: Optional[HeartState] = None
dircon_server: Optional[DirconServer] = None
mdns_advertiser: Optional[MdnsAdvertiser] = None
edf_recorder: Optional[EdfRecorder] = None

# Device status tracking (RSSI, battery, and last seen timestamp)
#
# `last_seen_ms` is the dongle's own stamp, kept for display. `seen_monotonic_ms`
# is *our* arrival clock and is what liveness is judged on: the dongle does not
# stamp every packet from one clock (ECG and R-R use different ones, and they
# drift apart), so comparing two dongle timestamps is not a valid age test.
device_status = {
    'hr': {'rssi': None, 'battery': None, 'last_seen_ms': None,
           'seen_monotonic_ms': None},
    'cp': {'rssi': None, 'battery': None, 'last_seen_ms': None,
           'seen_monotonic_ms': None},
    'ftms': {'rssi': None, 'battery': None, 'last_seen_ms': None,
             'seen_monotonic_ms': None}
}


def _mark_device_seen(device: str, dongle_ts: Optional[int]) -> None:
    """
    Note that a device just reported in, on both clocks.

    The dongle stamp is kept only for display. Liveness uses `time.monotonic()`
    because a device's packets can arrive on more than one dongle clock — ECG is
    stamped from the firmware's sample clock, R-R from true uptime — and those
    drift apart by minutes. Ageing one against the other is what made the header
    LED flap between green and grey.
    """
    if device not in device_status:
        return
    device_status[device]['last_seen_ms'] = dongle_ts
    device_status[device]['seen_monotonic_ms'] = time.monotonic() * 1000.0


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


# Interfaces that never carry the LAN identity we want to present to Zwift.
_VIRTUAL_IFACES = ('lo', 'docker0', 'br-', 'veth', 'virbr', 'tun', 'tap', 'wg')


def _sysfs_mac() -> Optional[str]:
    """Read a real NIC address straight from sysfs.

    uuid.getnode() shells out to `ip` or `ifconfig`, and the server image ships
    neither. Python then falls back to a random address with the multicast bit
    set, so every container start advertises a NEW identity (e.g. 9B:95:... then
    C1:6A:...). Zwift pairs by that address, so it keeps retrying a device that
    no longer exists. sysfs is always mounted and needs no tools.
    """
    try:
        names = os.listdir('/sys/class/net')
    except OSError:
        return None
    # Physical ethernet/wifi first: a stable identity should not ride on a
    # bridge that Docker recreates.
    def rank(name: str) -> int:
        if name.startswith(('en', 'eth', 'wl', 'wlan')):
            return 0
        return 1
    for name in sorted(names, key=rank):
        if name.startswith(_VIRTUAL_IFACES):
            continue
        try:
            with open(f'/sys/class/net/{name}/address', encoding='ascii') as f:
                mac = f.read().strip().upper()
        except OSError:
            continue
        if len(mac.split(':')) == 6 and mac != '00:00:00:00:00:00':
            return mac
    return None


def default_mac_address() -> str:
    stable = _sysfs_mac()
    if stable:
        return stable
    node = uuid.getnode()
    return ':'.join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


def init_dircon(dircon_config: Dict):
    """Start the DirCon server and its mDNS advertisement so Zwift can pair without Companion."""
    global dircon_server, mdns_advertiser

    if not dircon_config.get('enabled', False):
        logger.info("DirCon disabled")
        return

    if not dircon_config.get('log_frames', False):
        logging.getLogger('src.dircon').setLevel(logging.INFO)

    state = SensorState(stale_timeout_s=float(dircon_config.get('stale_timeout_s', 3)))
    # Renamed to the dongle's own name (Z-Relay-XXXX) once it reports it
    device = build_device('Z-Relay', int(dircon_config.get('port', 36866)))
    devices = [device]

    forward_control = dircon_config.get('forward_control', False)

    def on_control(cmd):
        # When forwarding, the dongle reports the grade itself with a "sim" line
        if not forward_control and cmd.grade is not None and data_buffer is not None:
            data_buffer.add_data_point('sim_grade', data_buffer.latest_timestamp_ms, float(cmd.grade))
        # What Zwift asked for is recorded separately from what the trainer
        # reports: the pair is what shows a control loop that is not tracking.
        if edf_recorder is not None:
            edf_recorder.on_control(cmd)

    bind_ip = dircon_config.get('bind_ip', '0.0.0.0')
    dircon_server = DirconServer(
        devices, state,
        bind_ip=bind_ip,
        notify_interval_s=float(dircon_config.get('notify_interval_s', 1.0)),
        notify_subscribed_only=dircon_config.get('notify_subscribed_only', True),
        on_control=on_control,
        command_sender=serial_reader.write_line if forward_control else None,
        control_timeout_s=float(dircon_config.get('control_timeout_s', 2.0)),
    )
    if not dircon_server.start():
        dircon_server = None
        return
    serial_reader.raw_listeners.append(dircon_server.on_serial_message)

    mdns_advertiser = MdnsAdvertiser(devices, mac_address=default_mac_address(), ip=bind_ip)
    identity = DongleIdentity(
        device, mdns_advertiser,
        send_line=serial_reader.write_line,
        fallback_mac=default_mac_address(),
        name=dircon_config.get('name'),
        mac_address=dircon_config.get('mac_address'),
        timeout_s=float(dircon_config.get('identity_timeout_s', 15)),
        # Off unless the firmware is known to read serial input. Firmware
        # without serial_cmd.c never drains what is written, and the backed-up
        # CDC endpoint silences the whole stream while the port stays open.
        probe=bool(dircon_config.get('probe_identity', False)),
    )
    serial_reader.raw_listeners.append(identity.on_serial_message)
    identity.start()


def init_app():
    """Initialize the application components."""
    global data_buffer, serial_reader, heart_state, edf_recorder
    
    # Prevent double initialization
    if serial_reader is not None and serial_reader.running:
        logger.warning("App already initialized, skipping re-initialization")
        return
    
    config = load_config()
    
    # Initialize data buffer
    max_minutes = config.get('buffer', {}).get('max_minutes', 60)
    data_buffer = DataBuffer(max_minutes=max_minutes)
    logger.info(f"Data buffer initialized with {max_minutes} minute capacity")

    # Heart graphs (ECG, beats, trends, RMSSD): fed from the same serial lines,
    # through the raw listener so the R-R batch arrives whole.
    heart_cfg = config.get('heart', {})
    heart_state = HeartState(fs_hz=float(heart_cfg.get('fs_hz', 130)))
    heart_state.set_ecg_window(float(heart_cfg.get('ecg_window_rr', 10)))
    heart_state.set_trend_width(int(heart_cfg.get('trend_width_s', 600)))
    heart_state.set_beats_limit(int(heart_cfg.get('beats_limit', 64)))
    heart_state.set_rmssd_config(
        width_seconds=int(heart_cfg.get('rmssd_width_s', 480)),
        side=int(heart_cfg.get('rmssd_side', 4)),
    )
    logger.info("Heart graphs initialized")

    # EDF recording: an independent listener on the same parsed lines, so a
    # session on disk holds exactly what the panels drew. Off by default in
    # config.conf because a recording folder that fills up is worse than none.
    edf_cfg = config.get('edf', {})
    if edf_cfg.get('enabled', False):
        recordings_dir = edf_cfg.get('folder', 'recordings')
        if not os.path.isabs(recordings_dir):
            recordings_dir = os.path.join(app_dir, recordings_dir)
        edf_recorder = EdfRecorder(
            folder=recordings_dir,
            prefix=edf_cfg.get('prefix', 'zrelay'),
            max_files=int(edf_cfg.get('max_files', 50)),
            enabled=True,
            fs_fallback=float(heart_cfg.get('fs_hz', 130)),
        )
        logger.info(f"EDF recording enabled -> {recordings_dir}")
    else:
        edf_recorder = None
        logger.info("EDF recording disabled")
    
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
                            global _last_message_monotonic
                            _last_message_monotonic = time.monotonic()

                            # Check for device events
                            event = data.get('event')
                            if event == 'device_seen':
                                # Liveness only: a device can report data
                                # without ever carrying an RSSI or a battery.
                                device = data.get('device')
                                timestamp_ms = data.get('timestamp_ms')
                                if device in device_status and timestamp_ms is not None:
                                    _mark_device_seen(device, timestamp_ms)
                            elif event == 'device_rssi':
                                device = data.get('device')
                                rssi = data.get('rssi')
                                timestamp_ms = data.get('timestamp_ms')
                                if device and rssi is not None:
                                    device_status[device]['rssi'] = rssi
                                    _mark_device_seen(device, timestamp_ms)
                            elif event == 'device_battery':
                                device = data.get('device')
                                battery = data.get('battery')
                                timestamp_ms = data.get('timestamp_ms')
                                if device and battery is not None:
                                    device_status[device]['battery'] = battery
                                    _mark_device_seen(device, timestamp_ms)
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
    
    init_dircon(config.get('dircon', {}))
    
    # The heart graphs see every parsed line, whole: an R-R batch must not be
    # split, and the raw listener is called before the metric queue is filled.
    serial_reader.raw_listeners.append(heart_state.on_message)

    # Same data, second consumer. Registered independently of the heart panels
    # so a session is still recorded when a strap sends no ECG at all.
    if edf_recorder is not None:
        serial_reader.raw_listeners.append(edf_recorder.on_message)

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


def shutdown_edf():
    """Finish the session in progress. Called from the SIGTERM/SIGINT handler."""
    if edf_recorder is not None:
        edf_recorder.close()


def _serialize_buffer_data(data: Dict[str, List]) -> Dict:
    result = {}
    for metric, points in data.items():
        result[metric] = {
            'timestamps_ms': [ts_ms for ts_ms, _ in points],
            'values': [val for _, val in points],
        }
    return result


# How long without a parsed dongle line before the UI stops claiming to be live.
DATA_STALE_AFTER_S = 5.0

# Set in process_serial_data() on every parsed message, in daemon wall time.
_last_message_monotonic = None


def data_staleness() -> Dict:
    """Report whether dongle data is still arriving, measured against the clock.

    The dongle's own timestamps cannot be used here: they restart at zero when
    it reboots, and that is exactly the case where they are least trustworthy.
    """
    if _last_message_monotonic is None:
        return {'data_stale': True, 'data_age_ms': None}

    age_s = time.monotonic() - _last_message_monotonic
    return {
        'data_stale': age_s > DATA_STALE_AFTER_S,
        'data_age_ms': int(age_s * 1000),
    }


@app.route('/api/data')
def get_data():
    """
    Get data for the specified time window.

    Query parameters:
        window: Time window in minutes (1, 5, or 60). Default: 5
        since_ms: If set, return only points with timestamp_ms > since_ms (delta).
                  Omit for a full snapshot of the current window.
    """
    window = request.args.get('window', '5', type=int)
    since_ms = request.args.get('since_ms', type=int)

    # Validate window
    if window not in [1, 5, 60]:
        window = 5

    if data_buffer is None:
        return jsonify({'error': 'Data buffer not initialized'}), 500

    if since_ms is not None:
        raw = data_buffer.get_data_delta(window, since_ms)
        delta = True
    else:
        raw = data_buffer.get_data_for_window(window)
        delta = False

    return jsonify({
        'data': _serialize_buffer_data(raw),
        'window_minutes': window,
        'latest_timestamp_ms': data_buffer.latest_timestamp_ms,
        'delta': delta,
        'timestamp': datetime.now().isoformat(),
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


@app.route('/api/edf/status')
def get_edf_status():
    """What the EDF recorder is doing: current session, last file, failures."""
    if edf_recorder is None:
        return jsonify({'enabled': False})
    return jsonify(edf_recorder.status())


@app.route('/api/edf/close', methods=['POST'])
def close_edf_session():
    """
    Finish the session in progress and start a new one on the next sample.

    Recording is enabled from `config.conf`; this exists so a session can be
    split without stopping the server, e.g. to keep one file per test case.
    """
    if edf_recorder is None:
        return jsonify({'error': 'EDF recording is disabled'}), 404
    edf_recorder.close()
    return jsonify(edf_recorder.status())


@app.route('/api/heart/status')
def get_heart_status():
    """
    What the heart graphs have accumulated.

    Counters rather than values: enough for the page to show whether a strap is
    reporting ECG, R–R, or only bpm, and why a panel may still be empty.
    """
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500
    return jsonify(heart_state.status())


@app.route('/api/heart/ecg')
def get_heart_ecg():
    """
    The scrolling ECG strip: the `window` mean-RRs of samples ending at the playhead.

    `frozen=1` keeps the playback clock with the data but returns no frame, so a
    paused strip resumes live instead of sliding to catch up.
    """
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500
    frozen = request.args.get('frozen') in ('1', 'true')
    frame = heart_state.ecg_frame(frozen=frozen)
    if frame is None:
        return '', 204
    return jsonify(frame)


@app.route('/api/heart/beats')
def get_heart_beats():
    """The beats stack: normalised waves on a shared time axis."""
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500
    return jsonify(heart_state.beats_frame())


@app.route('/api/heart/trends')
def get_heart_trends():
    """Heart rate and both RMSSDs over the selected 1-60 minute window."""
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500
    return jsonify(heart_state.trend_frame())


@app.route('/api/heart/rmssd')
def get_heart_rmssd():
    """The exponential RMSSD panel: tiles of RMSSD over time and window length."""
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500
    return jsonify(heart_state.rmssd_frame())


@app.route('/api/heart/config', methods=['POST'])
def set_heart_config():
    """
    Retune the heart panels, one control per request.

    Query parameters (all optional, only the ones present are applied):
        ecg_window:   ECG strip width, in mean RRs
        trend_width:  trends window, seconds
        beats_limit:  beats drawn in the stack
        rmssd_width:  RMSSD panel span, seconds
        rmssd_side:   RMSSD tiles per side
        reset:        "1" to drop every accumulated beat
    """
    if heart_state is None:
        return jsonify({'error': 'Heart state not initialized'}), 500

    if request.args.get('reset') in ('1', 'true'):
        heart_state.reset()
        return jsonify(heart_state.status())

    try:
        if request.args.get('ecg_window') is not None:
            heart_state.set_ecg_window(request.args.get('ecg_window', type=float))
        if request.args.get('trend_width') is not None:
            heart_state.set_trend_width(request.args.get('trend_width', type=int))
        if request.args.get('beats_limit') is not None:
            heart_state.set_beats_limit(request.args.get('beats_limit', type=int))
        if request.args.get('rmssd_width') is not None or request.args.get('rmssd_side') is not None:
            heart_state.set_rmssd_config(
                width_seconds=request.args.get('rmssd_width', type=int),
                side=request.args.get('rmssd_side', type=int),
            )
    except (TypeError, ValueError) as e:
        return jsonify({'error': f'Bad heart config: {e}'}), 400

    return jsonify(heart_state.status())


@app.route('/api/status')
def get_status():
    """Get server and connection status."""
    # Liveness is measured against the *host* clock, not the dongle's.
    #
    # It used to compare `last_seen_ms` with `data_buffer.latest_timestamp_ms`,
    # on the assumption that every dongle timestamp shares one clock. They do
    # not: ECG carries the firmware's own ECG sample clock (`ecg_t0_ms`), which
    # is anchored on the first frame of the connection and free-runs against the
    # sample count, while `hr` carries `k_uptime_get_32()` — true uptime at
    # arrival. The two drift apart (measured 21 minutes apart on a live link),
    # and both feed the `hr` device record, so `last_seen` alternated between a
    # value 21 minutes "behind" and one "ahead". Whichever arrived last decided
    # whether the LED was green, and it flapped every few seconds.
    #
    # Recording arrival time here makes the comparison clock-agnostic: it does
    # not matter which dongle clock stamped the packet, only that the packet
    # arrived.
    now_monotonic_ms = time.monotonic() * 1000.0

    timeout_ms = 5000  # 5 seconds - devices are considered connected if seen in last 5 seconds
    devices = {}
    for device_name, status in device_status.items():
        last_seen = status['last_seen_ms']
        seen_at = status.get('seen_monotonic_ms')
        if seen_at is not None:
            is_connected = (now_monotonic_ms - seen_at) < timeout_ms
        else:
            is_connected = False

        devices[device_name] = {
            'connected': is_connected,
            'rssi': status['rssi'],
            'battery': status['battery'],
            'last_seen_ms': last_seen
        }
    
    status = {
        'connected': serial_reader.connected if serial_reader else False,
        'running': serial_reader.running if serial_reader else False,
        'port': serial_reader.port if serial_reader else None,
        'devices': devices,
        'dircon': dircon_server.status() if dircon_server else None,
        'edf': edf_recorder.status() if edf_recorder else None
    }

    # Wall-clock staleness, not dongle time: 'connected' only means the serial
    # port is open, which is still true when the dongle has gone quiet or the
    # data clock stalled. Without this the page reports "Connected" forever
    # while every value on it is frozen.
    status.update(data_staleness())

    return jsonify(status)


# Initialize app on import
init_app()
