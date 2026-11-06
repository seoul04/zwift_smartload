"""
Virtual GATT devices exposed over DirCon, and the BLE packets rebuilt from the dongle's JSON lines.

Characteristic values and feature bits mirror qdomyos-zwift's "Wahoo KICKR" / "Wahoo HRM"
emulation (src/devices/dircon/dirconmanager.cpp), which Zwift is known to accept.
"""
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .protocol import PROP_INDICATE, PROP_NOTIFY, PROP_READ, PROP_WRITE

# Services
SVC_HEART_RATE = 0x180D
SVC_CYCLING_POWER = 0x1818
SVC_FTMS = 0x1826

# Characteristics
CHAR_HR_MEASUREMENT = 0x2A37
CHAR_BODY_SENSOR_LOCATION = 0x2A38
CHAR_CP_MEASUREMENT = 0x2A63
CHAR_CP_FEATURE = 0x2A65
CHAR_SENSOR_LOCATION = 0x2A5D
CHAR_FTMS_FEATURE = 0x2ACC
CHAR_INDOOR_BIKE_DATA = 0x2AD2
CHAR_TRAINING_STATUS = 0x2AD3
CHAR_RESISTANCE_RANGE = 0x2AD6
CHAR_POWER_RANGE = 0x2AD8
CHAR_FTMS_CONTROL_POINT = 0x2AD9
CHAR_FTMS_STATUS = 0x2ADA

# FTMS control point opcodes and result codes
FTMS_REQUEST_CONTROL = 0x00
FTMS_RESET = 0x01
FTMS_SET_TARGET_RESISTANCE = 0x04
FTMS_SET_TARGET_POWER = 0x05
FTMS_START_RESUME = 0x07
FTMS_STOP_PAUSE = 0x08
FTMS_SET_INDOOR_BIKE_SIM = 0x11
FTMS_RESPONSE_CODE = 0x80

FTMS_RESULT_SUCCESS = 0x01
FTMS_RESULT_NOT_SUPPORTED = 0x02
FTMS_RESULT_INVALID_PARAMETER = 0x03
FTMS_RESULT_FAILED = 0x04

# Same rule as the dongle's CP_TIMEOUT_MS: power meter power wins while it is fresh
CP_FRESH_S = 3.0


@dataclass
class Characteristic:
    uuid: int
    props: int
    value: bytes = b''


@dataclass
class Service:
    uuid: int
    characteristics: List[Characteristic]


@dataclass
class Device:
    """One DirCon endpoint: its own TCP port and mDNS record."""
    name: str
    port: int
    serial_number: str
    services: List[Service]

    def find_service(self, uuid) -> Optional[Service]:
        return next((s for s in self.services if s.uuid == uuid), None)

    def find_characteristic(self, uuid) -> Optional[Characteristic]:
        for service in self.services:
            for char in service.characteristics:
                if char.uuid == uuid:
                    return char
        return None

    @property
    def characteristic_uuids(self) -> Set[int]:
        return {c.uuid for s in self.services for c in s.characteristics}


def ftms_service() -> Service:
    return Service(SVC_FTMS, [
        # Machine features: cadence, total distance, resistance, HR, power measurement;
        # target settings: resistance, power, indoor bike simulation, wheel circumference, spin down
        Characteristic(CHAR_FTMS_FEATURE, PROP_READ, bytes.fromhex('835400000ce00000')),
        # Resistance level 1.0 .. 15.0, increment 1.0 (0.1 units)
        Characteristic(CHAR_RESISTANCE_RANGE, PROP_READ, bytes.fromhex('0a0096000a00')),
        # Power 0 .. 2000 W, increment 1 W
        Characteristic(CHAR_POWER_RANGE, PROP_READ, struct.pack('<hhH', 0, 2000, 1)),
        Characteristic(CHAR_FTMS_CONTROL_POINT, PROP_WRITE | PROP_INDICATE),
        Characteristic(CHAR_INDOOR_BIKE_DATA, PROP_NOTIFY),
        Characteristic(CHAR_TRAINING_STATUS, PROP_READ, bytes.fromhex('0001')),
        Characteristic(CHAR_FTMS_STATUS, PROP_NOTIFY),
    ])


def cycling_power_service() -> Service:
    return Service(SVC_CYCLING_POWER, [
        # Crank revolution data supported
        Characteristic(CHAR_CP_FEATURE, PROP_READ, bytes.fromhex('08000000')),
        # Rear hub
        Characteristic(CHAR_SENSOR_LOCATION, PROP_READ, bytes.fromhex('0d')),
        Characteristic(CHAR_CP_MEASUREMENT, PROP_NOTIFY),
    ])


def heart_rate_service() -> Service:
    return Service(SVC_HEART_RATE, [
        Characteristic(CHAR_HR_MEASUREMENT, PROP_NOTIFY),
        # Chest
        Characteristic(CHAR_BODY_SENSOR_LOCATION, PROP_READ, bytes.fromhex('01')),
    ])


def build_device(name: str, port: int) -> Device:
    """Trainer, power meter and heart rate behind one name, like the dongle over Bluetooth."""
    return Device(name, port, '0', [ftms_service(), cycling_power_service(), heart_rate_service()])


def _u16(value) -> int:
    return int(value) & 0xFFFF


def _s16(value) -> int:
    return max(-32768, min(32767, int(round(value))))


@dataclass
class SensorState:
    """Latest values decoded from the dongle's JSON lines (monotonic timestamps in seconds)."""
    stale_timeout_s: float = 3.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    hr_bpm: Optional[int] = None
    hr_time: Optional[float] = None

    cp_power: int = 0
    cp_cadence: Optional[float] = None
    cp_crank_revs: Optional[int] = None
    cp_crank_time: Optional[int] = None
    cp_balance: Optional[int] = None
    cp_flags: int = 0
    cp_time: Optional[float] = None

    ftms_speed: int = 0
    ftms_cadence: Optional[float] = None
    ftms_power: Optional[int] = None
    ftms_time: Optional[float] = None

    def update(self, msg: Dict, now: Optional[float] = None) -> Set[int]:
        """Apply one dongle JSON message; return the characteristics whose value changed."""
        now = time.monotonic() if now is None else now
        msg_type = msg.get('type')
        with self.lock:
            if msg_type == 'hr' and msg.get('bpm') is not None:
                self.hr_bpm = int(msg['bpm'])
                self.hr_time = now
                return {CHAR_HR_MEASUREMENT}
            if msg_type == 'cp' and msg.get('power') is not None:
                self.cp_power = int(msg['power'])
                self.cp_flags = int(msg.get('flags') or 0)
                self.cp_balance = msg.get('balance')
                if msg.get('crank_revs') is not None and msg.get('crank_time') is not None:
                    self.cp_crank_revs = int(msg['crank_revs'])
                    self.cp_crank_time = int(msg['crank_time'])
                if msg.get('cadence') is not None:
                    self.cp_cadence = float(msg['cadence'])
                self.cp_time = now
                return {CHAR_CP_MEASUREMENT, CHAR_INDOOR_BIKE_DATA}
            if msg_type == 'ftms':
                self.ftms_speed = int(msg.get('speed') or 0)
                self.ftms_cadence = float(msg['cadence']) if msg.get('cadence') is not None else None
                self.ftms_power = int(msg['power']) if msg.get('power') is not None else None
                self.ftms_time = now
                return {CHAR_INDOOR_BIKE_DATA}
        return set()

    def _fresh(self, t: Optional[float], now: float, timeout: Optional[float] = None) -> bool:
        timeout = self.stale_timeout_s if timeout is None else timeout
        return t is not None and (now - t) < timeout

    def build(self, uuid: int, now: Optional[float] = None) -> Optional[bytes]:
        now = time.monotonic() if now is None else now
        builder = {
            CHAR_HR_MEASUREMENT: self.build_hr,
            CHAR_CP_MEASUREMENT: self.build_cp,
            CHAR_INDOOR_BIKE_DATA: self.build_indoor_bike_data,
        }.get(uuid)
        return builder(now) if builder else None

    def build_hr(self, now: float) -> Optional[bytes]:
        """Heart Rate Measurement (0x2A37); nothing while the strap is silent."""
        with self.lock:
            if self.hr_bpm is None or not self._fresh(self.hr_time, now):
                return None
            bpm = max(0, self.hr_bpm)
        if bpm > 255:
            return struct.pack('<BH', 0x01, min(bpm, 0xFFFF))
        return struct.pack('<BB', 0x00, bpm)

    def build_cp(self, now: float) -> Optional[bytes]:
        """Cycling Power Measurement (0x2A63) with the power meter's own crank counters."""
        with self.lock:
            if self.cp_time is None:
                return None
            fresh = self._fresh(self.cp_time, now)
            power = self.cp_power if fresh else 0
            flags = 0
            body = b''
            if self.cp_balance is not None and fresh:
                flags |= 0x01 | (self.cp_flags & 0x02)
                body += struct.pack('<B', int(self.cp_balance) & 0xFF)
            if self.cp_crank_revs is not None:
                flags |= 0x20
                body += struct.pack('<HH', _u16(self.cp_crank_revs), _u16(self.cp_crank_time))
        return struct.pack('<Hh', flags, _s16(power)) + body

    def build_indoor_bike_data(self, now: float) -> Optional[bytes]:
        """FTMS Indoor Bike Data (0x2AD2): speed, cadence, power and, when known, heart rate."""
        with self.lock:
            if self.ftms_time is None and self.cp_time is None:
                return None
            ftms_fresh = self._fresh(self.ftms_time, now)
            cp_fresh = self._fresh(self.cp_time, now, min(CP_FRESH_S, self.stale_timeout_s))

            speed = self.ftms_speed if ftms_fresh else 0

            if ftms_fresh and self.ftms_cadence is not None:
                cadence = self.ftms_cadence
            elif cp_fresh and self.cp_cadence is not None:
                cadence = self.cp_cadence
            else:
                cadence = 0

            if cp_fresh:
                power = self.cp_power
            elif ftms_fresh and self.ftms_power is not None:
                power = self.ftms_power
            else:
                power = 0

            hr = self.hr_bpm if self._fresh(self.hr_time, now) else None

        flags = 0x0004 | 0x0040
        body = struct.pack('<HHh', _u16(speed), _u16(round(cadence * 2)), _s16(power))
        if hr is not None:
            flags |= 0x0200
            body += struct.pack('<B', max(0, min(255, hr)))
        return struct.pack('<H', flags) + body

    def snapshot(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        with self.lock:
            return {
                'hr_fresh': self._fresh(self.hr_time, now),
                'cp_fresh': self._fresh(self.cp_time, now),
                'ftms_fresh': self._fresh(self.ftms_time, now),
            }


@dataclass
class ControlCommand:
    opcode: int
    reply: bytes
    wind_speed: Optional[int] = None  # 0.001 m/s
    grade: Optional[int] = None       # 0.01 %
    crr: Optional[int] = None         # 0.0001
    cw: Optional[int] = None          # 0.01 kg/m
    target_power: Optional[int] = None
    target_resistance: Optional[int] = None


_ACKNOWLEDGED_OPCODES = {
    FTMS_REQUEST_CONTROL, FTMS_RESET, FTMS_SET_TARGET_RESISTANCE, FTMS_SET_TARGET_POWER,
    FTMS_START_RESUME, FTMS_STOP_PAUSE, FTMS_SET_INDOOR_BIKE_SIM,
}

_MIN_LENGTH = {
    FTMS_SET_TARGET_RESISTANCE: 2,
    FTMS_SET_TARGET_POWER: 3,
    FTMS_SET_INDOOR_BIKE_SIM: 7,
}


def parse_control_point(data: bytes) -> Optional[ControlCommand]:
    """Decode an FTMS control point write and build the local response ([0x80, opcode, result])."""
    if not data:
        return None
    opcode = data[0]

    def reply(result: int) -> bytes:
        return bytes([FTMS_RESPONSE_CODE, opcode, result])

    if opcode not in _ACKNOWLEDGED_OPCODES:
        return ControlCommand(opcode, reply(FTMS_RESULT_NOT_SUPPORTED))
    if len(data) < _MIN_LENGTH.get(opcode, 1):
        return ControlCommand(opcode, reply(FTMS_RESULT_INVALID_PARAMETER))

    cmd = ControlCommand(opcode, reply(FTMS_RESULT_SUCCESS))
    if opcode == FTMS_SET_INDOOR_BIKE_SIM:
        cmd.wind_speed, cmd.grade, cmd.crr, cmd.cw = struct.unpack_from('<hhBB', data, 1)
    elif opcode == FTMS_SET_TARGET_POWER:
        cmd.target_power = struct.unpack_from('<h', data, 1)[0]
    elif opcode == FTMS_SET_TARGET_RESISTANCE:
        cmd.target_resistance = data[1]
    return cmd
