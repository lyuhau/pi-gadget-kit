# pi-gadget-kit: a Raspberry Pi as a configurable USB device

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

- **Supported:** composite or not, at full or high speed, with audio (UAC1,
  UAC2) and HID interfaces; hand-written devices can also have network
  (ECM, NCM, RNDIS) and serial (ACM) functions.
- **Not supported:** other interface classes (mass storage, MIDI, video, and
  network or serial interfaces of a capture) are skipped, and the Pi can't
  present low speed, which many simple keyboards and mice use.
- **Partial matches:** anything else the description has that can't be
  reproduced is listed as a note when the card is built. The full list is in
  `gadget_plan.py`.

For full control, the `raw_gadget` module (built into `out/modules/` by
`alpine-module/build.sh`, not installed on the card) lets a program on the Pi
answer every USB request itself.

## Requirements (Mac)

- Docker, only for building the patched modules yourself or for the
  `diskless` image type, e.g. `brew install colima docker`, then
  `colima start --vm-type vz --cpu 6 --memory 12` (once per Mac restart).
  The `initramfs` type with the prebuilt modules needs none, and builds on
  Linux too.
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
2. **Device:** put the description at `config/device.json`, or write one by
   hand (see [Hand-written devices](#hand-written-devices)). To start a
   hand-written one from a capture, run
   `.venv/bin/python capture_to_toml.py capture.json -o config/device.toml`.
   An image can carry several, to switch between on the Pi (see
   [Flavors](#flavors)).
3. **Wi-Fi:** create `config/wifi.local` with `SSID=…`, `PASSWORD=…` and
   `COUNTRY=US` lines. The Zero 2 W only does 2.4 GHz. Optional: with
   `wifi = ""` the Pi leaves Wi-Fi alone (as it does SSH with
   `ssh_public_key = ""`).
4. **Patched modules:** `alpine-module/fetch.sh` downloads the prebuilt ones
   (this repo's release `modules-KERNEL-PKGREL`, checked against
   `MODULES_SHA256` in `kernel.env`) into `out/modules/`;
   `alpine-module/build.sh` builds them (Docker; the first run downloads about
   150 MB of kernel source).

## Hand-written devices

For a device with nothing to capture, write the description as TOML
(`description = "device.toml"`). It has the shape of a captured description's
`model`, with defaults for whatever is left out, so it plans exactly like a
capture. Unknown keys are refused. For example, UAC2 audio both ways
plus networking:

```toml
speed = "high-speed"                          # default
[device]
idVendor = 0x1d6b                             # required, as is idProduct
idProduct = 0x0104
[strings]
manufacturer = "example"                      # also product, serialnumber
[config]
max_power_ma = 250                            # also configuration, bmAttributes

[[functions]]                                 # in interface order
kind = "ecm"
id = "net"
dev = true                                    # only with [device] dev_functions (see below)
dev_addr = "02:a0:00:00:00:01"                # left out: random at every boot
host_addr = "02:a0:00:00:00:02"

[[functions]]
kind = "uac2"
id = "audio"
[functions.host_to_device]                    # the host plays (c_* in configfs)
rates = [48000]                               # defaults: 48 kHz, 2 channels, 2 bytes
[functions.device_to_host]                    # the host records (p_*); leave out for one direction
channels = 1
```

**Dev functions:** a function with `dev = true` is only in images whose
config has `[device] dev_functions = true`, like a package manager's dev
dependencies: USB ethernet for SSH in a dev image, say, left out of the release
image built from the same description. A build lists what it left out.

**No extras:** `extras = false` at the top of a description keeps both the
debug console and dev functions out of it, whatever `[device]` says: for a
faithful clone of a real device, with nothing a computer wouldn't expect.

| `kind` | Settings besides `id` and `dev` |
|---|---|
| `uac2`, `uac1` | `name` (control interface), `function_name` (uac2), `req_number`; per direction `channels` or `chmask`, `rates` (up to 10), `subslot_bytes`, `sync`, `bInterval` (high-speed `hs_bint`, 0 = auto), `mute`, `volume = {min, max, res}` (1/256 dB; any left out keep the kernel's default), `terminal_type`, terminal, unit, channel and clock names |
| `hid` | `report_desc` (hex, required), `subclass`, `protocol`, `report_length`, `name`, `has_out_endpoint`, `bInterval` |
| `ecm`, `ncm`, `rndis` | `dev_addr`, `host_addr`, `qmult`, `ifname` (with one `%d`), `max_segment_size` (ncm) |
| `acm` | `protocol` |

**Strings:** `manufacturer`, `product` and `serialnumber` are strings 1, 2
and 3, and `extra = [...]` continues at 4. The device descriptor points to the
first three; to keep one the device has but doesn't point to, set it to 0 in
`[device]` (`iSerialNumber = 0`).

The full list, and what stock UAC2 can't match, is in `gadget_plan.py`; the
descriptions in `tests/data/` use every setting.

**From a capture:** `capture_to_toml.py` writes a capture (from usb-capture's
`capture_usb.py`) as a hand-written description, leaving out defaults and
listing what can't be reproduced as comments. It writes `extras = false`, so
the clone is exact: with a debug console, an image would add a serial
interface and change the device class (`--extras` doesn't write it). The output
is `-o FILE`, or by default the captured product's name as a slug
(`headphones.toml`). It checks that the TOML plans exactly like the capture, and
doesn't overwrite an existing file without `--force`. Edit the result to add
what a capture can't know: `id`s, networking, `req_number`, uac2's
`function_name`.

**Names**, for apps that change the device at runtime (see [Apps](#apps)):
the gadget is `/sys/kernel/config/usb_gadget/device`, each function
`functions/KIND.ID` (`uac2.audio`), or `KIND.N` without an `id`, numbered
per kind from 0 as captures are (`hid.0`). The Pi lists them in
`/etc/usb-gadget/functions`, or with [flavors](#flavors), the running
flavor's in `/run/usb-gadget/functions`.

## Flavors

An image can carry several devices, *flavors*, and the Pi plans the one it
presents when it starts, or switches live. In `pi.toml`:

```toml
[device]
flavors = { audio = "device.toml", headset = "headset.toml" }   # NAME = description
default = "audio"
```

Paths are relative to the config. Names are lowercase letters, digits, `-`
and `_`. A lone `description = "device.toml"` still works: one flavor, named
after its file (`device`). Hand-written TOML or a capture's JSON both work.

The build plans every flavor with `[device]`'s options (`console`,
`dev_functions`, `max_speed`, `test_ids`) as a check: one that doesn't plan
fails the build, with the planner's reason. The default's plan is written as
before (`out/device.plan`, and the image's `/etc/usb-gadget/device.plan`).

On the Pi this needs an `initramfs` image with `python3` in its packages;
others (diskless, or no `python3`) only have the default's plan, made at
build time, as before. The image carries the flavors
(`/etc/usb-gadget/flavors/`) and the planner
(`/usr/lib/usb-gadget/gadget_plan.py`).

- **Choosing one:** the card's `settings.txt` has `flavor = NAME` (see
  [Card settings](#card-settings)); left out, the default.
- **Adding one:** put `NAME.toml` in a `flavors/` folder at the top of the
  card. The card's flavors come first, so one there can also replace a
  built-in one. `--install` keeps the folder: it's the Pi's.
- **Coming up:** `gadget.sh up` plans the chosen flavor into
  `/run/usb-gadget/device.plan` (and the list of its functions,
  `/run/usb-gadget/functions`; `/run/usb-gadget/flavor` names it) and
  presents it. If it's missing, doesn't plan, or the kernel refuses it, the
  default comes up instead; if that fails too, the image's prebuilt plan. The
  Pi says why in `/var/log/flavor.log`. A typo never leaves the Pi without a
  device.
- **Switching:** `gadget.sh use NAME` plans NAME and replaces the device with
  it, without a reboot: the host sees an unplug and a new device. One that
  doesn't plan is refused first, and the device stays as it is (exit 1). Exit
  0: NAME is up; 3: the kernel refused it and the default (or the prebuilt
  plan) is up instead; 1: nothing is up. It lasts until the next reboot;
  `settings.txt` is what's kept. Every switch unbinds and rebinds the USB
  controller, which has wedged when rebound several times in a row:
  `gadget.sh` doesn't space switches out, so callers must.
- **A flavor's identity** (IDs, strings) is its own: to rename the device,
  edit or add a flavor. A clone of a real device can say `extras = false`
  (see [Hand-written devices](#hand-written-devices)).

## Build and install the card

1. Build: `.venv/bin/python build_card.py config/pi.toml`. Output goes to
   `out/card-<hostname>/`, and the same inputs always give the same
   `out/card-<hostname>.manifest`. Another config, say `pi-release.toml`,
   builds to `out/card-<hostname>-release/`, with its own
   `pi-release.packages.lock` and `out/device-release.plan`.
2. Expose the card on the Mac: unplug the Pi, take the card out, plug the Pi
   in (with no card it waits in USB boot mode), insert the card, then run
   `rpiboot -d "$(brew --prefix rpiboot)/share/rpiboot/msd"`.
3. A new card needs one FAT32 format. Check the disk number first with
   `diskutil list external`:
   `diskutil eraseDisk FAT32 ALPINE MBRFormat /dev/diskN`.
4. Install, which replaces the card's contents (except an [app](#apps)'s
   state folder and logs, and the [card's settings](#card-settings)) and
   verifies every file:
   `.venv/bin/python build_card.py config/pi.toml --install /Volumes/ALPINE`,
   then `diskutil eject /Volumes/ALPINE`.
5. Replug the Pi with the card in. It boots (30–40 s diskless, see below):
   - installs its packages from the card (diskless only; offline);
   - reads the card's `settings.txt`;
   - joins Wi-Fi;
   - either presents the device (`start_at_boot`) or opens the debug console.

## Card settings

Some settings can change without a rebuild: `settings.txt` at the top of the
card, a text file to edit on any computer. The build writes every setting
there, commented out, with its built-in value:

```
# The Pi's name on the network: letters, digits and -.
#hostname = usb-gadget
```

One `key = value` per line; to change one, remove its `#` and edit the value.
`#` only starts a comment at the beginning of a line (`relay = host:443#pin`
is one value), surrounding `"` are dropped, and Windows line ends are fine.
A key that's left out or empty keeps the built-in value. So does a misspelt
key or a value that isn't valid: a typo never keeps the Pi from booting or
presenting its device. The Pi says why in `/var/log/settings.log`.

| Key | Value | Built-in value |
|---|---|---|
| `hostname` | letters, digits, `-` | `[system] hostname` |
| `console` | `on` or `off`: the debug console | `[access] debug_console` |
| `flavor` | a [flavor](#flavors)'s name (only in images that plan them) | `[device] default` |
| `APP.KEY` | text; the app checks it | `[app] settings` (see [Apps](#apps)) |

- **console:** `on` gives the debug console when no device is up, and in
  the device when it has room for it (`[device] console`); `off` leaves it
  out of both. So an image built with `debug_console = false` can still
  turn it on: anyone who has the card can change the image anyway.
- **flavor:** the device the Pi presents, built in or from the card's
  `flavors/`. Not a flavor's name: the default, and the reason in
  `/var/log/flavor.log`. The `usb.*` settings of older images are gone (a
  flavor owns the device's IDs and strings): a card that still has them gets
  a note in `settings.log`, and they're ignored.
- **SSH keys:** a file `ssh_authorized_keys` next to `settings.txt`, as
  `~/.ssh/authorized_keys`, adds keys for root. Only if the image has an SSH
  server (initramfs: `[access] ssh_public_key` set; diskless: `openssh` in
  the packages); without one, the Pi logs that and ignores the file.

`--install` keeps the card's `settings.txt` and `ssh_authorized_keys`: they
belong to whoever edited them. Settings the kept file doesn't mention yet
(a new image's) are added at its end, commented out; nothing else in it
changes. Delete the file before installing for a fresh one.

On the Pi, `/etc/local.d/05-settings.start` reads the file
(`image/settings.sh`) once the card is mounted, before the debug console and
the device. Readers source `/etc/usb-gadget/settings.defaults` (the build's
values), then `/run/usb-gadget/settings.env` (the card's valid ones):
`SETTING_HOSTNAME`, `SETTING_CONSOLE`, `SETTING_FLAVOR`, `APP_SETTING_KEY`.

## Image types

`[image] type` in `pi.toml` picks how the Pi gets its system. Both run Alpine
from RAM with the same kernel, packages, patched modules and config files.

- **`diskless`** (default): stock Alpine. The card holds the release, an
  apkovl with the config, and a package cache; every boot installs the
  packages into RAM. That install is the slow part: the debug console
  appears after about 17 s, services after about 27 s. On the Pi, `apk add`
  works and `lbu commit` saves changes to the card.
- **`initramfs`**: `image_initramfs.py` unpacks the packages on the Mac into
  one initramfs (`a/boot/initrd.img`, about 30 MB). The image (kernel,
  initramfs, device tree, overlays, `cmdline.txt` and its own `config.txt`) is
  in the slot folder `a/`; the card's `config.txt` only selects the slot
  (`os_prefix=a/`, `include a/config.txt`), so another image can go in `b/`
  next to it, be tried once (the firmware's `tryboot.txt`) and be switched to. The Pi's firmware loads it
  with the kernel, and `image/init` only starts things: watchdog, modules,
  the card, the same `/etc/local.d` scripts, the app, Wi-Fi, SSH, time. There is no OpenRC or
  apk (`alpine-base` is left out), so packages and changes only come with a
  rebuild. Needs no Docker for packages, and keeps one ssh
  host key per hostname in `out/`, so reinstalls keep the Pi's identity.

The card is mounted at `/media/mmcblk0p1` in both: read-only, unless an
[app](#apps) keeps state or logs there.

## Use the Pi

**Find the Pi.** Alpine doesn't announce its hostname, so check your router,
or ask over the debug console:
`.venv/bin/python pi_console.py 'ip -4 addr show wlan0'`.

**SSH:** `ssh root@<address>`, with the key from `[access]`. After a
diskless reinstall the Pi has new host keys, so first run `ssh-keygen -R <address>`.

**The device:**

```bash
ssh root@<address> sh /etc/usb-gadget/gadget.sh up      # present the device
ssh root@<address> sh /etc/usb-gadget/gadget.sh use headset   # another flavor, live (see Flavors)
ssh root@<address> sh /etc/usb-gadget/gadget.sh down    # remove it; the debug console comes back if enabled
ssh root@<address> sh /etc/usb-gadget/gadget.sh list
ssh root@<address> sh /etc/usb-gadget/gadget.sh set audio c_srate=44100   # see Apps
```

The device takes the USB port from the debug console. With `[device]
console = true` it includes the console instead (as `acm.console`, an extra
serial interface), so `pi_console.py` keeps working while the device is up;
unless the card's `console = off` leaves it out.

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
plugged into the Pi's USB port (`debug_console = false`, or the card's
`console = off`, drops it), and
the Wi-Fi password is stored in plain text on the card (as is the initramfs
image's ssh host key).

## Apps

Another project can run on the Pi as an app: its own repo, with its own
`config/pi.toml` (this README's settings plus `[app]`), built with this
repo's `build_card.py`. usb knows nothing about the app; the app relies only
on what's below. `examples/heartbeat/` is the smallest one.

```toml
[app]
name = "myapp"                      # lowercase; its state folder, as below
files = { "opt/myapp/myapp.pyz" = "../dist/myapp.pyz" }  # on the Pi = source
overlay = "pi/overlay"              # and/or a folder copied onto / as is
run = "/opt/myapp/start.sh --flag"  # shell command, started once
owns = ["wifi", "time"]             # the platform doesn't start them
state = true                        # /var/lib/myapp is the card's myapp/
keep_logs = true                    # /var/log on the card: logs/last, logs/prev
config_txt = ["gpio=21=ip,pu"]      # extra config.txt lines
kmods = ["snd_aloop"]               # stock modules to ship and load before run
settings = { mode = "fast" }        # card settings: key = default, or { default, help }
```

- **Files:** sources are relative to the config; executable ones stay
  executable; `files` wins over `overlay`. They may replace packages' files,
  not the platform's (`/etc/usb-gadget/`, refused at build).
- **run** starts once the device is up (`start_at_boot`) and the card is
  checked and mounted, before Wi-Fi and SSH: in the background, in its own
  session, output to `/var/log/app.log`. Only once: a crash is the app's to
  handle (a respawn loop). Without `start_at_boot` there's no device yet;
  `gadget.sh up` is then the app's call.
- **owns:** `wifi`: no `wpa_supplicant`, `udhcpc` (needs `[access] wifi`
  empty or left out; the app brings its own packages and config). `time`:
  no `chronyd`/`ntpd`.
- **state:** the card is mounted read-write and the app's folder on it
  (created) is bound to `/var/lib/NAME`. No card (booted over USB): that
  folder is in RAM. `--install` keeps it (and `logs/`). It's FAT and the Pi loses power any time: write a new
  file, `fsync`, rename, `sync`.
- **keep_logs:** at start, the card's `logs/last` becomes `logs/prev`; every
  10 minutes from a minute in, and at shutdown, `/var/log/*` is copied to
  `logs/last`.
- **settings:** on the card's `settings.txt` as `NAME.KEY` (`myapp.mode`),
  commented out with the default and the help (`mode = { default = "fast",
  help = "fast or slow." }`), for users to change without a rebuild (see
  [Card settings](#card-settings)). `run` gets them as `APP_SETTING_KEY`
  (`APP_SETTING_MODE`): the default, or the card's value, which can be any
  one line of text. Checking it is the app's job, and a bad value should
  fall back, not stop the app.

`/etc/usb-gadget/app.env` (shell syntax; exported to `run`) says where
things are:

| Variable | Value |
|---|---|
| `APP_NAME`, `APP_RUN`, `APP_OWNS` | from `[app]` |
| `APP_STATE` | `/var/lib/NAME` |
| `APP_LOG` | `/var/log/app.log` |
| `CARD` | `/media/mmcblk0p1` |
| `APP_LOGS` | `$CARD/logs` (with `keep_logs`) |
| `GADGET` | `/sys/kernel/config/usb_gadget/device` (with `[device]`) |
| `GADGET_SH` | `/etc/usb-gadget/gadget.sh` |
| `GADGET_FUNCTIONS` | `ID KIND DIRECTORY` per function: `/etc/usb-gadget/functions`, or with [flavors](#flavors) the running one's, `/run/usb-gadget/functions` (there once the device is up) |

**Changing the device at runtime**, e.g. another audio format:

```sh
sh "$GADGET_SH" set audio c_srate=96000 p_srate=96000   # FUNCTION ATTR=VALUE...
```

The host sees an unplug and a new device. Exit status 1 names a refused
write; the device is back either way. Don't do it by hand: attributes only
change while the function is in no configuration, and
- links are ordered: relinking one function moves its interfaces to the
  end, so `set` unlinks and relinks them all in the plan's order;
- after every bind, network functions' interfaces (ECM, NCM, RNDIS) must
  come up at once: dwc2 shares one receive FIFO between all OUT endpoints,
  and a packet for an interface that's down jams it, audio and control
  requests included. `set` and `up` do that (addresses are the app's).

**Image slots (initramfs images):** the image lives in a slot folder (`a/`), and
the card's `config.txt` selects it. Another image can go in another slot (`b/`) and
be tried: `sh /etc/usb-gadget/slot.sh try b/` boots it once (the firmware's
`tryboot`), and it stays only if it's committed (`slot.sh commit`); otherwise the
next boot is the old slot again. The image's `/init` reboots an uncommitted trial
after `[app] trial_seconds` (300), so an image that doesn't come up can't strand the
Pi. `/init` commits once the app has started, unless `[app] commits_trial = true`:
then the app commits when it knows it works (connected, say). `slot.sh status` says
which slot runs and whether it's a trial; `slot.sh switch SLOT` changes slots
without a trial. Trying a slot needs `python3` on the Pi (the reboot's argument).
Tested on a Zero 2 W: a trial that panics, or whose kernel is missing, falls back
on its own.

**Dev and release:** keep two configs. The dev one includes the release one
(same folder) and adds the ways in, section by section, key by key, so a
release image never inherits a dev setting:

```toml
# pi-dev.toml
include = "pi.toml"
[access]
ssh_public_key = "~/.ssh/id_ed25519.pub"
debug_console = true
[device]
dev_functions = true         # the description's dev = true functions, e.g. USB ethernet
```

**Diskless** runs apps too, with OpenRC: `run` from
`/etc/local.d/90-app.start`, `owns` takes services out of the runlevels,
`config_txt` goes to `usercfg.txt`. But the app starts 20–30 s after power,
and Alpine mounts the card before anything can check it, so state is written
to an unchecked card.

## Changing the Alpine release

The patched modules are built for one exact kernel. To move to another
release:
1. Update `[alpine]` and `repositories` in `config/pi.toml`.
2. Update `alpine-module/kernel.env` with the kernel version and checksums
   from that branch's `main/linux-rpi/APKBUILD` in aports.
3. Rebuild with `alpine-module/build.sh`. The patches may need adjusting.
4. Publish them for `fetch.sh`: `alpine-module/pack.sh` packs the modules
   `build_card.py` uses into `out/gadget-modules-KERNEL-PKGREL.tar.gz` and
   prints its sha256, which goes in `kernel.env` (`MODULES_SHA256`); then
   release it:
   `gh release create modules-KERNEL-PKGREL out/gadget-modules-*.tar.gz out/SHA256SUMS`
   (`SHA256SUMS`: `cd out && shasum -a 256 gadget-modules-KERNEL-PKGREL.tar.gz > SHA256SUMS`).
5. Mirror the kernel package on the same release, since Alpine's repositories
   keep only the newest kernel and a pinned build stops working once it moves
   on: upload `linux-rpi-KERNEL-PKGREL.apk` (from `out/cache/apks-*/`, after a
   build) and add its line to `SHA256SUMS`; its sha256 goes in `kernel.env`
   (`KERNEL_APK_SHA256`). `fetch.sh` downloads it to `out/kernel/`, and the build
   uses it when the repositories have another version (still checked against the
   release's kernel).

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
| `image/app.sh` | On the Pi: starts an `[app]` (state folder, logs, `run`) |
| `image/settings.sh` | On the Pi: reads the card's `settings.txt` |
| `examples/heartbeat/` | The smallest app: dev and release configs, a `run` script |
| `build_card.py` | Builds and installs the card; owns the fixed plumbing (boot modules, services) |
| `image_initramfs.py`, `image/init` | The initramfs image type: its build, and its PID 1 on the Pi |
| `gadget_plan.py` | Device description → plan of configfs operations (run by `build_card.py`) |
| `gadget.sh` | On the Pi: apply the plan (`up` / `use` / `down` / `list` / `set`), planning flavors |
| `tests/` | Golden plans for `gadget_plan.py`, `[app]` builds, flavors, card settings: `.venv/bin/python -m unittest discover tests` |
| `alpine-module/` | Patched gadget modules (`patches/`, built in Docker for `kernel.env`'s kernel by `build.sh`, or prebuilt from a release by `fetch.sh`); `pi_kmods.sh` loads them on the Pi |
| `pi_console.py` | Runs commands over the Pi's USB debug console |
| `out/` | Build output: modules, `device.plan`, card contents, manifests |

The patches in `alpine-module/patches/` make descriptor details of `f_uac1`,
`f_hid` and configfs settable. One also fixes stock `f_uac1`'s broken
full-speed audio. Each patch file says what it changes. The card loads the
other functions it supports (`f_uac2`, `f_acm`, networking) from the same
build. A Buildroot-based
image (fast boot, self-contained) is parked on the `buildroot` branch.
