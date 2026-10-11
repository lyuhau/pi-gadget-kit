#!/usr/bin/env python3
"""
Turn a capture_usb.py JSON into a Linux USB gadget (configfs), so a Raspberry Pi
presents itself to a host the way the captured device did. Stdlib only.

  sudo python3 pi_gadget.py prep                  # once: enable device mode, then reboot
  sudo python3 pi_gadget.py up headphones.json    # become the device
  sudo python3 pi_gadget.py down                  # detach and remove the gadget
  python3 pi_gadget.py up headphones.json --dry-run   # print what would be written

The kernel's uac1/uac2/hid functions build their own descriptors from these
settings, so the result matches what the host sees at the feature level (IDs,
strings, formats, rates, volume ranges, HID reports), not byte-for-byte.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

CONFIGFS = "/sys/kernel/config/usb_gadget"
LANG = "0x409"
# Gadgets we unbound to free the port (e.g. the debug console); "down" rebinds them.
DISPLACED = "/run/pi_gadget.displaced"


class Gadget:
    def __init__(self, root, dry_run):
        self.root, self.dry = root, dry_run
        self.skipped = []

    def path(self, rel):
        return os.path.join(self.root, rel)

    def mkdir(self, rel):
        if self.dry:
            print(f"mkdir  {rel}")
        else:
            os.makedirs(self.path(rel), exist_ok=True)

    def symlink(self, target, link):
        if self.dry:
            print(f"ln -s  {target} -> {link}")
        else:
            os.symlink(self.path(target), self.path(link))

    def write(self, rel, value, optional=False):
        """Write one configfs attribute. Optional attributes only exist on newer
        kernels; missing ones are reported, not fatal."""
        if value is None:
            return
        data = value if isinstance(value, bytes) else str(value).encode()
        if self.dry:
            shown = value.hex() if isinstance(value, bytes) else value
            print(f"write  {rel} = {shown}")
            return True
        p = self.path(rel)
        if not os.path.exists(p):
            if optional:
                self.skipped.append(f"{rel} (kernel has no such attribute)")
                return False
            sys.exit(f"missing configfs attribute: {p}")
        try:
            with open(p, "wb") as f:
                f.write(data)
            return True
        except OSError as e:
            if optional:
                self.skipped.append(f"{rel} = {value!r} rejected: {e.strerror}")
                return False
            raise


def pick(devs, index):
    if index is not None:
        return devs[index]
    return next((d for d in devs if d["device_descriptor"]["bDeviceClass"] != 9), devs[0])


def hexw(v):
    return f"0x{v:04x}"


def setup_audio(g, fn, inst, speed):
    """uac1/uac2 attribute naming is from the gadget's side: c_* = the gadget
    captures (host plays to it, i.e. headphones), p_* = the gadget plays (mic)."""
    base = f"functions/{inst}"
    g.mkdir(base)
    g.write(f"{base}/function_name", fn["name"], optional=True)
    for key, pfx in (("host_to_device", "c"), ("device_to_host", "p")):
        st = fn[key]
        if not st:
            g.write(f"{base}/{pfx}_chmask", 0)  # direction absent
            continue
        a = st["alts"][0]
        g.write(f"{base}/{pfx}_chmask", a["chmask"] or (1 << a["channels"]) - 1)
        g.write(f"{base}/{pfx}_ssize", a["subslot_bytes"] or 2)
        rates = a["rates"][:10] or [48000]
        # Newer kernels take a comma-separated rate list; older ones a single rate.
        if not g.write(f"{base}/{pfx}_srate", ",".join(map(str, rates)), optional=True):
            g.write(f"{base}/{pfx}_srate", max(rates))
            g.skipped.append(f"{pfx}_srate: kernel took only one rate, using {max(rates)} of {rates}")
        if fn["kind"] == "uac2":
            if pfx == "c" and a["sync"] in ("async", "adaptive"):
                g.write(f"{base}/c_sync", a["sync"], optional=True)
            if speed == "high-speed" and 1 <= a["bInterval"] <= 4:
                g.write(f"{base}/{pfx}_hs_bint", a["bInterval"], optional=True)
        g.write(f"{base}/{pfx}_mute_present", int(bool(st["mute"])), optional=True)
        vol = st["volume"]
        g.write(f"{base}/{pfx}_volume_present", int(bool(vol)), optional=True)
        if vol:
            # write() skips None: an unknown range or step keeps the kernel default
            g.write(f"{base}/{pfx}_volume_min", vol["min"], optional=True)
            g.write(f"{base}/{pfx}_volume_max", vol["max"], optional=True)
            g.write(f"{base}/{pfx}_volume_res", vol["res"], optional=True)
            if vol["min"] is None or vol["res"] is None:
                g.skipped.append(f"{key}: volume {'range' if vol['min'] is None else 'step'} unknown, kernel default used")
        if st["terminal_type"]:
            g.write(f"{base}/{pfx}_terminal_type", st["terminal_type"], optional=True)
        # The physical terminal is the gadget's output (c) or input (p) terminal.
        phys, usb_side = ("ot", "it") if pfx == "c" else ("it", "ot")
        g.write(f"{base}/{pfx}_{phys}_name", st["terminal_name"], optional=True)
        g.write(f"{base}/{pfx}_{usb_side}_name", st["usb_terminal_name"], optional=True)
        g.write(f"{base}/{pfx}_fu_vol_name", st["fu_name"], optional=True)
        if len(st["alts"]) > 1:
            g.skipped.append(f"{key}: only the first of {len(st['alts'])} alternate formats is reproduced")


def setup_hid(g, fn, inst):
    base = f"functions/{inst}"
    if not fn["report_desc"]:
        g.skipped.append(f"HID interface {fn['interface']}: no report descriptor captured, skipped")
        return False
    g.mkdir(base)
    g.write(f"{base}/protocol", fn["protocol"])
    g.write(f"{base}/subclass", fn["subclass"])
    g.write(f"{base}/report_length", fn["report_length"])
    g.write(f"{base}/report_desc", bytes.fromhex(fn["report_desc"]))
    if not fn["has_out_endpoint"]:
        g.write(f"{base}/no_out_endpoint", 1, optional=True)
    if fn.get("bInterval"):
        g.write(f"{base}/interval", fn["bInterval"], optional=True)
    return True


def cmd_up(args):
    with open(args.capture) as f:
        cap = pick(json.load(f)["devices"], args.device)
    m = cap["model"]
    if not args.dry_run:
        need_root()
        subprocess.run(["modprobe", "libcomposite"], check=False)
        if not os.path.isdir(CONFIGFS):
            sys.exit(f"{CONFIGFS} not found: is configfs mounted and libcomposite available?")
        if os.path.isdir(os.path.join(CONFIGFS, args.name)):
            teardown(args.name)

    g = Gadget(os.path.join(CONFIGFS, args.name), args.dry_run)
    g.mkdir("")
    dv = m["device"]
    vid, pid = (0x1209, 0x0001) if args.test_ids else (dv["idVendor"], dv["idProduct"])
    g.write("idVendor", hexw(vid))
    g.write("idProduct", hexw(pid))
    g.write("bcdDevice", hexw(dv["bcdDevice"]))
    g.write("bcdUSB", hexw(min(dv["bcdUSB"], 0x0210)))  # the Pi's controller is USB 2 only
    has_iad = any(f["kind"] == "uac2" for f in m["functions"])
    if dv["bDeviceClass"] == 0 and has_iad:
        # UAC2 needs an interface association; the kernel emits one, so declare it.
        dv = dict(dv, bDeviceClass=0xEF, bDeviceSubClass=0x02, bDeviceProtocol=0x01)
        g.skipped.append("device class set to 0xEF/02/01 (required for the kernel's UAC2 association)")
    g.write("bDeviceClass", hexw(dv["bDeviceClass"]))
    g.write("bDeviceSubClass", hexw(dv["bDeviceSubClass"]))
    g.write("bDeviceProtocol", hexw(dv["bDeviceProtocol"]))
    g.write("bMaxPacketSize0", dv["bMaxPacketSize0"], optional=True)
    if m["speed"] in ("full-speed", "high-speed"):
        g.write("max_speed", m["speed"], optional=True)

    g.mkdir(f"strings/{LANG}")
    for key in ("manufacturer", "product", "serialnumber"):
        if m["strings"][key]:
            g.write(f"strings/{LANG}/{key}", m["strings"][key])

    g.mkdir("configs/c.1")
    if m["config"]["bmAttributes"] is not None:
        g.write("configs/c.1/bmAttributes", hexw(m["config"]["bmAttributes"]), optional=True)
    g.write("configs/c.1/MaxPower", min(m["config"]["max_power_ma"] or 100, 500))
    if m["config"]["configuration"]:
        g.mkdir(f"configs/c.1/strings/{LANG}")
        g.write(f"configs/c.1/strings/{LANG}/configuration", m["config"]["configuration"])

    hid_n = 0
    for fn in m["functions"]:  # symlink order = interface order the host sees
        if fn["kind"] in ("uac1", "uac2"):
            inst = f"{fn['kind']}.0"
            setup_audio(g, fn, inst, m["speed"])
            g.symlink(f"functions/{inst}", f"configs/c.1/{inst}")
        elif fn["kind"] == "hid":
            inst = f"hid.{hid_n}"
            if setup_hid(g, fn, inst):
                g.symlink(f"functions/{inst}", f"configs/c.1/{inst}")
                hid_n += 1
        else:
            g.skipped.append(f"interface {fn['interface']} (class 0x{fn['class']:02x} '{fn['name'] or ''}') "
                             f"has no standard gadget function, not reproduced")

    udcs = sorted(os.listdir("/sys/class/udc")) if os.path.isdir("/sys/class/udc") else []
    if args.dry_run:
        print(f"write  UDC = {udcs[0] if udcs else '<first entry of /sys/class/udc>'}")
    elif not udcs:
        sys.exit("no USB device controller found: run 'sudo python3 pi_gadget.py prep' and reboot")
    else:
        displace_others(args.name, udcs[0])
        g.write("UDC", udcs[0])

    print(f"\ngadget '{args.name}' {'(dry run) ' if args.dry_run else ''}"
          f"as {m['strings']['manufacturer']} / {m['strings']['product']} ({vid:04x}:{pid:04x})")
    for s in g.skipped:
        print("  note:", s)
    if not args.dry_run:
        print("\nAudio from the host arrives on the Pi as an ALSA capture device:  arecord -l")
        if hid_n:
            print(f"HID reports can be sent by writing to /dev/hidg0..{hid_n - 1}")


def displace_others(name, udc):
    """A controller serves one gadget at a time: unbind whichever holds it."""
    moved = []
    for other in sorted(glob.glob(os.path.join(CONFIGFS, "*", "UDC"))):
        other_name = os.path.basename(os.path.dirname(other))
        if other_name != name and open(other).read().strip() == udc:
            with open(other, "w") as f:
                f.write("\n")
            moved.append(f"{other_name} {udc}")
            print(f"unbound gadget '{other_name}' from {udc} (pi_gadget.py down rebinds it)")
    if moved:
        with open(DISPLACED, "a") as f:
            f.write("\n".join(moved) + "\n")


def restore_displaced():
    if not os.path.exists(DISPLACED):
        return
    for line in open(DISPLACED).read().split("\n"):
        if not line.strip():
            continue
        other_name, udc = line.split()
        path = os.path.join(CONFIGFS, other_name, "UDC")
        try:
            with open(path, "w") as f:
                f.write(udc)
            print(f"rebound gadget '{other_name}' to {udc}")
        except OSError as e:
            print(f"could not rebind '{other_name}': {e.strerror}")
    os.remove(DISPLACED)


def teardown(name):
    g = os.path.join(CONFIGFS, name)
    if not os.path.isdir(g):
        return False
    try:
        with open(os.path.join(g, "UDC"), "w") as f:
            f.write("\n")
    except OSError:
        pass
    for cfg in glob.glob(os.path.join(g, "configs", "*")):
        for entry in os.listdir(cfg):
            p = os.path.join(cfg, entry)
            if os.path.islink(p):
                os.remove(p)
        for s in glob.glob(os.path.join(cfg, "strings", "*")):
            os.rmdir(s)
        os.rmdir(cfg)
    for fn in glob.glob(os.path.join(g, "functions", "*")):
        os.rmdir(fn)
    for s in glob.glob(os.path.join(g, "strings", "*")):
        os.rmdir(s)
    os.rmdir(g)
    return True


def cmd_down(args):
    need_root()
    print("removed" if teardown(args.name) else "no such gadget", args.name)
    restore_displaced()


def cmd_prep(args):
    need_root()
    model = open("/proc/device-tree/model").read().strip("\0\n") if os.path.exists("/proc/device-tree/model") else "?"
    print("board:", model)
    if any(x in model for x in ("Pi 3 Model B", "Pi 2", "Pi Model B", "Pi 1")):
        print("warning: this board's USB ports are host-only; device mode won't work")
    cfg = next((p for p in ("/boot/firmware/config.txt", "/boot/config.txt") if os.path.exists(p)), None)
    if not cfg:
        sys.exit("config.txt not found")
    text = open(cfg).read()
    if "dtoverlay=dwc2" in text:
        print(f"{cfg} already has a dwc2 overlay")
    else:
        with open(cfg + ".bak", "w") as f:
            f.write(text)
        with open(cfg, "a") as f:
            f.write("\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n")
        print(f"added dtoverlay=dwc2,dr_mode=peripheral to {cfg} (backup: {cfg}.bak); reboot to apply")
    cmdline = next((p for p in ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt") if os.path.exists(p)), None)
    if cmdline and "modules-load=dwc2,g_" in open(cmdline).read():
        print(f"warning: {cmdline} loads a legacy g_* gadget that will grab the port; remove it")


def need_root():
    if os.geteuid() != 0:
        sys.exit("run with sudo")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default="clone", help="gadget name under configfs (default: clone)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prep", help="enable USB device mode (needs reboot)").set_defaults(fn=cmd_prep)
    u = sub.add_parser("up", help="create and attach the gadget")
    u.add_argument("capture")
    u.add_argument("--device", type=int, help="which device in the capture (default: first non-hub)")
    u.add_argument("--test-ids", action="store_true", help="use test VID:PID 1209:0001 instead of the captured ones")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(fn=cmd_up)
    sub.add_parser("down", help="detach and remove the gadget").set_defaults(fn=cmd_down)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
