"""config/wifi.local (gitignored) -> wpa_supplicant values. Shared by build_card.py and
buildroot/make_local_overlay.py."""
import sys


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
