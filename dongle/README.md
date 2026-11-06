# Z-Relay: Fitness Sensor BLE Relay 🚴‍♂️

A Bluetooth Low Energy relay for fitness sensors, designed to work with Zwift through z_relay_server.

## Overview

Z-Relay is a BLE **central** / GATT client only: it connects to the sensors and writes their Heart Rate, Cycling Power, and Fitness Machine (FTMS) data as JSON lines on the USB serial port. z_relay_server turns that into a DirCon device for Zwift and sends trainer commands back over the same serial port. The dongle does not advertise, so nothing can connect to it over Bluetooth. This enables:

- Debugging BLE traffic between sensors and Zwift
- Working around unreliable Bluetooth in containerized/virtualized environments
- Command translation for trainers that don't support simulation mode

```
Zwift ↔ z_relay_server (DirCon) ↔ USB serial ↔ Z-Relay (this firmware) ↔ Sensors
                                                                          ├── HR Strap (Polar H10)
                                                                          ├── Power Pedals (Favero Assioma)
                                                                          └── Smart Trainer (TruTrainer)
```

## Features

- **Multi-sensor support**: Connect up to 3 devices simultaneously, one per role (trainer, power meter, heart rate)
- **Full service support**: Heart Rate (0x180D), Cycling Power (0x1818), Fitness Machine (0x1826)
- **Bidirectional FTMS**: ERG mode control, resistance commands, structured workouts
- **Command translation**: Converts 0x11 (simulation) to 0x04 (resistance) for trainer compatibility
- **Persistent storage**: Connected sensors saved to NVS, auto-reconnect on boot
- **Priority reconnection**: 6-minute exclusive window for saved devices on startup
- **Thermal management**: Learns trainer limits and prevents overheating via adaptive grade limiting
- **Auto-recovery**: Handles disconnections, restarts scanning as needed

## Requirements

### Hardware
- nRF52840-based dongle (tested with `nrf52840dongle/nrf52840` and Raytac MDBT50Q-CX)

### Software
- Zephyr SDK / Nordic NCS v3.2.1+
- `west` build tool
- nRF Connect for VS Code (recommended)

## Building

### For nRF52840 Dongle:
```bash
west build -b nrf52840dongle/nrf52840
```

### For Raytac MDBT50Q-CX:
```bash
west build -b nrf52840dk/nrf52840 -- -DBOARD_ROOT=. \
    -DDTC_OVERLAY_FILE=boards/raytac_mdbt50q_cx_nrf52840.overlay \
    -DCONF_FILE=boards/raytac_mdbt50q_cx_nrf52840.conf
```

Or using nRF Connect for VS Code:
1. Open the extension
2. Add build configuration
3. Board: `nrf52840dk/nrf52840`
4. Board root: `.` (current directory)
5. Extra CMake arguments: `-DDTC_OVERLAY_FILE=boards/raytac_mdbt50q_cx_nrf52840.overlay`

## Flashing

### nRF52840 Dongle (USB DFU)

Use **nRF Connect Desktop Programmer** or via command line:

```bash
nrfutil pkg generate --hw-version 52 --sd-req=0x00 \
    --application build/zephyr/zephyr.hex \
    --application-version 1 pkg.zip
nrfutil dfu usb-serial -pkg pkg.zip -p /dev/ttyACM0
```

### Raytac MDBT50Q-CX (SWD/J-Link Required)

The Raytac module is a bare module without USB bootloader. You need a **J-Link programmer** or **nRF52 DK** connected to the SWD pins:

**Via nRF Connect Desktop Programmer:**
1. Connect J-Link to module's SWD pins (SWDIO, SWDCLK, GND, VDD)
2. Select J-Link device in Programmer
3. Add `build_1/merged.hex`
4. Click "Erase & Write"

**Via command line:**
```bash
nrfjprog --program build_1/merged.hex --chiperase --verify -r
```

## Monitoring

Capture serial output:
```bash
stty -F /dev/ttyACM0 115200 && cat /dev/ttyACM0 | tee log.log
```

## Quick usage

See [USAGE.md](./USAGE.md) for the button actions and LED status patterns.

## Configuration

Key settings in `prj.conf`:

| Config | Value | Description |
|--------|-------|-------------|
| `CONFIG_BT_MAX_CONN` | 3 | Max simultaneous sensor connections |
| `CONFIG_NVS` | y | Non-volatile storage for device persistence |
| `CONFIG_HEAP_MEM_POOL_SIZE` | 2048 | Heap for dynamic allocations |

## Architecture

```
src/
├── main.c                 # Entry point, BLE init, connection callbacks
├── device_manager.c       # Central scanning, connection management
├── gatt_discovery.c       # GATT service/characteristic discovery
├── notification_handler.c # Parses sensor notifications, prints JSON lines
├── ftms_control_point.c   # FTMS commands from serial, 0x11 to 0x04 conversion
├── serial_cmd.c           # Serial commands from z_relay_server (ftms_cp, info)
├── nvs_storage.c          # Persistent device storage
└── common.h               # Shared structures and constants
```

## Supported Services

| Service | UUID | Features |
|---------|------|----------|
| Heart Rate | 0x180D | BPM measurement |
| Cycling Power | 0x1818 | Power, pedal balance, crank revolutions |
| Fitness Machine | 0x1826 | Indoor bike data, ERG control, simulation parameters |

## FTMS Command Translation

Z-Relay converts Zwift's simulation commands (received from z_relay_server) to resistance commands for trainers that don't support simulation mode:

- **0x11 → 0x04**: Converts "Set Indoor Bike Simulation" to "Set Target Resistance Level"
- Extracts grade percentage from simulation parameters
- Maps grade to appropriate resistance level
- Enables Zwift compatibility with resistance-only trainers

## License

Based on Zephyr RTOS samples. See Zephyr license for details.
