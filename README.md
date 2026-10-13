# usb: clone a USB device with a Raspberry Pi

Make a Raspberry Pi Zero 2 W present itself to a host as a captured USB
device: same IDs, descriptors and strings, with working audio and HID
endpoints. The Pi runs Alpine Linux, with patched USB gadget modules so that
details the stock kernel hard-codes can be matched.

A clone of SteelSeries Arctis GameBuds (UAC1 audio plus three HID interfaces)
had byte-identical device, configuration and HID report descriptors and the
same string table, and audio worked both ways at full speed.

## How it fits together

```
usb-capture (sibling repo)       this repo (Mac)                      Pi (Alpine, diskless)
capture_usb.py ─► mydevice.json ─► gadget_plan.py ─► gadgets/*.plan ─┐
                  image/<name>.toml ─► build_card.py ─► card files ──┴─► gadget.sh up PLAN
```

- **Capture** (sibling repo [usb-capture](../usb-capture)): `capture_usb.py`
  records what a device reports into JSON. Its `model` is the contract with
  this repo.
- **Plan:** `gadget_plan.py` turns a capture plus options into the exact
  configfs operations that reproduce it.
- **Image config** (`image/<name>.toml`): everything on the Pi's card. That
  covers the Alpine release, packages, services, access, patched modules, and
  which gadgets exist and which starts at boot.
- **Card:** `build_card.py` builds the card from the config, reproducibly.
- **Pi:** `gadget.sh` replays a plan. Python is on the Pi only for your own
  code, such as audio processing.

## Requirements (Mac)

- Docker, for building kernel modules and caching packages (e.g. `brew
  install colima docker && colima start --vm-type vz --cpu 6 --memory 12`)
- `brew install rpiboot`, to write the SD card through the Pi itself
  (no card reader needed)
- A Python venv for this repo:

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyserial tomli
```

Use Apple's `/usr/bin/python3`: Homebrew's Python 3.13.16 reports an empty
macOS version, which crashes pip.

## Set up

1. **Wi-Fi and SSH.** Create `wifi.local` (gitignored). The Zero 2 W only
   does 2.4 GHz:

   ```
   SSID=MyNetwork
   PASSWORD=...
   COUNTRY=US
   ```

   Root logs in over SSH with the public key named in the image config
   (default `~/.ssh/id_ed25519.pub`); there's no password login.

2. **Image config.** Copy the template, then add a gadget section per device
   you've captured:

   ```bash
   cp image/template.toml image/usbpi.toml          # image/*.toml besides the template are gitignored
   ```

   ```toml
   [gadgets.mydevice]
   capture = "../usb-capture/captures/mydevice.json"   # relative to this repo
   max_speed = ""     # "" = as captured, or "full-speed" / "high-speed"
   test_ids = false   # true: VID:PID 1209:0001 instead of the captured IDs

   [boot]
   gadget = "mydevice"   # start it at power-on; "" = USB debug console instead
   ```

3. **Patched kernel modules:**

   ```bash
   alpine-module/build.sh            # -> out/modules/, about 15 s after the first run
   ```

## Build and install the card

```bash
.venv/bin/python build_card.py image/usbpi.toml        # -> out/card-<hostname>/ + manifest
```

To get the card onto the Mac without a card reader:

1. Unplug the Pi and remove the card.
2. Plug the Pi into the Mac: with no card it waits in USB boot mode.
3. Insert the card.
4. Expose the card as a disk:

```bash
rpiboot -d "$(brew --prefix rpiboot)/share/rpiboot/msd"
```

A new card needs one FAT32 (MBR) format first. Check the disk number with
`diskutil list external` before running this:

```bash
diskutil eraseDisk FAT32 ALPINE MBRFormat /dev/diskN
```

Then install, which replaces the card's contents and verifies every file:

```bash
.venv/bin/python build_card.py image/usbpi.toml --install /Volumes/ALPINE
```

Eject the card and replug the Pi with the card in. It boots in about 30
seconds. It installs the configured packages from the card offline (Alpine
runs from RAM), joins Wi-Fi and loads the patched modules. Then it either
starts the `[boot] gadget` or exposes a USB serial root shell
(`pi_console.py 'command'`). After a reinstall the Pi has new SSH host keys,
so clear the old one with `ssh-keygen -R <pi-address>`.

Builds are reproducible: the same inputs give the same
`out/card-<hostname>.manifest`. The generated `image/packages.lock` lists the
packages cached on the card, with hashes, so updates from Alpine's mirrors
show up as a diff.

## Use the clone

```bash
ssh root@<pi> sh /etc/usbpi/gadget.sh up /etc/usbpi/gadgets/mydevice.plan
ssh root@<pi> sh /etc/usbpi/gadget.sh down clone       # back to the debug console
ssh root@<pi> sh /etc/usbpi/gadget.sh list
```

To verify it, capture the clone with usb-capture and compare it with the
original:

```bash
cd ../usb-capture
.venv/bin/python capture_usb.py capture --vid <vid> --pid <pid> --bus <bus> -o captures/clone.json
.venv/bin/python capture_usb.py compare captures/mydevice.json captures/clone.json
```

**Audio on the Pi:** the clone is ALSA card `hw:0,0`. Host playback arrives
as capture, and the device's mic is playback. The `Capture Rate` control
(`amixer -c 0 cget iface=PCM,name='Capture Rate'`) is 0 until the host starts
streaming, and recording fails with an I/O error until then. So wait for it to
be non-zero, and reopen when it drops back to 0. `Playback Rate` does the same
for the mic direction.

**Faster iteration, without rebuilding the card:**
- **Plans:** run `gadget_plan.py CAPTURE -o x.plan`, copy it to the Pi, then
  run `gadget.sh up x.plan`.
- **Modules:** copy `out/modules/*.ko` to the Pi, then run
  `sh /etc/usbpi/pi_kmods.sh swap DIR`.

Changes made on the Pi itself are lost at reboot unless saved with
`lbu commit`. Better to put them in the image config.

## Kernel patches (`alpine-module/patches/`)

Built against the exact source of Alpine's `linux-rpi` 6.18.52 (checked
against the APKBUILD's sha512sums). The matching headers package is cached in
a Docker volume in case Alpine's mirror moves on. `build_card.py` refuses
modules built for a different kernel than the configured release's.

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

## Limits

- **Classes:** UAC1, UAC2 and HID functions are reproduced. Other interface
  classes are reported and skipped.
- **Audio formats:** only the first alternate audio format per direction.
- **Languages:** English (`0x409`) strings only.
- **String gaps:** strings beyond a contiguous run 1..N can't be placed. The
  GameBuds had an extra one at index 33.
- **Speed:** the Pi's controller is USB 2.0. Full and high speed only.
- **Not in a capture:** vendor requests, timing, error behavior and what the
  HID interfaces send at runtime.

## Layout

| Path | What it is |
|---|---|
| `image/template.toml` | Template image config (copy to `image/<name>.toml`) |
| `image/overlay/` | Static files for the card's Alpine overlay (startup scripts, network) |
| `image/packages.lock` | Generated: packages cached on the card, with hashes |
| `build_card.py` | Mac: build and install the card |
| `gadget_plan.py` | Mac: capture → plan |
| `gadget.sh` | Pi: apply a plan (`up` / `down` / `list`) |
| `gadgets/*.plan` | Generated plans (gitignored) |
| `alpine-module/` | Patched gadget modules, built in Docker; `pi_kmods.sh` loads them on the Pi |
| `pi_console.py` | Mac: run commands over the Pi's USB serial debug console |
| `wifi_config.py` | `wifi.local` parsing, shared with the Buildroot tree |
| `buildroot/` | Buildroot tree for a self-contained image: builds, not yet booted or patched |
