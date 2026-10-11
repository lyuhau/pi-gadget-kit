#!/usr/bin/env python3
"""
Run shell commands on the Pi over its USB serial debug console (the root shell
alpine_overlay.py sets up on ttyGS0). For when SSH isn't available.

  pi_console.py 'dmesg | tail'
  pi_console.py --port /dev/cu.usbmodem101 --timeout 30 'rc-status'
"""
import argparse
import glob
import sys
import time
import uuid

import serial


def run(port, command, timeout):
    marker = f"__done_{uuid.uuid4().hex[:8]}__"
    with serial.Serial(port, 115200, timeout=0.2) as s:
        s.write(b"\x03\nstty -echo\n")  # clear any half-typed line, stop echoing input
        time.sleep(0.5)
        s.reset_input_buffer()
        # The marker (split so an echoed command can't match it) ends the read.
        s.write(f"{{ {command.rstrip(';')} ; }} 2>&1; echo; echo {marker[:6]}''{marker[6:]} $?\n".encode())
        out, end = b"", time.time() + timeout
        while time.time() < end:
            out += s.read(4096)
            if marker.encode() in out:
                break
        else:
            sys.stdout.write(out.decode(errors="replace"))
            sys.exit(f"\n[timed out after {timeout}s]")
    text = out.decode(errors="replace").replace("\r", "")
    body, _, tail = text.partition(marker)
    sys.stdout.write(body)
    return int(tail.split()[0]) if tail.split() else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command")
    p.add_argument("--port", help="default: the first /dev/cu.usbmodem*")
    p.add_argument("--timeout", type=float, default=15)
    a = p.parse_args()
    port = a.port or next(iter(sorted(glob.glob("/dev/cu.usbmodem*"))), None)
    if not port:
        sys.exit("no /dev/cu.usbmodem* device: is the Pi plugged in and booted?")
    sys.exit(run(port, a.command, a.timeout))


if __name__ == "__main__":
    main()
