# nRF52840 dongle USB crash — bisect log

Working notes for the `2fe3:0004` CDC-ACM link dying on the backup PCA10059
(`nrf52840dongle/nrf52840`) on host port `1-10`. The primary Raytac module runs
the same application indefinitely.

## Controls that pass (dangle indefinitely, 0 USB errors)

| Build | Notes |
|---|---|
| `samples/subsys/usb/cdc_acm` (upstream) | 130 s idle + 80 s traffic, 54 echoes |
| `samples/bluetooth/hci_usb` (upstream) | 120 s, BLE active, `hci1` up |
| **bisect "minimal" app** | USB + `printk` only; 180 s idle + 240 s port-open |
| **bisect "min+LED"** | minimal + real `led_feedback.c`; 240 s idle + 140 s load |
| **bisect "min+PRINTK_SYNC"** | minimal + `CONFIG_PRINTK_SYNC=y`; 200 s idle + 90 s load |
| **bisect "min+BT"** | minimal + `bt_enable()` and the GATT client config; 240 s idle + 120 s load |

The minimal, min+LED, min+PRINTK_SYNC and min+BT builds all keep
`CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT=y`, `CONFIG_UDC_BUF_COUNT=64`, the
16 KiB TX FIFO overlay and the RC clock, so none of those are the trigger.

## Builds that fail

| Build | First USB reset |
|---|---|
| shipping v1.21 | ~25 s |
| old baseline `ab4dbeb` | ~24 s |
| ours, `bt_enable()` skipped | ~25 s |
| ours, `serial_cmd` poller re-arm removed | ~25 s |
| ours, CDC FIFOs at driver default | ~25 s |
| ours, `led_feedback_init()` removed | ~145 s |
| ours, `board_early_init_hook` emptied (stays 1.8 V) | ~149 s |
| ours, both of the above | ~319 s idle, then fails on port open |

The latency moved with the two power-related changes but the failure never went
away, and `min+LED` (which runs the same LED code) is stable. **The 25/145/319 s
numbers are therefore timing, not cause.** Do not treat the power changes as a
fix.

## Ruled out, each by a build that still failed

- **Bluetooth** — `bt_enable()` skipped still died at 25 s, AND a minimal build
  with `bt_enable()` active plus the full GATT client config is stable for
  240 s idle + 120 s load. Both directions now point away from BT.
- **`serial_cmd` 20 ms poller** — re-arm removed, no change.
- **CDC ACM FIFO sizing** — driver defaults, no change.
- **`led_feedback` / LED blinking** — isolated in `min+LED`, stable.
- **`CONFIG_PRINTK_SYNC=y`** — isolated in `min+PRINTK_SYNC`, stable.
- **`CDC_ACM_SERIAL_INITIALIZE_AT_BOOT=y`** — every stable build has it `=y`,
  so it is not the trigger either.
- **Crystal vs RC clock** — both fail; `hci_usb` runs BLE on XTAL fine.
- **TLP/USB autosuspend** — the working `cdc_acm` sample is also
  `control=auto` / `status=suspended` and never misses a beat.
- **Flashing procedure** — was genuinely wrong (`dfu serial` UART transport
  instead of `dfu usb-serial`, plus an undocumented `--app-boot-validation`);
  corrected, no change in behaviour.

## Not yet tested

Every subsystem tested in isolation is stable, while the full app fails. The
remaining deltas between `min+BT` and shipping are all in the app layer:

- `ecg_stream` / `notification_handler` — the ~125 Hz JSON TX path
- `device_manager` — scanning, connection timeouts, NVS-backed reconnect list
- `gatt_discovery` — service/characteristic enumeration on connect
- `ftms_control_point` / `serial_cmd` — inbound command handling
- `nvs_storage` — flash writes at boot (`nvs_get_device_suffix`)

Next step: add these back to the minimal app one at a time and stop at the first
build that resets. `ecg_stream` + `notification_handler` (the high-rate TX path)
is the most likely candidate; `nvs_storage` (flash write at boot) is the second.

A faster alternative to drifting the minimal build upward is to start from the
shipping app and remove subsystems one at a time, since that searches from the
failing side.

## Build recipes used

Variants live in `/tmp/dongle_*`, built with:

```sh
source /home/christian/ncs/v3.2.1/zephyr/zephyr-env.sh
west build -b nrf52840dongle/nrf52840 -d /tmp/build_<name> /tmp/dongle_<name>
```

A sysbuild image config must be named after the image, e.g.
`sysbuild/dongle_noled.conf` for `project(dongle_noled)`, or the settings are
silently ignored.

Flash with the documented transport (note `usb-serial`, not `serial`):

```sh
nrfutil pkg generate --hw-version 52 --sd-req=0x00 \
    --application build/zephyr/zephyr.hex --application-version 1 pkg.zip
nrfutil dfu usb-serial -pkg pkg.zip -p /dev/ttyACM0
```
