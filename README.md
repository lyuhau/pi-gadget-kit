# USB device capture → Raspberry Pi gadget

`capture_usb.py` (Mac) reads everything a USB device reports about itself:
descriptors, strings, HID report descriptors, and audio sample-rate and volume
ranges. `pi_gadget.py` (Pi) turns that capture into a Linux USB gadget, so the
Pi presents itself to a host the same way.

## 1. Capture (Mac)

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyusb   # once; needs: brew install libusb
.venv/bin/python capture_usb.py capture --wait -o headphones.json  # then plug in the device
.venv/bin/python capture_usb.py show headphones.json
```

Use Apple's `/usr/bin/python3` for the venv. Homebrew's Python 3.13.16 reports
an empty macOS version, and that crashes pip.

## 2. Reproduce (Pi Zero / Zero 2 W / 4 / 5, Raspberry Pi OS)

```bash
scp pi_gadget.py headphones.json pi@<pi>.local:
sudo python3 pi_gadget.py prep && sudo reboot   # once
sudo python3 pi_gadget.py up headphones.json    # --test-ids to use 1209:0001 instead
sudo python3 pi_gadget.py down
```

Connect the Pi's **data** USB port to the Mac (on a Zero, the inner port
labelled "USB"). Use Wi-Fi for SSH, since the data port is busy being the
device.

## 3. Verify (Mac)

```bash
.venv/bin/python capture_usb.py capture --wait -o pi.json   # plug in the Pi / run "up"
.venv/bin/python capture_usb.py compare headphones.json pi.json
```

Expect some differences. The kernel generates its own descriptors, the Pi
reports USB 2.0, and UAC2 forces device class 0xEF. The settings that should
match are the IDs, strings, formats, rates, volume ranges, terminal types and
HID reports.
