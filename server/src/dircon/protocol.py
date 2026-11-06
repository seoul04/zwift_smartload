"""
Wahoo DirCon (Direct Connect) wire protocol.

Every message is a 6-byte header followed by a payload:

    byte 0   message version (always 1)
    byte 1   message id (see MSG_*)
    byte 2   sequence number (responses echo the request's, notifications use 0)
    byte 3   response code (see RESP_*)
    byte 4-5 payload length, big-endian

UUIDs in payloads are always sent as full 128-bit values (16 raw bytes).
Reference implementation: qdomyos-zwift src/devices/dircon/dirconpacket.cpp
"""
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

HEADER_LENGTH = 6
MESSAGE_VERSION = 1

MSG_DISCOVER_SERVICES = 0x01
MSG_DISCOVER_CHARACTERISTICS = 0x02
MSG_READ_CHARACTERISTIC = 0x03
MSG_WRITE_CHARACTERISTIC = 0x04
MSG_ENABLE_NOTIFICATIONS = 0x05
MSG_UNSOLICITED_NOTIFICATION = 0x06
MSG_UNKNOWN_0x07 = 0x07
MSG_ERROR = 0xFF

RESP_SUCCESS = 0x00
RESP_UNKNOWN_MESSAGE_TYPE = 0x01
RESP_UNEXPECTED_ERROR = 0x02
RESP_SERVICE_NOT_FOUND = 0x03
RESP_CHARACTERISTIC_NOT_FOUND = 0x04
RESP_OPERATION_NOT_SUPPORTED = 0x05
RESP_WRITE_FAILED = 0x06
RESP_UNKNOWN_PROTOCOL = 0x07

PROP_READ = 0x01
PROP_WRITE = 0x02
PROP_NOTIFY = 0x04
PROP_INDICATE = 0x08

MSG_NAMES = {
    MSG_DISCOVER_SERVICES: 'discover_services',
    MSG_DISCOVER_CHARACTERISTICS: 'discover_characteristics',
    MSG_READ_CHARACTERISTIC: 'read',
    MSG_WRITE_CHARACTERISTIC: 'write',
    MSG_ENABLE_NOTIFICATIONS: 'enable_notifications',
    MSG_UNSOLICITED_NOTIFICATION: 'notification',
    MSG_UNKNOWN_0x07: 'unknown_0x07',
    MSG_ERROR: 'error',
}

# Bluetooth SIG base UUID 0000xxxx-0000-1000-8000-00805f9b34fb
_BASE_UUID_PREFIX = b'\x00\x00'
_BASE_UUID_SUFFIX = bytes.fromhex('00001000800000805f9b34fb')

UUID_LENGTH = 16

Uuid = Union[int, bytes]


def uuid16_to_bytes(uuid16: int) -> bytes:
    """Expand a 16-bit Bluetooth SIG UUID to its 16-byte form."""
    return _BASE_UUID_PREFIX + struct.pack('>H', uuid16) + _BASE_UUID_SUFFIX


def uuid_to_bytes(uuid: Uuid) -> bytes:
    if isinstance(uuid, int):
        return uuid16_to_bytes(uuid)
    if len(uuid) != UUID_LENGTH:
        raise ValueError(f"UUID must be {UUID_LENGTH} bytes, got {len(uuid)}")
    return bytes(uuid)


def bytes_to_uuid(raw: bytes) -> Uuid:
    """Return the 16-bit form when raw is a SIG base UUID, otherwise the raw 16 bytes."""
    raw = bytes(raw)
    if raw[:2] == _BASE_UUID_PREFIX and raw[4:] == _BASE_UUID_SUFFIX:
        return struct.unpack('>H', raw[2:4])[0]
    return raw


def uuid_str(uuid: Uuid) -> str:
    if isinstance(uuid, int):
        return f"0x{uuid:04X}"
    return uuid.hex()


def uuid16_to_string(uuid16: int) -> str:
    """Canonical text form, as used in the mDNS TXT record."""
    return f"0000{uuid16:04X}-0000-1000-8000-00805F9B34FB"


@dataclass
class Frame:
    msg_id: int
    seq: int = 0
    resp_code: int = RESP_SUCCESS
    payload: bytes = b''
    version: int = MESSAGE_VERSION

    def encode(self) -> bytes:
        if len(self.payload) > 0xFFFF:
            raise ValueError("DirCon payload too long")
        return struct.pack('>BBBBH', self.version, self.msg_id, self.seq & 0xFF,
                           self.resp_code, len(self.payload)) + self.payload

    @property
    def name(self) -> str:
        return MSG_NAMES.get(self.msg_id, f"0x{self.msg_id:02X}")

    def __str__(self) -> str:
        return (f"{self.name} seq={self.seq} resp={self.resp_code} "
                f"len={len(self.payload)} payload={self.payload.hex()}")


@dataclass
class FrameParser:
    """Accumulates TCP bytes and yields complete frames (handles split and coalesced reads)."""
    buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> List[Frame]:
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= HEADER_LENGTH:
            version, msg_id, seq, resp_code, length = struct.unpack_from('>BBBBH', self.buffer)
            total = HEADER_LENGTH + length
            if len(self.buffer) < total:
                break
            payload = bytes(self.buffer[HEADER_LENGTH:total])
            del self.buffer[:total]
            frames.append(Frame(msg_id=msg_id, seq=seq, resp_code=resp_code,
                                payload=payload, version=version))
        return frames


def split_uuid(payload: bytes) -> Tuple[Optional[Uuid], bytes]:
    """Split a payload into its leading UUID and the remaining bytes."""
    if len(payload) < UUID_LENGTH:
        return None, b''
    return bytes_to_uuid(payload[:UUID_LENGTH]), payload[UUID_LENGTH:]


def response(request: Frame, payload: bytes = b'', resp_code: int = RESP_SUCCESS) -> Frame:
    """Build the response frame for a request; error responses carry no payload."""
    if resp_code != RESP_SUCCESS:
        payload = b''
    return Frame(msg_id=request.msg_id, seq=request.seq, resp_code=resp_code, payload=payload)


def notification(uuid: Uuid, value: bytes) -> Frame:
    return Frame(msg_id=MSG_UNSOLICITED_NOTIFICATION, seq=0,
                 payload=uuid_to_bytes(uuid) + bytes(value))
