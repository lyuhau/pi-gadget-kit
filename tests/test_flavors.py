"""
Flavors in an image config (README: Flavors): [device] flavors and default,
the build planning each one as a check, and what an initramfs image with
python3 carries to plan them on the Pi. The Pi's side (gadget.sh up/use) is
in test_settings.Flavors.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "tests", "data")
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, REPO)

import build_card as bc  # noqa: E402
from test_app import load, plan, validate  # noqa: E402


def flavored(**flavors):
    """heartbeat as an initramfs image with python3 and flavors: its own
    (the default), headset and clone from tests/data, and any given."""
    cfg = load()
    cfg["system"]["packages"].append("python3")
    del cfg["device"]["description"]
    cfg["device"]["flavors"] = {"heartbeat": "device.toml", "headset": os.path.join(DATA, "headset.toml"),
                                "clone": os.path.join(DATA, "clone.toml"), **flavors}
    cfg["device"]["default"] = "heartbeat"
    return cfg


def make_plan(cfg):
    with tempfile.TemporaryDirectory() as out, mock.patch.object(bc, "OUT", out), \
            contextlib.redirect_stdout(io.StringIO()) as log:
        return bc.make_plan(cfg), log.getvalue()


class Config(unittest.TestCase):
    def test_flavors(self):
        cfg = flavored()
        validate(cfg)
        names, default = bc.flavors(cfg)
        self.assertEqual(list(names), ["heartbeat", "headset", "clone"])
        self.assertEqual(default, "heartbeat")

    def test_description_is_one_flavor(self):
        cfg = load()
        self.assertEqual(bc.flavors(cfg), ({"device": "device.toml"}, "device"))
        cfg["device"]["description"] = "../Kitchen Pi.json"
        self.assertEqual(bc.flavors(cfg)[1], "kitchen-pi")

    def test_rejects(self):
        for change, what in [
            (lambda d: d.update(description="device.toml"), "or description"),
            (lambda d: d.pop("flavors"), "needs flavors"),
            (lambda d: d.update(default="nosuch"), "default is one of"),
            (lambda d: d.pop("default"), "default is one of"),
            (lambda d: d.update(flavors={"Big Name": "device.toml"}, default="Big Name"), "Big Name"),
            (lambda d: d.update(flavors={}), "flavors is a table"),
            (lambda d: d["flavors"].update(extra="missing.toml"), "missing.toml doesn't exist"),
            (lambda d: d["flavors"].update(extra="app/run.sh"), "is a .toml"),
        ]:
            with self.subTest(what):
                cfg = flavored()
                change(cfg["device"])
                with self.assertRaises(SystemExit) as e:
                    validate(cfg)
                self.assertIn(what, str(e.exception))
        cfg = load()
        cfg["device"]["default"] = "device"
        with self.assertRaises(SystemExit) as e:
            validate(cfg)
        self.assertIn("default goes with flavors", str(e.exception))
        cfg = flavored()
        cfg["device"] = {"flavors": {"one": "device.toml"}}  # a single flavor needs no default
        validate(cfg)


class Build(unittest.TestCase):
    def test_default_is_the_plan(self):
        (text, functions), log = make_plan(flavored())
        self.assertIn("from device.toml\n", text)
        self.assertIn("audio uac2 ", functions)
        self.assertIn("flavor headset: ", log)
        self.assertIn("default flavor: heartbeat", log)
        want = plan(flavored())
        self.assertEqual((text, functions), (want[0].replace(bc.rel("device.toml"), "device.toml"), want[1]))

    def test_a_flavor_that_doesnt_plan_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.toml")
            with open(bad, "w") as f:
                f.write("[device]\nidVendor = 0x1209\nidProduct = 1\n[[functions]]\nkind = \"msc\"\n")
            with self.assertRaises(SystemExit) as e:
                make_plan(flavored(bad=bad))
            self.assertIn("[device] flavor bad", str(e.exception))
            self.assertIn("kind is one of", str(e.exception))   # gadget_plan's message
            with open(bad, "w") as f:
                f.write("[device\n")
            with self.assertRaises(SystemExit) as e:
                make_plan(flavored(bad=bad))
            self.assertIn("[device] flavor bad", str(e.exception))

    def test_image_files(self):
        cfg = flavored()
        files = bc.config_files(cfg, plan(cfg))
        for name in ("heartbeat", "headset", "clone"):
            self.assertIn(f"etc/usb-gadget/flavors/{name}.toml", files)
        self.assertEqual(files["etc/usb-gadget/flavors/headset.toml"][1], bc.read(os.path.join(DATA, "headset.toml")))
        self.assertEqual(files["usr/lib/usb-gadget/gadget_plan.py"][1], bc.read(os.path.join(REPO, "gadget_plan.py")))
        self.assertEqual(files["etc/usb-gadget/flavors.env"][1].decode().splitlines()[1:],
                         ["FLAVOR_DEFAULT=heartbeat", "FLAVOR_OPTIONS=--console"])
        self.assertEqual(files["etc/usb-gadget/device.plan"][1].decode(), plan(cfg)[0])  # the last resort
        self.assertIn("GADGET_FUNCTIONS=/run/usb-gadget/functions\n", files["etc/usb-gadget/app.env"][1].decode())
        keys = files["etc/usb-gadget/settings.keys"][1].decode()
        self.assertIn("flavor flavor SETTING_FLAVOR\n", keys)
        self.assertNotIn("usb.", keys)

    def test_options(self):
        dev = {"max_speed": "full-speed", "test_ids": True, "console": True, "dev_functions": True}
        self.assertEqual(bc.plan_flags(dev), ["--max-speed full-speed", "--test-ids", "--console", "--dev"])
        self.assertEqual(bc.plan_flags({"max_speed": ""}), [])

    def test_only_initramfs_with_python3(self):
        for change in (lambda c: c["system"]["packages"].remove("python3"),
                       lambda c: c.update(image={"type": "diskless"})):
            cfg = flavored()
            change(cfg)
            files = bc.config_files(cfg, plan(cfg))
            self.assertFalse([p for p in files if "flavor" in p or p.startswith("usr/lib/usb-gadget")])
            self.assertIn("GADGET_FUNCTIONS=/etc/usb-gadget/functions\n", files["etc/usb-gadget/app.env"][1].decode())
            self.assertNotIn("flavor", bc.settings_text(cfg, plan(cfg)))

    def test_settings_text(self):
        cfg = flavored()
        text = bc.settings_text(cfg, plan(cfg))
        self.assertIn("built in, heartbeat, headset, clone; or\n", text)
        self.assertIn("\n#flavor = heartbeat\n", text)

    def test_install_keeps_the_cards_flavors(self):
        with tempfile.TemporaryDirectory() as tmp:
            card, target = os.path.join(tmp, "build"), os.path.join(tmp, "card")
            for path, text in ((os.path.join(card, "a/x"), "new"), (os.path.join(target, ".alpine-release"), ""),
                               (os.path.join(target, "flavors/mine.toml"), "mine")):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
            with contextlib.redirect_stdout(io.StringIO()):
                bc.install(card, target, bc.manifest(card))
            self.assertEqual(bc.read(os.path.join(target, "flavors/mine.toml")), b"mine")
        cfg = load()
        cfg["app"]["name"] = "flavors"
        with self.assertRaises(SystemExit):
            validate(cfg)


if __name__ == "__main__":
    unittest.main()
