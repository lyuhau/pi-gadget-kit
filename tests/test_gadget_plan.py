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
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, REPO)

import gadget_plan  # noqa: E402

# description, options, golden file stem
CASES = [
    ("capture.json", {}, "capture"),  # a usb-capture capture: UAC1, three HID, one unsupported
    ("capture.json", {"max_speed": "high-speed", "test_ids": True}, "capture-hs"),
    ("audiolink.toml", {"console": True}, "audiolink"),  # UAC2 + ECM + the debug console
    ("kitchen.toml", {"console": True}, "kitchen"),  # every hand-written setting
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
