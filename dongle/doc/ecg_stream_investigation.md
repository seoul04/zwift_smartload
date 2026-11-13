# ECG stream intermittency: what was measured

Investigated on the nRF52840 Dongle with a Movesense 240530000044 at 256 Hz,
November 2026. This records what the instrumentation actually showed, including
two attempts that made things worse, so the next person does not repeat them.

## The failure

The symptom was "we get ECG packets for a while and then nothing; sometimes
closing and reopening the port is enough to lose the stream."

The counters localised it precisely. When the stream died:

```
state=DONE  ref=28  restarts=4          <- watchdog had given up
notifs=469                              <- GSP notifications FROZEN
hr=302                                  <- HRS notifications still arriving
last=307100ms ago                       <- 5 minutes with no ECG
```

GSP ECG stops while the sensor's standard Heart Rate Service keeps notifying on
the **same connection**. After that the sensor ignores every further GSP command
for the session; re-subscribing does not revive it, but a new BLE connection
does.

So there are two separate facts:

1. The sensor abandons its ECG measurement on its own schedule (~every 20 s under
   poor RF, much less often when close).
2. Nothing in the firmware could recover it, so the stream stayed dead until the
   dongle was power cycled.

The counters added for this are in `src/common.h` (`gsp_notifs`, `all_notifs`,
`hr_notifs`, `gsp_frames_unknown`, `ecg_gap_ms_max`, `gsp_restarts`). Comparing
`gsp_notifs` against `all_notifs` is what separates "sensor stopped sending" from
"host stopped receiving"; `hr_notifs` is the control channel proving the link and
the receive path are alive.

## What throughput looks like when the link is healthy

From a clean 10-minute run, per-minute record counts:

```
60-120s : 959 records (16.0/s)
120-180s: 963 records (16.1/s)
```

16.0/s is the exact theoretical maximum (256 Hz / 16 samples per frame). This is
worth remembering: when the link is good the decode path is perfect, so a low
average means lost seconds, not corrupt or slow parsing.

Verified independently on the same firmware: 256.65 Hz against the device sample
clock, timestamp deltas alternating {62 ms: 343, 63 ms: 343} exactly, and an
ECG-derived heart rate agreeing with the strap's own RR to within 1 bpm.

## The 409 is expected on a fresh flash

`GET`/`SUBSCRIBE` is refused with HTTP 409 Conflict while the sensor still has the
measurement running under an earlier reference. On a fresh boot there are no
persisted references to release, so the first subscribe is *always* refused. This
is deterministic, not intermittent, and the firmware now says so:

```
[GSP] Slots enter with no persisted refs (fresh boot or new slot):
      cannot release the sensor's stale subscription, expect 409 on subscribe
```

Recovery is `GSP_STEP_RECLAIM`: learn the live reference from the frames, release
it with a real UNSUBSCRIBE, then subscribe afresh.

## Things that did not work

Recorded because both looked reasonable and both were wrong.

### 2M PHY

Reasoning: a 68-byte frame is on air half as long at 2M, so it is exposed to
interference for half the time.

Result: records fell from 9.05/s to 3.04/s while RSSI *improved* from -59 to
-52 dBm, and 240 of 600 seconds produced nothing at all. The sensor does not cope
with running its ECG measurement at 2M.

Note also that `CONFIG_BT_PHY_UPDATE` defaults to `y` in Zephyr, so the PHY update
procedure was running in *every* earlier build whether or not it was asked for. It
is now pinned off with `CONFIG_BT_PHY_UPDATE=n`, which removes a variable that was
silently in play all along. Checking `autoconf.h` is the only way to be sure a
Kconfig setting took effect; commenting a line out does not revert a default.

### An aggressive restart budget

Reasoning: more retries means a better chance of recovery.

Result: a budget of 40 churned through GSP references 106..115 in five minutes and
the sensor then stopped answering GSP commands entirely. The recovery mechanism
destroyed the session it was meant to repair.

Now `GSP_RESTART_MAX` is 4. When the budget is exhausted the firmware logs once
and drops the connection, because a fresh connection is the only thing that
actually revives the measurement.

## RF, and where the dongle sits

Measured on the same firmware, moving the dongle changed RSSI by about 13 dB:

| Position | Average RSSI |
|---|---|
| In a USB port at floor level | -72 dBm |
| Repositioned, clear line to the strap | -49 dBm |

With HCI reason `0x08` (connection timeout) the link was dying every few seconds
at the worse position. `+8 dBm` transmission (the nRF52840's hardware ceiling; the
default was 0 dBm) plus a 4 s supervision timeout essentially removed those drops.

Small HRS packets survive a marginal link even when 68-byte ECG frames do not,
which is why "HR works but ECG doesn't" is not evidence of a protocol bug.

## Current state

`pkg_ecg256_v21_1mphy.zip`: 1M PHY, +8 dBm, 4 s supervision, restart budget 4,
reconnect on exhaustion.

Measured over 10 minutes at -49 dBm: 7.52 records/s (120 samples/s), data in
10/10 minutes, no permanently dead stretches. The average is below the 9.05/s of
the build without the reconnect policy, and the trade is deliberate: restarts cost
a few seconds each, and continuous-with-gaps beats perfect-then-dead.

## Not resolved

The trigger for the sensor abandoning GSP is still unknown. The firmware now
survives it, but roughly 28 restarts per 10 minutes is heavy GSP churn, and the
budget-40 experiment showed the sensor degrades under that. A multi-hour soak test
would be the honest next check.

Also unresolved: the ATT MTU negotiates to 92, not the 247 that
`CONFIG_BT_L2CAP_TX_MTU` suggests. Harmless today because a frame is 68 bytes, but
the larger ACL buffers are not being used.

## Diagnostic aids left in the firmware

- A health line every 10 s per connected Movesense, with `restarts=` and a
  `<-- STALLED` marker. Silence now has a cause attached to it.
- `{"cmd":"gsp_reset"}` reproduces the 409 path without a power cycle, by
  forgetting this side's references while the sensor keeps streaming.

## Control experiment: the same stream from the host adapter

**Read the retraction at the end of this section first.** The tool and the raw
measurements below are sound, but the interpretation drawn from them - that the
sensor was abandoning its ECG measurement while the link stayed healthy - was
wrong, because the experiment's own HR subscription was a confounder.

The dongle hides sensor faults: it reconnects, re-subscribes and averages the
result, so a sensor that drops its measurement every few seconds can still look
healthy in aggregate. To separate "our firmware" from "the sensor",
`tools/host_gsp_ecg.py` speaks GSP from the host's own adapter with no firmware
in the path. It reimplements framing, decode and reference handling from the
wire rather than reusing the firmware's assumptions, so a shared misreading
cannot hide.

Three sessions, same sensor, same place, RSSI -56..-63 dBm:

| session | subscribe result | battery | delivered | duty | max gap |
|---|---|---|---|---|---|
| clean | `HTTP 200` | - | 256.10 Hz for 60.5 s | 100.7% | 210 ms |
| stale | `HTTP 409` then adopt | - | 16.09 Hz for ~8-23 s | 13-79% | 449 ms |
| fresh cell | `HTTP 200` | 100% | 255.60 Hz for 8.4 s | 10-18% | 298 ms |

The clean session is the reference: 16.01 frames/s against an ideal of 16.00,
210 ms worst gap, no discarded frames. The transport is capable of a
faultless 256 Hz stream, and the sample decode is right.

In the failing sessions, ECG stopped after 8-23 s and the Heart Rate service
kept notifying on the same link for another 31-51 s (+16 to +36 notifications).
The link was alive and the sensor was still talking; only the ECG measurement
stopped. The sensor then dropped the link entirely (`DISCONNECTED by remote`,
6.2 s after the last frame) and stopped advertising for ~40 s before recovering
on its own.

That is the dongle's exact signature, reproduced without the dongle.

### The cause is not the leftover measurement, and not the battery

Every stale session began the same way:

```
[adopt] streaming under ref 65 (68 bytes, 16 samples)
[cmd] ref 2: HTTP 409 (CONFLICT - measurement already running)
```

A measurement from a previous session was still running when the host
subscribed. The sensor does not stop its measurement when the subscriber
disappears, and it refuses a second `SUBSCRIBE` while that one is live. Adopting
the live reference works - frames flow at the full rate - but the session is
already abnormal: the sensor holds a measurement whose subscriber is gone.

The clean session differs in one way: it adopted ref 65, then `UNSUBSCRIBE`
succeeded (`HTTP 200`) and the stream ran perfectly. That made it look as
though the failure tracked whether the orphaned measurement was released.

**It does not.** A later run with a fresh battery settled this:

```
[bat] battery level: 100%
[cmd] ref 2: HTTP 200
[   8.4s] no frames for 1.0s (notifs=118, hr_notifs=8, last_hr=0.3s ago)
[  18.5s] still silent after 11s; hr_notifs +8 since the stall (link alive)
[  28.5s] still silent after 21s; hr_notifs +16 since the stall (link alive)
```

Battery at 100%, RSSI -56 dBm, and a clean `HTTP 200` subscribe with no
orphaned measurement anywhere - and the ECG stream still stopped after 8.4 s
while Heart Rate kept notifying for another 31 s. So the orphaned measurement
and the 409 are symptoms of the same underlying fault, not its cause. They
happen to appear together because both follow a session in which the sensor
abandoned a measurement.

This also rules out the two cheapest explanations at once: the cell is new and
reads 100%, and the link was strong and demonstrably alive throughout.

### The failure rate: 2/4 trials clean with a full battery

A single run cannot describe an intermittent fault, so `--trials` repeats the
whole cycle and reports a rate. Four trials, 30 s each, 30 s apart, battery
100% on every one:

| trial | subscribe | frames | duty | outcome |
|---|---|---|---|---|
| T1 | `HTTP 200` | 482 | 99.9% | clean |
| T2 | `HTTP 200` | 424 | 87.9% | stalled at 27.7 s |
| T3 | `HTTP 200` | **0** | 0% | no frames at all |
| T4 | `HTTP 200` | 483 | 100.2% | clean |

Every trial had the same battery reading, the same clean subscribe reply and the
same strong link, and half of them failed. T3 is the worst case: not a single
frame, with Heart Rate notifications still arriving. T2 shows the other shape -
a perfect stream for 27.7 s, then silence.

So the fault is intermittent rather than positional: it is not a warm-up, not a
bad session, and not something that recovers permanently once fixed. Roughly
half of sessions die, some immediately and some after tens of seconds.

The clean trials are equally informative: 16.04 frames/s, 180 ms worst gap, no
discarded frames. When the sensor does stream, it streams perfectly, and the
host side is not the variable.

### What this means for the firmware

Nothing in the firmware can prevent the sensor from abandoning the measurement;
the reclaim path, watchdog and forced reconnect are already the correct
response, and they are why the dongle recovers while this script simply
observes the stall.

The lever is the sensor: its firmware version, and the measurement's own
lifetime. The failure looks like a sensor-side fault in the ECG measurement's
own engine rather than a radio problem: it appears at unpredictable points
(immediately, or after 8-28 s), it happens on roughly half of sessions under
identical conditions, and when it does not happen the stream is flawless.

### RETRACTION: the HR "control" was the confounder

The sensor's firmware is in `movesense_fw/dual_gsp_app`, so this became readable
rather than inferable. `HrBridge.cpp` says of the per-beat resource:

```
// Every subscription restarts the AFE beat detector, so the warm-up
// discard window has to be re-armed here and not only at boot.
```

`HrBridge` subscribes to `/Component/MAX3000x/HeartRateData` whenever a client
enables `0x2A37` notifications, and the SDK's own description of that resource
confirms the AFE is shared:

```
setting to disable / re-enable /Meas/HR resource directly from MAX3000x chip
```

One MAX3000x produces both `/Meas/ECG` and HR. The host script enabled `0x2A37`
as a "link-liveness control", which means **the control was kicking the same
front end that was producing the ECG**.

Re-running the soak with `--no-hr-control`:

| configuration | trials clean |
|---|---|
| HR control enabled (default) | 2/4 |
| HR control disabled | **5/5** |

And a timestamped test (`tools/hr_interference_test.py`) settles the direction:

```
    1s 10 ##########
    2s 17 #################
    3s 16 ################
    4s 15 ###############
    5s  0
    ...
   16s  0  <-- HR on
```

ECG stopped at 5 s, eleven seconds before HR was enabled, and never resumed. So
"ECG stalled while HR kept notifying" does not mean a healthy sensor with a
broken measurement. It means the ECG stream was already dead and the HR path -
a different, slower consumer of the same AFE - was still alive. The liveness
control was measuring something else.

### RESOLVED NEGATIVE: HR is not the cause

The HR theory was tested on the dongle with a compile-time switch
(`HR_OFF=1 west build ... -d build_hr_off`), which drops the `0x180D`
subscription and the GSP `/Meas/HR` fallback - both consumers of the
MAX3000x - and logs `HR disabled by HR_OFF` on each subscribe.

Verified on the running firmware: every health line reports `hr=0`.

| build | HR consumers | stalls in ~3 min | worst gap |
|---|---|---|---|
| v22 | `hr=31..47` | 7 | 9,808 ms |
| v23 (`HR_OFF`) | `hr=0` | 5 | 8,376 ms |

Removing HR entirely did not fix the stalls, so the host-side 5/5-vs-2/4
difference was not causal. That comparison was also weak evidence on its own:
five and four trials cannot separate a real effect from a coin flip, and the
follow-up host run (77% battery, two connect failures) was confounded.

The earlier HrBridge reasoning was sound in mechanism - one MAX3000x does serve
both - but the effect is not decisive in practice.

### What is actually established

- The host-side tool, the decode and the framing are correct: 16.03-16.04
  frames/s against an ideal 16.00, zero discarded frames, 180-210 ms worst gap
  on clean trials.
- Neither RF, nor battery, nor ECG sample rate, nor HR consumers explain the
  stalls. Each was tested and each came back negative.
- The stall is in the sensor's ECG measurement or its GSP delivery path, at an
  unpredictable point in the session, and the dongle's watchdog recovers it by
  releasing and re-subscribing.
- `dropped=5` appeared in one health line, the first time the dongle has
  reported dropping records.

### Next step: instrument the sensor firmware

The dongle side has now been eliminated as far as compile-time changes can take
it. The next questions are only answerable from inside the sensor app
(`movesense_fw/dual_gsp_app`, built in the Docker container):

- Does `/Meas/ECG` itself stop producing notifications, or does the sensor keep
  sampling and only stop delivering over GSP? Logging the ECG notification
  arrival in the app distinguishes a measurement fault from a delivery fault.
- Is the stall preceded by a gap in the ECG provider, by an error result, or by
  nothing at all? A watchdog line in the app, printing notification counts and
  the last inter-arrival interval, answers this directly.
- Does the AFE report a fault (lead-off, saturation, electrode) at the moment of
  the stall? `/Component/MAX3000x/Register` reads are already exposed over GSP.

Instrumenting the app is the next move; the container builds it via
`movesense_fw/build.sh`.

### Rate is not the trigger

The obvious hypothesis was that the sensor cannot sustain the buffer drain at
full rate, which would make 128 Hz reliable. Tested, and it does not hold:

| rate | trials clean | ideal | observed when clean |
|---|---|---|---|
| 256 Hz | 2/4 | 16.00 fr/s | 16.04 fr/s, 180 ms gap |
| 128 Hz | 3/4 | 8.00 fr/s | 8.01-8.05 fr/s, 197-256 ms gap |

One 128 Hz trial still stalled exactly like the 256 Hz failures - 167 frames,
then silence while Heart Rate kept notifying. So halving the rate roughly
halves the data but does not remove the fault, and a throughput ceiling is ruled
out. Whatever stalls the measurement is not the volume of samples.

The samples that do arrive decode perfectly at both rates (128.19-128.79 Hz and
255.56-257.41 Hz against ideals of 128 and 256, zero discarded frames), so the
framing and decode are right at either rate.

Checking the sensor's firmware version is the next step, in particular whether
it predates fixes to the ECG measurement's lifecycle. The shape of the fault - a
measurement that dies while the device keeps talking, at either rate, on an
unpredictable subset of subscriptions - points at how the measurement is torn
down or re-armed rather than at the transport or the sample volume.

### Running it

The dongle must not hold the sensor: one subscriber at a time.

```
python3 -m pip install bleak
python3 tools/host_gsp_ecg.py --duration 120        # scan, connect, measure
python3 tools/host_gsp_ecg.py --address 0C:8C:DC:1B:64:CD --duration 300
python3 tools/host_gsp_ecg.py -v --duration 30     # dump the first frames
python3 tools/host_gsp_ecg.py --trials 6 --duration 30 --gap 30   # failure rate
```

A single run cannot describe an intermittent fault, so `--trials` is the form to
use when the question is "how often does it fail" rather than "is it failing
now". It repeats the whole connect/measure/disconnect cycle and prints a clean
rate, which is how the 2/4 and 3/4 figures above were produced.

It exits 0 only if the stream covered the whole window (every trial, in soak
mode), so it can run in a loop as a check. Reported per run: battery level,
frames/s against the ideal, whole-run duty, worst gap, discarded frames, device
clock anomalies, the Heart Rate liveness control, and whether the link dropped.
If the link drops, the verdict says so rather than blaming the stream.

## A direct ECG GATT service, to separate the two failure modes

Everything above establishes *that* the stream stops, not *where*. From the host
there is exactly one symptom - silence - and two very different causes produce
it:

- the measurement (`/Meas/ECG`) stopped producing samples, or
- the samples were produced but never delivered over the link.

GSP cannot tell these apart. Its frames carry a timestamp but no sequence
number, and the continuation-packet reassembly has no notion of a frame that was
sent and lost. So GSP silence is ambiguous.

`EcgUplink` (`movesense_fw/dual_gsp_app/EcgUplink.{h,cpp}`) adds a second, direct
path that resolves it. It is a plain custom GATT service - `00EC0001-...` with
one NOTIFY characteristic `00EC0002-...` - built on the SDK's
`custom_gattsvc` pattern, the same one GSP itself uses. Enabling notifications on
the characteristic subscribes to `/Meas/ECG/256` internally; every provider
notification becomes one GATT notification, with no GSP command layer, no client
reference and no continuation frames.

Payload, 72 bytes per notification at 256 Hz, little endian:

| offset | type | field |
|---|---|---|
| 0 | uint32 | timestamp, ms |
| 4 | uint32 | **sequence**, +1 per frame sent |
| 8 | int32[16] | samples, 0.38147 uV per count |

The sequence number is the whole point. It costs four bytes and makes the
ambiguity disappear:

| observation | conclusion |
|---|---|
| seq jumps forward | frames were sent and lost - **delivery** fault, fixable here |
| seq unbroken while frames stop | nothing was ever sent - **measurement** fault, inside the AFE or its driver |

72 bytes is one notification under the existing MTU 92 (89 bytes of payload), so
the 1:1 mapping between frames and notifications holds and a stall localizes to
a single packet. That constraint is why the sample array is capped at 16 rather
than sized generously: a frame that does not fit one notification is rejected by
the put, not split.

Two GATT resources grew in `App.cpp` to make room: `vs_uuid_count` 6 -> 8 and
`attr_tab_size` 1896 -> 2048. A table too small for the services fails at
runtime with 507 from `/Comm/Ble/GattSvc`, never at build time. Those are
SoftDevice allocations, so if the sensor ever fails to advertise after this
change the first thing to try is lowering `attr_tab_size` back - see the RAM
split note in the firmware README.

GSP keeps running alongside it. Both subscribe to the same
`/Meas/ECG/256`, and whiteboard fans one measurement out to every subscriber, so
a single session can watch both paths receive the same samples. That is what
makes the comparison meaningful: two separate runs cannot, because the stall is
intermittent and identical conditions produce both outcomes.

### Running it

The dongle must not hold the sensor: one connection at a time.

```
python3 tools/host_ecg_uplink.py --duration 120      # scan, connect, measure
python3 tools/host_ecg_uplink.py --address 0C:8C:DC:1B:64:CD --duration 300
python3 tools/host_ecg_uplink.py -v --duration 30    # dump the first frames
python3 tools/host_ecg_uplink.py --trials 6 --duration 30 --gap 30
```

The verdict names the failure mode rather than only measuring it: transport loss
when the sequence records frames the client never saw, measurement stall when the
sequence is unbroken across the gap. Exit code is 0 only if the stream was
healthy for the whole observation window, so it works as a soak check.

Before flashing, check the image matches the hardware. The sensor is a Movesense
**Flash**, which needs `HWCONFIG=SS2_NAND`. The SDK defaults to `SS2` (EEPROM),
and an EEPROM image does not install on a Flash board. `build.sh` now defaults to
NAND and names the output accordingly:

```
movesense_fw/dist/Movesense_dfu_dual_gsp_app_nand.zip
```


### Result: the direct stream stalls the same way, so it is not GSP

First run of the new path, 90 s from the host adapter. The stream degraded
progressively and the link dropped at 37.5 s:

| measure | value |
|---|---|
| frames delivered | 208 of ~1440 expected |
| frames/s | 6.49 (ideal 16.00) |
| duty | 34.6% |
| worst gap | 2055 ms |
| **frames lost on the link** | **0** |
| malformed / oversize | 0 / 0 |

The first ~18 s ran at 16.28 fr/s, essentially perfect, then decayed:
6.10, 4.00, then 8.90 for one window before collapsing to 1.80.

The sequence number settles the question the whole module was built to answer.
`seq_lost_frames = 0` over 208 frames, with `notifications == frames` exactly.
Every frame the sensor sent was decoded by the client; nothing was dropped in
the transport. So the fault is **not** in GSP, not in sbem serialisation, not in
the continuation-packet path, and not in the notify delivery layer. A completely
different, protocol-free path reproduces it exactly.

That is the strongest evidence yet that the stall lives in the measurement
(`/Meas/ECG` and the MAX3000x underneath it), and it means no amount of GSP or
dongle work can fix it.

### Correction: the device timestamp is NOT sample-counted

An earlier note here claimed `ECGData.timestamp` was anchored on the first frame
and free-ran against sample count, making it blind to delivery delay. This run
shows that is wrong.

- 208 frames is 13.0 s of samples at 62.5 ms each.
- The measured device clock span was **32023 ms**, matching the 32.1 s wall span.

So the timestamp advances with real time, and the 37 inter-frame timestamp gaps
over 3x a frame mean sample blocks were genuinely **skipped**, not merely
queued. Two consequences:

- `transport loss` and `measurement stall` are cleanly separable (as designed).
- A divergence between device-clock progress and sample count is a direct
  measure of lost samples, reported here as `gaps>3x_frame`.

The earlier claim would have hidden exactly the signal now being observed, so it
is worth stating plainly that it was wrong.

### Caveat on this run: the MTU

The negotiated ATT MTU was **23**, so the link carried 20-byte payloads against a
72-byte frame. The fragmentation itself is harmless; what matters is throughput.
One frame per connection event at a typical 7.5-15 ms interval puts a ceiling
around 66-133 fr/s, which is well above the 16 fr/s needed.

Two observations are harder to square with MTU 23 alone:

- the run still averaged 6.49 fr/s, far below that ceiling;
- the device timestamp was 5504 ms at connect but only ~2 s of samples had been
  produced, implying the measurement started before at a full 256 Hz rate, which
  only fits if MTU 92 was negotiated earlier.

So the link may have renegotiated partway through, making this a mixture of two
regimes rather than one clean profile. Before treating the shape as final, the
run should be repeated with `--address` (skipping the scan) and the MTU printed
alongside each report.

None of this changes the main result: `seq_lost_frames = 0` counts frames the
sensor *chose to send*, and an MTU can throttle delivery but cannot create
frames.

