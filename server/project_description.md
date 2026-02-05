# Zwift Data Visualization Server

## Project Overview

This is the second part of a project designed to help pinpoint a bug in a truTrainer smartroller. The goal is to graph real-time data from both the sensors and Zwift to detect patterns in the variation of resistance when climbing.

## Architecture

### Part 1: Data Collection Dongle

The first part of the project consists of an nRF52840 dongle with custom firmware that acts as a man-in-the-middle between the sensors and Zwift. This device intercepts and extracts information exchanged between the game and the sensors.

- **Firmware Location**: `/home/christian/Documents/programmation/zwift/dongle` (or `../dongle` relative to this file)
- **Data Output**: Serial port
- **Sample Data**: `sample.json`

### Part 2: Visualization Server (This Project)

A Python-based web server using Flask to visualize the collected data in real-time.

## Technical Stack

- **Language**: Python
- **Web Framework**: Flask
- **Visualization Library**: Plotly (planned)

## Features

### Initial Data Visualization

The following metrics will be plotted initially:
- Heart rate
- Power meter power
- Power meter cadence
- Trainer speed
- Trainer cadence

*Note: Additional metrics will be added later.*

### User Controls

- **Time Window Selection**: Users can select the graph width from:
  - 1 minute
  - 5 minutes
  - 60 minutes
- **Signal Visibility Toggle**: Users can turn on/off the visibility of each signal individually
