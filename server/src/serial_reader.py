"""
Serial port reader for the Zwift dongle data.
"""
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import serial


logger = logging.getLogger(__name__)


def rotate_log_file(log_file_path: str, max_backups: int = 5):
    """Rotate log file by renaming existing files."""
    if not log_file_path:
        return
    
    log_path = Path(log_file_path)
    if not log_path.exists():
        return
    
    # Delete oldest backup
    oldest = Path(f"{log_file_path}.{max_backups}")
    if oldest.exists():
        oldest.unlink()
    
    # Rotate: .4 -> .5, .3 -> .4, etc.
    for i in range(max_backups - 1, 0, -1):
        old = Path(f"{log_file_path}.{i}")
        new = Path(f"{log_file_path}.{i + 1}")
        if old.exists():
            old.rename(new)
    
    # Current -> .1
    try:
        log_path.rename(Path(f"{log_file_path}.1"))
    except Exception as e:
        logger.warning(f"Failed to rotate log file: {e}")


class SerialReader:
    """Reads and parses JSON data from the serial port."""
    
    def __init__(self, port: str, baudrate: int = 115200, 
                 data_queue: Optional[queue.Queue] = None, 
                 log_file: Optional[str] = None):
        self.port = port
        self.baudrate = baudrate
        self.data_queue = data_queue or queue.Queue()
        self.serial_conn: Optional[serial.Serial] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.log_file = log_file
        self.log_file_handle = None
        # Callables receiving every parsed JSON dict (called from the reader thread)
        self.raw_listeners: List[Callable[[dict], None]] = []
        self._write_lock = threading.Lock()
    
    @property
    def connected(self) -> bool:
        """Check if serial port is connected and open."""
        return self.serial_conn is not None and self.serial_conn.is_open
    
    def _close_port(self):
        """Fully close and release the serial port."""
        if self.serial_conn is not None:
            try:
                if self.serial_conn.is_open:
                    self.serial_conn.cancel_read()
            except Exception:
                pass
            try:
                self.serial_conn.close()
            except Exception:
                pass
            # Delete the object to fully release the file descriptor
            self.serial_conn = None
            # Small delay to let the OS release the port
            time.sleep(2)
            logger.debug("Serial port released")
    
    def start(self):
        """Start reading from the serial port."""
        if self.running:
            return
        
        # Open log file
        if self.log_file:
            try:
                rotate_log_file(self.log_file)
                Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
                self.log_file_handle = open(self.log_file, 'a', encoding='utf-8', buffering=1)
                logger.info(f"Logging to {self.log_file}")
            except Exception as e:
                logger.exception(f"Failed to open log file: {e}")
        
        # Start read loop thread (it will connect and retry every 2s if port unavailable)
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
    
    def stop(self):
        """Stop reading."""
        self.running = False
        self._close_port()
        if self.thread:
            self.thread.join(timeout=2)
        if self.log_file_handle:
            self.log_file_handle.close()
            self.log_file_handle = None
        logger.info("Serial reader stopped")
    
    def write_line(self, line: str) -> bool:
        """Send one command line to the dongle; returns False if the port is not open."""
        with self._write_lock:
            conn = self.serial_conn
            if conn is None or not conn.is_open:
                return False
            try:
                conn.write(line.rstrip('\n').encode('utf-8') + b'\n')
                conn.flush()
            except serial.SerialTimeoutException:
                logger.warning("Serial write timed out: the dongle is not "
                               "reading commands (firmware without serial input?)")
                return False
            except (serial.SerialException, OSError) as e:
                logger.warning(f"Serial write failed: {e}")
                return False
        if self.log_file_handle:
            self.log_file_handle.write(f">> {line.rstrip()}\n")
        return True
    
    def _read_loop(self):
        """Main reading loop."""
        while self.running:
            try:
                if self.serial_conn and self.serial_conn.is_open:
                    line = self.serial_conn.readline()
                    if line:
                        # readline() gives up at the timeout, so a long batch
                        # can arrive as two reads. Handing either half on would
                        # throw the message away (unparseable JSON), so a
                        # fragment waits for its other half here.
                        if not line.endswith(b'\n'):
                            self._partial = getattr(self, '_partial', b'') + line
                            continue
                        if getattr(self, '_partial', b''):
                            line = self._partial + line
                            self._partial = b''
                        self._process_line(line)
                else:
                    # Ensure port is fully released before reconnecting
                    self._close_port()
                    logger.info(f"Connecting to {self.port}...")
                    time.sleep(2)
                    try:
                        self.serial_conn = serial.Serial(
                            port=self.port,
                            baudrate=self.baudrate,
                            timeout=1.0,
                            # A dongle whose CDC endpoint nobody drains must not
                            # block the writer forever: control commands are
                            # sent from the DirCon event loop, and an
                            # unbounded write there wedges every client.
                            write_timeout=1.0,
                            xonxoff=False,
                            rtscts=False,
                            dsrdtr=False
                        )
                        # No reset_input_buffer(): it can drop a message that is
                        # half-delivered at the moment the port opens, which is
                        # exactly what a reboot leaves behind. Stale bytes are
                        # harmless — they fail to parse and are skipped.
                        self._partial = b''
                        logger.info(f"Connected to {self.port}")
                    except serial.SerialException as e:
                        logger.warning(f"Could not connect: {e}. Retrying in 2 seconds.")
            except serial.SerialException as e:
                logger.error(f"Serial error: {e}")
                self._close_port()
                time.sleep(2)
            except Exception as e:
                logger.exception(f"Error in read loop: {e}")
                time.sleep(1)
    
    def _process_line(self, line_bytes: bytes):
        """Process a line from the serial port."""
        try:
            line = line_bytes.decode('utf-8', errors='replace')
            
            # Log to file. Flushed every line so `tail -f` on the log reflects
            # what the dongle is saying right now — an unflushed buffer reads as
            # "the dongle went silent" when in fact data is arriving.
            if self.log_file_handle:
                self.log_file_handle.write(line.rstrip('\r\n') + '\n')
                self.log_file_handle.flush()
            
            # Extract and parse JSON
            stripped = line.strip()
            if not stripped:
                return
            
            start = stripped.find('{')
            end = stripped.rfind('}')
            if start != -1 and end > start:
                self._process_json(stripped[start:end + 1])
        except Exception as e:
            logger.exception(f"Error processing line: {e}")
    
    def _process_json(self, json_str: str):
        """Parse JSON and queue metrics and device status events."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return
        
        if not isinstance(data, dict):
            return
        
        for listener in self.raw_listeners:
            try:
                listener(data)
            except Exception as e:
                logger.exception(f"Raw listener failed: {e}")
        
        msg_type = data.get('type')
        ts = data.get('ts', 0)
        
        if msg_type == 'hr':
            bpm = data.get('bpm')
            if bpm is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'heart_rate', 'value': float(bpm)})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({
                    'event': 'device_rssi',
                    'device': 'hr',
                    'rssi': float(data['rssi']),
                    'timestamp_ms': ts
                })
            # Extract battery level if present
            if data.get('battery_level') is not None:
                self.data_queue.put({
                    'event': 'device_battery',
                    'device': 'hr',
                    'battery': float(data['battery_level']),
                    'timestamp_ms': ts
                })

        elif msg_type == 'ecg':
            # ECG is a second heart-rate stream, not a separate device: a strap
            # that reports ECG may never send a `hr` line at all (the Movesense
            # exposes R-R as a per-beat resource), so without this the dashboard
            # would show no sensor while the trace scrolls perfectly well.
            self.data_queue.put({'event': 'device_seen', 'device': 'hr', 'timestamp_ms': ts})
            if data.get('rssi') is not None:
                self.data_queue.put({
                    'event': 'device_rssi',
                    'device': 'hr',
                    'rssi': float(data['rssi']),
                    'timestamp_ms': ts
                })

        elif msg_type == 'cp':
            # Liveness, whether or not the packet carries an RSSI (a replayed
            # session never does). Without this the only paths that mark the
            # power meter as seen are `device_rssi`/`device_battery`, so a
            # recording streamed power and cadence while the header LED stayed
            # grey: the data was live and the device read as absent.
            self.data_queue.put({'event': 'device_seen', 'device': 'cp', 'timestamp_ms': ts})
            if data.get('power') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'power_meter_power', 'value': float(data['power'])})
            if data.get('cadence') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'power_meter_cadence', 'value': float(data['cadence'])})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({
                    'event': 'device_rssi',
                    'device': 'cp',
                    'rssi': float(data['rssi']),
                    'timestamp_ms': ts
                })
            # Extract battery level if present
            if data.get('battery_level') is not None:
                self.data_queue.put({
                    'event': 'device_battery',
                    'device': 'cp',
                    'battery': float(data['battery_level']),
                    'timestamp_ms': ts
                })
        
        elif msg_type == 'ftms':
            # Liveness for the trainer, for the same reason as `cp` above.
            self.data_queue.put({'event': 'device_seen', 'device': 'ftms', 'timestamp_ms': ts})
            if data.get('speed') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'trainer_speed', 'value': float(data['speed'])})
            if data.get('power') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'trainer_power', 'value': float(data['power'])})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({
                    'event': 'device_rssi',
                    'device': 'ftms',
                    'rssi': float(data['rssi']),
                    'timestamp_ms': ts
                })
            # Note: FTMS packet currently has no battery_level in your sample
        
        elif msg_type == 'sim':
            if data.get('grade') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'sim_grade', 'value': float(data['grade'])})
            if data.get('resistance') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'sim_resistance', 'value': float(data['resistance'])})
