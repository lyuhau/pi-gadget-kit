"""
[app] in an image config (README: Apps), with examples/heartbeat: the files
both image types put on the Pi, the diskless overlay, the initramfs root
filesystem and config.txt. No downloads, no hardware.
"""
import contextlib
import copy
import gzip
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, "examples", "heartbeat")
sys.path.insert(0, REPO)

import build_card as bc  # noqa: E402
import gadget_plan  # noqa: E402
import image_initramfs  # noqa: E402


def load(name="pi.toml"):
    bc.CONFIG_DIR = EXAMPLE
    cfg = bc.load_config(os.path.join(EXAMPLE, name))
    cfg["access"]["ssh_public_key"] = ""  # no ~/.ssh needed here
    return cfg


def plan(cfg):
    text, p = gadget_plan.plan_from_file(bc.rel(cfg["device"]["description"]), console=cfg["device"].get("console"))
    return text, gadget_plan.functions_text(p)


def validate(cfg):
    with contextlib.redirect_stdout(io.StringIO()):
        bc.validate(cfg, "pi.toml")


def overlay_members(cfg):
    _, data, _ = bc.overlay(cfg, plan(cfg), False)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as t:
        return {m.name: (m, t.extractfile(m).read() if m.isfile() else m.linkname) for m in t}


class Config(unittest.TestCase):
    def test_include(self):
        dev, rel = load(), load("pi-release.toml")
        self.assertEqual(rel["app"], dev["app"])
        self.assertEqual(rel["system"], dev["system"])
        self.assertFalse(rel["access"]["debug_console"])
        self.assertFalse(rel["device"]["console"])
        self.assertTrue(rel["device"]["start_at_boot"])  # from pi.toml
        validate(dev)
        validate(rel)

    def test_names(self):
        out = bc.OUT
        self.assertEqual(bc.names("config/pi.toml", "h"),
                         ("", f"{out}/card-h", os.path.abspath("config/packages.lock")))
        self.assertEqual(bc.names("config/pi-release.toml", "h"),
                         ("-release", f"{out}/card-h-release", os.path.abspath("config/pi-release.packages.lock")))
        self.assertEqual(bc.names("/x/dev.toml", "h"), ("-dev", f"{out}/card-h-dev", "/x/dev.packages.lock"))

    def test_rejects(self):
        for change, what in [
            (lambda a, c: a.update(name="Bad Name"), "name"),
            (lambda a, c: a.update(name="logs"), "name"),
            (lambda a, c: a.update(name="boot"), "name"),
            (lambda a, c: a.update(owns=["sound"]), "owns"),
            (lambda a, c: (a.update(owns=["wifi"]), c["access"].update(wifi="pi.toml")), "owns wifi"),
            (lambda a, c: a.update(files={"x": "missing.sh"}), "files"),
            (lambda a, c: a.update(files={"../x": "app/run.sh"}), "files"),
            (lambda a, c: a.update(state="yes"), "state"),
            (lambda a, c: a.update(surprise=1), "unknown"),
        ]:
            with self.subTest(what):
                cfg = load()
                change(cfg["app"], cfg)
                with self.assertRaises(SystemExit) as e:
                    validate(cfg)
                self.assertIn(what, str(e.exception))

    def test_ssh_key_relative_to_config(self):
        cfg = load()
        cfg["access"]["ssh_public_key"] = "device.toml"  # any file next to the config
        cwd = os.getcwd()
        os.chdir("/")
        try:
            validate(cfg)
            files = bc.config_files(cfg, plan(cfg))
        finally:
            os.chdir(cwd)
        self.assertEqual(files["root/.ssh/authorized_keys"][1],
                         bc.read(os.path.join(EXAMPLE, "device.toml")).strip() + b"\n")
        cfg["access"]["ssh_public_key"] = "~/no-such-key.pub"
        with self.assertRaises(SystemExit) as e:
            validate(cfg)
        self.assertIn(os.path.expanduser("~/no-such-key.pub"), str(e.exception))

    def test_platform_files_are_the_platforms(self):
        cfg = load()
        cfg["app"]["files"] = {"etc/usb-gadget/gadget.sh": "app/run.sh"}
        with self.assertRaises(SystemExit):
            bc.config_files(cfg, plan(cfg))


class Files(unittest.TestCase):
    def test_app_files(self):
        cfg = load()
        files = bc.config_files(cfg, plan(cfg))
        mode, data = files["opt/heartbeat/run.sh"]
        self.assertEqual(mode, 0o755)
        self.assertEqual(data, bc.read(os.path.join(EXAMPLE, "app", "run.sh")))
        self.assertEqual(files["etc/usb-gadget/app.sh"][0], 0o755)
        env = files["etc/usb-gadget/app.env"][1].decode()
        for line in ["APP_NAME=heartbeat", "APP_RUN=/opt/heartbeat/run.sh", "APP_STATE=/var/lib/heartbeat",
                     "CARD=/media/mmcblk0p1", "APP_LOGS=/media/mmcblk0p1/logs", "STATE_ON_CARD=1", "KEEP_LOGS=1",
                     "GADGET=/sys/kernel/config/usb_gadget/device", "GADGET_SH=/etc/usb-gadget/gadget.sh"]:
            self.assertIn(line + "\n", env)
        self.assertNotIn("etc/wpa_supplicant/wpa_supplicant.conf", files)  # wifi = ""
        self.assertEqual(files["etc/network/interfaces"][1], b"auto lo\niface lo inet loopback\n")

    def test_env_quotes(self):
        cfg = load()
        cfg["app"]["run"] = "/opt/x/start.sh --name 'a b'"
        env = bc.app_env(cfg)
        out = subprocess.run(["sh", "-c", env + 'printf %s "$APP_RUN"'], capture_output=True, text=True).stdout
        self.assertEqual(out, cfg["app"]["run"])

    def test_overlay_dir(self):
        cfg = load()
        cfg["app"]["overlay"] = "app"
        cfg["app"]["files"] = {}
        self.assertIn("run.sh", bc.app_files(cfg))

    def test_no_app(self):
        cfg = load()
        del cfg["app"]
        files = bc.config_files(cfg, plan(cfg))
        self.assertFalse([p for p in files if "app" in os.path.basename(p) or p.startswith("opt/")])
        self.assertFalse([p for p in overlay_members(cfg) if "90-app" in p])
        self.assertEqual(bc.config_txt(cfg), bc.BOOT_CONFIG)

    def test_shell_syntax(self):
        for p in ("image/app.sh", "image/init", "image/settings.sh", "gadget.sh", "examples/heartbeat/app/run.sh"):
            with self.subTest(p):
                subprocess.run(["sh", "-n", os.path.join(REPO, p)], check=True)


class Install(unittest.TestCase):
    def test_keeps_state_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            card, target = os.path.join(tmp, "build"), os.path.join(tmp, "card")
            for d, files in ((card, {"boot/initrd.img": "new", "config.txt": "new"}),
                             (target, {".alpine-release": "", "boot/initrd.img": "old", "stale.txt": "old",
                                       "heartbeat/boots": "7", "logs/last/app.log": "log", "other/x": "old"})):
                for p, text in files.items():
                    os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True)
                    with open(os.path.join(d, p), "w") as f:
                        f.write(text)
            expected = bc.manifest(card)
            keep = bc.kept_on_card(load())
            self.assertEqual(keep, ["heartbeat", "logs"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                bc.install(card, target, expected, keep)
            self.assertIn("keeping the app's heartbeat/, logs/", out.getvalue())
            got = sorted(os.path.relpath(os.path.join(r, n), target) for r, _, ns in os.walk(target) for n in ns)
            self.assertEqual(got, ["boot/initrd.img", "config.txt", "heartbeat/boots", "logs/last/app.log"])
            self.assertEqual(bc.read(os.path.join(target, "heartbeat/boots")), b"7")
            self.assertEqual(bc.read(os.path.join(target, "boot/initrd.img")), b"new")

    def test_macos_metadata_files(self):
        # On a FAT card, macOS removes "._X" itself when X is removed
        with tempfile.TemporaryDirectory() as tmp:
            card, target = os.path.join(tmp, "build"), os.path.join(tmp, "card")
            for d, files in ((card, {"config.txt": "new"}),
                             (target, {".alpine-release": "", "._.alpine-release": "", "config.txt": "old",
                                       "._config.txt": "", "settings.txt": "mine", "._settings.txt": ""})):
                for p, text in files.items():
                    os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True)
                    with open(os.path.join(d, p), "w") as f:
                        f.write(text)
            remove = os.remove

            def macos_remove(path):
                remove(path)
                meta = os.path.join(os.path.dirname(path), "._" + os.path.basename(path))
                if os.path.exists(meta):
                    remove(meta)
            with mock.patch("os.remove", macos_remove), contextlib.redirect_stdout(io.StringIO()):
                bc.install(card, target, bc.manifest(card))
            self.assertEqual(sorted(os.listdir(target)), ["config.txt", "settings.txt"])
            self.assertEqual(bc.read(os.path.join(target, "settings.txt")), b"mine")

    def test_nothing_kept_without_state(self):
        cfg = load()
        cfg["app"].update(state=False, keep_logs=False)
        self.assertEqual(bc.kept_on_card(cfg), [])
        del cfg["app"]
        self.assertEqual(bc.kept_on_card(cfg), [])


class Diskless(unittest.TestCase):
    def test_overlay(self):
        cfg = load("pi-release.toml")
        m = overlay_members(cfg)
        self.assertEqual(m["opt/heartbeat/run.sh"][0].mode, 0o755)
        self.assertIn(b"app.sh start", m["etc/local.d/90-app.start"][1])
        self.assertIn(b"app.sh stop", m["etc/local.d/90-app.stop"][1])
        runlevels = {p.split("/")[-1] for p in m if p.startswith("etc/runlevels/") and m[p][0].issym()}
        self.assertEqual(runlevels, {"local", "networking", "chronyd"})  # no Wi-Fi, no SSH key
        self.assertNotIn("root/.ssh/authorized_keys", m)
        self.assertIn("SETTING_CONSOLE=off\n", m["etc/usb-gadget/settings.defaults"][1].decode())  # still on the card

    def test_owns_time(self):
        cfg = load()
        cfg["app"]["owns"] = ["wifi", "time"]
        self.assertNotIn("chronyd", bc.services(cfg)["default"])
        self.assertIn("APP_OWNS='wifi time'\n", bc.app_env(cfg))

    def test_usercfg(self):
        self.assertEqual(bc.config_txt(load()), bc.BOOT_CONFIG + ["dtparam=act_led_trigger=none"])


class Initramfs(unittest.TestCase):
    def test_root_filesystem(self):
        cfg = load()
        tree = image_initramfs.Tree()
        tree.dir("opt", 0o755)
        image_initramfs.add_files(tree, bc.config_files(cfg, plan(cfg)))
        mode, data, _, _ = tree.entries["opt/heartbeat/run.sh"]
        self.assertEqual(mode & 0o7777, 0o755)
        self.assertIn("opt/heartbeat", tree.entries)
        self.assertIn("etc/usb-gadget/app.env", tree.entries)

    def test_config_txt(self):
        text = image_initramfs.config_txt(load())
        self.assertTrue(text.endswith("dtoverlay=dwc2,dr_mode=peripheral\ndtparam=act_led_trigger=none\n"))
        cfg = copy.deepcopy(load())
        del cfg["app"]
        self.assertTrue(image_initramfs.config_txt(cfg).endswith("dr_mode=peripheral\n"))


if __name__ == "__main__":
    unittest.main()
