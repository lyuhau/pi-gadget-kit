#!/usr/bin/env python3
"""
Turn a capture_usb.py capture into a gadget plan: the exact configfs
directories, attribute writes and links that make a Linux USB gadget present
itself like the captured device. gadget.sh replays a plan on the Pi.

  gadget_plan.py headphones.json -o gadgets/gamebuds.plan
  gadget_plan.py headphones.json --max-speed high-speed --test-ids
  gadget_plan.py headphones.json --stock     # for unpatched gadget modules

Only the capture's "model" is read (see capture_usb.MODEL_FORMAT). Plans are
deterministic, so they can be committed and diffed.

Plan format, one operation per line ("#" lines are comments):
  name NAME            gadget directory under /sys/kernel/config/usb_gadget
  mkdir PATH
  write PATH VALUE     VALUE is the rest of the line; written with a newline,
                       which configfs strips (an empty VALUE clears a string)
  write? PATH VALUE    same, but the attribute may be missing on some kernels
  hex PATH HEX         write these raw bytes, no newline (HID report descriptors)
  link FUNCTION CONFIG symlink a function into a configuration
"""
import argparse
import json
import sys

MODEL_FORMAT = 1  # matches capture_usb.MODEL_FORMAT
LANG = "0x409"


class Plan:
    def __init__(self, name, patched):
        self.name, self.patched = name, patched
        self.ops, self.notes = [], []

    def mkdir(self, path):
        self.ops.append(f"mkdir {path}")

    def write(self, path, value, optional=False):
        if value is None:
            return
        value = str(value)
        if "\n" in value or value != value.strip():
            sys.exit(f"{path}: values with newlines or surrounding spaces can't be planned: {value!r}")
        self.ops.append(f"{'write?' if optional else 'write'} {path} {value}".rstrip())

    def hex(self, path, data_hex):
        self.ops.append(f"hex {path} {data_hex}")

    def link(self, function, config):
        self.ops.append(f"link {function} {config}")

    def note(self, text):
        self.notes.append(text)


def hexw(v):
    return f"0x{v:04x}"


def plan_audio(p, fn, inst, speed):
    """uac1/uac2 attribute naming is from the gadget's side: c_* = the gadget
    captures (host plays to it, i.e. headphones), p_* = the gadget plays (mic)."""
    base = f"functions/{inst}"
    p.mkdir(base)
    # alpine-module/patches/0001 adds these to uac1; the stock module lacks them.
    patched = fn["kind"] == "uac1" and p.patched
    if fn["kind"] == "uac1" and not patched:
        p.note("stock usb_f_uac1: names, terminal types and endpoint layout can't be matched")

    def name(path, value):
        # Patched module: an empty value means "no string", like a descriptor
        # with string index 0. Stock: leave the kernel's default name.
        if patched:
            p.write(path, value or "")
        elif value:
            p.write(path, value, optional=True)

    name(f"{base}/function_name", fn["name"])
    if patched:
        p.write(f"{base}/int_ep_present", int(fn["control_endpoints"] > 0))
    for key, pfx in (("host_to_device", "c"), ("device_to_host", "p")):
        st = fn[key]
        if not st:
            p.write(f"{base}/{pfx}_chmask", 0)  # direction absent
            continue
        a = st["alts"][0]
        p.write(f"{base}/{pfx}_chmask", a["chmask"] or (1 << a["channels"]) - 1)
        p.write(f"{base}/{pfx}_ssize", a["subslot_bytes"] or 2)
        p.write(f"{base}/{pfx}_srate", ",".join(map(str, a["rates"][:10] or [48000])))
        if fn["kind"] == "uac2":
            if pfx == "c" and a["sync"] in ("async", "adaptive"):
                p.write(f"{base}/c_sync", a["sync"], optional=True)
            if speed == "high-speed" and 1 <= a["bInterval"] <= 4:
                p.write(f"{base}/{pfx}_hs_bint", a["bInterval"], optional=True)
        p.write(f"{base}/{pfx}_mute_present", int(bool(st["mute"])))
        vol = st["volume"]
        p.write(f"{base}/{pfx}_volume_present", int(bool(vol)))
        if vol:
            # None (unknown) skips the write: the kernel default stays
            p.write(f"{base}/{pfx}_volume_min", vol["min"])
            p.write(f"{base}/{pfx}_volume_max", vol["max"])
            p.write(f"{base}/{pfx}_volume_res", vol["res"])
            if vol["min"] is None or vol["res"] is None:
                p.note(f"{key}: volume {'range' if vol['min'] is None else 'step'} unknown, kernel default used")
        if st["terminal_type"] and (patched or fn["kind"] == "uac2"):
            p.write(f"{base}/{pfx}_terminal_type", st["terminal_type"])
        # The physical terminal is the gadget's output (c) or input (p) terminal.
        phys, usb_side = ("ot", "it") if pfx == "c" else ("it", "ot")
        name(f"{base}/{pfx}_{phys}_name", st["terminal_name"])
        name(f"{base}/{pfx}_{usb_side}_name", st["usb_terminal_name"])
        name(f"{base}/{pfx}_fu_vol_name", st["fu_name"])
        if patched:
            name(f"{base}/{pfx}_it_ch_name", None)  # channel names aren't captured
            name(f"{base}/{pfx}_alt0_name", st["alt_names"].get("0"))
            name(f"{base}/{pfx}_alt1_name", st["alt_names"].get("1"))
            p.write(f"{base}/{pfx}_max_packet_size", a["wMaxPacketSize"])
            p.write(f"{base}/{pfx}_ep_num", a["endpoint"] & 0x0F)
            # c_it/p_ot are the USB-side terminals
            p.write(f"{base}/{pfx}_{usb_side}_id", st.get("usb_terminal_id"))
            p.write(f"{base}/{pfx}_{phys}_id", st.get("terminal_id"))
            p.write(f"{base}/{pfx}_fu_id", st.get("fu_id"))
            if st.get("fu_control_size") in (1, 2):
                p.write(f"{base}/fu_control_size", st["fu_control_size"])
        if len(st["alts"]) > 1:
            p.note(f"{key}: only the first of {len(st['alts'])} alternate formats is reproduced")


def plan_strings(p, strings, dev):
    """The gadget numbers strings in order: 1 manufacturer, 2 product, 3 serial,
    then custom strings, so a contiguous run 1..N can be matched, including
    strings the device has but never references (patch 0003's hide_strings)."""
    if not p.patched or "table" not in strings:
        for key in ("manufacturer", "product", "serialnumber"):
            if strings[key]:
                p.write(f"strings/{LANG}/{key}", strings[key])
        return
    table = {int(k): v for k, v in strings["table"].items()}
    slots = (("manufacturer", 1, "iManufacturer"), ("product", 2, "iProduct"),
             ("serialnumber", 3, "iSerialNumber"))
    hide = 0
    for bit, (key, idx, field) in enumerate(slots):
        if idx in table:
            p.write(f"strings/{LANG}/{key}", table[idx])
            if dev[field] != idx:
                hide |= 1 << bit  # present at idx, but not what the descriptor points to
        if dev[field] not in (0, idx):
            p.note(f"{field} is {dev[field]}; the gadget can only use {idx} or none")
    p.write("hide_strings", hide)
    idx = 4
    while idx in table:
        p.mkdir(f"strings/{LANG}/s{idx}")
        p.write(f"strings/{LANG}/s{idx}/s", table[idx])
        idx += 1
    rest = sorted(i for i in table if i >= idx)
    if rest:
        p.note(f"strings {rest} not reproduced: custom strings only fill indices 4..N without gaps")
    if len(strings.get("langids") or []) > 1:
        p.note(f"only {LANG} strings are reproduced, not {strings['langids']}")


def plan_hid(p, fn, inst):
    base = f"functions/{inst}"
    if not fn["report_desc"]:
        p.note(f"HID interface {fn['interface']}: no report descriptor captured, skipped")
        return False
    p.mkdir(base)
    p.write(f"{base}/protocol", fn["protocol"])
    p.write(f"{base}/subclass", fn["subclass"])
    p.write(f"{base}/report_length", fn["report_length"])
    p.hex(f"{base}/report_desc", fn["report_desc"])
    if p.patched:  # alpine-module/patches/0002
        p.write(f"{base}/name", fn["name"] or "")
        if fn.get("bcdHID") is not None:
            p.write(f"{base}/bcd_hid", fn["bcdHID"])
        if fn.get("country_code") is not None:
            p.write(f"{base}/country_code", fn["country_code"])
    if not fn["has_out_endpoint"]:
        p.write(f"{base}/no_out_endpoint", 1, optional=True)
    if fn.get("bInterval"):
        p.write(f"{base}/interval", fn["bInterval"], optional=True)
    return True


def build_plan(model, name="clone", patched=True, max_speed=None, test_ids=False):
    if model.get("format") != MODEL_FORMAT:
        sys.exit(f"capture model format {model.get('format')} != {MODEL_FORMAT}: "
                 "run capture_usb.py refresh, or update gadget_plan.py")
    p = Plan(name, patched)
    dv = model["device"]
    vid, pid = (0x1209, 0x0001) if test_ids else (dv["idVendor"], dv["idProduct"])
    p.write("idVendor", hexw(vid))
    p.write("idProduct", hexw(pid))
    p.write("bcdDevice", hexw(dv["bcdDevice"]))
    p.write("bcdUSB", hexw(min(dv["bcdUSB"], 0x0210)))  # the Pi's controller is USB 2 only
    cls = (dv["bDeviceClass"], dv["bDeviceSubClass"], dv["bDeviceProtocol"])
    if cls[0] == 0 and any(f["kind"] == "uac2" for f in model["functions"]):
        cls = (0xEF, 0x02, 0x01)  # UAC2 needs the interface association the kernel emits
        p.note("device class set to 0xEF/02/01 (required for the kernel's UAC2 association)")
    p.write("bDeviceClass", hexw(cls[0]))
    p.write("bDeviceSubClass", hexw(cls[1]))
    p.write("bDeviceProtocol", hexw(cls[2]))
    p.write("bMaxPacketSize0", dv["bMaxPacketSize0"])
    speed = max_speed or model["speed"]
    if speed in ("full-speed", "high-speed"):
        p.write("max_speed", speed)

    p.mkdir(f"strings/{LANG}")
    plan_strings(p, model["strings"], dv)

    cfg = model["config"]
    p.mkdir("configs/c.1")
    if cfg["bmAttributes"] is not None:
        p.write("configs/c.1/bmAttributes", hexw(cfg["bmAttributes"]))
    p.write("configs/c.1/MaxPower", min(cfg["max_power_ma"] or 100, 500))
    if cfg["configuration"]:
        p.mkdir(f"configs/c.1/strings/{LANG}")
        p.write(f"configs/c.1/strings/{LANG}/configuration", cfg["configuration"])

    hid_n = 0
    for fn in model["functions"]:  # link order = interface order the host sees
        if fn["kind"] in ("uac1", "uac2"):
            inst = f"{fn['kind']}.0"
            plan_audio(p, fn, inst, speed)
            p.link(f"functions/{inst}", f"configs/c.1/{inst}")
        elif fn["kind"] == "hid":
            inst = f"hid.{hid_n}"
            if plan_hid(p, fn, inst):
                p.link(f"functions/{inst}", f"configs/c.1/{inst}")
                hid_n += 1
        else:
            p.note(f"interface {fn['interface']} (class 0x{fn['class']:02x} '{fn['name'] or ''}') "
                   f"has no standard gadget function, not reproduced")
    return p


def render(p, source, options):
    lines = [f"# gadget plan generated by gadget_plan.py from {source}",
             f"# options: {options}"]
    lines += [f"# note: {n}" for n in p.notes]
    lines += [f"name {p.name}"] + p.ops
    return "\n".join(lines) + "\n"


def pick(devs, index):
    if index is not None:
        return devs[index]
    return next((d for d in devs if d["device_descriptor"]["bDeviceClass"] != 9), devs[0])


def plan_from_file(capture, name="clone", device=None, patched=True, max_speed=None, test_ids=False):
    with open(capture) as f:
        model = pick(json.load(f)["devices"], device)["model"]
    p = build_plan(model, name, patched, max_speed, test_ids)
    options = (f"name={name} target={'patched' if patched else 'stock'} "
               f"max_speed={max_speed or 'as-captured'} test_ids={'yes' if test_ids else 'no'}")
    return render(p, capture, options), p


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("capture")
    a.add_argument("-o", "--output", help="plan file (default: stdout)")
    a.add_argument("--name", default="clone", help="gadget directory name (default: clone)")
    a.add_argument("--device", type=int, help="which device in the capture (default: first non-hub)")
    a.add_argument("--stock", action="store_true", help="plan for unpatched gadget modules")
    a.add_argument("--max-speed", choices=["full-speed", "high-speed"], help="override the captured speed")
    a.add_argument("--test-ids", action="store_true", help="use test VID:PID 1209:0001")
    args = a.parse_args()
    text, p = plan_from_file(args.capture, args.name, args.device, not args.stock, args.max_speed, args.test_ids)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"wrote {args.output} ({len(p.ops)} operations)")
        for n in p.notes:
            print("  note:", n)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
