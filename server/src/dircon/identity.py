"""
Advertise the DirCon device under the dongle's own name and Bluetooth address (Z-Relay-XXXX),
the way it appeared to Zwift over Bluetooth.
"""
import logging
import threading
import time
from typing import Callable, Dict, Optional

from .gatt import Device
from .mdns import MdnsAdvertiser

logger = logging.getLogger(__name__)

INFO_REQUEST = '{"cmd":"info"}'
FALLBACK_NAME = 'Z-Relay'


class DongleIdentity:
    def __init__(self, device: Device, advertiser: MdnsAdvertiser, send_line: Callable[[str], bool],
                 fallback_mac: str, name: Optional[str] = None, mac_address: Optional[str] = None,
                 timeout_s: float = 15.0, request_interval_s: float = 2.0,
                 probe: bool = True):
        self.device = device
        self.advertiser = advertiser
        self.send_line = send_line
        self.fallback_mac = fallback_mac
        # Values from config.conf win over what the dongle reports
        self.name_override = name
        self.mac_override = mac_address
        self.timeout_s = timeout_s
        self.request_interval_s = request_interval_s

        self.dongle_name: Optional[str] = None
        self.dongle_mac: Optional[str] = None
        self._last_request = 0.0
        # The info probe is bounded. Firmware built without serial command
        # input receives the line and never drains it, which backs up the USB
        # CDC endpoint and wedges the whole data stream: the dongle goes
        # silent while still connected. A couple of tries is enough to tell
        # an old firmware from a new one.
        self._probe_enabled = probe
        self._probe_max = 3
        self._probe_count = 0
        self._changed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self.name_override and self.mac_override:
            self._changed.set()
        self._thread = threading.Thread(target=self._run, name='dircon-identity', daemon=True)
        self._thread.start()

    def on_serial_message(self, msg: Dict):
        """Called from the serial reader thread for every parsed JSON line."""
        if msg.get('type') == 'info':
            name, mac = msg.get('name') or None, msg.get('addr') or None
            if (name, mac) != (self.dongle_name, self.dongle_mac):
                self.dongle_name, self.dongle_mac = name, mac
                self._changed.set()
            return
        if not self._probe_enabled or self.dongle_name is not None:
            return
        if self._probe_count >= self._probe_max:
            return
        now = time.monotonic()
        if now - self._last_request >= self.request_interval_s:
            self._last_request = now
            self._probe_count += 1
            if self.send_line(INFO_REQUEST) is False:
                return
            if self._probe_count == self._probe_max:
                logger.warning("DirCon: dongle never answered the info request (firmware without "
                               "serial command input); falling back to '%s'", self.resolved()[0])

    def resolved(self):
        name = self.name_override or self.dongle_name or FALLBACK_NAME
        mac = self.mac_override or self.dongle_mac or self.fallback_mac
        return name, mac

    def _run(self):
        if not self._changed.wait(self.timeout_s):
            logger.warning("DirCon: dongle did not report its name within %.0fs (firmware without "
                           "the info command?), advertising as '%s'", self.timeout_s, self.resolved()[0])
        advertised = None
        while True:
            self._changed.clear()
            name, mac = self.resolved()
            if (name, mac) != advertised:
                if advertised is not None:
                    self.advertiser.unregister()
                self.device.name = name
                self.advertiser.mac_address = mac
                if self.advertiser.register():
                    advertised = (name, mac)
            self._changed.wait()
