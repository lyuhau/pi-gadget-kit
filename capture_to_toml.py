#!/usr/bin/env python3
"""
Turn a usb-capture capture (capture_usb.py, in the usb-capture project) into
a hand-written device description (TOML): a clone of the device, or a start
to edit from, adding ids, networking, req_number, and other settings a
capture can't know (README: Hand-written devices).

  capture_to_toml.py captures/headphones.json      # writes PRODUCT.toml, e.g. headphones.toml
  capture_to_toml.py captures/headphones.json -o config/flavors/local/headphones.toml --index 1

The second is an audiolink flavor: then `audiolink flavor try` or `flavor add`
it. Without -o, the file is named after the captured product (lowercase,
other characters as "-"); a device with no product name needs -o.

The TOML says extras = false: a faithful clone, without the debug console
or dev functions an image might add (a console would add a serial interface
and change the device class). --extras leaves that line out.

Only what gadget_plan.py reproduces is written. Values equal to the
hand-written defaults are left out; what can't be reproduced (interfaces
with no gadget function, alternate formats after the first, strings beyond
1..N) is listed as comments at the top.

The result is checked: the TOML must plan exactly like the capture. If it
doesn't, the differences are printed and the exit status is 1 (the file is
still written). An existing output file is never overwritten without --force.
"""
import argparse
import json
import os
import re
import sys

import gadget_plan as gp

# Written as hex: IDs, versions, classes, bitmaps
HEX = {"idVendor", "idProduct", "bcdDevice", "bcdUSB", "bDeviceClass", "bDeviceSubClass", "bDeviceProtocol",
       "bmAttributes", "chmask", "terminal_type", "bcdHID", "endpoint"}


def value(key, v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"0x{v:04x}" if key in HEX else str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON's escapes are valid TOML basic-string escapes
    if isinstance(v, list):
        return "[" + ", ".join(value(key, x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        keys = {k: k if re.fullmatch(r"[A-Za-z0-9_-]+", str(k)) else json.dumps(str(k)) for k in v}
        return "{ " + ", ".join(f"{keys[k]} = {value(k, x)}" for k, x in v.items()) + " }"
    raise TypeError(f"{key}: can't write {v!r}")


def table(out, header, items):
    """Append a TOML table with the items that aren't None, or nothing when empty."""
    lines = [f"{k} = {value(k, v)}" for k, v in items if v is not None]
    if lines or header.startswith("[["):
        out += ["", header] + lines


def differs(d, defaults):
    """d's items that aren't None and differ from the hand-written defaults."""
    return [(k, d.get(k)) for k in defaults if d.get(k) is not None and d.get(k) != defaults[k]]


def stream(st):
    """One audio direction: its first alternate format plus the direction's settings."""
    a = st["alts"][0]
    items = [("channels", a.get("channels"))]
    alt = {k: v for k, v in a.items() if k != "channels"}
    if alt.get("chmask") == (1 << (a.get("channels") or 0)) - 1:
        alt["chmask"] = None  # what channels gives anyway
    items += differs(alt, {k: v for k, v in gp.ALT_DEFAULTS.items() if k != "channels"})
    rest = dict(st)
    rest["alt_names"] = {k: v for k, v in (st.get("alt_names") or {}).items() if v is not None} or None
    if rest.get("volume") is not None:
        vol = {k: rest["volume"].get(k) for k in ("min", "max", "res")}
        rest["volume"] = {k: v for k, v in vol.items() if v is not None}  # {} = present, range unknown
    items += differs(rest, {k: v for k, v in gp.STREAM_DEFAULTS.items() if k != "volume"} | {"volume": None})
    return items


def slug(model):
    """The captured product's name as a file name: "GameBuds v2" -> gamebuds-v2, or "" for none."""
    text = model["strings"]["table"]
    product = text.get(str(model["device"]["iProduct"])) or ""
    return re.sub(r"[^a-z0-9]+", "-", product.lower())[:32].strip("-")


def to_toml(model, source, extras=False):
    notes, out = [], []
    if not extras:
        out.append("extras = false   # a faithful clone: no debug console or dev functions")
    speed = model["speed"]
    if speed not in ("full-speed", "high-speed"):
        notes.append(f"the device is {speed}; the Pi can do full-speed or high-speed")
        speed = "high-speed"
    out.append(f"speed = {value('speed', speed)}")

    dev = model["device"]
    strings = model["strings"]
    text = {int(k): v for k, v in strings["table"].items()}
    items = [("idVendor", dev["idVendor"]), ("idProduct", dev["idProduct"])]
    items += differs(dev, {k: v for k, v in gp.DEVICE_DEFAULTS.items() if k not in ("idVendor", "idProduct")})
    for idx, field in enumerate(gp.STRING_FIELDS, 1):
        default = idx if idx in text else 0
        if dev[field] != default:
            items.append((field, dev[field]))
            if dev[field] not in (0, idx):
                notes.append(f"{field} is {dev[field]}; the gadget can only use {idx} or none")
    table(out, "[device]", items)

    items = [(key, text.get(idx)) for idx, key in enumerate(gp.STRING_FIELDS.values(), 1)]
    extra, idx = [], 4
    while idx in text:
        extra.append(text[idx])
        idx += 1
    table(out, "[strings]", items + [("extra", extra or None)])
    rest = {i: s for i, s in sorted(text.items()) if i >= idx}
    if rest:
        notes.append("strings past 1..N (custom strings can't leave gaps): "
                     + ", ".join(f"{i} {json.dumps(s, ensure_ascii=False)}" for i, s in rest.items()))
    if len(strings.get("langids") or []) > 1:
        notes.append(f"only language 0x0409 is reproduced, not {[f'0x{l:04x}' for l in strings['langids']]}")

    cfg = model["config"]
    table(out, "[config]", differs(cfg, {"bmAttributes": 0x80, "configuration": None, "max_power_ma": 100}))

    for fn in model["functions"]:
        kind = fn["kind"]
        if kind not in gp.FUNCTION_DEFAULTS:
            notes.append(f"interface {fn['interface']} (class 0x{fn['class']:02x}, {fn.get('name') or 'unnamed'}): "
                         "no gadget function, left out")
            continue
        defaults = gp.FUNCTION_DEFAULTS[kind]
        audio = kind in ("uac1", "uac2")
        plain = {k: v for k, v in defaults.items() if not (audio and k in ("host_to_device", "device_to_host"))}
        table(out, "[[functions]]", [("kind", kind)] + differs(fn, plain))
        if audio:
            for key in ("host_to_device", "device_to_host"):
                st = fn.get(key)
                if st:
                    table(out, f"[functions.{key}]", stream(st))
                    if len(st["alts"]) > 1:
                        notes.append(f"{kind} {key}: only the first of {len(st['alts'])} alternate formats "
                                     "is reproduced")

    head = [f"# Converted from {source} by capture_to_toml.py."]
    head += [f"# Not reproduced: {n}" for n in notes]
    return "\n".join(head + [""] + out) + "\n"


def plan_ops(model):
    return gp.build_plan(model).ops


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("capture", help="a usb-capture JSON")
    a.add_argument("-o", "--output", help="TOML to write (default: the product's name, PRODUCT.toml)")
    a.add_argument("--index", type=int, help="which device in the capture (default: first non-hub)")
    a.add_argument("--force", action="store_true", help="overwrite an existing output file")
    a.add_argument("--extras", action="store_true",
                   help="leave out extras = false, so an image may add its debug console and dev functions")
    args = a.parse_args()

    if args.output and os.path.exists(args.output) and not args.force:
        sys.exit(f"{args.output} exists (--force to overwrite)")
    with open(args.capture) as f:
        data = json.load(f)
    if "devices" not in data:
        sys.exit(f"{args.capture}: not a usb-capture capture (no \"devices\")")
    model = gp.pick(data["devices"], args.index)["model"]
    if model.get("format") != gp.MODEL_FORMAT:
        sys.exit(f"{args.capture}: model format {model.get('format')} != {gp.MODEL_FORMAT}: "
                 "run usb-capture's capture_usb.py refresh")

    if not args.output:
        name = slug(model)
        if not name:
            sys.exit("the device has no product name: name the file with -o")
        args.output = name + ".toml"
        if os.path.exists(args.output) and not args.force:
            sys.exit(f"{args.output} exists (--force to overwrite, or -o)")

    text = to_toml(model, os.path.basename(args.capture), args.extras)
    with open(args.output, "w") as f:
        f.write(text)
    print(f"wrote {args.output}", flush=True)

    got, want = plan_ops(gp.complete(gp.tomllib.loads(text), args.output)), plan_ops(model)
    if got != want:
        print("the TOML doesn't plan like the capture:", file=sys.stderr)
        for op in want:
            if op not in got:
                print(f"  capture only: {op}", file=sys.stderr)
        for op in got:
            if op not in want:
                print(f"  TOML only:    {op}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
