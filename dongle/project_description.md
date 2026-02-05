# Fitness Sensor Relay — Project Description 🚴‍♂️

## Summary ✨
This project implements an intermediate Bluetooth Low Energy (BLE) relay that sits between fitness sensors (HR strap, power meter pedals, smart trainer) and Zwift. It acts as both a BLE central (connecting to sensors) and peripheral (advertising to Zwift Companion app), forwarding Heart Rate, Cycling Power, and Fitness Machine data. This enables debugging BLE traffic and works around unreliable Bluetooth in containerized environments. Connected sensors are automatically saved to non-volatile storage (NVS) and given priority reconnection access on boot.

## Motivation 💡
I observed intermittent resistance drops on a TruTrainer smartroller during specific slopes/speeds. Because Zwift runs under Wine in a Docker container on Manjaro, direct Bluetooth access is unreliable. Using a BLE relay allows the Zwift Companion app to connect to the relay instead of the sensors, enabling observation and debugging of the BLE traffic.

## Pipeline
Zwift <-> Companion App <-> Fitness Relay (this firmware) <-> Sensors (Polar H10, Favero Assioma, TruTrainer)

## Hardware & Software Requirements 🔧
- Hardware: nRF52840-based dongle (e.g., `nrf52840dongle_nrf52840`)
- Toolchain: Zephyr SDK v4.2.99 / Nordic NCS v3.2.1, `west` build tool, and `cmake`
- Zephyr features used: Bluetooth dual role (central + peripheral), GATT client/server, notifications, multiple simultaneous connections, NVS (Non-Volatile Storage)
- Key configs: `CONFIG_BT_MAX_CONN=4`, `CONFIG_HEAP_MEM_POOL_SIZE=2048`, `CONFIG_LOG_BUFFER_SIZE=2048`, `CONFIG_NVS=y`
- Storage: Uses existing 16KB storage_partition at 0xdc000 for device persistence

## Build & Run ⚙️
1. Build: use nRF Connect vscode extension
2. Flash: use nRF Connect Desktop | Programmer
3. Monitor serial logs using nRF Serial extension

## Notes & Tips 📝
- The firmware advertises itself as both HR and Cycling Power service so the companion app recognizes all sensors.
- Supports up to 4 simultaneous connections (HR strap + left/right pedals + trainer).
- Only connects to devices with known names (not MAC addresses) to avoid connecting to random devices.
- **NVS Persistence**: Connected sensors are automatically saved to flash memory with up to 4 devices persisted across reboots.
- **Exclusive Reconnection Window**: On boot, saved devices get 6-minute exclusive window to reconnect before accepting new devices.
- Device list JSON output includes "saved":true/false field to indicate persistence status.
- Connected devices marked with "*" in periodic device list printout.
- Advertising automatically restarts when Zwift disconnects, enabling reconnection.
- Dynamic allocations used for device list management.
- A periodic timer prints the current device list every 30 seconds for easier inspection.
- Note: build and flashing performed using external Nordic tools (nrfutil or Nordic Programmer GUI); `west flash` may not work.

## Implementation Status 📊

### ✅ Completed
- **Heart Rate Service (0x180D)**: Full support
  - Discovers and subscribes to HR Measurement (0x2A37)
  - Parses BPM from notifications
  - Forwards to local GATT service for Zwift

- **Cycling Power Service (0x1818)**: Full support
  - Discovers and subscribes to CP Measurement (0x2A63)
  - Parses power, pedal balance, crank revolution data
  - Forwards to local GATT service

- **Multiple Simultaneous Connections**: Up to 4 devices
  - Tracks service type per subscription (HR vs CP)
  - Correct data routing even when device only has subset of services
  - Connection slot management

- **Device Management**:
  - Persistent device list with name capture
  - Only connects when device name is known
  - `was_connected` flag keeps devices in list for reconnection
  - Device list cleanup preserves connected devices
  - Periodic printout with "*" indicator for connected devices
  - **NVS Storage**: Auto-save on successful connection (up to 4 devices)
  - **Exclusive Window**: 6-minute priority reconnection period on boot for saved devices
  - Device persistence survives power cycles and firmware updates

- **Connection Handling**:
  - Scan restart error handling (-EALREADY)
  - Advertising restart after peripheral disconnect (Zwift reconnection)
  - Proper address comparison using `bt_conn_get_dst()`

- **Fitness Machine Service (0x1826)**: Full support
  - Discovers and subscribes to Indoor Bike Data (0x2AD2)
  - Parses speed, cadence, power, resistance from notifications
  - Bidirectional Control Point (0x2AD9) for ERG mode
  - Forwards control commands from Zwift to trainer
  - Routes trainer responses (indications) back to Zwift
  - Enables structured workouts and resistance control

### 🔥 Thermal Management & Grade Limiting
- **Adaptive Grade Limiting**: Learns trainer thermal limits and prevents overheating
  - Detects thermal release events via FTMS bit 8 (0x0100 flag)
  - Speed-based lookup table: 50 buckets covering 10000-30000 m/h range (400 m/h per bucket)
  - No restrictions below 10000 m/h (rider safety - balance) or above 30000 m/h (sufficient cooling)
  - Intercepts Set Indoor Bike Simulation (0x11) commands from Zwift
  - Modifies grade parameter to stay within learned safe limits
  - Natural decay: 1/10% per hour to adapt to changing ambient temperature
  - NVS persistence: learned limits survive reboots
  - JSON output includes "limited":true flag and both original/applied grades

### ✅ Feature Complete
All planned features have been implemented and tested:
- Multi-sensor relay (HR, Power, Trainer)
- Persistent device storage with priority reconnection
- Full FTMS support including ERG mode control
- Robust connection management and error handling
- Adaptive thermal management with grade limiting

## File Structure 📁

### Core Application Files
- **src/main.c** (150 lines)
  - Application entry point and Bluetooth initialization
  - Peripheral role setup: advertising as HR/CP/FTMS service
  - Connection callbacks: connected(), disconnected()
  - Starts scan for central role after peripheral advertising begins

- **src/device_manager.c** (280 lines)
  - Central device discovery and connection management
  - Scan callback: device filtering, name-based acceptance
  - GATT service/characteristic discovery
  - Connection tracking: device list with name, address, service mask
  - Subscription management with notification routing
  - NVS integration: auto-save on connection, exclusive window logic
  - Periodic device list printer (JSON format)

- **src/device_manager.h** (45 lines)
  - Public API for device manager
  - Function declarations: init, connection handling, discovery
  - Exposes connection lookup and device saving functions

### Service Handlers
- **src/notification_handler.c** (250 lines)
  - Central notification callback dispatcher
  - HR parsing: extracts BPM from 0x2A37 characteristic
  - CP parsing: power, pedal balance, crank revolutions from 0x2A63
  - FTMS parsing: Indoor Bike Data (0x2AD2) - speed, cadence, power, resistance
  - Data rebroadcast to peripheral GATT service for Zwift
  - Handles variable-length notifications with flag-based field presence

- **src/gatt_discovery.c** (243 lines)
  - GATT service and characteristic discovery state machine
  - discover_func(): primary service discovery callback
  - Handles multi-service discovery queue (HR → CP → FTMS)
  - Characteristic discovery for each service
  - CCC descriptor discovery for notifications/indications
  - Automatic subscription setup after discovery completes
  - Per-connection discovery state tracking

- **src/gatt_services.c** (69 lines)
  - Peripheral GATT service definitions (static declarations)
  - HR Service (0x180D): HR Measurement characteristic with CCC
  - CP Service (0x1818): CP Measurement characteristic with CCC
  - FTMS Service (0x1826): Indoor Bike Data, Control Point, Status characteristics
  - Measurement buffers for data forwarding
  - Service registration via BT_GATT_SERVICE_DEFINE macros

- **src/peripheral_service.c** (180 lines)
  - Peripheral GATT server implementation
  - Defines HR/CP/FTMS services and characteristics
  - Notification enable/disable callbacks (CCC descriptors)
  - Data forwarding functions: receives from central, notifies to Zwift
  - FTMS Control Point write handler: forwards commands to trainer
  - Indication handling: routes trainer responses back to Zwift

- **src/peripheral_service.h** (35 lines)
  - Public API for peripheral GATT services
  - Function declarations for service initialization and data forwarding

### Persistence Layer
- **src/nvs_storage.c** (171 lines)
  - Non-Volatile Storage implementation using Zephyr NVS
  - nvs_storage_init(): mounts filesystem at storage_partition, loads saved devices
  - nvs_save_device(): persists device (address, name, service mask) to NVS slots 1-4
  - nvs_load_devices(): returns saved device array
  - nvs_is_device_saved(): address lookup in saved list
  - Handles 3-sector wear leveling, 16KB partition

- **src/nvs_storage.h** (25 lines)
  - Public API for NVS operations
  - Function declarations for init, save, load, check operations

### Shared Definitions
- **src/common.h** (80 lines)
  - Shared data structures and constants
  - `struct device_info`: addr, name, conn, subscriptions, service mask, saved flag
  - `struct saved_device`: persistent device record structure
  - Constants: MAX_SAVED_DEVICES (4), EXCLUSIVE_WINDOW_MS (360000)
  - Service UUIDs and characteristic handles

### Configuration Files
- **prj.conf** (20 lines)
  - Zephyr Kconfig options
  - Bluetooth config: central, peripheral, GATT client/server, max connections
  - NVS config: FLASH, NVS subsystem
  - Logging config: buffer sizes, processing latency

- **CMakeLists.txt** (8 lines)
  - Build system configuration
  - Links all source files: main.c, device_manager.c, notification_handler.c, peripheral_service.c, nvs_storage.c

- **boards/nrf52840dongle_nrf52840.overlay** (8 lines)
  - Device tree overlay for nRF52840 dongle
  - Assigns storage_partition (16KB at 0xdc000) to zephyr,storage via chosen node
  - Enables NVS filesystem access

## random notes
- to capture the output: stty -F /dev/ttyACM0 115200 && cat /dev/ttyACM0 | sed '/^$/d' | tee log.log
---
*Keep this file updated with any changes to build steps or required Kconfig options.*