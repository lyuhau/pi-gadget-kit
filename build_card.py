#!/usr/bin/env python3
"""
Build the Pi's SD card contents from an image config (config/pi.toml).

  build_card.py config/pi.toml                          # -> out/card-<hostname>/
  build_card.py config/pi-release.toml                  # -> out/card-<hostname>-release/
  build_card.py config/pi.toml --install /Volumes/ALPINE
  build_card.py config/pi.toml --refetch                # re-download cached packages

Relative paths in the config (device description, Wi-Fi file, [app] files)
are relative to the config file. A config can include another one in its
folder (include = "pi.toml"), and run an app ([app]; README: Apps).

Steps:
  1. Alpine release tarball, verified against [alpine] sha256 (cached in out/cache/)
  2. the patched gadget modules (out/modules/, from alpine-module/fetch.sh or
     build.sh), checked against its kernel
  3. the [device] plan, written to out/device.plan and the card (with the
     list of its functions, /etc/usb-gadget/functions)
  4. packages Alpine doesn't bundle, downloaded into the card's apk cache via
     Docker; their files and hashes go in packages.lock next to the config
     (pi-NAME.packages.lock for a config called pi-NAME.toml)
  5. the apkovl overlay: image/overlay/ plus files generated from the config
     and the secrets under [access]
  6. a manifest of every file's sha256 (out/card-<hostname>.manifest)
Steps 1, 4 and 5 are for [image] type = "diskless" (the default); the
"initramfs" type builds one initramfs instead (image_initramfs.py).

Everything goes to out/card-<hostname>/ first, so builds can be compared;
--install then replaces the card's contents with it and verifies the copy.
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

import gadget_plan

REPO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(REPO, "out")
MTIME = 946684800  # fixed timestamp (2000-01-01) so the overlay is reproducible
CONFIG_DIR = REPO  # set from the config file's location in main()
VARIANT = ""  # set in main(): "" for pi.toml, "-release" for pi-release.toml (see names())

# What an image config may set; anything else is rejected as a likely mistake.
CONFIG_KEYS = {
    "image": {"type"},
    "alpine": {"version", "arch", "sha256"},
    "system": {"hostname", "packages", "repositories"},
    "access": {"wifi", "ssh_public_key", "debug_console"},
    "device": {"description", "max_speed", "test_ids", "start_at_boot", "console", "dev_functions"},
    "app": {"name", "files", "overlay", "run", "owns", "state", "keep_logs", "config_txt", "kmods", "settings",
            "commits_trial", "trial_seconds"},
}
# What an app can take over from the platform ([app] owns).
OWNABLE = {"wifi", "time"}
CARD = "/media/mmcblk0p1"  # the card's FAT partition on the Pi, in both image types
GADGET = "/sys/kernel/config/usb_gadget/device"
# Top-level folders of a card (both image types, and keep_logs'): not an app's name.
CARD_NAMES = {"logs", "boot", "overlays", "apks", "cache", "kmods", "a", "b"}  # a/, b/: image slots
# The card's settings (README: Card settings), and the SSH keys file next to
# it: its users' files, which --install keeps.
SETTINGS = "settings.txt"
USER_FILES = (SETTINGS, "ssh_authorized_keys")

# Fixed plumbing rather than config: changing these mostly breaks Wi-Fi, SSH
# or USB device mode.
# Loaded by the "modules" service. libcomposite comes later, with KMODS:
# loading the stock one here would block the patched one.
BOOT_MODULES = ["af_packet", "ipv6", "brcmfmac", "dwc2"]
# usercfg.txt: dwc2 in peripheral mode makes the Pi a USB device
BOOT_CONFIG = ["dtoverlay=dwc2,dr_mode=peripheral"]
# "local" runs /etc/local.d/*.start (patched modules, debug console, device);
# wpa_supplicant/networking go in "default", once the Wi-Fi driver has loaded.
SERVICES = {"boot": ["local"], "default": ["wpa_supplicant", "networking", "sshd", "chronyd"]}
# Gadget modules from alpine-module/build.sh or fetch.sh (patched, or built with them),
# loaded at boot in dependency order before anything can pull in the stock
# ones. The unpatched ones would also load next to the patched (symbol CRCs
# match), but from one build every function is the same, and pi_kmods.sh
# swap can unload them all.
KMODS_DIR = os.path.join(OUT, "modules")
KMODS = ["libcomposite", "u_audio", "u_serial", "u_ether", "usb_f_acm", "usb_f_uac1", "usb_f_uac2", "usb_f_hid",
         "usb_f_ecm", "usb_f_ncm", "usb_f_rndis"]


def load_config(path, seen=()):
    """The image config, with the file named by a top-level `include` (in the
    same folder) underneath: its sections are merged key by key, this file's
    keys winning. Lets a release config share a dev config's settings."""
    path = os.path.abspath(path)
    if path in seen:
        sys.exit(f"{path}: include loop")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = cfg.pop("include", None)
    if base is None:
        return cfg
    base_path = os.path.join(os.path.dirname(path), base)
    if not isinstance(base, str) or os.path.dirname(os.path.abspath(base_path)) != os.path.dirname(path):
        sys.exit(f"{path}: include names a file in the same folder")
    if not os.path.exists(base_path):
        sys.exit(f"{path}: include {base} doesn't exist")
    merged = load_config(base_path, seen + (path,))
    for sec, val in cfg.items():
        if isinstance(val, dict) and isinstance(merged.get(sec), dict):
            merged[sec] = {**merged[sec], **val}
        else:
            merged[sec] = val
    return merged


def platform_wifi(cfg):
    """Whether the platform joins Wi-Fi itself: [access] wifi is set."""
    return bool(cfg["access"].get("wifi"))


def names(config_path, host):
    """Where a config's build goes: out/card-HOST[-VARIANT]/, its manifest,
    out/device[-VARIANT].plan, and its lock next to it. pi.toml has no variant
    (out/card-HOST, packages.lock); any other config's file name is one, less
    a "pi-" prefix (pi-release.toml: out/card-HOST-release,
    pi-release.packages.lock), so a project's configs don't overwrite each
    other's builds. -> (variant, card dir, lock path)"""
    stem = os.path.splitext(os.path.basename(config_path))[0]
    variant = "" if stem == "pi" else "-" + (stem[3:] if stem.startswith("pi-") and stem != "pi-" else stem)
    lock = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        "packages.lock" if stem == "pi" else f"{stem}.packages.lock")
    return variant, os.path.join(OUT, f"card-{host}{variant}"), lock


def rel(path):
    """A path from the config: relative to the config file, ~ expanded."""
    return os.path.join(CONFIG_DIR, os.path.expanduser(path))


def read(path):
    with open(path, "rb") as f:
        return f.read()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def step(msg):
    print(f"== {msg}")


# ---------------------------------------------------------------- Wi-Fi

def read_wifi(path):
    vals = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    missing = [k for k in ("SSID", "PASSWORD", "COUNTRY") if not vals.get(k)]
    if missing:
        sys.exit(f"{path}: fill in {', '.join(missing)}")
    return vals


def wpa_quote(s):
    if '"' in s or "\n" in s:
        sys.exit("SSID/password containing a double quote or newline isn't supported")
    return f'"{s}"'


# ---------------------------------------------------------------- steps

def alpine_tarball(cfg, refetch):
    a = cfg["alpine"]
    name = f"alpine-rpi-{a['version']}-{a['arch']}.tar.gz"
    path = os.path.join(OUT, "cache", name)
    if refetch or not os.path.exists(path):
        branch = "v" + ".".join(a["version"].split(".")[:2])
        url = f"https://dl-cdn.alpinelinux.org/alpine/{branch}/releases/{a['arch']}/{name}"
        print(f"downloading {url}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    if sha256(path) != a["sha256"]:
        sys.exit(f"{name}: sha256 doesn't match [alpine] sha256")
    return path


def extract(tarball, dest):
    with tarfile.open(tarball) as t:
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not target.startswith(os.path.realpath(dest)) or m.issym() or m.islnk():
                sys.exit(f"refusing unsafe tar member {m.name}")
        t.extractall(dest)


def kernel_release(release):
    cfgs = [f for f in os.listdir(os.path.join(release, "boot")) if f.startswith("config-")]
    return cfgs[0][len("config-"):] if cfgs else None


def read_kmods(release):
    """The patched modules, name -> bytes, checked against the kernel."""
    kmods = {}
    for m in KMODS:
        src = os.path.join(KMODS_DIR, m + ".ko")
        if not os.path.exists(src):
            sys.exit(f"{src} missing: run alpine-module/fetch.sh (prebuilt) or alpine-module/build.sh (Docker)")
        kmods[m] = read(src)
        if f"vermagic={release} ".encode() not in kmods[m]:
            sys.exit(f"{m}.ko wasn't built for this release's kernel {release}: get its modules with "
                     "alpine-module/fetch.sh or build.sh (kernel.env names the kernel; README: Changing the Alpine release)")
    print(f"{len(KMODS)} modules for kernel {release}")
    return kmods


def copy_kmods(card, kmods):
    dest = os.path.join(card, "kmods")
    os.makedirs(dest)
    for m, data in kmods.items():
        with open(os.path.join(dest, m + ".ko"), "wb") as f:
            f.write(data)


def make_plan(cfg):
    """The [device] description plus its options -> the configfs plan and
    the list of its functions (gadget_plan.functions_text)."""
    dev = cfg.get("device")
    if not dev:
        print("no [device]: the Pi only offers the debug console")
        return None
    text, p = gadget_plan.plan_from_file(rel(dev["description"]), max_speed=dev.get("max_speed") or None,
                                         test_ids=dev.get("test_ids", False), console=dev.get("console", False),
                                         dev=dev.get("dev_functions", False))
    text = text.replace(f"from {rel(dev['description'])}", f"from {dev['description']}")
    path = os.path.join(OUT, f"device{VARIANT}.plan")
    old = open(path).read() if os.path.exists(path) else None
    with open(path, "w") as f:
        f.write(text)
    state = "unchanged" if old == text else "updated" if old else "new"
    print(f"{os.path.relpath(path, REPO)}: {len(p.ops)} operations ({state})")
    for n in p.notes:
        print(f"  note: {n}")
    return text, gadget_plan.functions_text(p)


def bundled_packages(card, arch):
    """Package names in the release's own apks/ repository."""
    idx = os.path.join(card, "apks", arch, "APKINDEX.tar.gz")
    with tarfile.open(idx) as t:
        text = t.extractfile("APKINDEX").read().decode()
    return {line[2:] for line in text.splitlines() if line.startswith("P:")}


def fetch_packages(cfg, card, refetch, lock_path):
    """Download packages that aren't on the release media (with dependencies)
    into an apk cache, as a Pi would with /etc/apk/cache on the card. Uses an
    Alpine container of the same release and architecture."""
    s, a = cfg["system"], cfg["alpine"]
    missing = [p for p in s["packages"] if p not in bundled_packages(card, a["arch"])]
    if not missing:
        return False
    stamp = "\n".join([a["version"], a["arch"]] + s["repositories"] + sorted(missing)) + "\n"
    cache = os.path.join(OUT, "cache", f"apk-{a['version']}-{a['arch']}")
    if refetch or not os.path.exists(os.path.join(cache, ".stamp")) or \
            open(os.path.join(cache, ".stamp")).read() != stamp:
        shutil.rmtree(cache, ignore_errors=True)
        os.makedirs(cache)
        print(f"downloading {', '.join(missing)} (+ dependencies) with alpine:{a['version']}")
        repos = "\\n".join(["/apks"] + s["repositories"])
        script = (f"printf '{repos}\\n' > /etc/apk/repositories && ln -s /cache /etc/apk/cache && "
                  f"apk update -q && apk add -q --no-scripts {' '.join(missing)}")
        platform = {"aarch64": "linux/arm64", "armv7": "linux/arm/v7", "armhf": "linux/arm/v6"}[a["arch"]]
        subprocess.run(["docker", "run", "--rm", "--platform", platform,
                        "-v", f"{os.path.join(card, 'apks')}:/apks:ro", "-v", f"{cache}:/cache",
                        f"alpine:{a['version']}", "sh", "-c", script], check=True)
        with open(os.path.join(cache, ".stamp"), "w") as f:
            f.write(stamp)
    files = sorted(f for f in os.listdir(cache) if not f.startswith("."))
    shutil.copytree(cache, os.path.join(card, "cache"), ignore=shutil.ignore_patterns(".*"))
    lock = "".join(f"{sha256(os.path.join(cache, f))}  {f}\n" for f in files if f.endswith(".apk"))
    old = open(lock_path).read() if os.path.exists(lock_path) else None
    with open(lock_path, "w") as f:
        f.write("# packages downloaded into the card's apk cache (build_card.py)\n" + lock)
    n = lock.count("\n")
    print(f"{n} cached packages; {os.path.relpath(lock_path, REPO)} "
          + ("unchanged" if old and old.split("\n", 1)[1] == lock else "updated: review the diff"))
    return True


def app_files(cfg):
    """[app] overlay and files: path on the Pi -> (mode, bytes). Executable
    sources stay executable; files wins over overlay."""
    app = cfg.get("app") or {}
    files = {}

    def add(dest, src):
        files[dest] = (0o755 if os.access(src, os.X_OK) else 0o644, read(src))

    if app.get("overlay"):
        top = rel(app["overlay"])
        for root, dirs, names in os.walk(top):
            dirs.sort()
            for n in sorted(names):
                if n != ".DS_Store":
                    add(os.path.relpath(os.path.join(root, n), top), os.path.join(root, n))
    for dest, src in (app.get("files") or {}).items():
        add(os.path.normpath(dest.lstrip("/")), rel(src))
    return files


def app_env(cfg):
    """/etc/usb-gadget/app.env: where things are, for the app (sourced by
    app.sh before [app] run) and image/init. Shell syntax."""
    app = cfg["app"]
    name = app["name"]
    env = [("APP_NAME", name), ("APP_RUN", app.get("run", "")), ("APP_OWNS", " ".join(app.get("owns", []))),
           ("APP_STATE", f"/var/lib/{name}"), ("APP_LOG", "/var/log/app.log"),
           ("CARD", CARD)]
    if app.get("keep_logs"):
        env.append(("APP_LOGS", f"{CARD}/logs"))
    if cfg.get("device"):
        env += [("GADGET", GADGET), ("GADGET_SH", "/etc/usb-gadget/gadget.sh"),
                ("GADGET_FUNCTIONS", "/etc/usb-gadget/functions")]
    env += [("STATE_ON_CARD", "1" if app.get("state") else ""), ("KEEP_LOGS", "1" if app.get("keep_logs") else ""),
            ("APP_KMODS", " ".join(app.get("kmods", []))),
            # image slots (image/slot.sh): who commits a trial boot, and how long it has
            ("TRIAL_COMMIT", "app" if app.get("commits_trial") else ""),
            ("TRIAL_SECONDS", str(app.get("trial_seconds", 300)))]
    return ("# [app] of the image config (build_card.py): sourced by app.sh, which\n"
            "# exports it to [app] run, and by the initramfs image's init\n"
            + "".join(f"{k}={shlex.quote(v) if v else ''}\n" for k, v in env))


# ---------------------------------------------------------------- card settings

def has_sshd(cfg):
    if cfg.get("image", {}).get("type") == "initramfs":
        return bool(cfg["access"].get("ssh_public_key"))
    return "openssh" in cfg["system"]["packages"] or "openssh-server" in cfg["system"]["packages"]


def settings(cfg, plan):
    """The card's settings: blocks of (help, [(key, type, variable, default,
    shown)]), for settings.txt and the Pi's settings.keys/.defaults. `shown`
    is the default settings.txt shows; usb.* default to "" (the plan's)."""
    dev = cfg.get("device") or {}
    console = "on" if cfg["access"].get("debug_console", True) else "off"
    blocks = [
        ("The Pi's name on the network: letters, digits and -.",
         [("hostname", "hostname", "SETTING_HOSTNAME", cfg["system"]["hostname"], cfg["system"]["hostname"])]),
        ("The debug console, on or off: a root shell over USB, with no password, while no\n"
         "device is up" + (", and in the device (a serial port next to its other interfaces)."
                           if dev.get("console") else "."),
         [("console", "onoff", "SETTING_CONSOLE", console, console)]),
    ]
    if plan:
        writes = dict(line.split(" ", 2)[1:] for line in plan[0].splitlines()
                      if line.startswith("write ") and line.count(" ") >= 2)
        usb = [("vendor_id", "hex16", "idVendor"), ("product_id", "hex16", "idProduct"),
               ("manufacturer", "usbstring", f"strings/{gadget_plan.LANG}/manufacturer"),
               ("product", "usbstring", f"strings/{gadget_plan.LANG}/product"),
               ("serial", "usbstring", f"strings/{gadget_plan.LANG}/serialnumber")]
        blocks.append(("The USB device as the computer sees it: vendor and product ID (hex), and the\n"
                       "manufacturer, product and serial number (at most 126 characters each).",
                       [(f"usb.{k}", t, f"SETTING_USB_{k.upper()}", "", writes.get(attr, "")) for k, t, attr in usb]))
    app = cfg.get("app") or {}
    for k, v in (app.get("settings") or {}).items():
        default, text = (v, "") if isinstance(v, str) else (v.get("default", ""), v.get("help", ""))
        blocks.append((text, [(f"{app['name']}.{k}", "text", f"APP_SETTING_{k.upper()}", default, default)]))
    keys = [k for _, es in blocks for k, *_ in es]
    if len(set(keys)) < len(keys):
        sys.exit(f"[app] settings: {app['name']}.KEY is one of the platform's settings (README: Card settings)")
    return blocks


def settings_block(text, entries):
    return ("".join(f"# {l}\n" for l in text.splitlines())
            + "".join(f"#{k} = {shown}".rstrip() + "\n" for k, _, _, _, shown in entries))


def settings_text(cfg, plan):
    """settings.txt, as a new card gets it: every setting commented out, with its default."""
    app = (cfg.get("app") or {}).get("name")
    head = ("# This Pi's settings: edit them on any computer, then put the card back in\n"
            "# the Pi.\n"
            "#\n"
            "# One \"key = value\" per line. Lines starting with # are comments, as are the\n"
            "# settings below, which show the built-in values: to change one, remove its #\n"
            "# and edit the value. A # later in a line is part of the value. A setting\n"
            "# that's left out, empty, misspelt or not valid keeps the built-in value (the\n"
            "# Pi logs why, in /var/log/settings.log).\n")
    body, apps = [], False
    for text, entries in settings(cfg, plan):
        if entries[0][0].startswith(f"{app}.") and not apps:
            body.append(f"# ---- {app}\n")
            apps = True
        body.append(settings_block(text, entries))
    if has_sshd(cfg):
        body.append("# SSH: public keys in a file named ssh_authorized_keys next to this one, one\n"
                    "# per line as in ~/.ssh/authorized_keys, can log in as root.\n")
    return head + "".join("\n" + b for b in body)


def merge_settings(old, cfg, plan):
    """A card's settings.txt, kept by --install: as it was, plus the settings
    it doesn't mention yet, commented out. -> (text, new keys)"""
    known = set(re.findall(r"^[ \t\ufeff]*#?[ \t]*([A-Za-z0-9_.-]+)[ \t]*=", old, re.M))
    add, keys = [], []
    for text, entries in settings(cfg, plan):
        if not known & {k for k, *_ in entries}:
            add.append(settings_block(text, entries))
            keys += [k for k, *_ in entries]
    if not add:
        return old, []
    return (old.rstrip("\n") + "\n\n# ---- new in this image (build_card.py --install)\n"
            + "".join("\n" + b for b in add)), keys


def settings_files(cfg, plan):
    """The Pi's side: the parser and what it checks against."""
    entries = [e for _, es in settings(cfg, plan) for e in es]
    keys = "".join(f"{k} {t} {var}\n" for k, t, var, _, _ in entries)
    defaults = "".join(f"{var}={shlex.quote(d) if d else ''}\n" for _, _, var, d, _ in entries)
    return {
        "etc/usb-gadget/settings.sh": (0o755, read(os.path.join(REPO, "image", "settings.sh"))),
        "etc/usb-gadget/settings.keys": (0o644, ("# the card's settings (build_card.py): KEY TYPE VARIABLE\n"
                                                 + keys).encode()),
        "etc/usb-gadget/settings.defaults": (0o644, ("# the card's settings' built-in values (build_card.py)\n"
                                                     + defaults).encode()),
    }


def config_files(cfg, plan):
    """Files for the Pi in both image types: image/overlay/ plus files made
    from the config. `plan` is make_plan's (plan, functions) or None.
    path -> (mode, bytes)"""
    s, acc = cfg["system"], cfg["access"]
    host = s["hostname"]
    files = {}  # path -> (mode, bytes)

    src = os.path.join(REPO, "image", "overlay")
    for root, _, names in os.walk(src):
        for n in names:
            p = os.path.join(root, n)
            files[os.path.relpath(p, src)] = (0o755 if os.access(p, os.X_OK) else 0o644, read(p))
    files.update(settings_files(cfg, plan))

    gen = {
        "etc/hostname": f"{host}\n",
        "etc/hosts": f"127.0.0.1\tlocalhost {host}\n::1\t\tlocalhost {host}\n",
        "etc/modules": "\n".join(BOOT_MODULES) + "\n",
    }
    for p, text in gen.items():
        files[p] = (0o644, text.encode())
    if platform_wifi(cfg):
        wifi = read_wifi(rel(acc["wifi"]))
        files["etc/wpa_supplicant/wpa_supplicant.conf"] = (0o600, (
            "ctrl_interface=/var/run/wpa_supplicant\n"
            f"country={wifi['COUNTRY'].upper()}\n"
            "network={\n"
            f"\tssid={wpa_quote(wifi['SSID'])}\n"
            f"\tpsk={wpa_quote(wifi['PASSWORD'])}\n"
            "}\n").encode())
    else:  # nothing brings wlan0 up: only the loopback
        files["etc/network/interfaces"] = (0o644, b"auto lo\niface lo inet loopback\n")
    if acc.get("ssh_public_key"):
        pubkey = open(rel(acc["ssh_public_key"])).read().strip()
        files["root/.ssh/authorized_keys"] = (0o600, (pubkey + "\n").encode())
    files["etc/usb-gadget/kmods.list"] = (0o644, (" ".join(KMODS) + "\n").encode())
    files["etc/usb-gadget/pi_kmods.sh"] = (0o755, read(os.path.join(REPO, "alpine-module", "pi_kmods.sh")))
    if cfg.get("image", {}).get("type") == "initramfs":    # images in slots (image_initramfs.py)
        files["etc/usb-gadget/slot.sh"] = (0o755, read(os.path.join(REPO, "image", "slot.sh")))
    if plan:
        files["etc/usb-gadget/gadget.sh"] = (0o755, read(os.path.join(REPO, "gadget.sh")))
        files["etc/usb-gadget/device.plan"] = (0o644, plan[0].encode())
        files["etc/usb-gadget/functions"] = (0o644, plan[1].encode())
        if cfg["device"].get("start_at_boot", False):
            files["etc/usb-gadget/start-at-boot"] = (0o644, b"")
    if cfg.get("app"):
        files["etc/usb-gadget/app.env"] = (0o644, app_env(cfg).encode())
        files["etc/usb-gadget/app.sh"] = (0o755, read(os.path.join(REPO, "image", "app.sh")))
        for p, f in app_files(cfg).items():
            if p in files:
                sys.exit(f"[app] {p} is one of the platform's files; put it elsewhere")
            files[p] = f
    return files


def config_txt(cfg):
    """Lines for the firmware's config.txt (diskless: usercfg.txt)."""
    return BOOT_CONFIG + list((cfg.get("app") or {}).get("config_txt", []))


def services(cfg):
    """SERVICES, without what the config leaves to others or out (diskless)."""
    drop = set()
    if not platform_wifi(cfg):
        drop.add("wpa_supplicant")
    if not cfg["access"].get("ssh_public_key"):
        drop.add("sshd")
    if "time" in (cfg.get("app") or {}).get("owns", []):
        drop.add("chronyd")
    return {level: [s for s in svcs if s not in drop] for level, svcs in SERVICES.items()}


def overlay(cfg, plan, cached):
    s, host = cfg["system"], cfg["system"]["hostname"]
    files = config_files(cfg, plan)
    if cfg.get("app"):  # (the initramfs image's init starts the app itself, once the card is mounted)
        files["etc/local.d/90-app.start"] = (0o755, b"#!/bin/sh\nexec sh /etc/usb-gadget/app.sh start\n")
        files["etc/local.d/90-app.stop"] = (0o755, b"#!/bin/sh\nexec sh /etc/usb-gadget/app.sh stop\n")
    gen = {
        "etc/apk/world": "\n".join(s["packages"]) + "\n",
        "etc/apk/repositories": "\n".join(["/media/mmcblk0p1/apks"] + s["repositories"]) + "\n",
        # makes Alpine's init add its standard boot services next to ours
        "etc/.default_boot_services": "",
    }
    for p, text in gen.items():
        files[p] = (0o644, text.encode())

    links = {f"etc/runlevels/{level}/{svc}": f"/etc/init.d/{svc}"
             for level, svcs in services(cfg).items() for svc in svcs}
    if cached:
        links["etc/apk/cache"] = "/media/mmcblk0p1/cache"

    dirs = set()
    for p in list(files) + list(links):
        d = os.path.dirname(p)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        def add(name, type_, mode, data=b"", target=""):
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.mtime, ti.uid, ti.gid = type_, mode, MTIME, 0, 0
            ti.uname = ti.gname = "root"
            ti.size, ti.linkname = len(data), target
            tar.addfile(ti, io.BytesIO(data) if data else None)

        for d in sorted(dirs):
            add(d, tarfile.DIRTYPE, 0o700 if d.startswith("root") else 0o755)
        for p, (mode, data) in sorted(files.items()):
            add(p, tarfile.REGTYPE, mode, data)
        for p, target in sorted(links.items()):
            add(p, tarfile.SYMTYPE, 0o777, target=target)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return f"{host}.apkovl.tar.gz", out.getvalue(), len(files)


def manifest(card):
    lines = []
    for root, _, names in os.walk(card):
        for n in names:
            p = os.path.join(root, n)
            lines.append(f"{sha256(p)}  {os.path.relpath(p, card)}")
    return "\n".join(sorted(lines, key=lambda l: l[66:])) + "\n"


def kept_on_card(cfg):
    """What --install leaves on the card: the app's state folder and logs."""
    app = cfg.get("app") or {}
    return ([app["name"]] if app.get("state") else []) + (["logs"] if app.get("keep_logs") else [])


def install(card, target, expected, keep=(), merge=None):
    """Replace the card's contents with the build, except the top-level
    folders in `keep` (an app's state and logs) and USER_FILES, and verify
    the copy. A card's settings.txt stays as its users left it, plus what
    merge(old text) -> (text, new keys) adds (merge_settings)."""
    clash = [k for k in keep if os.path.exists(os.path.join(card, k))]
    if clash:
        sys.exit(f"the build has {', '.join(clash)}, which --install keeps on the card")
    if not os.path.exists(os.path.join(target, ".alpine-release")) and \
            [f for f in os.listdir(target) if not f.startswith(".")]:
        sys.exit(f"{target} isn't empty and doesn't look like an Alpine card; not touching it")
    step(f"installing to {target}")
    kept = sorted(k for k in keep if os.path.isdir(os.path.join(target, k)))
    if kept:
        print(f"keeping the app's {', '.join(k + '/' for k in kept)} on the card")
    old = None
    if os.path.isfile(os.path.join(target, SETTINGS)):
        with open(os.path.join(target, SETTINGS), "rb") as f:
            old = f.read()
    for name in os.listdir(target):
        if name in (".Spotlight-V100", ".fseventsd", ".Trashes") or name in keep or name in USER_FILES:
            continue
        p = os.path.join(target, name)
        try:
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        except FileNotFoundError:
            pass  # a "._" file macOS removed along with its file
    env = dict(os.environ, COPYFILE_DISABLE="1")
    # -X (macOS): no extended attributes, which FAT would keep as ._ files
    cp = ["cp", "-RX"] if sys.platform == "darwin" else ["cp", "-R"]
    subprocess.run(cp + [card + "/.", target + "/"], check=True, env=env)
    subprocess.run(["find", target, "-name", "._*", "-delete"], check=False,
                   stderr=subprocess.DEVNULL)
    if old is not None:  # the users' file, not the build's
        text, new = merge(old.decode("utf-8", "replace")) if merge else (None, [])
        with open(os.path.join(target, SETTINGS), "wb") as f:
            f.write(text.encode() if new else old)
        print(f"keeping the card's {SETTINGS}" + (f", with the new {', '.join(new)} added (commented out)"
                                                  if new else ""))
        expected = "".join(l + "\n" for l in expected.splitlines() if l[66:] != SETTINGS)
    subprocess.run(["sync"], check=True)
    step("verifying the copy")
    got = manifest_of(target, expected)
    if got != expected:
        sys.exit("card contents don't match the build manifest")
    print("card matches the manifest")


def manifest_of(target, expected):
    lines = []
    for line in expected.splitlines():
        rel_path = line[66:]
        p = os.path.join(target, rel_path)
        lines.append(f"{sha256(p) if os.path.exists(p) else 'missing'}  {rel_path}")
    return "\n".join(lines) + "\n"


def validate(cfg, path):
    """Catch an unfilled template before doing any work."""
    missing = [f"[{sec}] {key}" for sec, key in (("alpine", "version"), ("alpine", "sha256"),
                                                ("system", "hostname"))
               if not cfg.get(sec, {}).get(key)]
    if missing:
        sys.exit(f"{path}: fill in {', '.join(missing)} (see image/template.toml)")
    acc = cfg.setdefault("access", {})
    key = rel(acc["ssh_public_key"]) if acc.get("ssh_public_key") else ""
    if key and not os.path.exists(key):
        sys.exit(f"{path}: [access] ssh_public_key {key} doesn't exist")
    if acc.get("wifi") and not os.path.exists(rel(acc["wifi"])):
        sys.exit(f"{path}: [access] wifi file {rel(acc['wifi'])} doesn't exist")
    unknown = [f"[{sec}]" for sec in cfg if sec not in CONFIG_KEYS] + \
        [f"[{sec}] {key}" for sec, keys in cfg.items() if sec in CONFIG_KEYS
         for key in keys if key not in CONFIG_KEYS[sec]]
    if unknown:
        sys.exit(f"{path}: unknown settings {', '.join(unknown)} (see image/template.toml)")
    dev = cfg.get("device")
    if dev is not None:
        if "description" not in dev:
            sys.exit(f"{path}: [device] needs description = \"<file>.json\" (or a hand-written .toml)")
        if not os.path.exists(rel(dev["description"])):
            sys.exit(f"{path}: [device] description {rel(dev['description'])} doesn't exist")
    if cfg.get("app") is not None:
        validate_app(cfg, path)
    if cfg.get("image", {}).get("type", "diskless") not in ("diskless", "initramfs"):
        sys.exit(f"{path}: [image] type is \"diskless\" or \"initramfs\"")


def validate_app(cfg, path):
    app = cfg["app"]

    def bad(msg):
        sys.exit(f"{path}: [app] {msg} (see README: Apps)")

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app.get("name", "")) or app["name"] in CARD_NAMES:
        bad('name: lowercase letters, digits, "-" and "_" (not a folder the card has, such as "logs"), '
            'e.g. name = "myapp"')
    for key in ("run",):
        if not isinstance(app.get(key, ""), str):
            bad(f"{key} is a string")
    for key in ("state", "keep_logs", "commits_trial"):
        if not isinstance(app.get(key, False), bool):
            bad(f"{key} is true or false")
    t = app.get("trial_seconds", 300)
    if isinstance(t, bool) or not isinstance(t, int) or not 30 <= t <= 3600:
        bad("trial_seconds is a number of seconds, 30-3600")
    for key in ("owns", "config_txt", "kmods"):
        v = app.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x and "\n" not in x for x in v):
            bad(f"{key} is a list of strings")
    if set(app.get("owns", [])) - OWNABLE:
        bad(f"owns: only {', '.join(sorted(OWNABLE))}")
    if "wifi" in app.get("owns", []) and cfg["access"].get("wifi"):
        bad("owns wifi: leave [access] wifi out (or empty)")
    if app.get("overlay") and not os.path.isdir(rel(app["overlay"])):
        bad(f"overlay {rel(app['overlay'])} isn't a folder")
    files = app.get("files", {})
    if not isinstance(files, dict):
        bad('files is a table: { "path/on/pi" = "source" }')
    sets = app.get("settings", {})
    if not isinstance(sets, dict):
        bad('settings is a table: { key = "default" }')
    for k, v in sets.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", k):
            bad(f"settings: {k!r}: lowercase letters, digits and \"_\"")
        if isinstance(v, dict) and not set(v) <= {"default", "help"}:
            bad(f"settings: {k} is \"default\" or {{ default = \"...\", help = \"...\" }}")
        default, text = (v.get("default", ""), v.get("help", "")) if isinstance(v, dict) else (v, "")
        if not isinstance(default, str) or "\n" in default or not isinstance(text, str):
            bad(f"settings: {k}: a default (one line of text) and its help")
    for dest, src in files.items():
        d = os.path.normpath(dest.lstrip("/"))
        if d.startswith("..") or d == "." or not isinstance(src, str):
            bad(f"files: {dest!r} = {src!r}")
        if not os.path.isfile(rel(src)):
            bad(f"files: {rel(src)} doesn't exist")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("config")
    p.add_argument("--install", metavar="CARD", help="replace the card's contents with the build and verify")
    p.add_argument("--refetch", action="store_true", help="download the release and packages again")
    a = p.parse_args()

    global CONFIG_DIR, VARIANT
    CONFIG_DIR = os.path.dirname(os.path.abspath(a.config))
    os.makedirs(OUT, exist_ok=True)
    cfg = load_config(a.config)
    validate(cfg, a.config)
    host = cfg["system"]["hostname"]
    VARIANT, card, lock_path = names(a.config, host)
    shutil.rmtree(card, ignore_errors=True)
    os.makedirs(card)

    step("Alpine release")
    tarball = alpine_tarball(cfg, a.refetch)
    if cfg.get("image", {}).get("type") == "initramfs":
        import image_initramfs
        step("device plan")
        plan = make_plan(cfg)
        image_initramfs.build(cfg, tarball, card, config_files(cfg, plan), a.refetch, lock_path)
    else:
        extract(tarball, card)
        print(f"alpine-rpi {cfg['alpine']['version']} {cfg['alpine']['arch']}, kernel {kernel_release(card)}")
        step("kernel modules")
        copy_kmods(card, read_kmods(kernel_release(card)))
        step("device plan")
        plan = make_plan(cfg)
        step("packages")
        cached = fetch_packages(cfg, card, a.refetch, lock_path)
        step("overlay")
        name, data, n = overlay(cfg, plan, cached)
        with open(os.path.join(card, name), "wb") as f:
            f.write(data)
        with open(os.path.join(card, "usercfg.txt"), "w") as f:
            f.write("\n".join(config_txt(cfg)) + "\n")
        print(f"{name}: {n} files")
    with open(os.path.join(card, SETTINGS), "w") as f:
        f.write(settings_text(cfg, plan))
    dev = cfg.get("device") or {}
    console = cfg["access"].get("debug_console", True)
    print("at boot: " + ("device" + (" with the debug console" if dev.get("console") and console else "")
                         if plan and dev.get("start_at_boot") else "debug console" if console else "nothing on USB"))

    step("manifest")
    text = manifest(card)
    mpath = card + ".manifest"
    with open(mpath, "w") as f:
        f.write(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    print(f"{text.count(chr(10))} files, manifest sha256 {digest[:16]}  ({os.path.relpath(mpath, REPO)})")

    if a.install:
        install(card, a.install, text, kept_on_card(cfg), lambda old: merge_settings(old, cfg, plan))


if __name__ == "__main__":
    main()
