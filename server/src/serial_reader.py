"""
Serial port reader for the Zwift dongle data.
"""
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

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
    
    @property
    def connected(self) -> bool:
        """Check if serial port is connected and open."""
        return self.serial_conn is not None and self.serial_conn.is_open
    
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
        
        # Connect to serial port
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )
            self.serial_conn.reset_input_buffer()
            logger.info(f"Connected to {self.port}")
        except serial.SerialException as e:
            logger.exception(f"Failed to connect: {e}")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
    
    def stop(self):
        """Stop reading."""
        self.running = False
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        if self.thread:
            self.thread.join(timeout=2)
        if self.log_file_handle:
            self.log_file_handle.close()
            self.log_file_handle = None
        logger.info("Serial reader stopped")
    
    def _read_loop(self):
        """Main reading loop."""
        while self.running:
            try:
                if self.serial_conn and self.serial_conn.is_open:
                    line = self.serial_conn.readline()
                    if line:
                        self._process_line(line)
                else:
                    # Try to reconnect
                    time.sleep(2)
                    try:
                        self.serial_conn = serial.Serial(
                            port=self.port,
                            baudrate=self.baudrate,
                            timeout=1.0,
                            xonxoff=False,
                            rtscts=False,
                            dsrdtr=False
                        )
                        self.serial_conn.reset_input_buffer()
                        logger.info(f"Reconnected to {self.port}")
                    except serial.SerialException:
                        pass
            except serial.SerialException as e:
                logger.error(f"Serial error: {e}")
                if self.serial_conn:
                    try:
                        self.serial_conn.close()
                    except:
                        pass
                    self.serial_conn = None
                time.sleep(2)
            except Exception as e:
                logger.exception(f"Error in read loop: {e}")
                time.sleep(1)
    
    def _process_line(self, line_bytes: bytes):
        """Process a line from the serial port."""
        try:
            line = line_bytes.decode('utf-8', errors='replace')
            
            # Log to file
            if self.log_file_handle:
                self.log_file_handle.write(line.rstrip('\r\n') + '\n')
            
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
        """Parse JSON and queue metrics."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return
        
        if not isinstance(data, dict):
            return
        
        msg_type = data.get('type')
        ts = data.get('ts', 0)
        
        if msg_type == 'hr':
            bpm = data.get('bpm')
            if bpm is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'heart_rate', 'value': float(bpm)})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({'event': 'device_rssi', 'device': 'hr', 'rssi': float(data['rssi']), 'timestamp_ms': ts})
        
        elif msg_type == 'cp':
            if data.get('power') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'power_meter_power', 'value': float(data['power'])})
            if data.get('cadence') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'power_meter_cadence', 'value': float(data['cadence'])})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({'event': 'device_rssi', 'device': 'cp', 'rssi': float(data['rssi']), 'timestamp_ms': ts})
        
        elif msg_type == 'ftms':
            if data.get('speed') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'trainer_speed', 'value': float(data['speed'])})
            if data.get('power') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'trainer_power', 'value': float(data['power'])})
            # Extract RSSI if present
            if data.get('rssi') is not None:
                self.data_queue.put({'event': 'device_rssi', 'device': 'ftms', 'rssi': float(data['rssi']), 'timestamp_ms': ts})
        
        elif msg_type == 'sim':
            if data.get('grade') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'sim_grade', 'value': float(data['grade'])})
            if data.get('resistance') is not None:
                self.data_queue.put({'timestamp_ms': ts, 'metric': 'sim_resistance', 'value': float(data['resistance'])})
