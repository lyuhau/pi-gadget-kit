"""
capture_to_toml.py: a capture -> a hand-written description that plans the
same. tests/data/capture.toml is the golden conversion of capture.json
(UPDATE_GOLDEN=1 to regenerate, as in test_gadget_plan.py).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, REPO)

import capture_to_toml  # noqa: E402
import gadget_plan  # noqa: E402


def model():
    with open(os.path.join(DATA, "capture.json")) as f:
        return gadget_plan.pick(json.load(f)["devices"], None)["model"]


class Convert(unittest.TestCase):
    def test_golden(self):
        got = capture_to_toml.to_toml(model(), "capture.json")
        path = os.path.join(DATA, "capture.toml")
        if os.environ.get("UPDATE_GOLDEN"):
            with open(path, "w") as f:
                f.write(got)
        with open(path) as f:
            self.assertEqual(f.read(), got, "capture.toml differs (UPDATE_GOLDEN=1 to regenerate)")

    def test_plans_like_the_capture(self):
        m = model()
        desc = gadget_plan.tomllib.loads(capture_to_toml.to_toml(m, "capture.json"))
        self.assertEqual(gadget_plan.build_plan(gadget_plan.complete(desc)).ops, gadget_plan.build_plan(m).ops)

    def test_unknown_volume_step(self):
        # without --seize a capture knows the volume range but not the step
        m = model()
        m["functions"][0]["host_to_device"]["volume"]["res"] = None
        desc = gadget_plan.tomllib.loads(capture_to_toml.to_toml(m, "capture.json"))
        self.assertEqual(desc["functions"][0]["host_to_device"]["volume"], {"min": -19712, "max": 0})
        self.assertEqual(gadget_plan.build_plan(gadget_plan.complete(desc)).ops, gadget_plan.build_plan(m).ops)

    def test_extras(self):
        m = model()
        self.assertIs(gadget_plan.tomllib.loads(capture_to_toml.to_toml(m, "capture.json"))["extras"], False)
        self.assertNotIn("extras", gadget_plan.tomllib.loads(capture_to_toml.to_toml(m, "capture.json", extras=True)))
        # a clone stays exact in an image with a console: no acm.console, the capture's device class
        desc = gadget_plan.tomllib.loads(capture_to_toml.to_toml(m, "capture.json"))
        self.assertEqual(gadget_plan.build_plan(gadget_plan.complete(desc), console=True).ops,
                         gadget_plan.build_plan(m).ops)

    def test_slug(self):
        m = model()
        self.assertEqual(capture_to_toml.slug(m), "")
        m["strings"]["table"]["2"] = "  My Head-Set (v2) "
        self.assertEqual(capture_to_toml.slug(m), "my-head-set-v2")
        m["strings"]["table"]["2"] = "x" * 40
        self.assertEqual(capture_to_toml.slug(m), "x" * 32)

    def test_default_name(self):
        with open(os.path.join(DATA, "capture.json")) as f:
            data = json.load(f)
        with tempfile.TemporaryDirectory() as d:
            cap = os.path.join(d, "capture.json")
            with open(cap, "w") as f:
                json.dump(data, f)
            cmd = [sys.executable, os.path.join(REPO, "capture_to_toml.py"), cap]
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=d)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("-o", p.stderr)  # no product name
            self.assertFalse(os.path.exists(os.path.join(d, "device.toml")))
            gadget_plan.pick(data["devices"], None)["model"]["strings"]["table"]["2"] = "Test Phones"
            with open(cap, "w") as f:
                json.dump(data, f)
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=d)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(d, "test-phones.toml"), "rb") as f:
                self.assertIs(gadget_plan.tomllib.load(f)["extras"], False)
            p = subprocess.run(cmd + ["--extras", "--force"], capture_output=True, text=True, cwd=d)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(d, "test-phones.toml"), "rb") as f:
                self.assertNotIn("extras", gadget_plan.tomllib.load(f))

    def test_keeps_an_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "device.toml")
            with open(out, "w") as f:
                f.write("mine\n")
            cmd = [sys.executable, os.path.join(REPO, "capture_to_toml.py"), os.path.join(DATA, "capture.json"),
                   "-o", out]
            p = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("--force", p.stderr)
            with open(out) as f:
                self.assertEqual(f.read(), "mine\n")
            p = subprocess.run(cmd + ["--force"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
