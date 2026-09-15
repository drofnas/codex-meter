#!/usr/bin/env python3
"""Capture a bounded USB log without holding the ESP32 in reset."""

import argparse
from pathlib import Path
import time

import serial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reset", action="store_true", help="Pulse EN to capture a fresh boot")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with serial.Serial(port=None, baudrate=115200, timeout=0.2) as port:
        port.dtr = False
        port.rts = False
        port.port = args.port
        port.open()
        if args.reset:
            port.rts = True
            time.sleep(0.1)
            port.rts = False
        end = time.monotonic() + args.seconds
        with args.output.open("wb") as output:
            while time.monotonic() < end:
                data = port.read(4096)
                if data:
                    output.write(data)
                    output.flush()
                    print(data.decode("utf-8", errors="replace"), end="", flush=True)


if __name__ == "__main__":
    main()
