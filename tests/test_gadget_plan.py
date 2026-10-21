"""
Golden tests for gadget_plan.py: descriptions in tests/data/ -> plans and
function lists next to them. Plans are deterministic text, so a change shows
as a diff. After an intended change, regenerate and review the diff:

  .venv/bin/python -m unittest discover tests            # run
  UPDATE_GOLDEN=1 .venv/bin/python -m unittest discover tests
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, REPO)

import gadget_plan  # noqa: E402

# description, options, golden file stem
CASES = [
    ("capture.json", {}, "capture"),  # a usb-capture capture: UAC1, three HID, one unsupported
    ("capture.json", {"max_speed": "high-speed", "test_ids": True}, "capture-hs"),
    ("audiolink.toml", {"console": True, "dev": True}, "audiolink"),  # UAC2 + ECM (dev) + the debug console
    ("audiolink.toml", {"console": True}, "audiolink-release"),  # the same without its dev ECM
    ("kitchen.toml", {"console": True, "dev": True}, "kitchen"),  # every hand-written setting
]


class Golden(unittest.TestCase):
    def check(self, got, name):
        path = os.path.join(DATA, name)
        if os.environ.get("UPDATE_GOLDEN"):
            with open(path, "w") as f:
                f.write(got)
        with open(path) as f:
            self.assertEqual(f.read(), got, f"{name} differs (UPDATE_GOLDEN=1 to regenerate)")

    def test_cases(self):
        for desc, opts, stem in CASES:
            with self.subTest(stem):
                text, p = gadget_plan.plan_from_file(os.path.join(DATA, desc), **opts)
                self.check(text.replace(DATA + os.sep, ""), stem + ".plan")
                self.check(gadget_plan.functions_text(p), stem + ".functions")


class HandWritten(unittest.TestCase):
    BASE = {"device": {"idVendor": 0x1209, "idProduct": 0x0001}}

    def plan(self, functions, **opts):
        return gadget_plan.build_plan(gadget_plan.complete(dict(self.BASE, functions=functions)), **opts)

    def refused(self, functions):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            self.plan(functions)
        return str(e.exception.code)

    def test_names(self):
        p = self.plan([{"kind": "acm"}, {"kind": "acm"}, {"kind": "ecm", "id": "net"}], console=True)
        self.assertEqual([i for _, _, i in p.functions], ["acm.0", "acm.1", "ecm.net", "acm.console"])
        self.assertEqual(p.ops[-1], "console functions/acm.console")

    def test_minimal_audio_defaults(self):
        ops = self.plan([{"kind": "uac2", "host_to_device": {}, "device_to_host": {"channels": 1}}]).ops
        self.assertIn("write functions/uac2.0/c_chmask 3", ops)  # an empty table: all defaults
        self.assertIn("write functions/uac2.0/p_chmask 1", ops)
        ops = self.plan([{"kind": "uac2", "host_to_device": False, "device_to_host": {}}]).ops
        self.assertIn("write functions/uac2.0/c_chmask 0", ops)  # absent direction
        self.assertIn("write functions/uac2.0/p_srate 48000", ops)
        self.assertIn("write functions/uac2.0/p_volume_present 0", ops)

    def test_strings(self):
        desc = dict(self.BASE, strings={"manufacturer": "", "serialnumber": "S1", "extra": ["four", "five"]})
        desc["device"] = dict(desc["device"], iSerialNumber=0)
        m = gadget_plan.complete(desc)
        self.assertEqual(m["strings"]["table"], {"1": "", "3": "S1", "4": "four", "5": "five"})
        self.assertEqual([m["device"][k] for k in ("iManufacturer", "iProduct", "iSerialNumber")], [1, 0, 0])
        ops = gadget_plan.build_plan(m).ops
        self.assertIn("write hide_strings 4", ops)  # string 3 present, not pointed to
        self.assertIn("write strings/0x409/s5/s five", ops)

    def test_partial_volume(self):
        ops = self.plan([{"kind": "uac2", "host_to_device": {"volume": {"min": -100, "max": 0}}}]).ops
        self.assertIn("write functions/uac2.0/c_volume_present 1", ops)
        self.assertIn("write functions/uac2.0/c_volume_min -100", ops)
        self.assertFalse(any("c_volume_res" in op for op in ops))  # the kernel's default

    def test_dev_functions(self):
        fns = [{"kind": "ecm", "id": "net", "dev": True}, {"kind": "uac2", "id": "audio", "host_to_device": {}}]
        p = self.plan(fns)
        self.assertEqual([i for _, _, i in p.functions], ["uac2.audio"])
        self.assertIn("net: a dev function, left out (--dev / [device] dev_functions)", p.notes)
        p = self.plan(fns, dev=True)
        self.assertEqual([i for _, _, i in p.functions], ["ecm.net", "uac2.audio"])   # in interface order
        self.assertIn("dev is true or false", self.refused([{"kind": "acm", "dev": "yes"}]))

    def test_no_extras(self):
        fns = [{"kind": "ecm", "id": "net", "dev": True}, {"kind": "uac2", "id": "audio", "host_to_device": {}}]
        m = gadget_plan.complete(dict(self.BASE, functions=fns, extras=False))
        p = gadget_plan.build_plan(m, console=True, dev=True)
        self.assertEqual([i for _, _, i in p.functions], ["uac2.audio"])  # no console, no dev ECM
        self.assertNotIn("console functions/acm.console", p.ops)
        self.assertIn("extras = false: no debug console or dev functions", p.notes)
        self.assertNotIn("extras", gadget_plan.complete(dict(self.BASE, extras=True)))  # a capture's model, as before
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            gadget_plan.complete(dict(self.BASE, extras="no"))
        self.assertIn("extras is true or false", str(e.exception.code))

    def test_functions_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, os.path.join(REPO, "gadget_plan.py"), os.path.join(DATA, "kitchen.toml"),
                                  "-o", os.path.join(tmp, "p"), "--functions", os.path.join(tmp, "f"), "--console"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertIn("wrote", out)
            text, p = gadget_plan.plan_from_file(os.path.join(DATA, "kitchen.toml"), console=True)
            self.assertEqual(open(os.path.join(tmp, "f")).read(), gadget_plan.functions_text(p))

    def test_refusals(self):
        self.assertIn("unknown keys", self.refused([{"kind": "ecm", "mac": "x"}]))
        self.assertIn("kind is one of", self.refused([{"kind": "msc"}]))
        self.assertIn("MAC address", self.refused([{"kind": "ecm", "dev_addr": "02:00"}]))
        self.assertIn("%d", self.refused([{"kind": "ncm", "ifname": "usb0"}]))
        self.assertIn("different ids", self.refused([{"kind": "acm", "id": "a"}, {"kind": "acm", "id": "a"}]))
        self.assertIn("needs host_to_device", self.refused([{"kind": "uac2"}]))
        self.assertIn("report_desc", self.refused([{"kind": "hid"}]))


if __name__ == "__main__":
    unittest.main()
