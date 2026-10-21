"""
The card's settings (README: Card settings): settings.txt as the build writes
it, image/settings.sh parsing it (with the Mac's sh, in a fake root), gadget.sh
applying it to a fake configfs, and --install keeping it. Flavors on the Pi
(README: Flavors): gadget.sh planning the chosen one, falling back, and use.
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, REPO)

import build_card as bc  # noqa: E402
from test_app import load, plan, validate  # noqa: E402
from test_flavors import flavored  # noqa: E402


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    os.chmod(path, mode)


class Root:
    """A fake root with the image's files from the config (settings.sh,
    settings.keys/.defaults, gadget.sh, the plan, flavors), and stubs for what
    the Mac doesn't have. Its python3 is the tests' (with tomli)."""

    def __init__(self, cfg, tmp):
        self.cfg, self.root, self.plan = cfg, tmp, plan(cfg)
        for p, (mode, data) in bc.config_files(cfg, self.plan).items():
            write(os.path.join(tmp, p), data, mode)
        self.bin = os.path.join(tmp, "stub-bin")
        for cmd in ("mountpoint", "hostname", "rc-update"):
            write(os.path.join(self.bin, cmd), "#!/bin/sh\nexit 0\n", 0o755)
        write(os.path.join(self.bin, "modprobe"), '#!/bin/sh\necho "$*" >> "$USB_TEST_ROOT/modprobe.log"\n', 0o755)
        # configfs makes a directory's attributes with it; a function named
        # *.refuse is one the kernel refuses
        write(os.path.join(self.bin, "mkdir"), "#!/bin/sh\nfor d; do case $d in *.refuse) exit 1 ;; esac; done\n"
              "/bin/mkdir \"$@\"\nfor d; do case $d in\n"
              "*/strings/0x409) touch $d/manufacturer $d/product $d/serialnumber ;;\n"
              "*/functions/acm.*) echo 0 > $d/port_num ;;\nesac; done\n", 0o755)
        # configfs removes a directory's attributes with it
        write(os.path.join(self.bin, "rmdir"), '#!/bin/sh\nrm -rf "$@"\n', 0o755)
        write(self.path("usr/bin/python3"), f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', 0o755)
        write(self.path("etc/usb-gadget/console.sh"), "#!/bin/sh\necho console.sh $*\n", 0o755)
        os.makedirs(os.path.join(tmp, "sys/class/udc/3f980000.usb"))
        os.makedirs(os.path.join(tmp, "sys/kernel/config/usb_gadget"))
        os.makedirs(os.path.join(tmp, "var/log"))

    def path(self, p):
        return os.path.join(self.root, p)

    def card(self, text, name="settings.txt"):
        write(self.path("media/mmcblk0p1/" + name), text)

    def sh(self, *args, check=True):
        env = dict(os.environ, USB_TEST_ROOT=self.root, PATH=self.bin + ":" + os.environ["PATH"])
        p = subprocess.run(["sh", *args], env=env, capture_output=True, text=True)
        if check and p.returncode:
            raise AssertionError(f"{args}: {p.returncode}\n{p.stdout}{p.stderr}")
        return p

    def boot(self):
        self.sh(self.path("etc/usb-gadget/settings.sh"), "boot")
        return open(self.path("var/log/settings.log")).read()

    def values(self, *names):
        """The variables as readers see them: defaults, then the card's."""
        script = (f". {self.path('etc/usb-gadget/settings.defaults')}; "
                  f"[ -f {self.path('run/usb-gadget/settings.env')} ] && . {self.path('run/usb-gadget/settings.env')}; "
                  + "".join(f'printf "%s\\n" "${n}"; ' for n in names))
        return self.sh("-c", script).stdout.splitlines()


@contextlib.contextmanager
def root(cfg=None):
    with tempfile.TemporaryDirectory() as tmp:
        yield Root(cfg or load(), tmp)


VARS = ["SETTING_HOSTNAME", "SETTING_CONSOLE", "SETTING_FLAVOR", "APP_SETTING_BLINK"]


class Text(unittest.TestCase):
    def test_defaults_commented_out(self):
        cfg = load()
        text = bc.settings_text(cfg, plan(cfg))
        for line in ["#hostname = heartbeat", "#console = on", "# ---- heartbeat", "# Seconds between blinks: 1 to 10.",
                     "#heartbeat.blink = 1"]:
            self.assertIn(line + "\n", text)
        self.assertNotIn("usb.", text)  # a flavor owns the device's identity
        self.assertFalse([l for l in text.splitlines() if l and not l.startswith("#")])
        self.assertNotIn("ssh_authorized_keys", text)  # no sshd: ssh_public_key = "" in load()
        cfg["access"]["ssh_public_key"] = "device.toml"
        self.assertIn("ssh_authorized_keys", bc.settings_text(cfg, plan(cfg)))

    def test_as_built_changes_nothing(self):
        with root() as r:
            r.card(bc.settings_text(r.cfg, r.plan))
            self.assertEqual(r.boot(), "")
            self.assertEqual(r.values(*VARS), ["heartbeat", "on", "", "1"])

    def test_uncommented_defaults_are_valid(self):
        with root(flavored()) as r:
            text = bc.settings_text(r.cfg, r.plan)
            r.card(re.sub(r"^#([a-z][a-z.]*_?[a-z]* = )", r"\1", text, flags=re.M))
            log = r.boot()
            self.assertNotIn("line", log)
            self.assertEqual(r.values("SETTING_FLAVOR", "APP_SETTING_BLINK"), ["heartbeat", "1"])

    def test_no_device_no_flavor_key(self):
        cfg = flavored()
        del cfg["device"]
        self.assertNotIn("flavor", bc.settings_text(cfg, None))

    def test_app_settings_rejected(self):
        for settings, what in [({"Bad": "x"}, "Bad"), ({"x": 1}, "x"), ({"x": {"default": "a\nb"}}, "x"),
                               ({"x": {"other": "a"}}, "x"), ("x", "table")]:
            with self.subTest(what):
                cfg = load()
                cfg["app"]["settings"] = settings
                with self.assertRaises(SystemExit) as e:
                    validate(cfg)
                self.assertIn(what, str(e.exception))


class Parse(unittest.TestCase):
    def test_values(self):
        with root(flavored()) as r:
            r.card("﻿# a comment\r\n"
                   "  hostname   =   kitchen-pi  \r\n"
                   "console=no\r\n"
                   'flavor = "headset"\n'
                   "heartbeat.blink =\n"                              # empty: the built-in value
                   "heartbeat.blink = 3")                             # no newline at the end
            self.assertNotIn("line", r.boot())
            self.assertEqual(r.values(*VARS), ["kitchen-pi", "off", "headset", "3"])
            self.assertEqual(open(r.path("etc/hostname")).read(), "kitchen-pi\n")
            self.assertIn("localhost kitchen-pi\n", open(r.path("etc/hosts")).read())

    def test_mistakes_keep_the_defaults(self):
        with root(flavored()) as r:
            r.card("hostname = not a name\n"
                   "console = maybe\n"
                   "flavor = My Headset\n"
                   "flavor = ../etc/x\n"
                   "usb.colour = blue\n"
                   "just words\n"
                   "heartbeat.blink = 5\n")
            log = r.boot()
            for msg in ["line 1: hostname = not a name isn't letters, digits and -",
                        "line 2: console = maybe isn't on or off", "line 3: flavor = My Headset isn't a flavor's name",
                        "line 4: flavor = ../etc/x", "line 5: unknown key usb.colour", "line 6: not key = value"]:
                self.assertIn(msg, log)
            self.assertEqual(r.values(*VARS), ["heartbeat", "on", "heartbeat", "5"])
            self.assertEqual(open(r.path("etc/hostname")).read(), "heartbeat\n")

    def test_no_file(self):
        with root() as r:
            self.assertIn("built-in values", r.boot())
            self.assertEqual(r.values(*VARS), ["heartbeat", "on", "", "1"])

    def test_ssh_keys(self):
        with root() as r:
            r.card("ssh-ed25519 AAAAC3 me@mac\r\n# old one\nnot a key\n", "ssh_authorized_keys")
            self.assertIn("no SSH server", r.boot())
            self.assertFalse(os.path.exists(r.path("root/.ssh/authorized_keys")))
            write(r.path("usr/sbin/sshd"), "", 0o755)
            self.assertIn("isn't a public key", r.boot())
            self.assertEqual(open(r.path("root/.ssh/authorized_keys")).read(), "ssh-ed25519 AAAAC3 me@mac\n")

    def test_app_gets_its_settings(self):
        with root() as r:
            r.card("heartbeat.blink = 2 seconds; $(reboot)\n")
            r.boot()
            env = r.path("etc/usb-gadget/app.env")
            out = r.sh("-c", f"set -a; . {env}; . {r.path('etc/usb-gadget/settings.defaults')}; "
                       f". {r.path('run/usb-gadget/settings.env')}; set +a; sh -c 'echo \"$APP_SETTING_BLINK\"'")
            self.assertEqual(out.stdout, "2 seconds; $(reboot)\n")


class Gadget(unittest.TestCase):
    def up(self, r, settings):
        r.card(settings)
        r.boot()
        out = r.sh(r.path("etc/usb-gadget/gadget.sh"), "up")
        self.assertIn("gadget 'device' up on 3f980000.usb", out.stdout)
        g = r.path("sys/kernel/config/usb_gadget/device")
        return lambda p: open(os.path.join(g, p)).read() if os.path.isfile(os.path.join(g, p)) else None

    def test_usb_settings_are_gone(self):
        with root() as r:
            read = self.up(r, "usb.vendor_id = 0x1d6b\nusb.product = me\nusb.serial = 0042\n")
            log = open(r.path("var/log/settings.log")).read()
            self.assertIn("line 1: usb.vendor_id is no longer a setting: the flavor names the device; ignored", log)
            self.assertIn("line 3: usb.serial is no longer a setting", log)
            self.assertEqual(read("idVendor"), "0x1209\n")  # the plan's
            self.assertEqual(read("strings/0x409/product"), "heartbeat\n")
            self.assertEqual(read("strings/0x409/serialnumber"), "")
            self.assertTrue(os.path.islink(r.path("sys/kernel/config/usb_gadget/device/configs/c.1/acm.console")))

    def test_console_off(self):
        with root() as r:
            self.up(r, "console = off\n")
            g = r.path("sys/kernel/config/usb_gadget/device")
            self.assertFalse(os.path.exists(os.path.join(g, "functions/acm.console")))
            self.assertTrue(os.path.islink(os.path.join(g, "configs/c.1/uac2.audio")))
            self.assertNotIn("acm.console", open(r.path("run/gadget.plan")).read())  # what set relinks

    def test_plan_without_settings(self):
        with root() as r:
            out = r.sh(r.path("etc/usb-gadget/gadget.sh"), "plan").stdout
            self.assertEqual(out, r.plan[0])


class Flavors(unittest.TestCase):
    """gadget.sh up/use with flavors (test_flavors.flavored): heartbeat (the
    default), headset and clone built in."""
    G = "sys/kernel/config/usb_gadget/device"

    def gadget(self, r, *args, status=0):
        p = r.sh(r.path("etc/usb-gadget/gadget.sh"), *args, check=False)
        self.assertEqual(p.returncode, status, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def up(self, r, settings="", status=0):
        r.card(settings)
        r.boot()
        return self.gadget(r, "up", status=status)

    def read(self, r, p):
        p = r.path(os.path.join(self.G, p))
        return open(p).read() if os.path.isfile(p) else None

    def running(self, r):
        """(flavor, product string, the functions apps read)"""
        run = r.path("run/usb-gadget")
        return (open(os.path.join(run, "flavor")).read().strip(), self.read(r, "strings/0x409/product").strip(),
                [line.split()[0] for line in open(os.path.join(run, "functions")) if not line.startswith("#")])

    def log(self, r):
        p = r.path("var/log/flavor.log")
        return open(p).read() if os.path.exists(p) else ""

    def card_flavor(self, r, name, text):
        write(r.path(f"media/mmcblk0p1/flavors/{name}"), text)

    def test_default(self):
        with root(flavored()) as r:
            out = self.up(r)
            self.assertIn("gadget 'device' up", out)
            self.assertEqual(self.running(r), ("heartbeat", "heartbeat", ["audio", "console"]))
            plan_text = open(r.path("run/usb-gadget/device.plan")).read()
            self.assertIn("options: name=device max_speed=as-described test_ids=no console=yes\n", plan_text)
            self.assertEqual(self.log(r), "")
            self.assertIn("usb_f_uac2", open(r.path("modprobe.log")).read())   # the flavor's modules

    def test_card_setting(self):
        with root(flavored()) as r:
            self.up(r, "flavor = headset\n")
            self.assertEqual(self.running(r), ("headset", "headset", ["audio", "console"]))
            self.assertEqual(self.read(r, "functions/uac2.audio/p_chmask"), "1\n")

    def test_card_first(self):
        with root(flavored()) as r:
            headset = bc.read(os.path.join(r.root, "etc/usb-gadget/flavors/headset.toml")).decode()
            self.card_flavor(r, "headset.toml", headset.replace('product = "headset"', 'product = "my headset"'))
            self.card_flavor(r, "mine.toml", headset.replace('product = "headset"', 'product = "mine"'))
            self.up(r, "flavor = headset\n")
            self.assertEqual(self.running(r)[:2], ("headset", "my headset"))
            self.gadget(r, "use", "mine")
            self.assertEqual(self.running(r)[:2], ("mine", "mine"))

    def test_extras_false(self):
        with root(flavored()) as r:
            out = self.up(r, "flavor = clone\n")
            self.assertIn("flavor clone: note: extras = false", out)
            self.assertEqual(self.running(r), ("clone", "clone", ["audio"]))  # no console, whatever [device] says
            self.assertFalse(os.path.exists(r.path(os.path.join(self.G, "functions/acm.console"))))

    def test_missing_falls_back(self):
        with root(flavored()) as r:
            self.up(r, "flavor = nosuch\n")
            self.assertEqual(self.running(r)[0], "heartbeat")
            self.assertEqual(self.log(r), "flavor nosuch: not on the card or built in\n"
                                          "the default flavor instead: heartbeat\n")

    def test_doesnt_plan_falls_back(self):
        with root(flavored()) as r:
            self.card_flavor(r, "bad.toml", "[device]\nidVendor = 1\nidProduct = 2\ncolour = 3\n")
            self.up(r, "flavor = bad\n")
            self.assertEqual(self.running(r)[0], "heartbeat")
            self.assertIn("flavor bad (", self.log(r))
            self.assertIn("doesn't plan: ", self.log(r))
            self.assertIn("unknown keys colour", self.log(r))   # gadget_plan's message

    def test_kernel_refuses(self):
        with root(flavored()) as r:
            self.card_flavor(r, "refused.json", '{"device": {"idVendor": 1, "idProduct": 2}, '
                                                '"functions": [{"kind": "acm", "id": "refuse"}]}')
            self.up(r, "flavor = refused\n")
            self.assertEqual(self.running(r)[0], "heartbeat")
            self.assertIn("flavor refused: refused by the kernel\nthe default flavor instead: heartbeat\n", self.log(r))

    def test_prebuilt_last(self):
        with root(flavored()) as r:
            write(r.path("etc/usb-gadget/flavors/heartbeat.toml"), "broken")
            self.up(r, "flavor = nosuch\n")
            self.assertIn("the prebuilt device instead", self.log(r))
            self.assertEqual(self.running(r), ("heartbeat", "heartbeat", ["audio", "console"]))
            self.assertEqual(open(r.path("run/usb-gadget/device.plan")).read(), r.plan[0])
            # and nothing at all: status 1, the reason in the log
            write(r.path("etc/usb-gadget/device.plan"), "name device\nwrite nowhere/x 1\n")
            self.gadget(r, "down")
            os.remove(r.path("run/usb-gadget/flavor"))
            self.gadget(r, "up", status=1)
            self.assertIn("that failed too: no device", self.log(r))

    def test_use(self):
        with root(flavored()) as r:
            self.up(r)
            self.assertIn("flavor headset up", self.gadget(r, "use", "headset"))
            self.assertEqual(self.running(r)[:2], ("headset", "headset"))
            self.assertIn("audio", open(r.path("run/gadget.plan")).read())  # what set relinks
            # not a flavor: refused, and the device stays as it is
            self.assertIn("flavor nosuch: not on the card or built in", self.gadget(r, "use", "nosuch", status=1))
            self.assertIn("not a flavor's name", self.gadget(r, "use", "../x", status=1))
            self.assertEqual(self.running(r)[:2], ("headset", "headset"))
            # down and up again: the flavor in use
            self.gadget(r, "down")
            self.gadget(r, "up")
            self.assertEqual(self.running(r)[0], "headset")
            # refused by the kernel: the default comes up, status 3
            self.card_flavor(r, "refused.toml", '[device]\nidVendor = 1\nidProduct = 2\n'
                                                '[[functions]]\nkind = "acm"\nid = "refuse"\n')
            self.assertIn("flavor refused refused; up instead: heartbeat", self.gadget(r, "use", "refused", status=3))
            self.assertEqual(self.running(r)[0], "heartbeat")
            self.gadget(r, "use", status=1)  # usage

    def test_without_python3(self):
        with root(flavored()) as r:
            os.remove(r.path("usr/bin/python3"))
            self.up(r, "flavor = headset\n")
            self.assertIn("no python3: the prebuilt device", self.log(r))
            self.assertEqual(self.read(r, "strings/0x409/product"), "heartbeat\n")
            self.assertIn("no flavors", self.gadget(r, "use", "headset", status=1))

    def test_image_without_flavors(self):
        with root() as r:  # heartbeat as it is: no python3, so no flavors
            self.assertFalse(os.path.exists(r.path("etc/usb-gadget/flavors.env")))
            self.up(r)
            self.assertEqual(self.read(r, "strings/0x409/product"), "heartbeat\n")
            self.assertFalse(os.path.exists(r.path("run/usb-gadget/device.plan")))
            self.assertEqual(self.log(r), "")


class Install(unittest.TestCase):
    def test_keeps_users_settings(self):
        cfg = load()
        with tempfile.TemporaryDirectory() as tmp:
            card, target = os.path.join(tmp, "build"), os.path.join(tmp, "card")
            write(os.path.join(card, "settings.txt"), bc.settings_text(cfg, plan(cfg)))
            write(os.path.join(card, "boot/initrd.img"), "new")
            write(os.path.join(card, ".alpine-release"), "")
            mine = "# mine\nhostname = kitchen\n#console = on\n"
            for p, text in {".alpine-release": "", "settings.txt": mine, "ssh_authorized_keys": "ssh-ed25519 A",
                            "stale.txt": "old"}.items():
                write(os.path.join(target, p), text)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                bc.install(card, target, bc.manifest(card), (), lambda old: bc.merge_settings(old, cfg, plan(cfg)))
            self.assertIn("keeping the card's settings.txt, with the new heartbeat.blink added", out.getvalue())
            text = open(os.path.join(target, "settings.txt")).read()
            self.assertTrue(text.startswith(mine))
            self.assertIn("\n#heartbeat.blink = 1\n", text)
            self.assertNotIn("#hostname = heartbeat", text)
            self.assertEqual(sorted(os.listdir(target)), [".alpine-release", "boot", "settings.txt",
                                                          "ssh_authorized_keys"])
            # again: nothing new, the file stays as it is
            with contextlib.redirect_stdout(io.StringIO()) as out:
                bc.install(card, target, bc.manifest(card), (), lambda old: bc.merge_settings(old, cfg, plan(cfg)))
            self.assertIn("keeping the card's settings.txt\n", out.getvalue())
            self.assertEqual(open(os.path.join(target, "settings.txt")).read(), text)

    def test_new_card_gets_the_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            card, target = os.path.join(tmp, "build"), os.path.join(tmp, "card")
            write(os.path.join(card, "settings.txt"), "#hostname = x\n")
            os.makedirs(target)
            with contextlib.redirect_stdout(io.StringIO()):
                bc.install(card, target, bc.manifest(card))
            self.assertEqual(open(os.path.join(target, "settings.txt")).read(), "#hostname = x\n")


if __name__ == "__main__":
    unittest.main()
