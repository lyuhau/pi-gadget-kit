# USB device capture → Raspberry Pi gadget

Capture what a USB device reports about itself from a Mac, then make a
Raspberry Pi Zero 2 W present itself the same way. With the patched kernel
modules, a clone of SteelSeries Arctis GameBuds (UAC1 audio plus three HID
interfaces) had byte-identical device, configuration and HID report
descriptors and the same string table, and audio worked both ways at full speed.

| Path | What it is |
|---|---|
| `gadget_plan.py` | Mac: capture → gadget plan (the exact configfs writes) |
| `gadget.sh` | Pi: apply a plan (`up` / `down` / `list`) |
| `image/template.toml` | Template image config: everything that goes on the Pi's card |
| `image/overlay/` | Static files for the card's Alpine overlay |
| `build_card.py` | Mac: build the card from the image config |
| `gadgets/*.plan` | Generated plans: capture + gadget options (gitignored) |
| `image/packages.lock` | Generated: packages cached on the card, with hashes |
| `alpine-module/` | Patched gadget modules for the Pi's Alpine kernel, built in Docker |
| `pi_console.py` | Mac: run commands on the Pi over its USB serial debug console |
| `buildroot/` | Buildroot tree for a self-contained image (work in progress) |

## 1. Capture (Mac)

Captures come from the sibling project
[usb-capture](../usb-capture) (`capture_usb.py`); see its README. Point a
`[gadgets.<name>]` section of your image config at the capture file. Paths are
relative to this repo, e.g. `../usb-capture/captures/mydevice.json`.

This repo's venv only needs `pyserial` (`pi_console.py`) and `tomli`
(`build_card.py` on Python < 3.11):

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyserial tomli
```

## 2. Build the card (Mac, needs Docker, e.g. Colima)

```bash
cp image/template.toml image/usbpi.toml                 # once, then fill it in (gitignored)
alpine-module/build.sh                                  # patched modules -> out/modules/
.venv/bin/python build_card.py image/usbpi.toml         # -> out/card-<hostname>/ + manifest
.venv/bin/python build_card.py image/usbpi.toml --install /Volumes/ALPINE
```

Your image config (`image/*.toml` other than the template) stays out of git.
It sets what's on the card: Alpine release,
packages, services, boot modules, debug console, gadgets and their plan
options, and which gadget starts at boot. Secrets stay outside the repo:
`wifi.local` holds `SSID`, `PASSWORD` and `COUNTRY` (the Zero 2 W only does
2.4 GHz), and your SSH public key is read from `~/.ssh`. Builds are
reproducible: the same inputs give the same `out/card-<hostname>.manifest`.

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
ssh root@<pi> sh /etc/usbpi/gadget.sh up /etc/usbpi/gadgets/mydevice.plan
cd ../usb-capture && .venv/bin/python capture_usb.py capture --vid <vid> --pid <pid> --bus <bus> -o captures/clone.json
.venv/bin/python capture_usb.py compare captures/mydevice.json captures/clone.json
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

Not reproduced: strings beyond a contiguous run 1..N (the GameBuds had one at
index 33). Only probing tools ask for those.
