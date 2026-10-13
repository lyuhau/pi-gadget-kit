#!/usr/bin/env python3
"""
Build the Pi's SD card contents from an image config (image/pi.toml).

  build_card.py image/pi.toml                          # -> out/card-<hostname>/
  build_card.py image/pi.toml --install /Volumes/ALPINE
  build_card.py image/pi.toml --refetch                # re-download cached packages

Steps:
  1. Alpine release tarball, verified against [alpine] sha256 (cached in out/cache/)
  2. patched kernel modules from [kernel_modules], checked against its kernel
  3. gadget plans from [gadgets], written to gadgets/<name>.plan and the card
  4. packages Alpine doesn't bundle, downloaded into the card's apk cache via
     Docker; their files and hashes go in image/packages.lock
  5. the apkovl overlay: image/overlay/ plus files generated from the config
     and the secrets under [access]
  6. a manifest of every file's sha256 (out/card-<hostname>.manifest)

Everything goes to out/card-<hostname>/ first, so builds can be compared;
--install then replaces the card's contents with it and verifies the copy.
"""
import argparse
import gzip
import hashlib
import io
import os
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
from wifi_config import read_wifi, wpa_quote

REPO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(REPO, "out")
MTIME = 946684800  # fixed timestamp (2000-01-01) so the overlay is reproducible


def rel(path):
    return os.path.join(REPO, os.path.expanduser(path))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def step(msg):
    print(f"== {msg}")


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


def kernel_release(card):
    cfgs = [f for f in os.listdir(os.path.join(card, "boot")) if f.startswith("config-")]
    return cfgs[0][len("config-"):] if cfgs else None


def copy_kmods(cfg, card):
    km = cfg.get("kernel_modules")
    if not km:
        return []
    release = kernel_release(card)
    dest = os.path.join(card, "kmods")
    os.makedirs(dest)
    for m in km["load"]:
        src = os.path.join(rel(km["dir"]), m + ".ko")
        if not os.path.exists(src):
            sys.exit(f"{src} missing: run alpine-module/build.sh")
        data = open(src, "rb").read()
        if f"vermagic={release} ".encode() not in data:
            sys.exit(f"{m}.ko wasn't built for this release's kernel {release}: rebuild with alpine-module/build.sh")
        shutil.copyfile(src, os.path.join(dest, m + ".ko"))
    print(f"{len(km['load'])} modules for kernel {release}")
    return km["load"]


def make_plans(cfg):
    plans = {}
    patched = bool(cfg.get("kernel_modules"))
    os.makedirs(os.path.join(REPO, "gadgets"), exist_ok=True)
    for name, g in cfg.get("gadgets", {}).items():
        text, p = gadget_plan.plan_from_file(rel(g["capture"]), name=name, patched=patched,
                                             max_speed=g.get("max_speed") or None,
                                             test_ids=g.get("test_ids", False))
        text = text.replace(f"from {rel(g['capture'])}", f"from {g['capture']}")
        path = os.path.join(REPO, "gadgets", f"{name}.plan")
        old = open(path).read() if os.path.exists(path) else None
        with open(path, "w") as f:
            f.write(text)
        state = "unchanged" if old == text else "updated" if old else "new"
        print(f"gadgets/{name}.plan: {len(p.ops)} operations ({state})")
        for n in p.notes:
            print(f"  note: {n}")
        plans[name] = text
    boot = cfg.get("boot", {}).get("gadget", "")
    if boot and boot not in plans:
        sys.exit(f"[boot] gadget '{boot}' isn't defined under [gadgets]")
    return plans


def bundled_packages(card, arch):
    """Package names in the release's own apks/ repository."""
    idx = os.path.join(card, "apks", arch, "APKINDEX.tar.gz")
    with tarfile.open(idx) as t:
        text = t.extractfile("APKINDEX").read().decode()
    return {line[2:] for line in text.splitlines() if line.startswith("P:")}


def fetch_packages(cfg, card, refetch):
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
    lock_path = os.path.join(REPO, "image", "packages.lock")
    old = open(lock_path).read() if os.path.exists(lock_path) else None
    with open(lock_path, "w") as f:
        f.write("# packages downloaded into the card's apk cache (build_card.py)\n" + lock)
    n = lock.count("\n")
    print(f"{n} cached packages; image/packages.lock "
          + ("unchanged" if old and old.split("\n", 1)[1] == lock else "updated: review the diff"))
    return True


def overlay(cfg, plans, kmods, cached):
    s, acc = cfg["system"], cfg["access"]
    wifi = read_wifi(rel(acc["wifi"]))
    pubkey = open(os.path.expanduser(acc["ssh_public_key"])).read().strip()
    host = s["hostname"]
    files = {}  # path -> (mode, bytes)

    src = os.path.join(REPO, "image", "overlay")
    for root, _, names in os.walk(src):
        for n in names:
            p = os.path.join(root, n)
            files[os.path.relpath(p, src)] = (0o755 if os.access(p, os.X_OK) else 0o644, open(p, "rb").read())
    if not acc.get("debug_console", True):
        files.pop("etc/local.d/10-usb-console.start", None)

    gen = {
        "etc/hostname": f"{host}\n",
        "etc/hosts": f"127.0.0.1\tlocalhost {host}\n::1\t\tlocalhost {host}\n",
        "etc/apk/world": "\n".join(s["packages"]) + "\n",
        "etc/apk/repositories": "\n".join(["/media/mmcblk0p1/apks"] + s["repositories"]) + "\n",
        # makes Alpine's init add its standard boot services next to ours
        "etc/.default_boot_services": "",
        "etc/modules": "\n".join(s["boot_modules"]) + "\n",
    }
    for p, text in gen.items():
        files[p] = (0o644, text.encode())
    files["etc/wpa_supplicant/wpa_supplicant.conf"] = (0o600, (
        "ctrl_interface=/var/run/wpa_supplicant\n"
        f"country={wifi['COUNTRY'].upper()}\n"
        "network={\n"
        f"\tssid={wpa_quote(wifi['SSID'])}\n"
        f"\tpsk={wpa_quote(wifi['PASSWORD'])}\n"
        "}\n").encode())
    files["root/.ssh/authorized_keys"] = (0o600, (pubkey + "\n").encode())
    if kmods:
        files["etc/usb-gadget/kmods.list"] = (0o644, (" ".join(kmods) + "\n").encode())
        files["etc/usb-gadget/pi_kmods.sh"] = (0o755, open(os.path.join(REPO, "alpine-module", "pi_kmods.sh"), "rb").read())
    if plans:
        files["etc/usb-gadget/gadget.sh"] = (0o755, open(os.path.join(REPO, "gadget.sh"), "rb").read())
        for name, text in plans.items():
            files[f"etc/usb-gadget/gadgets/{name}.plan"] = (0o644, text.encode())
    files["etc/usb-gadget/boot-gadget"] = (0o644, (cfg.get("boot", {}).get("gadget", "") + "\n").encode())

    links = {f"etc/runlevels/{level}/{svc}": f"/etc/init.d/{svc}"
             for level, svcs in cfg["services"].items() for svc in svcs}
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


def install(card, target, expected):
    if not os.path.exists(os.path.join(target, ".alpine-release")) and \
            [f for f in os.listdir(target) if not f.startswith(".")]:
        sys.exit(f"{target} isn't empty and doesn't look like an Alpine card; not touching it")
    step(f"installing to {target}")
    for name in os.listdir(target):
        if name in (".Spotlight-V100", ".fseventsd", ".Trashes"):
            continue
        p = os.path.join(target, name)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    env = dict(os.environ, COPYFILE_DISABLE="1")
    subprocess.run(["cp", "-RX", card + "/.", target + "/"], check=True, env=env)
    subprocess.run(["find", target, "-name", "._*", "-delete"], check=False,
                   stderr=subprocess.DEVNULL)
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
                                                ("system", "hostname"), ("access", "ssh_public_key"))
               if not cfg.get(sec, {}).get(key)]
    if missing:
        sys.exit(f"{path}: fill in {', '.join(missing)} (see image/template.toml)")
    key = os.path.expanduser(cfg["access"]["ssh_public_key"])
    if not os.path.exists(key):
        sys.exit(f"{path}: [access] ssh_public_key {key} doesn't exist")
    wifi = rel(cfg["access"]["wifi"])
    if not os.path.exists(wifi):
        sys.exit(f"{path}: [access] wifi file {wifi} doesn't exist")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("config")
    p.add_argument("--install", metavar="CARD", help="replace the card's contents with the build and verify")
    p.add_argument("--refetch", action="store_true", help="download the release and packages again")
    a = p.parse_args()

    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    validate(cfg, a.config)
    host = cfg["system"]["hostname"]
    card = os.path.join(OUT, f"card-{host}")
    shutil.rmtree(card, ignore_errors=True)
    os.makedirs(card)

    step("Alpine release")
    extract(alpine_tarball(cfg, a.refetch), card)
    print(f"alpine-rpi {cfg['alpine']['version']} {cfg['alpine']['arch']}, kernel {kernel_release(card)}")
    step("kernel modules")
    kmods = copy_kmods(cfg, card)
    step("gadget plans")
    plans = make_plans(cfg)
    step("packages")
    cached = fetch_packages(cfg, card, a.refetch)
    step("overlay")
    name, data, n = overlay(cfg, plans, kmods, cached)
    with open(os.path.join(card, name), "wb") as f:
        f.write(data)
    with open(os.path.join(card, "usercfg.txt"), "w") as f:
        f.write("\n".join(cfg["system"]["boot_config"]) + "\n")
    print(f"{name}: {n} files; boot gadget: {cfg.get('boot', {}).get('gadget') or 'none (debug console)'}")

    step("manifest")
    text = manifest(card)
    mpath = os.path.join(OUT, f"card-{host}.manifest")
    with open(mpath, "w") as f:
        f.write(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    print(f"{text.count(chr(10))} files, manifest sha256 {digest[:16]}  ({os.path.relpath(mpath, REPO)})")

    if a.install:
        install(card, a.install, text)


if __name__ == "__main__":
    main()
