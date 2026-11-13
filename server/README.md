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
- EDF+ session recording — every session can be written to a file EDFbrowser
  reads, and replayed through the server (see [EDF recording](#edf-recording))
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

Or replay an EDF session, which is what exercises the heart panels — a JSON log
carries no `ecg` line and no `rr` array, so it leaves ECG, Beats, Trends and
RMSSD empty:
```bash
python playback_edf.py recordings/zrelay_20261107_182456.edf --output /tmp/ttyV0
python run.py
```

Unit tests:
```bash
pip install -r requirements-dev.txt
pytest
```

## EDF recording

With `[edf] enabled = true`, every session is written to an EDF+C file. EDF was
chosen because it is the format **EDFbrowser** reads, so a recording is both
analysable by hand and replayable through this server.

```
sensors --BLE--> dongle --USB JSON--> server --EDF+--> recordings/
                                         `--playback_edf.py--> pty --> server
```

Channels in every file:

| Channel | Rate | Unit | Contents |
|---|---|---|---|
| `ECG` | `fs` (256 Hz) | µV | the raw single lead, as the dongle streams it |
| `RR_MS` | 5 Hz | ms | sensor R–R, sampled 5× a second (see below) |
| `SENSOR_HR` | 1 Hz | bpm | the bpm the strap itself reported |
| `HR_EMA`, `MEAN_RR`, `HRV_RMSSD30`, `HRV_RMSSD300` | 1 Hz | bpm / ms | derived from `RR_MS`, see below |
| `POWER`, `CADENCE` | 1 Hz | W / rpm | power meter |
| `TRAINER_POWER`, `TRAINER_CADENCE`, `SPEED` | 1 Hz | W / rpm / m/h | trainer (FTMS) |
| `RESISTANCE` | 1 Hz | % | the setpoint the dongle derived from the commanded grade |
| `TRAINER_RES` | 1 Hz | % | the resistance the trainer says it is applying |
| `GRADE` | 1 Hz | % | the grade Zwift commanded (what the dongle forwards) |
| `CMD_GRADE`, `CMD_RESISTANCE` | 1 Hz | % | the control point write as the server saw it |
| `EDF Annotations` | — | — | `GAP` markers where delivery stalled |

`SENSOR_HR` is stored next to the derived rate rather than instead of it
because the two are not always the same number: on a live session the strap's
bpm matched `60000/RR` exactly only 42 % of the time, with a tail up to 15 bpm
apart. Keeping both means a strap disagreeing with its own intervals shows up
in the file instead of being silently smoothed over. `GRADE` and `RESISTANCE`
are the same pair for the trainer: the dongle converts a commanded grade into a
resistance setpoint (`RESISTANCE`), while the trainer separately reports the
resistance it is actually applying (`TRAINER_RES`) — which lags by seconds and
is the only evidence that it followed.

The four heart metrics are **computed, not received**. The dongle sends
intervals, not heart rate: `HR_EMA` is the smoothed rate from `HrFilter` (locks
after three consistent intervals, follows small changes, and re-acquires rather
than chasing one ectopic beat), `MEAN_RR` is the mean of the last 30 intervals,
and the two RMSSDs cover the last 30 and 300 intervals. They are the same
filters, fed in the same order, as `src/heart` — so the file reports the values
the page was showing, and a disagreement would mean a bug in one of the two.
A zero means *not known yet* (the EMA has not locked, or the RMSSD window is
short of its minimum), exactly as the dashboard leaves the tile blank.

`RR_MS` is a *step* channel: it holds the latest interval, so a change in the
held value is what marks a beat. That makes its rate a fidelity decision, not
just a size one — two beats inside one sample period are indistinguishable from
one, and the earlier interval is simply gone.

It used to run at the full ECG rate (one `int16` per sample, 512 B/s at 256 Hz
for ~1.5 B/s of information), then briefly at 1 Hz. 1 Hz was a mistake: it needs
two beats within 1000 ms, i.e. anything above 60 bpm, and because replaying a
file reads its beats back out of this channel, the loss became **wrong RMSSD**
rather than a cosmetic gap. A 582-beat ride recovered only 366 intervals — 37 %
missing.

It is now 5 Hz. The shortest gap that can be hidden is 200 ms (300 bpm), and the
same 582-beat ride round-trips with all 582 intervals and an identical RMSSD
(18.00 ms, zero difference). The cost is 8 B/s more than 1 Hz and still 56 %
less than the full-rate layout. It also matches what the Android app writes, so
files from either producer read alike.

A step channel has one limit that **no sample rate can fix**: consecutive beats
with an *identical* interval produce no change in the held value, so the second
beat is invisible. A real capture had 26 such pairs and recovered 158 of 167
beats at 1, 5, 10 and 50 Hz alike. So every beat is also written as a `BEAT`
annotation (onset only — the interval stays in `RR_MS`, so the two cannot
disagree). Round-tripping that capture writes 167 annotations against 141
step-channel marks: the 26 that the channel can never represent.

In short, `RR_MS` is the plottable staircase and `BEAT` annotations are the
complete beat log. Neither substitutes for the other, and dropping the
annotations silently truncates the rhythm.

`GRADE` and `CMD_GRADE` both come off the wire in hundredths of a percent (the
FTMS simulation field is an `int16` in 0.01 %), and both are divided by 100 on
the way in, so a 3 % climb reads 3 in the file. `RESISTANCE` needs no such
treatment: the dongle has already converted the grade to a unitless 0–100 target
before it logs it.

The command channels are kept apart from the reported ones because the
difference between them is usually the question being asked — a trainer that
acknowledges a grade and then holds a different resistance shows up as the two
series diverging, which a single fused "resistance" line would hide.

### Sessions

The dongle timestamps with its uptime clock, so a reboot (a USB reset is
enough) restarts `ts` near zero. That is treated as the end of a session and the
start of the next file: splicing two clocks into one EDF would put a jump in the
timeline. Late points *within* a session are not affected — only a rewind larger
than 5 s.

Files are named `zrelay_<yyyyMMdd_HHmmss>.edf`, so a directory listing sorts
chronologically. `max_files` keeps the newest and deletes the oldest; only files
matching the prefix and the timestamp pattern are ever removed, so a hand-copied
recording in the same folder is safe.

Stop the server with SIGTERM or Ctrl-C, not SIGKILL: records are coalesced for
about a minute, and the shutdown handler is what flushes and patches the record
count. A SIGKILL leaves a file whose header still says `n_rec = -1`; it reads
correctly (the count is recovered from the file length) but drops the last
unsaved minute. `POST /api/edf/close` finishes a session and starts a new one
without stopping the server, and `GET /api/edf/status` reports the current file.

### Replaying a session

```bash
python playback_edf.py session.edf --inspect          # what is in the file
python playback_edf.py session.edf --output /tmp/ttyV0
python playback_edf.py session.edf --loop --speed 2   # 2x, continuous
```

The replayer opens a pty and writes the same JSON lines the firmware emits,
paced on the recording's own clock (measured drift is under 1 ms per event at 1×
and 2×). ECG goes out in ~1 s bursts of 16-sample lines a fraction of a
millisecond apart, like the dongle, because the scrolling strip learns that
cadence to size its playback lead — a uniformly-paced stream would replay
correctly but would not exercise the burst handling.

What is not replayed: the `CMD_` *input* channels. Those record what Zwift asked
for, which is not sensor data; live control-point writes during the run
regenerate them.

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

### Measuring the beats stack's alignment

The Beats pane pins each wave on its own detected R so the stack superposes, but
that only works if the *slice* put the complex in roughly the same place on
every beat. When it does not, the stack fans out — and "it looks fanned" is not
something you can iterate on.

`tools/eval_beat_alignment.py` turns it into a number. It replays a recording
through the real `HeartState` (same beat clock, same finder) and reports, per
beat, where the true R ends up drawn relative to the shared line:

```sh
# provisional yardstick, from the recording's own R-R marks
python tools/eval_beat_alignment.py recording/zrelay_20261108_020954.edf

# against hand-tagged R positions (sample indices, as EDFbrowser reads out)
python tools/eval_beat_alignment.py recording/x.edf --peaks peaks.txt --csv out.csv

# any stretch, not just the tail: --start/--end scroll the replay window
python tools/eval_beat_alignment.py recording/x.edf --start 380 --peaks peaks.txt
```

The stack holds only the last 64 beats of whatever is replayed, so a tag file
that covers the *end* of the file needs no window; for any other stretch, pass
`--start`. Tag offsets are in the recording's own timeline either way.

The two numbers that matter are `median |residual|` (a constant offset, which
the anchoring hides — not a visual problem) and `residual MAD` (the *spread*,
which is exactly the fan). The replay is deterministic, so two runs over the
same file are directly comparable.

`--auto` is not independent of the algorithm under test: it takes the beat
*times* from the recording's `RR_MS` channel and only asks the ECG where the
apex is near each mark, which is enough to rank two algorithms on one file but
is not hand-tagged truth. Tag a stretch of peaks for a real benchmark.

**What it already found.** On `zrelay_20261108_020954.edf` the slice is placed a
median **97 ms** from the true R (p90 297 ms), with the errors split evenly
either side — a scatter problem, not a bias. `WaveAnchor` is not the culprit:
narrowing `MAX_PEAK_SHIFT` to 25 samples made the residual spread *worse*
(3.6 → 8.6 samples), because the anchor was absorbing some of that scatter.

The fan was fixed by changing where the beat comes from. The stack used to take
the sensor's announcement as truth and only ask *where inside this known beat is
its R* (`PeakFinder`, whole-codebase notes in `src/heart/ecg.py`). It now asks
*is this a beat at all* — a port of EDFbrowser's Pan–Tompkins detector
(`src/heart/qrs.py`, GPLv3, `filt/pt_qrs.c`), whose detected peak is the slice
centre directly. Measured on `zrelay_20261108_125551_qrs.edf` against EDFbrowser's
own annotations, through `HeartState`:

| slice centre | median | MAD | within 2 samples | slice error |
|---|---|---|---|---|
| `PeakFinder` | 4.79 | 5.50 | 36 % | 226 ms |
| QRS peak, used directly | **0.99** | **0.90** | **84 %** | **0.2 ms** |

Feeding the detector's peak *back* through `PeakFinder` is worse than using it
raw (1.66 samples of residual against 0.99): the refiner and the clock
calibration exist to correct a peak derived from a sensor timestamp, and running
them on a measured one just moves it. That is why the QRS path does no
refinement at all. Below 200 Hz — where the port's refractory and integration
windows are not defined — the stack falls back to `PeakFinder` unchanged.

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
- EDF recording (`[edf]`)
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
