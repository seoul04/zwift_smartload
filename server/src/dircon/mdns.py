"""
mDNS advertisement of the DirCon devices (_wahoo-fitness-tnp._tcp), which is how Zwift finds them.
"""
import logging
import socket
from typing import List, Optional

from .gatt import Device
from .protocol import uuid16_to_string

logger = logging.getLogger(__name__)

SERVICE_TYPE = '_wahoo-fitness-tnp._tcp.local.'


def detect_lan_ip() -> str:
    """IPv4 address of the interface holding the default route (no packet is actually sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('10.255.255.255', 1))
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def txt_properties(device: Device, mac_address: str) -> dict:
    return {
        'ble-service-uuids': ','.join(uuid16_to_string(s.uuid) for s in device.services),
        'mac-address': mac_address,
        'serial-number': device.serial_number,
    }


class MdnsAdvertiser:
    def __init__(self, devices: List[Device], mac_address: str, ip: Optional[str] = None):
        self.devices = devices
        self.mac_address = mac_address
        self.ip = ip if ip and ip != '0.0.0.0' else detect_lan_ip()
        self._registrations = []

    def register(self) -> bool:
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError:
            logger.error("python-zeroconf is not installed; DirCon devices will not be discoverable")
            return False

        try:
            for device in self.devices:
                # One responder per device: Zwift only reads the TXT record of the last service
                # when several are answered in the same packet.
                zc = Zeroconf(ip_version=IPVersion.V4Only)
                # MyWhoosh matches the SRV target against the instance name with spaces hyphenated
                hostname = device.name.replace(' ', '-') + '.local.'
                info = ServiceInfo(
                    SERVICE_TYPE,
                    f"{device.name}.{SERVICE_TYPE}",
                    addresses=[socket.inet_aton(self.ip)],
                    port=device.port,
                    properties=txt_properties(device, self.mac_address),
                    server=hostname,
                )
                # strict=False: "wahoo-fitness-tnp" exceeds the RFC 6763 15-byte service name limit
                self._registrations.append((zc, info))
                zc.register_service(info, allow_name_change=True, strict=False)
                logger.info("mDNS: advertised '%s' at %s:%d", device.name, self.ip, device.port)
            return True
        except Exception as e:
            logger.error("mDNS registration failed: %s", e)
            return False

    def unregister(self):
        for zc, info in self._registrations:
            try:
                zc.unregister_service(info)
            except Exception:
                pass
            zc.close()
        self._registrations = []
