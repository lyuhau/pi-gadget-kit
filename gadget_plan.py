#!/usr/bin/env python3
"""
Turn a device description into a gadget plan: the exact configfs
directories, attribute writes and links that make a Linux USB gadget present
itself as described. gadget.sh replays a plan on the Pi.

  gadget_plan.py config/device.json -o out/device.plan
  gadget_plan.py config/device.json --max-speed high-speed --test-ids
  gadget_plan.py config/device.toml --console

build_card.py runs this for the card; run it by hand only to experiment.
Plans are deterministic, so they can be diffed. Plans assume the patched
gadget modules from alpine-module/.

A device description is either
  - a capture: a JSON in usb-capture's format (capture_usb.py,
    ~/Code/usb-capture), of which only the "model" is read (usb-capture
    README: Capture format), or
  - hand-written: a TOML (or JSON) file that is such a model, minus whatever
    has a default, plus function kinds a capture doesn't produce (ecm, ncm,
    rndis, acm). README: Hand-written devices.

Names: the gadget is /sys/kernel/config/usb_gadget/NAME (default "device"),
its functions are functions/KIND.ID, ID being the function's "id" or, without
one, a count per kind from 0 (uac1.0, hid.0, hid.1). The debug console
(--console) is acm.console. functions_text() lists them for apps.

Reproduced: device IDs and class, strings (a contiguous run 1..N, including
unreferenced ones), configuration attributes and power, UAC1 audio (fully,
via patch 0001), UAC2 audio (every setting the stock 6.18 module has), HID
interfaces, and (hand-written) ECM/NCM/RNDIS networking and ACM serial.
Not reproduced, reported as notes in the plan: other interface classes,
alternate audio formats after the first, what stock UAC2 fixes (alternate
setting names, feedback endpoint, bit resolution, packet sizes),
configurations after the first, strings beyond 1..N or in other languages
than 0x409. BOS, Microsoft OS and WebUSB descriptors aren't set.

Plan format, one operation per line ("#" lines are comments):
  name NAME            gadget directory under /sys/kernel/config/usb_gadget
  mkdir PATH
  write PATH VALUE     VALUE is the rest of the line; written with a newline,
                       which configfs strips (an empty VALUE clears a string)
  write? PATH VALUE    same, but the attribute may be missing on some kernels
  hex PATH HEX         write these raw bytes, no newline (HID report descriptors)
  link FUNCTION CONFIG symlink a function into a configuration
  console FUNCTION     once bound, run the debug console's shell on this acm
                       function's serial port
"""
import argparse
import json
import re
import sys

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

MODEL_FORMAT = 1  # usb-capture's capture_usb.MODEL_FORMAT
LANG = "0x409"
CFS = "/sys/kernel/config/usb_gadget"
CONSOLE = {"kind": "acm", "id": "console"}
# Functions the kernel gives an interface association descriptor, which
# hosts only honour with device class 0xEF/02/01.
IAD_KINDS = ("uac2", "acm", "ecm", "ncm", "rndis")
NET_KINDS = ("ecm", "ncm", "rndis")


class Plan:
    def __init__(self, name):
        self.name = name
        self.ops, self.notes = [], []
        self.functions = []  # (id, kind, instance), in interface order

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
    captures (host plays to it, i.e. headphones), p_* = the gadget plays (mic).
    Except uac2's terminal types, see below."""
    base = f"functions/{inst}"
    p.mkdir(base)
    # uac1 has alpine-module/patches/0001's extra settings; uac2 is stock.
    patched = fn["kind"] == "uac1"

    def name(path, value):
        # Patched: an empty value means "no string", like a descriptor with
        # string index 0. Stock: an empty value leaves the kernel's default.
        if patched:
            p.write(path, value or "")
        elif value:
            p.write(path, value, optional=True)

    if patched:
        name(f"{base}/function_name", fn["name"])  # the control interface's string
        p.write(f"{base}/int_ep_present", int(fn["control_endpoints"] > 0))
    else:
        name(f"{base}/function_name", fn.get("function_name"))  # the interface association's
        name(f"{base}/if_ctrl_name", fn["name"])  # the control interface's
        p.write(f"{base}/fb_max", fn.get("fb_max"))
    p.write(f"{base}/req_number", fn.get("req_number"))
    for key, pfx in (("host_to_device", "c"), ("device_to_host", "p")):
        st = fn[key]
        if not st:
            p.write(f"{base}/{pfx}_chmask", 0)  # direction absent
            continue
        a = st["alts"][0]
        p.write(f"{base}/{pfx}_chmask", a["chmask"] or (1 << a["channels"]) - 1)
        p.write(f"{base}/{pfx}_ssize", a["subslot_bytes"] or 2)
        p.write(f"{base}/{pfx}_srate", ",".join(map(str, a["rates"][:10] or [48000])))
        if len(a["rates"]) > 10:
            p.note(f"{key}: only the first 10 of {len(a['rates'])} rates are reproduced")
        if not patched:
            plan_uac2_stream(p, base, key, pfx, st, a, speed)
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
            elif not patched and (vol["max"] <= vol["min"] or vol["res"] <= 0
                                  or (vol["max"] - vol["min"]) % vol["res"]):
                p.note(f"{key}: the kernel refuses volume {vol['min']}..{vol['max']} step {vol['res']} "
                       "(needs max > min, in whole steps)")
        if st["terminal_type"]:
            # Stock uac2 names these from the host's side: p_terminal_type is
            # the physical output terminal (host_to_device), c_ the input.
            tpfx = {"c": "p", "p": "c"}[pfx] if not patched else pfx
            p.write(f"{base}/{tpfx}_terminal_type", st["terminal_type"])
        # The physical terminal is the gadget's output (c) or input (p) terminal.
        phys, usb_side = ("ot", "it") if pfx == "c" else ("it", "ot")
        name(f"{base}/{pfx}_{phys}_name", st["terminal_name"])
        name(f"{base}/{pfx}_{usb_side}_name", st["usb_terminal_name"])
        name(f"{base}/{pfx}_fu_vol_name", st["fu_name"])
        # channel names are the input terminal's: the USB side for c, physical for p
        name(f"{base}/{pfx}_it_ch_name", st.get("channel_names"))
        if patched:
            name(f"{base}/{pfx}_alt0_name", st["alt_names"].get("0"))
            name(f"{base}/{pfx}_alt1_name", st["alt_names"].get("1"))
            p.write(f"{base}/{pfx}_max_packet_size", a["wMaxPacketSize"])
            p.write(f"{base}/{pfx}_ep_num", None if a["endpoint"] is None else a["endpoint"] & 0x0F)
            # c_it/p_ot are the USB-side terminals
            p.write(f"{base}/{pfx}_{usb_side}_id", st.get("usb_terminal_id"))
            p.write(f"{base}/{pfx}_{phys}_id", st.get("terminal_id"))
            p.write(f"{base}/{pfx}_fu_id", st.get("fu_id"))
            if st.get("fu_control_size") in (1, 2):
                p.write(f"{base}/fu_control_size", st["fu_control_size"])
        if len(st["alts"]) > 1:
            p.note(f"{key}: only the first of {len(st['alts'])} alternate formats is reproduced")
    return True


def plan_uac2_stream(p, base, key, pfx, st, a, speed):
    """Stock f_uac2's per-direction settings beyond format and controls."""
    sync = a.get("sync")
    if pfx == "c":
        if sync in ("async", "adaptive"):
            p.write(f"{base}/c_sync", sync, optional=True)
        if sync in (None, "async") and a.get("has_feedback_endpoint") is False:
            p.note(f"{key}: async with a feedback endpoint (the kernel always adds one)")
    elif sync not in (None, "async"):
        p.note(f"{key}: {sync} endpoint described; the kernel's is always async")
    # 1..4: fixed; 0 (the kernel default): the longest interval the format fits
    if speed == "high-speed" and 1 <= (a.get("bInterval") or 0) <= 4:
        p.write(f"{base}/{pfx}_hs_bint", a["bInterval"], optional=True)
    p.write(f"{base}/clksrc_{'out' if pfx == 'c' else 'in'}_name", st.get("clock_name") or None, optional=True)
    bits = a.get("bit_resolution")
    if bits and bits != 8 * (a["subslot_bytes"] or 2):
        p.note(f"{key}: {bits}-bit samples in {a['subslot_bytes']} bytes; the kernel describes them as full width")
    if any(st["alt_names"].values()):
        p.note(f"{key}: alternate setting names are fixed by the kernel")


def plan_strings(p, strings, dev):
    """The gadget numbers strings in order: 1 manufacturer, 2 product, 3 serial,
    then custom strings, so a contiguous run 1..N can be matched, including
    strings the device has but never references (patch 0003's hide_strings)."""
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


def plan_hid(p, fn, inst, speed):
    base = f"functions/{inst}"
    if not fn["report_desc"]:
        p.note(f"HID interface {fn['interface']}: no report descriptor in the description, skipped")
        return False
    p.mkdir(base)
    p.write(f"{base}/protocol", fn["protocol"])
    p.write(f"{base}/subclass", fn["subclass"])
    p.write(f"{base}/report_length", fn["report_length"])
    p.hex(f"{base}/report_desc", fn["report_desc"])
    p.write(f"{base}/name", fn["name"] or "")  # alpine-module/patches/0002
    if fn.get("bcdHID") is not None:
        p.write(f"{base}/bcd_hid", fn["bcdHID"])
    if fn.get("country_code") is not None:
        p.write(f"{base}/country_code", fn["country_code"])
    if not fn["has_out_endpoint"]:
        p.write(f"{base}/no_out_endpoint", 1, optional=True)
    if fn.get("bInterval"):
        p.write(f"{base}/interval", fn["bInterval"], optional=True)
    return True


MAC = re.compile(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}")


def plan_net(p, fn, inst, speed):
    """ECM, NCM, RNDIS (u_ether). Without dev_addr/host_addr the kernel picks
    random addresses at every boot, and the host sees a new interface."""
    base = f"functions/{inst}"
    p.mkdir(base)
    for key in ("dev_addr", "host_addr"):
        if fn.get(key) is not None and not MAC.fullmatch(fn[key]):
            sys.exit(f"{inst}: {key} {fn[key]!r} isn't a MAC address like 02:00:00:00:00:01")
        p.write(f"{base}/{key}", fn.get(key))
    p.write(f"{base}/qmult", fn.get("qmult"))
    ifname = fn.get("ifname")
    if ifname is not None and (ifname.count("%") != 1 or "%d" not in ifname or len(ifname) > 15):
        sys.exit(f"{inst}: ifname {ifname!r} needs exactly one %d, which the kernel numbers (e.g. \"usb%d\")")
    p.write(f"{base}/ifname", ifname)
    if fn["kind"] == "ncm":
        p.write(f"{base}/max_segment_size", fn.get("max_segment_size"))
    return True


def plan_acm(p, fn, inst, speed):
    base = f"functions/{inst}"
    p.mkdir(base)
    p.write(f"{base}/protocol", fn.get("protocol"))
    return True


PLANNERS = {"uac1": plan_audio, "uac2": plan_audio, "hid": plan_hid, "acm": plan_acm,
            **{k: plan_net for k in NET_KINDS}}


def build_plan(model, name="device", max_speed=None, test_ids=False, console=False):
    if model.get("format") != MODEL_FORMAT:
        sys.exit(f"description model format {model.get('format')} != {MODEL_FORMAT}: "
                 "run usb-capture's capture_usb.py refresh, or update gadget_plan.py")
    p = Plan(name)
    functions = model["functions"] + ([CONSOLE] if console else [])
    dv = model["device"]
    vid, pid = (0x1209, 0x0001) if test_ids else (dv["idVendor"], dv["idProduct"])
    p.write("idVendor", hexw(vid))
    p.write("idProduct", hexw(pid))
    p.write("bcdDevice", hexw(dv["bcdDevice"]))
    p.write("bcdUSB", hexw(min(dv["bcdUSB"], 0x0210)))  # the Pi's controller is USB 2 only
    cls = (dv["bDeviceClass"], dv["bDeviceSubClass"], dv["bDeviceProtocol"])
    iad = sorted({f["kind"] for f in functions if f["kind"] in IAD_KINDS})
    if cls[0] == 0 and iad:
        cls = (0xEF, 0x02, 0x01)
        p.note(f"device class set to 0xEF/02/01 (required for the kernel's {'/'.join(iad)} interface association)")
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

    count = {}
    for fn in functions:  # link order = interface order the host sees
        kind = fn["kind"]
        if kind not in PLANNERS:
            p.note(f"interface {fn['interface']} (class 0x{fn['class']:02x} '{fn['name'] or ''}') "
                   f"has no standard gadget function, not reproduced")
            continue
        fid = fn.get("id")
        inst = f"{kind}.{count.get(kind, 0) if fid is None else fid}"
        if any(inst == i for _, _, i in p.functions):
            sys.exit(f"two functions named {inst}: give them different ids")
        if PLANNERS[kind](p, fn, inst, speed):
            p.link(f"functions/{inst}", f"configs/c.1/{inst}")
            p.functions.append((inst if fid is None else fid, kind, inst))
            count[kind] = count.get(kind, 0) + 1
    if console:
        p.ops.append("console functions/acm.console")
    return p


# ---------------------------------------------------------------- hand-written descriptions

def check_keys(where, d, allowed):
    if not isinstance(d, dict):
        sys.exit(f"{where}: expected a table, got {d!r}")
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        sys.exit(f"{where}: unknown keys {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


# Per audio direction: the format (a capture's first alternate setting), then
# the direction's controls and names. The defaults are what a hand-written
# device usually wants: 48 kHz, 16-bit stereo, no volume or mute control.
ALT_DEFAULTS = {"channels": 2, "chmask": None, "subslot_bytes": 2, "bit_resolution": None, "rates": [48000],
                "sync": None, "bInterval": 0, "wMaxPacketSize": None, "endpoint": None}
STREAM_DEFAULTS = {"mute": False, "volume": None, "terminal_type": None, "terminal_name": None,
                   "usb_terminal_name": None, "fu_name": None, "channel_names": None, "clock_name": None,
                   "alt_names": {}, "terminal_id": None, "usb_terminal_id": None, "fu_id": None,
                   "fu_control_size": None}
FUNCTION_DEFAULTS = {
    "uac1": {"name": None, "control_endpoints": 0, "req_number": None,
             "host_to_device": None, "device_to_host": None},
    "uac2": {"name": None, "function_name": None, "req_number": None, "fb_max": None,
             "host_to_device": None, "device_to_host": None},
    "hid": {"name": None, "subclass": 0, "protocol": 0, "report_length": 8, "report_desc": None,
            "bcdHID": None, "country_code": None, "has_out_endpoint": True, "bInterval": None},
    "acm": {"protocol": None},
    "ecm": {"dev_addr": None, "host_addr": None, "qmult": None, "ifname": None},
    "ncm": {"dev_addr": None, "host_addr": None, "qmult": None, "ifname": None, "max_segment_size": None},
    "rndis": {"dev_addr": None, "host_addr": None, "qmult": None, "ifname": None},
}
DEVICE_DEFAULTS = {"idVendor": None, "idProduct": None, "bcdDevice": 0x0100, "bcdUSB": 0x0200,
                   "bDeviceClass": 0, "bDeviceSubClass": 0, "bDeviceProtocol": 0, "bMaxPacketSize0": 64}


def complete_stream(where, d):
    if not d:
        return None
    check_keys(where, d, set(ALT_DEFAULTS) | set(STREAM_DEFAULTS))
    st = {k: d.get(k, v) for k, v in STREAM_DEFAULTS.items()}
    st["alts"] = [{k: d.get(k, v) for k, v in ALT_DEFAULTS.items()}]
    if st["volume"]:
        check_keys(f"{where}.volume", st["volume"], {"min", "max", "res"})
        if len(st["volume"]) != 3:
            sys.exit(f"{where}.volume: needs min, max and res (in 1/256 dB)")
    return st


def complete(desc, where="description"):
    """A hand-written description -> the model a capture would have."""
    check_keys(where, desc, {"format", "speed", "device", "strings", "config", "functions"})
    if desc.get("format", MODEL_FORMAT) != MODEL_FORMAT:
        sys.exit(f"{where}: format {desc['format']} != {MODEL_FORMAT}")
    check_keys(f"{where} [device]", desc.get("device", {}), DEVICE_DEFAULTS)
    dev = {k: desc.get("device", {}).get(k, v) for k, v in DEVICE_DEFAULTS.items()}
    if dev["idVendor"] is None or dev["idProduct"] is None:
        sys.exit(f"{where}: [device] needs idVendor and idProduct")
    strings = desc.get("strings", {})
    check_keys(f"{where} [strings]", strings, {"manufacturer", "product", "serialnumber"})
    table = {}
    for idx, (key, field) in enumerate((("manufacturer", "iManufacturer"), ("product", "iProduct"),
                                        ("serialnumber", "iSerialNumber")), 1):
        if strings.get(key):
            table[str(idx)] = strings[key]
        dev[field] = idx if strings.get(key) else 0
    cfg = desc.get("config", {})
    check_keys(f"{where} [config]", cfg, {"bmAttributes", "configuration", "max_power_ma"})
    speed = desc.get("speed", "high-speed")
    if speed not in ("full-speed", "high-speed"):
        sys.exit(f"{where}: speed is \"full-speed\" or \"high-speed\"")

    functions = []
    for i, fn in enumerate(desc.get("functions", [])):
        at = f"{where} functions[{i}]"
        kind = fn.get("kind") if isinstance(fn, dict) else None
        if kind not in FUNCTION_DEFAULTS:
            sys.exit(f"{at}: kind is one of {', '.join(FUNCTION_DEFAULTS)}")
        check_keys(at, fn, {"kind", "id"} | set(FUNCTION_DEFAULTS[kind]))
        out = {"kind": kind, "interface": i, **{k: fn.get(k, v) for k, v in FUNCTION_DEFAULTS[kind].items()}}
        if "id" in fn:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(fn["id"])):
                sys.exit(f"{at}: id {fn['id']!r}: use letters, digits, _ and -")
            out["id"] = str(fn["id"])
        if kind in ("uac1", "uac2"):
            for key in ("host_to_device", "device_to_host"):
                out[key] = complete_stream(f"{at}.{key}", out[key])
            if not (out["host_to_device"] or out["device_to_host"]):
                sys.exit(f"{at}: needs host_to_device, device_to_host or both")
        if kind == "hid":
            if not out["report_desc"]:
                sys.exit(f"{at}: needs report_desc (hex)")
            out["report_desc"] = re.sub(r"\s", "", out["report_desc"])
        functions.append(out)
    return {"format": MODEL_FORMAT, "device": dev, "speed": speed,
            "strings": {"table": table, "langids": [0x0409]},
            "config": {"bmAttributes": cfg.get("bmAttributes", 0x80), "configuration": cfg.get("configuration"),
                       "max_power_ma": cfg.get("max_power_ma", 100)},
            "functions": functions}


# ---------------------------------------------------------------- files

def render(p, source, options):
    lines = [f"# gadget plan generated by gadget_plan.py from {source}",
             f"# options: {options}"]
    lines += [f"# note: {n}" for n in p.notes]
    lines += [f"name {p.name}"] + p.ops
    return "\n".join(lines) + "\n"


def functions_text(p):
    """The plan's functions, for apps on the Pi: ID KIND DIRECTORY per line,
    in interface order. ID is the description's id, or KIND.N without one."""
    return (f"# functions of gadget '{p.name}' (gadget_plan.py): ID KIND DIRECTORY\n"
            + "".join(f"{fid} {kind} {CFS}/{p.name}/functions/{inst}\n" for fid, kind, inst in p.functions))


def pick(devs, index):
    if index is not None:
        return devs[index]
    return next((d for d in devs if d["device_descriptor"]["bDeviceClass"] != 9), devs[0])


def load_model(description, index=None):
    """A capture's model, or a hand-written description completed into one."""
    if description.endswith(".toml"):
        with open(description, "rb") as f:
            data = tomllib.load(f)
    else:
        with open(description) as f:
            data = json.load(f)
    if "devices" in data:  # usb-capture
        return pick(data["devices"], index)["model"]
    return complete(data, description)


def plan_from_file(description, name="device", index=None, max_speed=None, test_ids=False, console=False):
    p = build_plan(load_model(description, index), name, max_speed, test_ids, console)
    options = (f"name={name} "
               f"max_speed={max_speed or 'as-described'} test_ids={'yes' if test_ids else 'no'}")
    if console:
        options += " console=yes"
    return render(p, description, options), p


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("description", help="device description: a usb-capture JSON, or hand-written TOML/JSON")
    a.add_argument("-o", "--output", help="plan file (default: stdout)")
    a.add_argument("--name", default="device", help="gadget directory name (default: device)")
    a.add_argument("--index", type=int, help="which device in a capture (default: first non-hub)")
    a.add_argument("--max-speed", choices=["full-speed", "high-speed"], help="override the described speed")
    a.add_argument("--test-ids", action="store_true", help="use test VID:PID 1209:0001")
    a.add_argument("--console", action="store_true", help="include the debug console (acm.console)")
    args = a.parse_args()
    text, p = plan_from_file(args.description, args.name, args.index, args.max_speed, args.test_ids,
                             args.console)
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
