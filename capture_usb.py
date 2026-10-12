#!/usr/bin/env python3
"""
Capture everything a USB device defines about itself that the host can read
back: device/config/BOS descriptors, strings, HID report descriptors, and USB
Audio Class sample-rate and volume ranges. Read-only: only GET requests are sent.

  capture_usb.py list
  capture_usb.py capture --wait -o headphones.json    # then plug the device in
  capture_usb.py capture --vid 0x1234 --pid 0x5678 -o headphones.json
  capture_usb.py show headphones.json
  capture_usb.py compare headphones.json pi.json

The JSON holds the raw descriptor bytes plus a "model" summary that
pi_gadget.py turns into a Linux USB gadget.
"""
import argparse
import datetime
import json
import platform
import plistlib
import subprocess
import sys
import time

try:
    import usb.core
    import usb.backend.libusb1
except ImportError:
    sys.exit("pyusb missing: python3 -m pip install pyusb   (and: brew install libusb)")

SPEEDS = {1: "low-speed", 2: "full-speed", 3: "high-speed", 4: "super-speed", 5: "super-speed-plus"}
DT_DEVICE, DT_CONFIG, DT_STRING, DT_QUALIFIER, DT_OTHER_SPEED, DT_BOS = 1, 2, 3, 6, 7, 0x0F
TERMINAL_TYPES = {
    0x0101: "USB streaming", 0x0201: "microphone", 0x0202: "desktop microphone",
    0x0203: "personal microphone", 0x0204: "omni-directional microphone",
    0x0205: "microphone array", 0x0301: "speaker", 0x0302: "headphones",
    0x0303: "head-mounted display audio", 0x0304: "desktop speaker",
    0x0402: "headset", 0x0403: "speakerphone", 0x0405: "echo-cancelling speakerphone",
}


# ---------------------------------------------------------------- libusb access

def get_backend():
    for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib", None):
        be = usb.backend.libusb1.get_backend(find_library=(lambda _, p=path: p) if path else None)
        if be:
            return be
    sys.exit("libusb not found: brew install libusb")


def ctrl(dev, bm, req, value, index, length):
    return bytes(dev.ctrl_transfer(bm, req, value, index, length, timeout=1000))


def get_desc(dev, dtype, index, length, langid=0, bm=0x80):
    return ctrl(dev, bm, 0x06, (dtype << 8) | index, langid, length)


def try_(errors, label, fn, *args):
    try:
        return fn(*args)
    except (usb.core.USBError, NotImplementedError, ValueError) as e:
        errors.append(f"{label}: {e}")
        return None


def u16(b, o): return b[o] | (b[o + 1] << 8)
def s16(b, o): return int.from_bytes(b[o:o + 2], "little", signed=True)
def u24(b, o): return int.from_bytes(b[o:o + 3], "little")
def u32(b, o): return int.from_bytes(b[o:o + 4], "little")


# ---------------------------------------------------------------- descriptor parsing

def parse_device(d):
    return {"bcdUSB": u16(d, 2), "bDeviceClass": d[4], "bDeviceSubClass": d[5],
            "bDeviceProtocol": d[6], "bMaxPacketSize0": d[7], "idVendor": u16(d, 8),
            "idProduct": u16(d, 10), "bcdDevice": u16(d, 12), "iManufacturer": d[14],
            "iProduct": d[15], "iSerialNumber": d[16], "bNumConfigurations": d[17]}


def parse_config(buf):
    """Split a full configuration blob into decoded descriptors, tracking which
    interface/alt setting each belongs to (class-specific ones depend on it)."""
    descs, ctx, i = [], {"if": None, "alt": None, "cls": None, "sub": None, "uac": None}, 0
    while i + 2 <= len(buf):
        length, dtype = buf[i], buf[i + 1]
        if length < 2 or i + length > len(buf):
            descs.append({"name": "TRUNCATED", "interface": ctx["if"], "alt": ctx["alt"], "raw": buf[i:].hex()})
            break
        d = buf[i:i + length]
        i += length
        e = {"bDescriptorType": dtype}
        try:
            e.update(decode(d, dtype, ctx))
        except IndexError:
            e["note"] = "shorter than the spec says"
        e["interface"], e["alt"], e["raw"] = ctx["if"], ctx["alt"], d.hex()
        descs.append(e)
    return descs


def decode(d, t, ctx):
    if t == 0x02:
        return {"name": "CONFIGURATION", "wTotalLength": u16(d, 2), "bNumInterfaces": d[4],
                "bConfigurationValue": d[5], "iConfiguration": d[6], "bmAttributes": d[7],
                "bMaxPower": d[8]}
    if t == 0x04:
        ctx.update({"if": d[2], "alt": d[3], "cls": d[5], "sub": d[6],
                    "uac": {0x20: 2, 0x30: 3}.get(d[7], 1) if d[5] == 1 else None})
        return {"name": "INTERFACE", "bInterfaceNumber": d[2], "bAlternateSetting": d[3],
                "bNumEndpoints": d[4], "bInterfaceClass": d[5], "bInterfaceSubClass": d[6],
                "bInterfaceProtocol": d[7], "iInterface": d[8]}
    if t == 0x05:
        a, mps = d[3], u16(d, 4)
        r = {"name": "ENDPOINT", "bEndpointAddress": d[2], "direction": "IN" if d[2] & 0x80 else "OUT",
             "bmAttributes": a, "transfer": ["control", "isochronous", "bulk", "interrupt"][a & 3],
             "sync": ["none", "async", "adaptive", "sync"][(a >> 2) & 3],
             "usage": ["data", "feedback", "implicit-feedback", "reserved"][(a >> 4) & 3],
             "wMaxPacketSize": mps & 0x7FF, "mult": (mps >> 11) & 3, "bInterval": d[6]}
        if len(d) >= 9:
            r.update(bRefresh=d[7], bSynchAddress=d[8])
        return r
    if t == 0x0B:
        return {"name": "INTERFACE_ASSOCIATION", "bFirstInterface": d[2], "bInterfaceCount": d[3],
                "bFunctionClass": d[4], "bFunctionSubClass": d[5], "bFunctionProtocol": d[6],
                "iFunction": d[7]}
    if t == 0x21 and ctx["cls"] == 3:
        return {"name": "HID", "bcdHID": u16(d, 2), "bCountryCode": d[4], "bNumDescriptors": d[5],
                "descriptors": [{"bDescriptorType": d[6 + 3 * k], "wDescriptorLength": u16(d, 7 + 3 * k)}
                                for k in range(d[5])]}
    if t == 0x24 and ctx["cls"] == 1:
        return audio_cs_interface(d, ctx)
    if t == 0x25 and ctx["cls"] == 1:
        if ctx["uac"] == 2:
            return {"name": "AS_ENDPOINT", "bmAttributes": d[3], "bmControls": d[4],
                    "bLockDelayUnits": d[5], "wLockDelay": u16(d, 6)}
        return {"name": "AS_ENDPOINT", "bmAttributes": d[3], "bLockDelayUnits": d[4], "wLockDelay": u16(d, 5)}
    return {"name": f"UNKNOWN_0x{t:02x}"}


def audio_cs_interface(d, ctx):
    st, v2 = d[2], ctx["uac"] == 2
    r = {"subtype": st}
    if ctx["sub"] == 1:  # AudioControl
        if st == 1:
            if v2:
                r.update(name="AC_HEADER", bcdADC=u16(d, 3), bCategory=d[5], wTotalLength=u16(d, 6), bmControls=d[8])
            else:
                r.update(name="AC_HEADER", bcdADC=u16(d, 3), wTotalLength=u16(d, 5), baInterfaceNr=list(d[8:8 + d[7]]))
        elif st == 2:
            if v2:
                r.update(name="INPUT_TERMINAL", bTerminalID=d[3], wTerminalType=u16(d, 4), bAssocTerminal=d[6],
                         bCSourceID=d[7], bNrChannels=d[8], bmChannelConfig=u32(d, 9), iChannelNames=d[13],
                         bmControls=u16(d, 14), iTerminal=d[16])
            else:
                r.update(name="INPUT_TERMINAL", bTerminalID=d[3], wTerminalType=u16(d, 4), bAssocTerminal=d[6],
                         bNrChannels=d[7], bmChannelConfig=u16(d, 8), iChannelNames=d[10], iTerminal=d[11])
        elif st == 3:
            if v2:
                r.update(name="OUTPUT_TERMINAL", bTerminalID=d[3], wTerminalType=u16(d, 4), bAssocTerminal=d[6],
                         bSourceID=d[7], bCSourceID=d[8], bmControls=u16(d, 9), iTerminal=d[11])
            else:
                r.update(name="OUTPUT_TERMINAL", bTerminalID=d[3], wTerminalType=u16(d, 4), bAssocTerminal=d[6],
                         bSourceID=d[7], iTerminal=d[8])
        elif st in (4, 5):
            r.update(name="MIXER_UNIT" if st == 4 else "SELECTOR_UNIT", bUnitID=d[3], baSourceID=list(d[5:5 + d[4]]))
        elif st == 6:
            if v2:
                n = (len(d) - 6) // 4
                ctl = [u32(d, 5 + 4 * k) for k in range(n)]
                r.update(name="FEATURE_UNIT", bUnitID=d[3], bSourceID=d[4], bmaControls=ctl, iFeature=d[-1])
            else:
                cs = d[5]
                n = (len(d) - 7) // cs
                ctl = [int.from_bytes(d[6 + cs * k:6 + cs * (k + 1)], "little") for k in range(n)]
                r.update(name="FEATURE_UNIT", bUnitID=d[3], bSourceID=d[4], bControlSize=cs, bmaControls=ctl,
                         iFeature=d[-1])
        elif st == 0x0A and v2:
            r.update(name="CLOCK_SOURCE", bClockID=d[3], bmAttributes=d[4], bmControls=d[5],
                     bAssocTerminal=d[6], iClockSource=d[7])
        elif st == 0x0B and v2:
            n = d[4]
            r.update(name="CLOCK_SELECTOR", bClockID=d[3], baCSourceID=list(d[5:5 + n]), bmControls=d[5 + n],
                     iClockSelector=d[6 + n])
        elif st == 0x0C and v2:
            r.update(name="CLOCK_MULTIPLIER", bClockID=d[3], bCSourceID=d[4], bmControls=d[5], iClockMultiplier=d[6])
    elif ctx["sub"] == 2:  # AudioStreaming
        if st == 1:
            if v2:
                r.update(name="AS_GENERAL", bTerminalLink=d[3], bmControls=d[4], bFormatType=d[5],
                         bmFormats=u32(d, 6), bNrChannels=d[10], bmChannelConfig=u32(d, 11), iChannelNames=d[15])
            else:
                r.update(name="AS_GENERAL", bTerminalLink=d[3], bDelay=d[4], wFormatTag=u16(d, 5))
        elif st == 2:
            if v2:
                r.update(name="FORMAT_TYPE", bFormatType=d[3], bSubslotSize=d[4], bBitResolution=d[5])
            else:
                n = d[7]
                rates = [u24(d, 8 + 3 * k) for k in range(n)] if n else {"min": u24(d, 8), "max": u24(d, 11)}
                r.update(name="FORMAT_TYPE", bFormatType=d[3], bNrChannels=d[4], bSubslotSize=d[5],
                         bBitResolution=d[6], bSamFreqType=n, tSamFreq=rates)
    r.setdefault("name", f"AUDIO_CS_0x{st:02x}")
    return r


def string_refs(obj):
    """Every iXxx string index referenced anywhere in the decoded descriptors."""
    refs = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k[:1] == "i" and k[1:2].isupper() and isinstance(v, int) and v:
                refs.add(v)
            else:
                refs |= string_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            refs |= string_refs(v)
    return refs


# ---------------------------------------------------------------- capture

def capture(dev, probe_strings=16):
    errors = []
    cap = {"captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "host": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
           "bus": dev.bus, "address": dev.address, "speed": SPEEDS.get(dev.speed, "unknown"),
           "errors": errors}

    raw_dev = try_(errors, "device descriptor", get_desc, dev, DT_DEVICE, 0, 18)
    if raw_dev is None:  # fall back to libusb's cached copy
        raw_dev = bytes([18, 1]) + dev.bcdUSB.to_bytes(2, "little") + bytes(
            [dev.bDeviceClass, dev.bDeviceSubClass, dev.bDeviceProtocol, dev.bMaxPacketSize0]) + \
            dev.idVendor.to_bytes(2, "little") + dev.idProduct.to_bytes(2, "little") + \
            dev.bcdDevice.to_bytes(2, "little") + bytes(
            [dev.iManufacturer, dev.iProduct, dev.iSerialNumber, dev.bNumConfigurations])
    cap["device_descriptor"] = {"raw": raw_dev.hex(), **parse_device(raw_dev)}
    dd = cap["device_descriptor"]

    cap["configurations"] = []
    for n in range(dd["bNumConfigurations"]):
        head = try_(errors, f"config {n} header", get_desc, dev, DT_CONFIG, n, 9)
        if not head:
            continue
        full = try_(errors, f"config {n}", get_desc, dev, DT_CONFIG, n, u16(head, 2)) or head
        cap["configurations"].append({"index": n, "raw": full.hex(), "descriptors": parse_config(full)})

    if dd["bcdUSB"] >= 0x0201:
        head = try_(errors, "BOS header", get_desc, dev, DT_BOS, 0, 5)
        if head:
            full = try_(errors, "BOS", get_desc, dev, DT_BOS, 0, u16(head, 2)) or head
            cap["bos"] = {"raw": full.hex()}
    if dd["bcdUSB"] >= 0x0200:
        q = try_(errors, "device qualifier (normal for full-speed-only devices)", get_desc, dev, DT_QUALIFIER, 0, 10)
        if q:
            cap["device_qualifier"] = {"raw": q.hex()}
            head = try_(errors, "other-speed config header", get_desc, dev, DT_OTHER_SPEED, 0, 9)
            if head:
                full = try_(errors, "other-speed config", get_desc, dev, DT_OTHER_SPEED, 0, u16(head, 2)) or head
                cap["other_speed_configuration"] = {"raw": full.hex(), "descriptors": parse_config(full)}

    # Strings: everything referenced, plus a probe of low indices for unreferenced ones.
    cap["strings"] = {}
    s0 = try_(errors, "string 0 (language list)", get_desc, dev, DT_STRING, 0, 255)
    langids = [u16(s0, o) for o in range(2, s0[0], 2)] if s0 and len(s0) >= 4 else [0x0409]
    cap["langids"] = langids
    wanted = sorted(string_refs(cap) | set(range(1, probe_strings + 1)))
    referenced = string_refs(cap)
    for lang in langids:
        table = {}
        for idx in wanted:
            label = f"string {idx} lang 0x{lang:04x}"
            s = try_(errors if idx in referenced else [], label, get_desc, dev, DT_STRING, idx, 255, lang)
            if s and len(s) >= 2:
                table[str(idx)] = s[2:s[0]].decode("utf-16-le", "replace")
        cap["strings"][f"0x{lang:04x}"] = table

    descs = cap["configurations"][0]["descriptors"] if cap["configurations"] else []
    cap["hid_report_descriptors"] = capture_hid(dev, descs, dd, errors)
    product = cap["strings"].get(f"0x{langids[0]:04x}", {}).get(str(dd["iProduct"]))
    cap["uac_requests"] = capture_uac(dev, descs, errors, product)
    cap["model"] = build_model(cap)
    return cap


def capture_hid(dev, descs, dd, errors):
    out = {}
    hid_ifaces = [d for d in descs if d["name"] == "HID" and d["alt"] == 0]
    for h in hid_ifaces:
        for sub in h.get("descriptors", []):
            if sub["bDescriptorType"] != 0x22:
                continue
            r = try_(errors, f"HID report descriptor if{h['interface']}", get_desc, dev, 0x22, 0,
                     sub["wDescriptorLength"], h["interface"], 0x81)
            if r:
                out[str(h["interface"])] = {"raw": r.hex(), "source": "device"}
    missing = [h["interface"] for h in hid_ifaces if str(h["interface"]) not in out]
    if missing and platform.system() == "Darwin":
        # macOS's HID driver holds the interface; ask IOKit for its cached copy instead.
        try:
            xml = subprocess.run(["ioreg", "-r", "-c", "IOHIDDevice", "-l", "-a"],
                                 capture_output=True, check=True).stdout
            nodes = plistlib.loads(xml) if xml.strip() else []
            ours = [n for n in nodes if n.get("VendorID") == dd["idVendor"]
                    and n.get("ProductID") == dd["idProduct"] and "ReportDescriptor" in n]
            # Pair by length: each interface's HID descriptor declares its report descriptor size.
            for h in (h for h in hid_ifaces if h["interface"] in missing):
                want = next((x["wDescriptorLength"] for x in h["descriptors"] if x["bDescriptorType"] == 0x22), None)
                node = next((n for n in ours if len(n["ReportDescriptor"]) == want), None)
                if node is None:
                    errors.append(f"ioreg HID fallback: no {want}-byte report descriptor for if{h['interface']}")
                    continue
                ours.remove(node)
                out[str(h["interface"])] = {"raw": bytes(node["ReportDescriptor"]).hex(), "source": "ioreg"}
        except (subprocess.CalledProcessError, plistlib.InvalidFileException, OSError) as e:
            errors.append(f"ioreg HID fallback: {e}")
    return out


def coreaudio_volume(name, errors):
    """macOS's audio driver holds the audio interfaces, so feature-unit requests
    are refused. CoreAudio still knows the dB range the driver read from the
    device; the step size isn't exposed."""
    import ctypes
    try:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    except OSError as e:
        errors.append(f"CoreAudio: {e}")
        return {}

    class Addr(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

    cc = lambda s: int.from_bytes(s.encode(), "big")

    def get(obj, sel, scope, elem, buf):
        size = ctypes.c_uint32(ctypes.sizeof(buf))
        return ca.AudioObjectGetPropertyData(obj, ctypes.byref(Addr(cc(sel), cc(scope), elem)), 0, None,
                                             ctypes.byref(size), ctypes.byref(buf)) == 0

    ids = (ctypes.c_uint32 * 64)()
    if not get(1, "dev#", "glob", 0, ids):
        return {}
    out = {}
    for dev_id in (i for i in ids if i):
        nm = ctypes.create_string_buffer(256)
        if not get(dev_id, "name", "glob", 0, nm) or nm.value.decode(errors="replace") != name:
            continue
        for scope, key in (("outp", "output"), ("inpt", "input")):
            for elem in (0, 1):  # master, then first channel
                rng = (ctypes.c_double * 2)()
                if get(dev_id, "vdb#", scope, elem, rng):
                    out.setdefault(key, {"min_db": rng[0], "max_db": rng[1], "element": elem})
                    break
    return out


def capture_uac(dev, descs, errors, product=None):
    """Ranges the descriptors don't contain: UAC2 sample rates live behind a
    clock-source request; volume min/max/step behind feature-unit requests."""
    out = {"clocks": {}, "volume": {}}
    ac = [d for d in descs if d["name"] == "INTERFACE" and d["bInterfaceClass"] == 1 and d["bInterfaceSubClass"] == 1]
    if not ac:
        return out
    ac_if = ac[0]["bInterfaceNumber"]
    v2 = ac[0]["bInterfaceProtocol"] == 0x20
    units = [d for d in descs if d["interface"] == ac_if and d.get("subtype") is not None]

    for c in (u for u in units if u["name"] == "CLOCK_SOURCE"):
        idx = (c["bClockID"] << 8) | ac_if
        head = try_(errors, f"clock {c['bClockID']} rate range", ctrl, dev, 0xA1, 0x02, 0x0100, idx, 2)
        entry = {}
        if head and len(head) >= 2:
            n = u16(head, 0)
            body = try_(errors, f"clock {c['bClockID']} rate range", ctrl, dev, 0xA1, 0x02, 0x0100, idx, 2 + 12 * n)
            if body:
                entry["ranges"] = [{"min": u32(body, 2 + 12 * k), "max": u32(body, 6 + 12 * k),
                                    "res": u32(body, 10 + 12 * k)} for k in range(n) if 14 + 12 * k <= len(body)]
        cur = try_(errors, f"clock {c['bClockID']} current rate", ctrl, dev, 0xA1, 0x01, 0x0100, idx, 4)
        if cur and len(cur) == 4:
            entry["current"] = u32(cur, 0)
        out["clocks"][str(c["bClockID"])] = entry

    for fu in (u for u in units if u["name"] == "FEATURE_UNIT"):
        vol_bit = 0x0C if v2 else 0x02
        chans = [ch for ch, ctl in enumerate(fu["bmaControls"]) if ctl & vol_bit]
        if not chans:
            continue
        ch, idx = chans[0], (fu["bUnitID"] << 8) | ac_if
        label = f"feature unit {fu['bUnitID']} volume range"
        if v2:
            body = try_(errors, label, ctrl, dev, 0xA1, 0x02, 0x0200 | ch, idx, 8)
            if body and len(body) >= 8:
                out["volume"][str(fu["bUnitID"])] = {"channel": ch, "min": s16(body, 2), "max": s16(body, 4),
                                                     "res": s16(body, 6)}
        else:
            vals = [try_(errors, label, ctrl, dev, 0xA1, req, 0x0200 | ch, idx, 2) for req in (0x82, 0x83, 0x84)]
            if all(v and len(v) == 2 for v in vals):
                out["volume"][str(fu["bUnitID"])] = {"channel": ch, "min": s16(vals[0], 0),
                                                     "max": s16(vals[1], 0), "res": s16(vals[2], 0)}
    vol_units = [u for u in units if u["name"] == "FEATURE_UNIT" and any(c & (0x0C if v2 else 0x02) for c in u["bmaControls"])]
    if product and platform.system() == "Darwin" and len(out["volume"]) < len(vol_units):
        out["coreaudio_volume"] = coreaudio_volume(product, errors)
    return out


# ---------------------------------------------------------------- model (what pi_gadget.py consumes)

def expand_rates(ranges):
    rates = []
    for r in ranges:
        if r["min"] == r["max"] or not r["res"]:
            rates += sorted({r["min"], r["max"]})
        else:
            rates += list(range(r["min"], r["max"] + 1, r["res"]))[:16]
    return sorted(set(rates))


def build_model(cap):
    dd = cap["device_descriptor"]
    strings = next(iter(cap["strings"].values()), {})
    s = lambda i: strings.get(str(i)) if i else None
    descs = cap["configurations"][0]["descriptors"] if cap["configurations"] else []
    cfg = next((d for d in descs if d["name"] == "CONFIGURATION"), {})
    super_speed = cap["speed"].startswith("super")

    model = {
        "device": {k: dd[k] for k in ("idVendor", "idProduct", "bcdDevice", "bcdUSB", "bDeviceClass",
                                      "bDeviceSubClass", "bDeviceProtocol", "bMaxPacketSize0")},
        "speed": cap["speed"],
        "strings": {"manufacturer": s(dd["iManufacturer"]), "product": s(dd["iProduct"]),
                    "serialnumber": s(dd["iSerialNumber"])},
        "config": {"bmAttributes": cfg.get("bmAttributes"), "configuration": s(cfg.get("iConfiguration")),
                   "max_power_ma": cfg.get("bMaxPower", 0) * (8 if super_speed else 2)},
        "functions": [],
    }

    by_if = {}
    for d in descs:
        if d["interface"] is not None:
            by_if.setdefault(d["interface"], {}).setdefault(d["alt"], []).append(d)

    audio_done = False
    for ifnum in sorted(by_if):
        alt0 = by_if[ifnum][0][0] if 0 in by_if[ifnum] else by_if[ifnum][min(by_if[ifnum])][0]
        cls, sub = alt0.get("bInterfaceClass"), alt0.get("bInterfaceSubClass")
        if cls == 1 and sub in (1, 2):
            if not audio_done:
                model["functions"].append(build_audio(cap, descs, by_if, s))
                audio_done = True
        elif cls == 3:
            model["functions"].append(build_hid(cap, by_if[ifnum], ifnum, s))
        else:
            model["functions"].append({"kind": "other", "interface": ifnum, "class": cls, "subclass": sub,
                                       "protocol": alt0.get("bInterfaceProtocol"), "name": s(alt0.get("iInterface"))})
    return model


def build_audio(cap, descs, by_if, s):
    ac = next(d for d in descs if d["name"] == "INTERFACE" and d["bInterfaceClass"] == 1 and d["bInterfaceSubClass"] == 1)
    v2 = ac["bInterfaceProtocol"] == 0x20
    units = {}
    for d in descs:
        if d["interface"] == ac["bInterfaceNumber"]:
            key = d.get("bTerminalID", d.get("bUnitID", d.get("bClockID")))
            if key is not None and d["name"] not in ("CLOCK_SOURCE", "CLOCK_SELECTOR", "CLOCK_MULTIPLIER"):
                units[key] = d
    clocks = {d["bClockID"]: d for d in descs if d["name"] in ("CLOCK_SOURCE", "CLOCK_SELECTOR", "CLOCK_MULTIPLIER")}

    def upstream(uid, seen=None):
        seen = set() if seen is None else seen
        u = units.get(uid)
        if u is None or uid in seen:
            return seen
        seen.add(uid)
        for src in ([u["bSourceID"]] if "bSourceID" in u else u.get("baSourceID", [])):
            upstream(src, seen)
        return seen

    def clock_rates(cid, depth=0):
        c = clocks.get(cid)
        if c is None or depth > 4:
            return []
        if c["name"] == "CLOCK_SOURCE":
            return expand_rates(cap["uac_requests"]["clocks"].get(str(cid), {}).get("ranges", []))
        srcs = c.get("baCSourceID") or [c.get("bCSourceID")]
        return clock_rates(srcs[0], depth + 1)

    vol_bit, mute_bit = (0x0C, 0x03) if v2 else (0x02, 0x01)

    def feature(fu_ids, direction):
        fus = [units[i] for i in fu_ids if units[i]["name"] == "FEATURE_UNIT"]
        if not fus:
            return {"mute": False, "volume": None, "fu_name": None, "fu_id": None, "fu_control_size": None}
        fu = fus[0]
        ctl = fu["bmaControls"]
        vol = None
        if any(c & vol_bit for c in ctl[:2]):
            ca = cap["uac_requests"].get("coreaudio_volume", {}).get("output" if direction == "host_to_device" else "input")
            vol = cap["uac_requests"]["volume"].get(str(fu["bUnitID"]))
            if vol:
                vol = dict(vol, source="device")
            elif ca:
                vol = {"min": round(ca["min_db"] * 256), "max": round(ca["max_db"] * 256), "res": None, "source": "coreaudio"}
            else:
                vol = {"min": None, "max": None, "res": None, "source": "unknown"}
        return {"mute": any(c & mute_bit for c in ctl[:2]), "volume": vol, "fu_name": s(fu.get("iFeature")),
                "fu_id": fu["bUnitID"], "fu_control_size": fu.get("bControlSize", 4)}

    out = {"kind": "uac2" if v2 else "uac1", "control_interface": ac["bInterfaceNumber"],
           "name": s(ac.get("iInterface")), "host_to_device": None, "device_to_host": None}
    for ifnum, alts in sorted(by_if.items()):
        head = alts[min(alts)][0]
        if head.get("bInterfaceClass") != 1 or head.get("bInterfaceSubClass") != 2:
            continue
        stream_alts = []
        for alt, ds in sorted(alts.items()):
            gen = next((d for d in ds if d["name"] == "AS_GENERAL"), None)
            fmt = next((d for d in ds if d["name"] == "FORMAT_TYPE"), None)
            ep = next((d for d in ds if d["name"] == "ENDPOINT" and d["usage"] != "feedback"), None)
            if not (gen and fmt and ep):
                continue
            link = units.get(gen["bTerminalLink"], {})
            chans = gen.get("bNrChannels") or fmt.get("bNrChannels") or 2
            chmask = gen.get("bmChannelConfig") or link.get("bmChannelConfig") or (1 << chans) - 1
            rates = fmt["tSamFreq"] if not v2 else clock_rates(link.get("bCSourceID"))
            if isinstance(rates, dict):
                rates = [rates["min"], rates["max"]]
            stream_alts.append({"alt": alt, "terminal_link": gen["bTerminalLink"], "channels": chans,
                                "chmask": chmask, "subslot_bytes": fmt.get("bSubslotSize"),
                                "bit_resolution": fmt.get("bBitResolution"), "rates": rates,
                                "endpoint": ep["bEndpointAddress"], "sync": ep["sync"],
                                "wMaxPacketSize": ep["wMaxPacketSize"], "bInterval": ep["bInterval"],
                                "has_feedback_endpoint": any(d["name"] == "ENDPOINT" and d["usage"] == "feedback" for d in ds)})
        if not stream_alts:
            continue
        first = stream_alts[0]
        usb_term = first["terminal_link"]
        if first["endpoint"] & 0x80 == 0:  # OUT: host plays into the device
            outs = [u for u in units.values() if u["name"] == "OUTPUT_TERMINAL" and usb_term in upstream(u["bTerminalID"])]
            phys = outs[0] if outs else {}
            path = [i for i in upstream(phys.get("bTerminalID")) if usb_term in upstream(i)] if phys else []
            direction = "host_to_device"
        else:  # IN: device records for the host
            path = list(upstream(usb_term))
            ins = [units[i] for i in path if units[i]["name"] == "INPUT_TERMINAL" and units[i]["wTerminalType"] != 0x0101]
            phys = ins[0] if ins else {}
            direction = "device_to_host"
        out[direction] = {"interface": ifnum, "alts": stream_alts,
                          "terminal_type": phys.get("wTerminalType"),
                          "terminal_type_name": TERMINAL_TYPES.get(phys.get("wTerminalType"), "unknown"),
                          "terminal_name": s(phys.get("iTerminal")),
                          "usb_terminal_name": s(units.get(usb_term, {}).get("iTerminal")),
                          "terminal_id": phys.get("bTerminalID"), "usb_terminal_id": usb_term,
                          **feature(path, direction)}
    return out


def build_hid(cap, alts, ifnum, s):
    ds = alts.get(0) or alts[min(alts)]
    iface = ds[0]
    eps = [d for d in ds if d["name"] == "ENDPOINT" and d["transfer"] == "interrupt"]
    ep_in = next((e for e in eps if e["direction"] == "IN"), None)
    rd = cap["hid_report_descriptors"].get(str(ifnum))
    hid = next((d for d in ds if d["name"] == "HID"), {})
    return {"kind": "hid", "interface": ifnum, "subclass": iface["bInterfaceSubClass"],
            "protocol": iface["bInterfaceProtocol"], "name": s(iface.get("iInterface")),
            "bcdHID": hid.get("bcdHID"), "country_code": hid.get("bCountryCode"),
            "endpoint": ep_in["bEndpointAddress"] if ep_in else None,
            "report_length": ep_in["wMaxPacketSize"] if ep_in else 8,
            "bInterval": ep_in["bInterval"] if ep_in else None,
            "has_out_endpoint": any(e["direction"] == "OUT" for e in eps),
            "report_desc": rd["raw"] if rd else None}


# ---------------------------------------------------------------- CLI

def describe(dev):
    name = "?"
    try:
        name = f"{usb.util.get_string(dev, dev.iManufacturer) or ''} {usb.util.get_string(dev, dev.iProduct) or ''}".strip()
    except Exception:
        pass
    return f"bus {dev.bus} addr {dev.address:3}  {dev.idVendor:04x}:{dev.idProduct:04x}  {SPEEDS.get(dev.speed, '?'):11}  {name}"


def cmd_list(args):
    for dev in usb.core.find(find_all=True, backend=get_backend()):
        print(describe(dev))


def cmd_capture(args):
    be = get_backend()
    if args.wait:
        before = {(d.bus, d.address) for d in usb.core.find(find_all=True, backend=be)}
        print("Waiting for a new USB device... plug it in now (Ctrl-C to cancel).")
        while True:
            time.sleep(0.5)
            new = [d for d in usb.core.find(find_all=True, backend=be) if (d.bus, d.address) not in before]
            if new:
                time.sleep(1.5)  # let enumeration and any internal hub's children settle
                devs = [d for d in usb.core.find(find_all=True, backend=be) if (d.bus, d.address) not in before]
                break
    else:
        kw = {}
        if args.vid is not None:
            kw["idVendor"] = args.vid
        if args.pid is not None:
            kw["idProduct"] = args.pid
        if not kw:
            sys.exit("give --vid/--pid or --wait (see: capture_usb.py list)")
        devs = list(usb.core.find(find_all=True, backend=be, **kw))
        if not devs:
            sys.exit("no matching device")

    caps = []
    for dev in devs:
        print("capturing", describe(dev))
        caps.append(capture(dev, args.probe_strings))
    with open(args.output, "w") as f:
        json.dump({"tool": "capture_usb.py", "devices": caps}, f, indent=2)
    print(f"wrote {args.output}\n")
    for c in caps:
        show(c)


def load(path):
    with open(path) as f:
        return json.load(f)["devices"]


def pick(devs, index):
    if index is not None:
        return devs[index]
    # skip hubs (class 9): headphones with a built-in hub show up as hub + audio device
    return next((d for d in devs if d["device_descriptor"]["bDeviceClass"] != 9), devs[0])


def show(cap):
    m = cap["model"]
    dv = m["device"]
    print(f"{m['strings']['manufacturer']} / {m['strings']['product']}  "
          f"({dv['idVendor']:04x}:{dv['idProduct']:04x}, {m['speed']}, USB {dv['bcdUSB'] >> 8}.{(dv['bcdUSB'] >> 4) & 0xF})")
    print(f"  serial: {m['strings']['serialnumber']}   power: {m['config']['max_power_ma']} mA")
    for fn in m["functions"]:
        if fn["kind"] in ("uac1", "uac2"):
            print(f"  audio ({fn['kind'].upper()}): {fn['name'] or ''}")
            for key, label in (("host_to_device", "playback (host->device)"), ("device_to_host", "capture (device->host)")):
                st = fn[key]
                if not st:
                    continue
                a = st["alts"][0]
                vol = st["volume"]
                if not vol:
                    vtxt = "no volume"
                elif vol["min"] is None:
                    vtxt = "volume (range unknown)"
                else:
                    step = f" step {vol['res'] / 256:.2f}" if vol["res"] is not None else ""
                    vtxt = f"vol {vol['min'] / 256:.1f}..{vol['max'] / 256:.1f} dB{step} (from {vol['source']})"
                print(f"    {label}: {st['terminal_type_name']} '{st['terminal_name'] or ''}', "
                      f"{a['channels']}ch {a['bit_resolution']}-bit, rates {a['rates']}, sync {a['sync']}, "
                      f"mute {'yes' if st['mute'] else 'no'}, {vtxt}"
                      + (f"  (+{len(st['alts']) - 1} more alt formats)" if len(st["alts"]) > 1 else ""))
        elif fn["kind"] == "hid":
            n = len(fn["report_desc"]) // 2 if fn["report_desc"] else 0
            print(f"  HID interface {fn['interface']}: report descriptor {n} bytes, report length {fn['report_length']}")
        else:
            print(f"  other interface {fn['interface']}: class 0x{fn['class']:02x} sub 0x{fn['subclass']:02x} '{fn['name'] or ''}'")
    if cap["errors"]:
        print(f"  ({len(cap['errors'])} requests failed; see \"errors\" in the JSON)")


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list) and obj and isinstance(obj[0], (dict, list)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def cmd_show(args):
    for c in load(args.file):
        show(c)
        print()


def cmd_compare(args):
    a, b = pick(load(args.a), args.index_a), pick(load(args.b), args.index_b)
    # rebuild from the raw data so captures taken by older versions compare fully
    fa, fb = flatten(build_model(a)), flatten(build_model(b))
    diffs = [(k, fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]
    print(f"model fields: {len(set(fa) | set(fb))}, matching: {len(set(fa) | set(fb)) - len(diffs)}, different: {len(diffs)}")
    for k, va, vb in diffs:
        print(f"  {k}\n      {args.a}: {va}\n      {args.b}: {vb}")
    ca = [c["raw"] for c in a["configurations"]]
    cb = [c["raw"] for c in b["configurations"]]
    print("raw config descriptors:", "identical" if ca == cb else "differ (expected: the Linux gadget builds its own)")


def cmd_refresh(args):
    """Recompute the stored model from the raw capture data, e.g. after this
    script learned to extract more fields."""
    with open(args.file) as f:
        data = json.load(f)
    for cap in data["devices"]:
        cap["model"] = build_model(cap)
    with open(args.file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"refreshed {args.file}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list attached USB devices").set_defaults(fn=cmd_list)
    c = sub.add_parser("capture", help="capture a device to JSON")
    c.add_argument("--vid", type=lambda x: int(x, 0))
    c.add_argument("--pid", type=lambda x: int(x, 0))
    c.add_argument("--wait", action="store_true", help="wait for a newly plugged device")
    c.add_argument("--probe-strings", type=int, default=16, help="also probe string indices 1..N (default 16)")
    c.add_argument("-o", "--output", default="capture.json")
    c.set_defaults(fn=cmd_capture)
    s = sub.add_parser("show", help="summarize a capture")
    s.add_argument("file")
    s.set_defaults(fn=cmd_show)
    k = sub.add_parser("compare", help="compare two captures (e.g. real headphones vs Pi gadget)")
    k.add_argument("a")
    k.add_argument("b")
    k.add_argument("--index-a", type=int)
    k.add_argument("--index-b", type=int)
    k.set_defaults(fn=cmd_compare)
    r = sub.add_parser("refresh", help="recompute a capture's model from its raw data")
    r.add_argument("file")
    r.set_defaults(fn=cmd_refresh)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
