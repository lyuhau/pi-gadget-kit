# USB device capture → Raspberry Pi gadget

Capture what a USB device reports about itself from a Mac, then make a
Raspberry Pi Zero 2 W present itself the same way. With the patched kernel
modules, the clone of the SteelSeries Arctis GameBuds in `headphones.json` has
byte-identical device, configuration and HID report descriptors, and audio
works both ways at full speed.

| Path | What it is |
|---|---|
| `capture_usb.py` | Mac: capture, show, compare, and refresh device captures |
| `pi_gadget.py` | Pi: turn a capture into a configfs gadget (`up` / `down`) |
| `alpine_overlay.py` | Mac: headless Alpine setup (Wi-Fi, SSH, USB debug console) for a fresh card |
| `pi_console.py` | Mac: run commands on the Pi over the USB serial debug console |
| `alpine-module/` | Patched gadget modules for the Pi's Alpine kernel, built in Docker |
| `buildroot/` | Buildroot tree for a self-contained image (work in progress) |

## 1. Capture (Mac)

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyusb pyserial   # once; needs: brew install libusb
.venv/bin/python capture_usb.py capture --wait -o headphones.json          # then plug the device in
.venv/bin/python capture_usb.py show headphones.json
```

Use Apple's `/usr/bin/python3` for the venv. Homebrew's Python 3.13.16
reports an empty macOS version, which crashes pip.

## 2. Pi card (Alpine, diskless)

With no SD card, the Pi boots into USB boot mode. Insert the card after it's
powered, then `rpiboot -d "$(brew --prefix rpiboot)/share/rpiboot/msd"`
exposes the card to the Mac. Format it FAT32 (MBR), copy the contents of the
`alpine-rpi-*-aarch64.tar.gz` tarball onto it, then:

```bash
alpine-module/build.sh                                       # patched modules -> out/modules/ (needs Docker, e.g. Colima)
/usr/bin/python3 alpine_overlay.py /Volumes/ALPINE --kmods out/modules
```

`wifi.local` (gitignored) holds `SSID`, `PASSWORD` and `COUNTRY`; the Zero 2 W
only does 2.4 GHz. Boot the Pi from the card. It joins Wi-Fi as root with your
SSH key, loads the patched modules, and exposes a USB serial root shell
(`pi_console.py`) until a gadget takes over the port. Changes on the Pi persist
only after `lbu commit`. Install `python3 alsa-utils` with an apk cache on the
card (`/etc/apk/cache` → `/media/mmcblk0p1/cache`) so they survive reboots.

## 3. Clone and verify

```bash
scp pi_gadget.py headphones.json root@<pi>:
ssh root@<pi> python3 pi_gadget.py up headphones.json        # down: restore the debug console
.venv/bin/python capture_usb.py capture --wait -o pi.json    # start before "up"
.venv/bin/python capture_usb.py compare headphones.json pi.json
```

To try new modules without rebooting, copy them to the Pi and run
`sh pi_kmods.sh swap DIR`. Recording on the Pi (`arecord -D hw:0,0`) must
start after the host has started playing, or it fails with an I/O error.

## Kernel patches (`alpine-module/patches/`)

Built against the exact source of Alpine's `linux-rpi` 6.18.52 (checked
against the APKBUILD's sha512sums). The headers package is cached in the
Docker volume in case Alpine's mirror moves on.

- **0001 `f_uac1`:**
  - correct full-speed descriptors: stock reused the high-speed `bInterval`
    of 4, which means 8 ms at full speed, and broke full-speed audio
  - configurable terminal types, packet sizes, endpoint numbers, terminal
    and unit IDs, feature-unit control size, and an optional interrupt
    endpoint
  - an empty name means no string
  - string attributes no longer report short writes, which made writers
    retry forever
- **0002 `f_hid`:** per-interface name (empty means none), `bcd_hid`,
  `country_code`.
- **0003 `configfs`:** an empty manufacturer, product or serial gets string
  index 0.

Not yet reproduced: strings the device has but never references (the
GameBuds have three). Only probing tools ask for those.
