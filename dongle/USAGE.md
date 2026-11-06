# Dongle usage

This dongle is meant to be used as a Bluetooth sensor relay. The main controls are the button and the onboard LED.

## Button actions

Use the board's button (`sw0`):

- Short press (under 2 seconds): print the current device list to the serial console.
- Long press (2 seconds or more): disconnect all connected sensors, clear saved devices, and start a 5-minute scan window so the dongle can re-discover and pair fresh devices.

After power-on, wait a few seconds for the firmware to finish booting before using the button.

## LED output

The LED (`led0`) shows the dongle state:

- Idle: slow single pulse when no sensors are connected.
- Pairing / scan window active: slow blinking pattern while actively scanning for devices.
- Connected: quick blinks equal to the number of connected devices, followed by a short pause.

Example:

- 0 devices connected: idle pulse
- 1 device connected: one quick blink
- 3 devices connected: three quick blinks, then pause

This makes it easy to tell whether the dongle is waiting, scanning, or connected to sensors.
