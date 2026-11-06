import asyncio
import json
import socket
import struct

import pytest

from src.dircon import protocol as p
from src.dircon.gatt import (CHAR_CP_MEASUREMENT, CHAR_FTMS_CONTROL_POINT, CHAR_FTMS_FEATURE,
                             CHAR_HR_MEASUREMENT, CHAR_INDOOR_BIKE_DATA, SVC_CYCLING_POWER,
                             SVC_FTMS, SVC_HEART_RATE, SensorState, build_device,
                             parse_control_point)
from src.dircon.identity import DongleIdentity
from src.dircon.mdns import txt_properties
from src.dircon.server import ClientSession, DirconServer

FTMS_UUID_BYTES = bytes.fromhex('00001826 00001000800000805f9b34fb'.replace(' ', ''))


def make_server(**kwargs):
    devices = [build_device('Z-Relay-3ED5', 36866)]
    return DirconServer(devices, SensorState(), **kwargs), devices


def request(msg_id, payload=b'', seq=7):
    return p.Frame(msg_id=msg_id, seq=seq, payload=payload)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- protocol

def test_uuid_round_trip():
    assert p.uuid16_to_bytes(0x1826) == FTMS_UUID_BYTES
    assert p.bytes_to_uuid(FTMS_UUID_BYTES) == 0x1826
    custom = bytes(range(16))
    assert p.bytes_to_uuid(custom) == custom


def test_frame_encode_header():
    frame = p.Frame(msg_id=p.MSG_READ_CHARACTERISTIC, seq=3, payload=b'\xAA' * 17)
    assert frame.encode()[:6] == bytes([1, 0x03, 3, 0, 0x00, 17])


def test_parser_handles_split_and_coalesced_frames():
    a = p.Frame(msg_id=p.MSG_DISCOVER_SERVICES, seq=1).encode()
    b = p.Frame(msg_id=p.MSG_DISCOVER_CHARACTERISTICS, seq=2, payload=FTMS_UUID_BYTES).encode()
    parser = p.FrameParser()
    stream = a + b
    assert parser.feed(stream[:3]) == []
    frames = parser.feed(stream[3:10])
    assert [f.seq for f in frames] == [1]
    frames = parser.feed(stream[10:])
    assert len(frames) == 1
    assert frames[0].msg_id == p.MSG_DISCOVER_CHARACTERISTICS
    assert frames[0].payload == FTMS_UUID_BYTES
    assert parser.buffer == bytearray()


def test_error_response_has_no_payload():
    resp = p.response(request(p.MSG_READ_CHARACTERISTIC), b'xyz', p.RESP_CHARACTERISTIC_NOT_FOUND)
    assert resp.encode() == bytes([1, 0x03, 7, 0x04, 0, 0])


# ---------------------------------------------------------------- packet builders

def test_hr_measurement():
    state = SensorState()
    assert state.update({'type': 'hr', 'bpm': 142}, now=0) == {CHAR_HR_MEASUREMENT}
    assert state.build_hr(1) == bytes([0x00, 142])
    assert state.build_hr(10) is None


def test_cp_measurement_keeps_crank_counters():
    state = SensorState()
    state.update({'type': 'cp', 'power': 250, 'flags': 35, 'balance': 100,
                  'crank_revs': 1234, 'crank_time': 40000, 'cadence': 90}, now=0)
    assert state.build_cp(1) == struct.pack('<HhBHH', 0x23, 250, 100, 1234, 40000)
    # Stale: power drops to zero, crank counters stay (so cadence decays to zero on the client)
    assert state.build_cp(10) == struct.pack('<HhHH', 0x20, 0, 1234, 40000)


def test_indoor_bike_data_prefers_power_meter():
    state = SensorState()
    state.update({'type': 'ftms', 'speed': 2950, 'power': 180}, now=0)
    state.update({'type': 'cp', 'power': 210, 'crank_revs': 1, 'crank_time': 2, 'cadence': 88}, now=0)
    state.update({'type': 'hr', 'bpm': 130}, now=0)
    assert state.build_indoor_bike_data(1) == struct.pack('<HHHhB', 0x0244, 2950, 176, 210, 130)


def test_indoor_bike_data_falls_back_to_trainer():
    state = SensorState()
    state.update({'type': 'ftms', 'speed': 1000, 'cadence': 75, 'power': 150}, now=0)
    assert state.build_indoor_bike_data(1) == struct.pack('<HHHh', 0x0044, 1000, 150, 150)
    assert state.build_indoor_bike_data(10) == struct.pack('<HHHh', 0x0044, 0, 0, 0)


def test_indoor_bike_data_none_without_data():
    assert SensorState().build_indoor_bike_data(0) is None


def test_control_point_sim():
    cmd = parse_control_point(bytes.fromhex('11 0000 9001 28 33'.replace(' ', '')))
    assert cmd.reply == bytes([0x80, 0x11, 0x01])
    assert (cmd.wind_speed, cmd.grade, cmd.crr, cmd.cw) == (0, 400, 0x28, 0x33)


def test_control_point_unsupported_and_invalid():
    assert parse_control_point(b'\x02\x00\x00').reply == bytes([0x80, 0x02, 0x02])
    assert parse_control_point(b'\x11\x00').reply == bytes([0x80, 0x11, 0x03])
    assert parse_control_point(b'') is None


def test_mdns_txt_record():
    txt = txt_properties(build_device('Z-Relay-3ED5', 36866), 'AA:BB:CC:DD:EE:FF')
    assert txt['ble-service-uuids'] == ('00001826-0000-1000-8000-00805F9B34FB,'
                                        '00001818-0000-1000-8000-00805F9B34FB,'
                                        '0000180D-0000-1000-8000-00805F9B34FB')
    assert txt['mac-address'] == 'AA:BB:CC:DD:EE:FF'


class FakeAdvertiser:
    def __init__(self):
        self.mac_address = None
        self.registered = []

    def register(self):
        self.registered.append(self.mac_address)
        return True

    def unregister(self):
        pass


def wait_until(predicate, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_identity_uses_dongle_name_and_address():
    device = build_device('Z-Relay', 36866)
    adv = FakeAdvertiser()
    sent = []
    identity = DongleIdentity(device, adv, sent.append, fallback_mac='00:00:00:00:00:01', timeout_s=5)
    identity.start()
    identity.on_serial_message({'type': 'hr', 'bpm': 120})
    identity.on_serial_message({'type': 'hr', 'bpm': 121})
    assert sent == ['{"cmd":"info"}']
    identity.on_serial_message({'type': 'info', 'name': 'Z-Relay-3ED5', 'addr': 'F1:2B:CA:19:48:1F'})
    assert wait_until(lambda: adv.registered == ['F1:2B:CA:19:48:1F'])
    assert device.name == 'Z-Relay-3ED5'


def test_identity_falls_back_without_info_and_honours_overrides():
    device = build_device('Z-Relay', 36866)
    adv = FakeAdvertiser()
    DongleIdentity(device, adv, lambda line: True, fallback_mac='00:00:00:00:00:01', timeout_s=0.05).start()
    assert wait_until(lambda: adv.registered == ['00:00:00:00:00:01'])
    assert device.name == 'Z-Relay'

    device = build_device('Z-Relay', 36866)
    adv = FakeAdvertiser()
    identity = DongleIdentity(device, adv, lambda line: True, fallback_mac='00:00:00:00:00:01',
                              name='My Bike', timeout_s=5)
    identity.start()
    identity.on_serial_message({'type': 'info', 'name': 'Z-Relay-3ED5', 'addr': 'F1:2B:CA:19:48:1F'})
    assert wait_until(lambda: adv.registered == ['F1:2B:CA:19:48:1F'])
    assert device.name == 'My Bike'


# ---------------------------------------------------------------- request handlers

def test_discover_services():
    server, devices = make_server()
    session = ClientSession(devices[0], 'test')
    [resp] = run(server.handle_frame(session, request(p.MSG_DISCOVER_SERVICES)))
    assert resp.seq == 7 and resp.resp_code == p.RESP_SUCCESS
    assert resp.payload == (p.uuid16_to_bytes(SVC_FTMS) + p.uuid16_to_bytes(SVC_CYCLING_POWER)
                            + p.uuid16_to_bytes(SVC_HEART_RATE))


def test_discover_characteristics():
    server, devices = make_server()
    session = ClientSession(devices[0], 'test')
    [resp] = run(server.handle_frame(
        session, request(p.MSG_DISCOVER_CHARACTERISTICS, p.uuid16_to_bytes(SVC_FTMS))))
    assert resp.payload[:16] == FTMS_UUID_BYTES
    entries = resp.payload[16:]
    assert len(entries) % 17 == 0
    chars = {p.bytes_to_uuid(entries[i:i + 16]): entries[i + 16] for i in range(0, len(entries), 17)}
    assert chars[CHAR_FTMS_CONTROL_POINT] == p.PROP_WRITE | p.PROP_INDICATE
    assert chars[CHAR_INDOOR_BIKE_DATA] == p.PROP_NOTIFY

    [resp] = run(server.handle_frame(
        session, request(p.MSG_DISCOVER_CHARACTERISTICS, p.uuid16_to_bytes(SVC_HEART_RATE))))
    assert p.bytes_to_uuid(resp.payload[16:32]) == CHAR_HR_MEASUREMENT

    [resp] = run(server.handle_frame(
        session, request(p.MSG_DISCOVER_CHARACTERISTICS, p.uuid16_to_bytes(0x180F))))
    assert resp.resp_code == p.RESP_SERVICE_NOT_FOUND


def test_read_characteristic():
    server, devices = make_server()
    session = ClientSession(devices[0], 'test')
    [resp] = run(server.handle_frame(
        session, request(p.MSG_READ_CHARACTERISTIC, p.uuid16_to_bytes(CHAR_FTMS_FEATURE))))
    assert resp.payload == p.uuid16_to_bytes(CHAR_FTMS_FEATURE) + bytes.fromhex('835400000ce00000')

    [resp] = run(server.handle_frame(
        session, request(p.MSG_READ_CHARACTERISTIC, p.uuid16_to_bytes(CHAR_INDOOR_BIKE_DATA))))
    assert resp.resp_code == p.RESP_OPERATION_NOT_SUPPORTED

    [resp] = run(server.handle_frame(
        session, request(p.MSG_READ_CHARACTERISTIC, p.uuid16_to_bytes(0x2A00))))
    assert resp.resp_code == p.RESP_CHARACTERISTIC_NOT_FOUND


def test_enable_notifications_and_greeting():
    server, devices = make_server()
    server.state.update({'type': 'ftms', 'speed': 1000, 'power': 100})
    session = ClientSession(devices[0], 'test')
    out = run(server.handle_frame(session, request(
        p.MSG_ENABLE_NOTIFICATIONS, p.uuid16_to_bytes(CHAR_INDOOR_BIKE_DATA) + b'\x01')))
    assert out[0].payload == p.uuid16_to_bytes(CHAR_INDOOR_BIKE_DATA)
    assert out[1].msg_id == p.MSG_UNSOLICITED_NOTIFICATION and out[1].seq == 0
    assert CHAR_INDOOR_BIKE_DATA in session.subscriptions

    run(server.handle_frame(session, request(
        p.MSG_ENABLE_NOTIFICATIONS, p.uuid16_to_bytes(CHAR_INDOOR_BIKE_DATA) + b'\x00')))
    assert CHAR_INDOOR_BIKE_DATA not in session.subscriptions


def test_control_point_write_acknowledged():
    received = []
    server, devices = make_server(on_control=received.append)
    session = ClientSession(devices[0], 'test')
    cp = p.uuid16_to_bytes(CHAR_FTMS_CONTROL_POINT)
    out = run(server.handle_frame(session, request(p.MSG_WRITE_CHARACTERISTIC, cp + b'\x00')))
    assert out[0].payload == cp + bytes([0x80, 0x00, 0x01])
    # Indication goes out even though Zwift never subscribed to the control point
    assert out[1].msg_id == p.MSG_UNSOLICITED_NOTIFICATION
    assert out[1].payload == cp + bytes([0x80, 0x00, 0x01])

    out = run(server.handle_frame(session, request(
        p.MSG_WRITE_CHARACTERISTIC, cp + bytes.fromhex('110000c8ff2833'))))
    assert out[0].payload == cp + bytes([0x80, 0x11, 0x01])
    assert received[-1].grade == -56


def test_write_to_read_only_characteristic():
    server, devices = make_server()
    session = ClientSession(devices[0], 'test')
    [resp] = run(server.handle_frame(session, request(
        p.MSG_WRITE_CHARACTERISTIC, p.uuid16_to_bytes(CHAR_FTMS_FEATURE) + b'\x01')))
    assert resp.resp_code == p.RESP_OPERATION_NOT_SUPPORTED


def test_unknown_messages():
    server, devices = make_server()
    session = ClientSession(devices[0], 'test')
    [resp] = run(server.handle_frame(session, request(p.MSG_UNKNOWN_0x07)))
    assert resp.encode() == bytes([1, 0x07, 7, 0, 0, 0])
    [resp] = run(server.handle_frame(session, request(0x42)))
    assert resp.resp_code == p.RESP_UNKNOWN_MESSAGE_TYPE


# ---------------------------------------------------------------- control forwarding (phase 2)

CP_BYTES = p.uuid16_to_bytes(CHAR_FTMS_CONTROL_POINT)
SIM_WRITE = bytes.fromhex('110000c8ff2833')


def forwarding_scenario(dongle_replies, sender_ok=True, timeout=0.5, writes=(SIM_WRITE,)):
    """Send writes through a forwarding server. The fake dongle answers each command it
    receives with the next entry of dongle_replies (a hex string, or a tuple of them).
    Returns the lines sent and the frames of the last write."""
    async def scenario():
        loop = asyncio.get_running_loop()
        sent = []
        replies = list(dongle_replies)
        server, devices = make_server(control_timeout_s=timeout)

        def sender(line):
            sent.append(line)
            if replies:
                entry = replies.pop(0)
                for reply in (entry if isinstance(entry, tuple) else (entry,)):
                    loop.call_soon(server._apply_serial_message, {'type': 'cp_resp', 'data': reply})
            return sender_ok

        server.command_sender = sender
        session = ClientSession(devices[0], 'test')
        for data in writes:
            out = await server.handle_frame(session, request(p.MSG_WRITE_CHARACTERISTIC, CP_BYTES + data))
        return sent, out
    return run(scenario())


def test_forward_control_uses_trainer_response():
    sent, out = forwarding_scenario(['801101'])
    assert sent == ['{"cmd":"ftms_cp","data":"110000c8ff2833"}']
    assert out[0].payload == CP_BYTES + bytes.fromhex('801101')
    assert out[1].msg_id == p.MSG_UNSOLICITED_NOTIFICATION
    assert out[1].payload == CP_BYTES + bytes.fromhex('801101')


def test_forward_control_ignores_stale_response_for_other_opcode():
    sent, out = forwarding_scenario([('800501', '801104')])
    assert out[0].payload == CP_BYTES + bytes.fromhex('801104')


def test_forward_control_refused_request_control_resets_and_retries():
    sent, out = forwarding_scenario(['800004', '800101', '800001'], writes=(b'\x00',))
    assert [json.loads(line)['data'] for line in sent] == ['00', '01', '00']
    assert out[0].payload == CP_BYTES + bytes.fromhex('800001')
    assert out[1].payload == CP_BYTES + bytes.fromhex('800001')


def test_forward_control_request_control_still_refused_after_reset():
    sent, out = forwarding_scenario(['800004', '800101', '800004'], writes=(b'\x00',))
    assert len(sent) == 3
    assert out[0].payload == CP_BYTES + bytes.fromhex('800004')


def test_forward_control_timeout_reports_failure():
    sent, out = forwarding_scenario([], timeout=0.05)
    assert out[0].payload == CP_BYTES + bytes.fromhex('801104')


def test_forward_control_port_closed_reports_failure():
    sent, out = forwarding_scenario(['801101'], sender_ok=False)
    assert out[0].payload == CP_BYTES + bytes.fromhex('801104')


def test_serial_reader_write_line():
    import os
    import pty

    import serial

    from src.serial_reader import SerialReader

    master, slave = pty.openpty()
    reader = SerialReader(port=os.ttyname(slave))
    assert reader.write_line('{"cmd":"x"}') is False
    reader.serial_conn = serial.Serial(os.ttyname(slave), timeout=1)
    try:
        assert reader.write_line('{"cmd":"ftms_cp","data":"00"}')
        assert os.read(master, 100) == b'{"cmd":"ftms_cp","data":"00"}\n'
    finally:
        reader.serial_conn.close()
        os.close(master)
        os.close(slave)


# ---------------------------------------------------------------- end to end over TCP

def free_port_pair():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        try:
            with socket.socket() as s:
                s.bind(('127.0.0.1', port + 1))
            return port
        except OSError:
            continue


def recv_frames(sock, parser, count, timeout=3.0):
    sock.settimeout(timeout)
    frames = []
    while len(frames) < count:
        data = sock.recv(4096)
        if not data:
            break
        frames.extend(parser.feed(data))
    return frames


def test_tcp_session_streams_serial_data():
    port = free_port_pair()
    devices = [build_device('Z-Relay-3ED5', port)]
    server = DirconServer(devices, SensorState(), bind_ip='127.0.0.1', notify_interval_s=0.2)
    assert server.start()
    try:
        with socket.create_connection(('127.0.0.1', port)) as sock:
            parser = p.FrameParser()
            sock.sendall(request(p.MSG_DISCOVER_SERVICES, seq=1).encode())
            [resp] = recv_frames(sock, parser, 1)
            assert resp.seq == 1 and len(resp.payload) == 48

            sock.sendall(request(p.MSG_ENABLE_NOTIFICATIONS,
                                 p.uuid16_to_bytes(CHAR_CP_MEASUREMENT) + b'\x01', seq=2).encode())
            [resp] = recv_frames(sock, parser, 1)
            assert resp.msg_id == p.MSG_ENABLE_NOTIFICATIONS and resp.seq == 2

            server.on_serial_message({'type': 'cp', 'power': 300, 'flags': 32,
                                      'crank_revs': 10, 'crank_time': 1024, 'cadence': 60})
            [note] = recv_frames(sock, parser, 1)
            assert note.msg_id == p.MSG_UNSOLICITED_NOTIFICATION
            uuid, value = p.split_uuid(note.payload)
            assert uuid == CHAR_CP_MEASUREMENT
            assert value == struct.pack('<HhHH', 0x20, 300, 10, 1024)

            # Not subscribed to 0x2AD2, so only CP keeps flowing from the ticker
            notes = recv_frames(sock, parser, 2)
            assert all(p.split_uuid(n.payload)[0] == CHAR_CP_MEASUREMENT for n in notes)
            assert server.status()['clients'][0]['subscriptions'] == ['0x2A63']
    finally:
        server.stop()
