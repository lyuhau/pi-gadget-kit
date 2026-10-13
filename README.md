# usb: a Raspberry Pi as a configurable USB device

Build a Raspberry Pi Zero 2 W SD card from one config file, so the Pi boots
into a known state and presents itself to a host as the USB device you
describe. The device can be composite, with several interfaces such as audio
plus HID. The Pi runs Alpine Linux with patched USB gadget modules, so details
the stock kernel hard-codes can be matched. Use it as is, or as a template
for a project that needs a Pi to act as a USB device.

```
config/  ─►  build_card.py  ─►  SD card  ─►  Pi: gadget.sh up
```

## What it can present

One device at a time: the Zero 2 W has a single USB device port.

- **Supported:** composite or not, at full or high speed, with audio (UAC1;
  UAC2 is untested) and HID interfaces.
- **Not supported:** other interface classes (mass storage, serial, network,
  MIDI, video) are skipped, and the Pi can't present low speed, which many
  simple keyboards and mice use.
- **Partial matches:** anything else the description has that can't be
  reproduced is listed as a note when the card is built. The full list is in
  `gadget_plan.py`.

For full control, the `raw_gadget` module (in `out/modules/`, not installed on
the card) lets a program on the Pi answer every USB request itself.

## Requirements (Mac)

- Docker for the builds, e.g. `brew install colima docker`, then
  `colima start --vm-type vz --cpu 6 --memory 12` (once per Mac restart)
- `brew install rpiboot`, to write the SD card through the Pi (no card reader needed)
- A venv. `tomli` is needed because Apple's Python is 3.9. Use Apple's
  `/usr/bin/python3`: some Homebrew builds crash pip.

```bash
/usr/bin/python3 -m venv .venv && .venv/bin/pip install pyserial tomli
```

## Set up

Your settings live in `config/`. Paths in `pi.toml` are relative to it. This
template repo ignores `config/`; a project made from it commits `config/` but
keeps `wifi.local` out of git.

1. **Image config:** `mkdir -p config && cp image/template.toml config/pi.toml`,
   then edit it. Each setting is commented.
2. **Device:** put the description at `config/device.json`. Capture a real
   device with the sibling project [usb-capture](../usb-capture) and copy the
   file here, or write one in its format (that README describes it).
3. **Wi-Fi:** create `config/wifi.local` with `SSID=…`, `PASSWORD=…` and
   `COUNTRY=US` lines. The Zero 2 W only does 2.4 GHz.
4. **Patched modules:** `alpine-module/build.sh` writes them to
   `out/modules/`. The first run downloads about 150 MB of kernel source.

## Build and install the card

1. Build: `.venv/bin/python build_card.py config/pi.toml`. Output goes to
   `out/card-<hostname>/`, and the same inputs always give the same
   `out/card-<hostname>.manifest`.
2. Expose the card on the Mac: unplug the Pi, take the card out, plug the Pi
   in (with no card it waits in USB boot mode), insert the card, then run
   `rpiboot -d "$(brew --prefix rpiboot)/share/rpiboot/msd"`.
3. A new card needs one FAT32 format. Check the disk number first with
   `diskutil list external`:
   `diskutil eraseDisk FAT32 ALPINE MBRFormat /dev/diskN`.
4. Install, which replaces the card's contents and verifies every file:
   `.venv/bin/python build_card.py config/pi.toml --install /Volumes/ALPINE`,
   then `diskutil eject /Volumes/ALPINE`.
5. Replug the Pi with the card in. It boots (30–40 s diskless, see below):
   - installs its packages from the card (diskless only; offline);
   - joins Wi-Fi;
   - either presents the device (`start_at_boot`) or opens the debug console.

## Image types

`[image] type` in `pi.toml` picks how the Pi gets its system. Both run Alpine
from RAM with the same kernel, packages, patched modules and config files.

- **`diskless`** (default): stock Alpine. The card holds the release, an
  apkovl with the config, and a package cache; every boot installs the
  packages into RAM. That install is the slow part: the debug console
  appears after about 17 s, services after about 27 s. On the Pi, `apk add`
  works and `lbu commit` saves changes to the card.
- **`initramfs`**: `image_initramfs.py` unpacks the packages on the Mac into
  one initramfs (`boot/initrd.img`, about 30 MB). The Pi's firmware loads it
  with the kernel, and `image/init` only starts things: watchdog, modules,
  the same `/etc/local.d` scripts, Wi-Fi, SSH, time. There is no OpenRC or
  apk (`alpine-base` is left out), so packages and changes only come with a
  rebuild. Needs no Docker for packages, and keeps one ssh
  host key per hostname in `out/`, so reinstalls keep the Pi's identity.

The card is mounted read-only at `/media/mmcblk0p1` in both.

## Use the Pi

**Find the Pi.** Alpine doesn't announce its hostname, so check your router,
or ask over the debug console:
`.venv/bin/python pi_console.py 'ip -4 addr show wlan0'`.

**SSH:** `ssh root@<address>`, with the key from `[access]`. After a
diskless reinstall the Pi has new host keys, so first run `ssh-keygen -R <address>`.

**The device:**

```bash
ssh root@<address> sh /etc/usb-gadget/gadget.sh up      # present the device
ssh root@<address> sh /etc/usb-gadget/gadget.sh down    # remove it; the debug console comes back if enabled
ssh root@<address> sh /etc/usb-gadget/gadget.sh list
```

To check what the host actually sees, use usb-capture's "Verify a clone".

**Audio:** your code on the Pi uses ALSA `hw:0,0`. Host playback arrives as
capture, and the device's microphone is playback. Reads fail until the host
streams, so wait for the `Capture Rate` control to become non-zero:
`amixer -c 0 cget iface=PCM,name='Capture Rate'`. Reopen when it drops back
to 0. `Playback Rate` does the same for the microphone direction.

**Faster iteration** (without reinstalling the card):
- **Plan:** after editing `device.json` or `[device]`, rerun
  `build_card.py config/pi.toml` without `--install`. Copy `out/device.plan`
  to the Pi and run `gadget.sh up device.plan`.
- **Modules:** copy `out/modules/*.ko` to the Pi and run
  `sh /etc/usb-gadget/pi_kmods.sh swap DIR`. It removes the device, reloads
  the modules, and restores the debug console.

Changes made on the Pi are lost at reboot unless saved with `lbu commit`
(diskless only). Put lasting ones in the config.

**Security:** the debug console is a root shell with no password for anything
plugged into the Pi's USB port (set `debug_console = false` to drop it), and
the Wi-Fi password is stored in plain text on the card (as is the initramfs
image's ssh host key).

## Changing the Alpine release

The patched modules are built for one exact kernel. To move to another
release:
1. Update `[alpine]` and `repositories` in `config/pi.toml`.
2. Update `alpine-module/kernel.env` with the kernel version and checksums
   from that branch's `main/linux-rpi/APKBUILD` in aports.
3. Rebuild with `alpine-module/build.sh`. The patches may need adjusting.

`build_card.py` refuses modules built for a different kernel than the
release's. The initramfs type also needs the release kernel's `linux-rpi`
package. Alpine's repositories keep only the newest, so once it has moved on
the build works only from the copy in `out/cache/`.

## Layout

| Path | What it is |
|---|---|
| `config/` | Your settings: `pi.toml`, `device.json`, `wifi.local`; plus the generated `packages.lock`, listing cached packages with hashes |
| `image/template.toml` | Template for `config/pi.toml` |
| `image/overlay/` | Files placed on the Pi as-is (startup scripts, network) |
| `build_card.py` | Builds and installs the card; owns the fixed plumbing (boot modules, services) |
| `image_initramfs.py`, `image/init` | The initramfs image type: its build, and its PID 1 on the Pi |
| `gadget_plan.py` | Device description → plan of configfs operations (run by `build_card.py`) |
| `gadget.sh` | On the Pi: apply the plan (`up` / `down` / `list`) |
| `alpine-module/` | Patched gadget modules (`patches/`, built in Docker for `kernel.env`'s kernel); `pi_kmods.sh` loads them on the Pi |
| `pi_console.py` | Runs commands over the Pi's USB debug console |
| `out/` | Build output: modules, `device.plan`, card contents, manifests |

The patches in `alpine-module/patches/` make descriptor details of `f_uac1`,
`f_hid` and configfs settable. One also fixes stock `f_uac1`'s broken
full-speed audio. Each patch file says what it changes. A Buildroot-based
image (fast boot, self-contained) is parked on the `buildroot` branch.
