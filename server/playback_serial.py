#!/usr/bin/env python3
"""
Virtual serial port playback script for sample.json.

This script creates a virtual serial port and replays JSON messages from
sample.json at the original timing based on the 'ts' timestamp field.
"""
import argparse
import json
import os
import pty
import sys
import time
from typing import Optional, Tuple


def is_json_line(line: str) -> bool:
    """Check if a line is a JSON message (starts with '{')."""
    return line.strip().startswith('{')


def parse_timestamp(line: str) -> Optional[int]:
    """
    Parse timestamp from a JSON line.
    
    Args:
        line: JSON line string
        
    Returns:
        Timestamp in milliseconds, or None if parsing fails
    """
    try:
        data = json.loads(line.strip())
        return data.get('ts')
    except (json.JSONDecodeError, AttributeError):
        return None


def create_virtual_serial() -> Tuple[int, str]:
    """
    Create a virtual serial port using pty.
    
    Returns:
        Tuple of (master_fd, slave_name)
        master_fd: File descriptor for writing
        slave_name: Path to the virtual serial port
    """
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    
    # Make it readable/writable
    os.chmod(slave_name, 0o666)
    
    return master_fd, slave_name


def playback_file(file_path: str, master_fd: int, speed_multiplier: float = 1.0, loop: bool = True):
    """
    Playback JSON messages from file to virtual serial port.
    
    Args:
        file_path: Path to sample.json file
        master_fd: File descriptor for the virtual serial port
        speed_multiplier: Speed multiplier (1.0 = normal, 2.0 = 2x speed, etc.)
        loop: If True, replay the file continuously (default: True)
    """
    print(f"Reading from {file_path}...")
    
    json_lines = []
    timestamps = []
    
    # First pass: collect all JSON lines and their timestamps
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if is_json_line(line):
                ts = parse_timestamp(line)
                if ts is not None:
                    json_lines.append(line.rstrip())
                    timestamps.append(ts)
    
    if not json_lines:
        print("Error: No valid JSON lines found in file!")
        return
    
    print(f"Found {len(json_lines)} JSON messages")
    print(f"Time range: {timestamps[0]}ms to {timestamps[-1]}ms")
    duration = (timestamps[-1] - timestamps[0]) / 1000.0 / speed_multiplier
    print(f"Duration: {duration:.2f} seconds (at {speed_multiplier}x speed)")
    
    # Calculate the duration of one loop in milliseconds
    loop_duration_ms = timestamps[-1] - timestamps[0]
    
    if loop:
        print("\nStarting continuous playback (looping)... (Press Ctrl+C to stop)\n")
    else:
        print("\nStarting playback (once)... (Press Ctrl+C to stop)\n")
    
    iteration = 0
    total_messages = 0
    
    try:
        while True:
            iteration += 1
            if loop and iteration > 1:
                print(f"\n--- Loop {iteration} ---\n")
            
            # Calculate timestamp offset for continuous timestamps across loops
            # Each loop adds the full duration of one loop to the timestamps
            timestamp_offset = (iteration - 1) * loop_duration_ms
            
            # Playback with timing
            start_time = time.time()
            
            for i, (line, ts) in enumerate(zip(json_lines, timestamps)):
                # Calculate delay from previous message
                if i == 0:
                    delay = 0
                else:
                    delay_ms = (ts - timestamps[i-1]) / speed_multiplier
                    delay = max(0, delay_ms / 1000.0)  # Convert to seconds
                
                # Wait for the calculated delay
                if delay > 0:
                    time.sleep(delay)
                
                # Modify the JSON to add timestamp offset for continuous timestamps
                if timestamp_offset > 0:
                    try:
                        line_data = json.loads(line)
                        line_data['ts'] = ts + timestamp_offset
                        line = json.dumps(line_data)
                    except (json.JSONDecodeError, KeyError):
                        # If we can't parse/modify, use original line
                        pass
                
                # Write the line to the virtual serial port
                line_bytes = (line + '\n').encode('utf-8')
                os.write(master_fd, line_bytes)
                
                total_messages += 1
                
                # Progress indicator
                if (i + 1) % 100 == 0:
                    elapsed = time.time() - start_time
                    progress = (i + 1) / len(json_lines) * 100
                    if loop:
                        print(f"Loop {iteration}: {i+1}/{len(json_lines)} ({progress:.1f}%) - {elapsed:.1f}s elapsed", end='\r')
                    else:
                        print(f"Progress: {i+1}/{len(json_lines)} ({progress:.1f}%) - {elapsed:.1f}s elapsed", end='\r')
            
            if not loop:
                # Single playback complete
                print(f"\n\nPlayback complete! Sent {len(json_lines)} messages.")
                break
            else:
                # Loop complete, will restart
                print(f"\nLoop {iteration} complete. Restarting...", end='\r')
        
    except KeyboardInterrupt:
        if loop:
            print(f"\n\nPlayback interrupted. Completed {iteration} loop(s), sent {total_messages} messages total.")
        else:
            print(f"\n\nPlayback interrupted. Sent {total_messages}/{len(json_lines)} messages.")
    except Exception as e:
        print(f"\n\nError during playback: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Create a virtual serial port and playback sample.json',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Normal speed playback (loops continuously by default)
  python playback_serial.py sample.json
  
  # Play once (no looping)
  python playback_serial.py sample.json --once
  
  # 2x speed playback (looping)
  python playback_serial.py sample.json --speed 2.0
  
  # Custom output path
  python playback_serial.py sample.json --output /tmp/virtual_serial
        """
    )
    parser.add_argument(
        'file',
        help='Path to sample.json file'
    )
    parser.add_argument(
        '--speed',
        type=float,
        default=1.0,
        help='Playback speed multiplier (default: 1.0 = normal speed)'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Custom path for virtual serial port (optional)'
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Play the file once instead of looping (default: loop continuously)'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}")
        sys.exit(1)
    
    # Create virtual serial port
    print("Creating virtual serial port...")
    master_fd, slave_name = create_virtual_serial()
    
    if args.output:
        # Create symlink if custom path requested
        try:
            if os.path.exists(args.output):
                os.remove(args.output)
            os.symlink(slave_name, args.output)
            print(f"Virtual serial port created: {args.output} -> {slave_name}")
            print(f"  (Actual device: {slave_name})")
        except OSError as e:
            print(f"Warning: Could not create symlink: {e}")
            print(f"Using actual device: {slave_name}")
    else:
        print(f"Virtual serial port created: {slave_name}")
    
    print(f"\nUpdate your config.conf with:")
    print(f"  dongle_serial = {args.output or slave_name}")
    print()
    
    try:
        # Start playback (loop by default, unless --once is specified)
        playback_file(args.file, master_fd, args.speed, loop=not args.once)
    finally:
        # Clean up
        os.close(master_fd)
        if args.output and os.path.exists(args.output) and os.path.islink(args.output):
            os.remove(args.output)
        print("\nVirtual serial port closed.")


if __name__ == '__main__':
    main()
