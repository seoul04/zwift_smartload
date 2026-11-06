"""
DirCon TCP server: answers Zwift's GATT-like requests and pushes sensor notifications.

Runs its own asyncio loop in a daemon thread so it can live next to Flask and the serial reader.
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from . import protocol as p
from .gatt import (CHAR_CP_MEASUREMENT, CHAR_FTMS_CONTROL_POINT, CHAR_HR_MEASUREMENT,
                   CHAR_INDOOR_BIKE_DATA, FTMS_REQUEST_CONTROL, FTMS_RESET, FTMS_RESPONSE_CODE,
                   FTMS_RESULT_FAILED, FTMS_RESULT_SUCCESS, ControlCommand, Device, SensorState, parse_control_point)

logger = logging.getLogger(__name__)

STREAMED_CHARACTERISTICS = (CHAR_INDOOR_BIKE_DATA, CHAR_CP_MEASUREMENT, CHAR_HR_MEASUREMENT)


@dataclass(eq=False)
class ClientSession:
    device: Device
    peer: str
    writer: Optional[asyncio.StreamWriter] = None
    parser: p.FrameParser = field(default_factory=p.FrameParser)
    subscriptions: Set = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    frame_count: int = 0

    def send(self, frame: p.Frame) -> bool:
        if self.writer is None or self.writer.is_closing():
            return False
        logger.debug("DirCon -> %s [%s] %s", self.peer, self.device.name, frame)
        self.writer.write(frame.encode())
        return True


class DirconServer:
    def __init__(self, devices: List[Device], state: SensorState, bind_ip: str = '0.0.0.0',
                 notify_interval_s: float = 1.0, min_notify_interval_s: float = 0.2,
                 notify_subscribed_only: bool = True,
                 on_control: Optional[Callable[[ControlCommand], None]] = None,
                 command_sender: Optional[Callable[[str], bool]] = None,
                 control_timeout_s: float = 2.0):
        self.devices = devices
        self.state = state
        self.bind_ip = bind_ip
        self.notify_interval_s = notify_interval_s
        self.min_notify_interval_s = min_notify_interval_s
        self.notify_subscribed_only = notify_subscribed_only
        self.on_control = on_control
        # When set, control point writes go to the trainer through the dongle instead of
        # being acknowledged locally
        self.command_sender = command_sender
        self.control_timeout_s = control_timeout_s

        self.sessions: List[ClientSession] = []
        self.last_control: Optional[Dict] = None
        self._control_lock: Optional[asyncio.Lock] = None
        self._pending_control: Optional[asyncio.Future] = None
        self._pending_opcode: Optional[int] = None
        self._last_sent: Dict[int, float] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._servers: List[asyncio.base_events.Server] = []
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 5.0) -> bool:
        """Start the loop thread and bind every device port; returns False if binding failed."""
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name='dircon', daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._start_error is not None:
            logger.error("DirCon server failed to start: %s", self._start_error)
            return False
        return self._ready.is_set()

    def stop(self):
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_servers())
        except Exception as e:
            self._start_error = e
            self._ready.set()
            return
        self._loop.create_task(self._ticker())
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            for server in self._servers:
                server.close()
            for session in self.sessions:
                if session.writer:
                    session.writer.close()
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._loop.close()

    async def _start_servers(self):
        for device in self.devices:
            server = await asyncio.start_server(
                lambda r, w, d=device: self._handle_client(r, w, d),
                host=self.bind_ip, port=device.port)
            self._servers.append(server)
            logger.info("DirCon '%s' listening on %s:%d", device.name, self.bind_ip, device.port)

    # ------------------------------------------------------------------ serial side

    def on_serial_message(self, msg: Dict):
        """Called from the serial reader thread for every parsed JSON line."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._apply_serial_message, msg)

    def _apply_serial_message(self, msg: Dict):
        if msg.get('type') == 'cp_resp':
            self._resolve_control(msg.get('data', ''))
            return
        now = time.monotonic()
        for uuid in self.state.update(msg, now):
            if now - self._last_sent.get(uuid, 0) >= self.min_notify_interval_s:
                self._push(uuid, now)

    async def _ticker(self):
        """Resend the latest values so Zwift keeps the sensors alive between serial updates."""
        while True:
            await asyncio.sleep(self.notify_interval_s / 2)
            now = time.monotonic()
            for uuid in STREAMED_CHARACTERISTICS:
                if now - self._last_sent.get(uuid, 0) >= self.notify_interval_s:
                    self._push(uuid, now)

    def _push(self, uuid: int, now: float):
        value = self.state.build(uuid, now)
        if value is None:
            return
        self._last_sent[uuid] = now
        self.broadcast(uuid, value)

    def broadcast(self, uuid: int, value: bytes):
        frame = p.notification(uuid, value)
        for session in list(self.sessions):
            if uuid not in session.device.characteristic_uuids:
                continue
            if self.notify_subscribed_only and uuid not in session.subscriptions:
                continue
            try:
                session.send(frame)
            except Exception as e:
                logger.warning("DirCon notify to %s failed: %s", session.peer, e)

    # ------------------------------------------------------------------ client side

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             device: Device):
        peername = writer.get_extra_info('peername')
        peer = f"{peername[0]}:{peername[1]}" if peername else '?'
        session = ClientSession(device=device, peer=peer, writer=writer)
        self.sessions.append(session)
        logger.info("DirCon client %s connected to '%s'", peer, device.name)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    logger.warning("DirCon client %s sent EOF after %d frames (closed by peer)",
                                   peer, session.frame_count)
                    break
                if session.frame_count == 0:
                    logger.warning("DirCon client %s first bytes (%d): %s", peer, len(data), data[:32].hex())
                for frame in session.parser.feed(data):
                    session.frame_count += 1
                    logger.debug("DirCon <- %s [%s] %s", peer, device.name, frame)
                    for out in await self.handle_frame(session, frame):
                        session.send(out)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("DirCon client %s error", peer)
        finally:
            self.sessions.remove(session)
            writer.close()
            logger.info("DirCon client %s disconnected from '%s'", peer, device.name)

    async def handle_frame(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        """Process one request and return the frames to send back, response first."""
        if frame.resp_code != p.RESP_SUCCESS or frame.msg_id == p.MSG_UNSOLICITED_NOTIFICATION:
            return []

        handler = {
            p.MSG_DISCOVER_SERVICES: self._discover_services,
            p.MSG_DISCOVER_CHARACTERISTICS: self._discover_characteristics,
            p.MSG_READ_CHARACTERISTIC: self._read,
            p.MSG_WRITE_CHARACTERISTIC: self._write,
            p.MSG_ENABLE_NOTIFICATIONS: self._enable_notifications,
            p.MSG_UNKNOWN_0x07: self._unknown_0x07,
        }.get(frame.msg_id)
        if handler is None:
            return [p.response(frame, resp_code=p.RESP_UNKNOWN_MESSAGE_TYPE)]
        try:
            return await handler(session, frame)
        except Exception:
            logger.exception("DirCon error handling %s", frame)
            return [p.response(frame, resp_code=p.RESP_UNEXPECTED_ERROR)]

    async def _discover_services(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        payload = b''.join(p.uuid_to_bytes(s.uuid) for s in session.device.services)
        return [p.response(frame, payload)]

    async def _discover_characteristics(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        uuid, _ = p.split_uuid(frame.payload)
        service = session.device.find_service(uuid) if uuid is not None else None
        if service is None:
            return [p.response(frame, resp_code=p.RESP_SERVICE_NOT_FOUND)]
        payload = p.uuid_to_bytes(service.uuid) + b''.join(
            p.uuid_to_bytes(c.uuid) + bytes([c.props]) for c in service.characteristics)
        return [p.response(frame, payload)]

    async def _read(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        uuid, _ = p.split_uuid(frame.payload)
        char = session.device.find_characteristic(uuid) if uuid is not None else None
        if char is None:
            return [p.response(frame, resp_code=p.RESP_CHARACTERISTIC_NOT_FOUND)]
        if not char.props & p.PROP_READ:
            return [p.response(frame, resp_code=p.RESP_OPERATION_NOT_SUPPORTED)]
        return [p.response(frame, p.uuid_to_bytes(char.uuid) + char.value)]

    async def _write(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        uuid, data = p.split_uuid(frame.payload)
        char = session.device.find_characteristic(uuid) if uuid is not None else None
        if char is None:
            return [p.response(frame, resp_code=p.RESP_CHARACTERISTIC_NOT_FOUND)]
        if not char.props & p.PROP_WRITE:
            return [p.response(frame, resp_code=p.RESP_OPERATION_NOT_SUPPORTED)]
        if char.uuid != CHAR_FTMS_CONTROL_POINT or not data:
            return [p.response(frame, resp_code=p.RESP_WRITE_FAILED)]

        cmd = parse_control_point(data)
        reply = await self.process_control(cmd, data)
        # Zwift never subscribes to the control point over DirCon but waits for the
        # indication anyway, so it always goes to the writer.
        return [p.response(frame, p.uuid_to_bytes(char.uuid) + reply),
                p.notification(char.uuid, reply)]

    async def process_control(self, cmd: ControlCommand, data: bytes) -> bytes:
        """Return the FTMS control point response for a write: the trainer's, or a local ack."""
        if self.command_sender is None:
            reply = cmd.reply
        else:
            reply = await self._forward_control(data)
            if data[0] == FTMS_REQUEST_CONTROL and not self._succeeded(reply):
                reply = await self._retake_control(data)
        logger.info("DirCon control point write %s -> reply %s", data.hex(), reply.hex())
        self._record_control(cmd, data)
        return reply

    async def _forward_control(self, data: bytes) -> bytes:
        """Send the command to the dongle and wait for the trainer's cp_resp (one at a time)."""
        failed = bytes([FTMS_RESPONSE_CODE, data[0], FTMS_RESULT_FAILED])
        if self._control_lock is None:
            self._control_lock = asyncio.Lock()
        async with self._control_lock:
            future = asyncio.get_running_loop().create_future()
            self._pending_control = future
            self._pending_opcode = data[0]
            deadline = time.monotonic() + self.control_timeout_s
            try:
                line = json.dumps({'cmd': 'ftms_cp', 'data': data.hex()}, separators=(',', ':'))
                # Zwift reconnects faster than the serial port reopens after a restart.
                # The write goes to a worker thread: a stalled CDC endpoint must not
                # block this event loop, which serves every DirCon client.
                loop = asyncio.get_running_loop()
                while not await loop.run_in_executor(None, self.command_sender, line):
                    if time.monotonic() >= deadline:
                        logger.warning("DirCon: dongle serial port not available for control command")
                        return failed
                    await asyncio.sleep(0.1)
                return await asyncio.wait_for(future, max(deadline - time.monotonic(), 0.01))
            except asyncio.TimeoutError:
                logger.warning("DirCon: no trainer response to %s within %.1fs",
                               data.hex(), self.control_timeout_s)
                return failed
            finally:
                self._pending_control = None
                self._pending_opcode = None

    @staticmethod
    def _succeeded(reply: bytes) -> bool:
        return len(reply) >= 3 and reply[0] == FTMS_RESPONSE_CODE and reply[2] == FTMS_RESULT_SUCCESS

    async def _retake_control(self, request: bytes) -> bytes:
        """Some trainers (TruTrainer) refuse Request Control while the dongle's link already
        holds control, e.g. after Zwift reconnects; a Reset releases it."""
        logger.info("DirCon: trainer refused Request Control, sending Reset and retrying")
        await self._forward_control(bytes([FTMS_RESET]))
        return await self._forward_control(request)

    def _resolve_control(self, hex_data: str):
        try:
            reply = bytes.fromhex(hex_data)
        except ValueError:
            logger.warning("DirCon: invalid cp_resp data %r", hex_data)
            return
        future = self._pending_control
        if future is None or future.done():
            logger.info("DirCon: unsolicited trainer response %s", reply.hex())
            return
        if len(reply) >= 2 and reply[0] == FTMS_RESPONSE_CODE and reply[1] != self._pending_opcode:
            logger.info("DirCon: ignoring trainer response %s (waiting for opcode 0x%02X)",
                        reply.hex(), self._pending_opcode)
            return
        future.set_result(reply)

    def _record_control(self, cmd: ControlCommand, data: bytes):
        self.last_control = {'data': data.hex(), 'opcode': cmd.opcode, 'grade': cmd.grade,
                             'target_power': cmd.target_power, 'time': time.time()}
        if self.on_control:
            try:
                self.on_control(cmd)
            except Exception:
                logger.exception("DirCon on_control callback failed")

    async def _enable_notifications(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        uuid, rest = p.split_uuid(frame.payload)
        char = session.device.find_characteristic(uuid) if uuid is not None else None
        if char is None:
            return [p.response(frame, resp_code=p.RESP_CHARACTERISTIC_NOT_FOUND)]
        if not char.props & (p.PROP_NOTIFY | p.PROP_INDICATE):
            return [p.response(frame, resp_code=p.RESP_OPERATION_NOT_SUPPORTED)]

        enable = bool(rest[0]) if rest else True
        if enable:
            session.subscriptions.add(char.uuid)
        else:
            session.subscriptions.discard(char.uuid)
        logger.info("DirCon %s %s notifications for %s", session.peer,
                    'enabled' if enable else 'disabled', p.uuid_str(char.uuid))

        out = [p.response(frame, p.uuid_to_bytes(char.uuid))]
        if enable and char.uuid in STREAMED_CHARACTERISTICS:
            value = self.state.build(char.uuid)
            if value is not None:
                out.append(p.notification(char.uuid, value))
        return out

    async def _unknown_0x07(self, session: ClientSession, frame: p.Frame) -> List[p.Frame]:
        return [p.response(frame)]

    # ------------------------------------------------------------------ status

    def status(self) -> Dict:
        return {
            'devices': [{'name': d.name, 'port': d.port} for d in self.devices],
            'clients': [{
                'peer': s.peer,
                'device': s.device.name,
                'subscriptions': sorted(p.uuid_str(u) for u in s.subscriptions),
                'connected_at': s.connected_at,
            } for s in list(self.sessions)],
            'sensors': self.state.snapshot(),
            'forward_control': self.command_sender is not None,
            'last_control': self.last_control,
        }
