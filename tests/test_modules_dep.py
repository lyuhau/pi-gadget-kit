"""modules.dep order (image_initramfs.dep_order): modprobe loads a module's
dependencies from the last listed to the first, so each must come before
everything it depends on, as depmod writes them."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_initramfs import dep_order  # noqa: E402

# the gadget audio chain in linux-rpi 6.18, plus a shared dependency
DIRECT = {
    "usb_f_uac2": ["u_audio", "libcomposite"],
    "u_audio": ["snd_pcm", "libcomposite"],
    "snd_pcm": ["snd", "snd_timer"],
    "snd_timer": ["snd"],
    "snd": ["soundcore"],
    "soundcore": [],
    "libcomposite": ["udc_core"],
    "udc_core": [],
}


def closure(n):
    out = set()
    for d in DIRECT[n]:
        out |= {d} | closure(d)
    return out


class DepOrder(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(dep_order("snd_pcm", DIRECT), ["snd_timer", "snd", "soundcore"])
        self.assertEqual(dep_order("u_audio", DIRECT),
                         ["libcomposite", "udc_core", "snd_pcm", "snd_timer", "snd", "soundcore"])

    def test_every_module(self):
        for n in DIRECT:
            with self.subTest(n):
                order = dep_order(n, DIRECT)
                self.assertEqual(set(order), closure(n))  # all of them, once
                self.assertEqual(len(order), len(set(order)))
                for i, m in enumerate(order):  # loading last-to-first works
                    self.assertFalse(closure(m) & set(order[:i]), f"{m} listed after a dependency")


if __name__ == "__main__":
    unittest.main()
