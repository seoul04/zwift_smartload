# Zwift Data Visualization Server

Real-time visualization server for Zwift sensor data from the nRF52840 dongle.
It also exposes the dongle's sensors to Zwift over Wahoo DirCon (Direct Connect),
so Zwift can pair over the LAN without the Zwift Companion app.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure the serial port in `config.conf` (TOML format):
```toml
[dongle]
serial = "/dev/ttyACM0"

[buffer]
max_minutes = 60

[frontend]
update_interval_ms = 500
```

## Running

Start the server:
```bash
python run.py
```

The dashboard will be available at `http://localhost:5000`

Or with Docker (uses host networking, see DirCon below):
```bash
cd docker && docker compose up -d --build
```

## Features

- Real-time data visualization from serial port
- Time window selection (1 min, 5 min, 60 min)
- Toggle signal visibility
- Displays: Heart rate, Power meter power/cadence, Trainer speed
- DirCon server: Zwift pairs with the relay over Wi-Fi/Ethernet instead of Bluetooth
- Heart tabs, ported from the `my_heart` Android app and drawn on canvas:
  - **ECG** — the raw single-lead strip, scrolled by a playback clock the way the
    app does it (see below). The width is set in mean R–Rs (3 to 20), so the
    trace looks the same at any heart rate; tapping the plot freezes it without
    stopping the clock behind it.
  - **Beats** — one QRS per recent beat, aligned on the detected R peak and
    normalised against its own baseline and peak. Traces fade from oldest to
    newest; the stepper picks 1 to 64 of them. Each trace is drawn on its own
    detected stroke (`WaveAnchor`), so every R lands on the shared centre line
    instead of wherever the beat clock's jitter left it.
  - **Trends** — heart rate (bpm, left) and RMSSD over 30 and 300 beats (ms,
    right) on a 1 to 60 minute rolling window.
  - **RMSSD** — a tile grid of RMSSD: each column to the right spans half the
    time of the one before, each row down halves the window length N.

## DirCon (Zwift without Companion)

```
Sensors --BLE--> dongle --USB serial JSON--> this server --DirCon TCP + mDNS--> Zwift
```

The server rebuilds standard BLE packets from the dongle's JSON lines and serves them
the way a Wahoo KICKR does over Wi-Fi, as one device on port 36866 with Fitness Machine
(0x1826), Cycling Power (0x1818) and Heart Rate (0x180D).

- The device takes the dongle's own name and Bluetooth address (e.g. `Z-Relay-3ED5`), as it
  appears over Bluetooth: the server sends `{"cmd":"info"}` and the dongle answers
  `{"type":"info","name":...,"addr":...}` (also printed at boot). With older firmware it is
  advertised as `Z-Relay` with this machine's MAC after `identity_timeout_s`. `name` and
  `mac_address` in `config.conf` override what the dongle reports.

- Indoor Bike Data (0x2AD2) carries trainer speed, cadence, power and heart rate.
  Power comes from the power meter while it is reporting, otherwise from the trainer.
- Cycling Power Measurement (0x2A63) carries the power meter's own crank counters.
- Values are re-sent every second and drop to zero after `stale_timeout_s` without data.
- Control point writes (grade, ERG, request control):
  - `forward_control = false` (default): acknowledged locally so Zwift treats the trainer
    as controllable and the requested grade is plotted, but the trainer is not driven.
  - `forward_control = true`: sent to the dongle as `{"cmd":"ftms_cp","data":"<hex>"}`;
    the dongle converts 0x11 (simulation) to 0x04 (resistance), and the trainer's
    answer (`{"type":"cp_resp",...}`) is returned to Zwift. Requires dongle firmware with
    `serial_cmd.c`. If the trainer does not answer within `control_timeout_s`, Zwift gets
    "operation failed". If the trainer refuses Request Control (TruTrainer does while the
    dongle already holds control), the server sends Reset and asks again.
  - The answer is also sent as a 0x2AD9 notification: Zwift waits for it even though it
    never subscribes to the control point over DirCon.
- Each device is advertised by its own mDNS responder: when several services share one
  answer packet, Zwift only reads the TXT record of the last one.

Configuration lives in the `[dircon]` section of `config.conf` (port, name overrides,
bind IP, staleness timeout, control forwarding, frame logging). The current DirCon clients, their
subscriptions and the last control command are reported by `GET /api/status`.

### Networking

Zwift finds DirCon devices through mDNS multicast, which does not cross Docker's
default bridge network:

- This server: run it directly on the host, or with the provided `docker-compose.yml`
  (`network_mode: host`).
- Zwift ([netbrain/zwift](https://github.com/netbrain/zwift)): add `NETWORKING=host`
  to `~/.config/zwift/config`.
- The dongle no longer advertises over Bluetooth, so DirCon is the only way for Zwift to reach the sensors.
- Allow TCP 36866 and UDP 5353 (mDNS) if a firewall is running.

In Zwift's pairing screen, pick `Z-Relay-XXXX` for power, cadence, controllable and heart rate.

### Testing without the dongle

Replay recorded data on a virtual serial port, point `[dongle] serial` at it and start the server:
```bash
python playback_serial.py sample.json --output /tmp/ttyV0
python run.py
```

Unit tests:
```bash
pip install -r requirements-dev.txt
pytest
```

### How the ECG strip scrolls

The dongle does not stream ECG evenly: it collects about a second of samples and
then dumps them as a run of 15-sample lines a fraction of a millisecond apart,
roughly once a second. A strip anchored on the newest sample therefore sits
still and then jumps once per second, however often it is redrawn.

So the strip is not anchored on the data. A **playback clock** (`EcgRing`'s
playhead) advances with the wall clock on every frame the browser requests, and
the window follows it. It is the same design as the app's `EcgTrace`:

- The playhead runs at the measured delivery rate, not a nominal 1.0×. The rate
  is learned over a ~3 s baseline, because a single arrival interval measures
  the gap *inside* a burst and is meaningless.
- It is kept a **lead** behind the newest sample, sized at 1.5× the measured
  burst span, so one burst refills exactly what one gap drains. Sizing it to a
  single line instead is far too short and the playhead would run into the data
  and stall.
- The lead error only ever nudges the *rate*; the playhead is never teleported,
  so a burst changes the speed slightly instead of jumping a packet width.
- The window is a plain slice of the ring — never an interpolation, because a
  QRS apex is one sample wide and blending it with a neighbour makes R and S
  flicker in height. Smoothness comes from `left_fraction`, the sub-sample
  distance the window has slid, which the pane uses to place every sample at its
  true fractional x.

The browser fetches a frame per animation frame (~60 fps) while the ECG tab is
visible, so this is the one panel that does not use the shared 250 ms interval.
Samples are sent as integers to keep the payload near 6 KB per frame.

### Troubleshooting

**The dongle is connected but no data arrives, and `zwift_serial.log` stops
after a line or two.** The identity probe writes `{"cmd":"info"}` to the dongle
to learn its name. Firmware built without `serial_cmd.c` never reads serial
input, so it never drains that line; the USB CDC endpoint backs up and the
firmware's logging blocks, silencing *all* sensor data while the port still
looks open. Set `probe_identity = false` under `[dircon]` and restart. As a
quick check, read the port directly with the server stopped — if data flows
there but not through the server, this is why.

**No sensors are paired.** Pairing is button-driven, not a serial command:

| Action | Effect |
|---|---|
| Long press ≥2 s (`sw0`) | Clears saved devices, disconnects, opens a 5-minute scan window |
| Short press <2 s | Prints the device list to serial |

Press and hold, then wake the strap. `{"type":"devices",...,"count":N}` on the
serial line confirms what the dongle has saved.

**A strap that sends ECG but never a `hr` line** is still counted as the heart
rate device: ECG is treated as a second heart-rate stream, so the dashboard
shows it connected as soon as samples arrive.

## Configuration

Edit `config.conf` to adjust:
- Serial port path
- Buffer size (max minutes to retain)
- Update interval
- DirCon server (`[dircon]`)
- Heart graph defaults (`[heart]`) — each one is also settable from the page and
  that choice wins until the next restart:

```ini
[heart]
fs_hz = 130          # sample rate assumed when an ECG batch omits one
ecg_window_rr = 10   # ECG strip width, in mean R-Rs
trend_width_s = 600  # trends window, seconds
beats_limit = 64     # beats in the stack
rmssd_width_s = 480  # RMSSD panel span, seconds
rmssd_side = 4       # RMSSD tiles per side
```
