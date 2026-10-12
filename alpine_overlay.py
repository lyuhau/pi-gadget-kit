#!/usr/bin/env python3
"""
Build an Alpine "apkovl" overlay so a Pi running Alpine (diskless mode) comes
up headless on first boot: Wi-Fi from wifi.local, root SSH with your key, and
the USB gadget drivers loaded. Also writes usercfg.txt to enable device mode.

  python3 alpine_overlay.py /Volumes/ALPINE [--hostname usbpi]

Alpine's initramfs unpacks <hostname>.apkovl.tar.gz from the boot media, then
installs the packages in etc/apk/world from the card's bundled apks/ repo (no
network needed). etc/.default_boot_services makes it add the standard boot
services on top of the ones enabled here.
"""
import argparse
import io
import os
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGES = ["alpine-base", "openssh", "wpa_supplicant", "ifupdown-ng", "chrony", "iw"]
# wpa_supplicant/networking go in "default" so the boot runlevel (which loads
# the Wi-Fi driver and firmware) has finished and wlan0 exists by then.
SERVICES = {"default": ["wpa_supplicant", "networking", "sshd", "chronyd"]}
# "local" runs /etc/local.d/*.start after everything else in its runlevel; in
# "boot" it brings up the USB debug console even if networking misbehaves.
CONSOLE_SERVICES = {"boot": ["local"]}

# USB serial gadget with a root shell, for when Wi-Fi/SSH don't come up. The
# Mac sees /dev/cu.usbmodem*. pi_gadget.py unbinds it to take over the port.
USB_CONSOLE = """#!/bin/sh
G=/sys/kernel/config/usb_gadget/console
modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
[ -d $G ] && exit 0
mkdir -p $G/strings/0x409 $G/configs/c.1 $G/functions/acm.0
echo 0x1d6b > $G/idVendor
echo 0x0104 > $G/idProduct
echo usbpi > $G/strings/0x409/manufacturer
echo "usbpi console" > $G/strings/0x409/product
ln -s $G/functions/acm.0 $G/configs/c.1/
ls /sys/class/udc | head -n 1 > $G/UDC
# root shell without a login prompt (busybox login refuses root on ttyGS0)
( while :; do /sbin/getty -n -l /bin/sh -L 115200 ttyGS0 vt100; sleep 1; done ) >/dev/null 2>&1 &
# so the gadget can be removed later: an open ttyGS0 blocks removing acm.0
echo $! > /run/usb-console.pid
"""


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


def build(hostname, wifi, pubkey, console=True):
    files = {  # path -> (mode, content)
        "etc/hostname": (0o644, f"{hostname}\n"),
        "etc/hosts": (0o644, f"127.0.0.1\tlocalhost {hostname}\n::1\t\tlocalhost {hostname}\n"),
        "etc/apk/world": (0o644, "\n".join(PACKAGES) + "\n"),
        "etc/apk/repositories": (0o644, "/media/mmcblk0p1/apks\n"),
        "etc/.default_boot_services": (0o644, ""),
        "etc/network/interfaces": (0o644, "auto lo\niface lo inet loopback\n\nauto wlan0\niface wlan0 inet dhcp\n"),
        "etc/wpa_supplicant/wpa_supplicant.conf": (0o600, (
            "ctrl_interface=/var/run/wpa_supplicant\n"
            f"country={wifi['COUNTRY'].upper()}\n"
            "network={\n"
            f"\tssid={wpa_quote(wifi['SSID'])}\n"
            f"\tpsk={wpa_quote(wifi['PASSWORD'])}\n"
            "}\n")),
        "etc/conf.d/wpa_supplicant": (0o644, 'wpa_supplicant_args=""\nwpa_supplicant_if="wlan0"\n'),
        "etc/modules": (0o644, "af_packet\nipv6\nbrcmfmac\ndwc2\nlibcomposite\n"),
        "etc/lbu/lbu.conf": (0o644, "LBU_MEDIA=mmcblk0p1\n"),
        "root/.ssh/authorized_keys": (0o600, pubkey.strip() + "\n"),
    }
    services = {k: list(v) for k, v in SERVICES.items()}
    if console:
        files["etc/local.d/usb-console.start"] = (0o755, USB_CONSOLE)
        dirs_extra = {"etc/local.d"}
        for level, svcs in CONSOLE_SERVICES.items():
            services.setdefault(level, []).extend(svcs)
    else:
        dirs_extra = set()
    dirs = {"etc", "etc/apk", "etc/network", "etc/wpa_supplicant", "etc/conf.d", "etc/lbu",
            "etc/runlevels", "root", "root/.ssh"} | dirs_extra
    links = {}
    for level, svcs in services.items():
        dirs.add(f"etc/runlevels/{level}")
        for s in svcs:
            links[f"etc/runlevels/{level}/{s}"] = f"/etc/init.d/{s}"

    buf = io.BytesIO()
    now = time.time()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        def add(name, type_, mode, data=b"", target=""):
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.mtime, ti.uid, ti.gid = type_, mode, now, 0, 0
            ti.uname = ti.gname = "root"
            ti.size, ti.linkname = len(data), target
            tar.addfile(ti, io.BytesIO(data) if data else None)

        for d in sorted(dirs):
            add(d, tarfile.DIRTYPE, 0o700 if d in ("root", "root/.ssh") else 0o755)
        for path, (mode, text) in sorted(files.items()):
            add(path, tarfile.REGTYPE, mode, text.encode())
        for path, target in sorted(links.items()):
            add(path, tarfile.SYMTYPE, 0o777, target=target)
    return buf.getvalue()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bootfs", help="mounted Alpine boot partition")
    p.add_argument("--hostname", default="usbpi")
    p.add_argument("--wifi", default=os.path.join(HERE, "wifi.local"))
    p.add_argument("--pubkey", default=os.path.expanduser("~/.ssh/id_ed25519.pub"))
    p.add_argument("--no-console", action="store_true", help="leave out the USB serial debug console")
    a = p.parse_args()

    if not os.path.exists(os.path.join(a.bootfs, ".alpine-release")):
        sys.exit(f"{a.bootfs} doesn't look like an Alpine boot partition")
    ovl = build(a.hostname, read_wifi(a.wifi), open(a.pubkey).read(), console=not a.no_console)
    for old in (f for f in os.listdir(a.bootfs) if f.endswith(".apkovl.tar.gz")):
        os.remove(os.path.join(a.bootfs, old))  # init uses the first one it finds
    with open(os.path.join(a.bootfs, f"{a.hostname}.apkovl.tar.gz"), "wb") as f:
        f.write(ovl)
    print(f"wrote {a.hostname}.apkovl.tar.gz ({len(ovl)} bytes)")

    with open(os.path.join(a.bootfs, "usercfg.txt"), "w") as f:
        f.write("dtoverlay=dwc2,dr_mode=peripheral\n")
    print("wrote usercfg.txt (USB device mode)")


if __name__ == "__main__":
    main()
