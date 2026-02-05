# Zwift Data Visualization Server

Real-time visualization server for Zwift sensor data from the nRF52840 dongle.

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

## Features

- Real-time data visualization from serial port
- Time window selection (1 min, 5 min, 60 min)
- Toggle signal visibility
- Displays: Heart rate, Power meter power/cadence, Trainer speed

## Configuration

Edit `config.conf` to adjust:
- Serial port path
- Buffer size (max minutes to retain)
- Update interval
