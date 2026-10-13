# USB device capture → Raspberry Pi gadget

Capture what a USB device reports about itself from a Mac, then make a
Raspberry Pi Zero 2 W present itself the same way. With the patched kernel
modules, the clone of the SteelSeries Arctis GameBuds in `headphones.json` has
byte-identical device, configuration and HID report descriptors and the same
string table, and audio works both ways at full speed.

| Path | What it is |
|---|---|
| `capture_usb.py` | Mac: capture, show, compare and refresh device captures |
| `gadget_plan.py` | Mac: capture → gadget plan (the exact configfs writes) |
| `gadget.sh` | Pi: apply a plan (`up` / `down` / `list`) |
| `image/usbpi.toml` | Everything that goes on the Pi's card |
| `image/overlay/` | Static files for the card's Alpine overlay |
| `build_card.py` | Mac: build the card from the image config |
| `gadgets/*.plan` | Generated plans (committed, so changes show in diffs) |
| `image/packages.lock` | Generated: packages cached on the card, with hashes |
| `alpine-module/` | Patched gadget modules for the Pi's Alpine kernel, built in Docker |
| `pi_console.py` | Mac: run commands on the Pi over its USB serial debug console |
| `buildroot/` | Buildroot tree for a self-contained image (work in progress) |

## 1. Capture (Mac)

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyusb pyserial tomli   # once; needs: brew install libusb
.venv/bin/python capture_usb.py capture --wait -o headphones.json                # then plug the device in
sudo .venv/bin/python capture_usb.py capture --vid 0x1038 --pid 0x230a --seize -o headphones.json
.venv/bin/python capture_usb.py show headphones.json
```

`--seize` detaches macOS's drivers (sudo needed), so audio-class and HID
requests go through. Use `--bus` when two identical devices are plugged in.
Use Apple's `/usr/bin/python3` for the venv; Homebrew's Python 3.13.16 reports
an empty macOS version, which crashes pip.

## 2. Build the card (Mac, needs Docker, e.g. Colima)

```bash
alpine-module/build.sh                                  # patched modules -> out/modules/
.venv/bin/python build_card.py image/usbpi.toml         # -> out/card-usbpi/ + manifest
.venv/bin/python build_card.py image/usbpi.toml --install /Volumes/ALPINE
```

Edit `image/usbpi.toml` to change what's on the card: Alpine release,
packages, services, boot modules, debug console, gadgets and their plan
options, and which gadget starts at boot. Secrets stay outside the repo:
`wifi.local` holds `SSID`, `PASSWORD` and `COUNTRY` (the Zero 2 W only does
2.4 GHz), and your SSH public key is read from `~/.ssh`. Builds are
reproducible: the same inputs give the same `out/card-usbpi.manifest`.

To reach the card without a card reader: power the Pi with no card (it waits
in USB boot mode), insert the card, then run
`rpiboot -d "$(brew --prefix rpiboot)/share/rpiboot/msd"`. The card first
needs a FAT32 (MBR) format.

The Pi runs Alpine diskless: it reinstalls the configured packages from the
card at every boot, offline. It joins Wi-Fi with root SSH by key, loads the
patched modules, and exposes the USB debug console (`pi_console.py`), or
starts the `[boot] gadget`.

## 3. Clone and verify

```bash
ssh root@<pi> sh /etc/usbpi/gadget.sh up /etc/usbpi/gadgets/gamebuds.plan
.venv/bin/python capture_usb.py capture --vid 0x1038 --pid 0x230a --bus <bus> -o out/pi.json
.venv/bin/python capture_usb.py compare headphones.json out/pi.json
ssh root@<pi> sh /etc/usbpi/gadget.sh down clone        # back to the debug console
```

To try a plan without rebuilding the card, generate it with `gadget_plan.py`,
copy it over and run `gadget.sh up`. To try new modules, copy them over and run
`sh /etc/usbpi/pi_kmods.sh swap DIR`. On the Pi, the `Capture Rate` ALSA control
is 0 until the host starts playing; recording (`arecord -D hw:0,0`) fails with an
I/O error until then.

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
  - an empty name means no string, and with all names empty it claims no
    string IDs
  - string attributes no longer report short writes, which made writers
    retry forever
- **0002 `f_hid`:** per-interface name (empty means none), `bcd_hid`,
  `country_code`.
- **0003 `configfs`:**
  - an empty manufacturer, product or serial gets string index 0
  - `hide_strings` keeps a string (and its ID) without referencing it

Not reproduced: strings beyond a contiguous run 1..N (the GameBuds' index-33
per-unit identifier, which is also redacted from `headphones.json`). Only
probing tools ask for those.
