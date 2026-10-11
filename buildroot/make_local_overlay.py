#!/usr/bin/env python3
"""Write the per-user rootfs overlay (gitignored): Wi-Fi config from
wifi.local and your SSH public key."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from alpine_overlay import read_wifi, wpa_quote  # noqa: E402

wifi = read_wifi(os.path.join(REPO, "wifi.local"))
pubkey = open(os.path.expanduser("~/.ssh/id_ed25519.pub")).read().strip()
root = os.path.join(HERE, "local", "overlay")

files = {
    "etc/wpa_supplicant.conf": (0o600, (
        "ctrl_interface=/var/run/wpa_supplicant\n"
        f"country={wifi['COUNTRY'].upper()}\n"
        "network={\n"
        f"\tssid={wpa_quote(wifi['SSID'])}\n"
        f"\tpsk={wpa_quote(wifi['PASSWORD'])}\n"
        "}\n")),
    "root/.ssh/authorized_keys": (0o600, pubkey + "\n"),
}
for rel, (mode, text) in files.items():
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
os.chmod(os.path.join(root, "root"), 0o700)
os.chmod(os.path.join(root, "root", ".ssh"), 0o700)
print(f"wrote {root}")
