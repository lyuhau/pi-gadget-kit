"""image_initramfs.add_users: users and groups from package install scripts."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import image_initramfs as ii  # noqa: E402

AVAHI = """#!/bin/sh
addgroup -S avahi 2>/dev/null
adduser -S -D -h /var/lib/avahi -s /sbin/nologin \\
\t-G avahi -g "Avahi System User" avahi 2>/dev/null
exit 0
"""


class AddUsers(unittest.TestCase):
    def test_continued_lines(self):
        tree = ii.Tree()
        tree.file("etc/passwd", b"root:x:0:0:root:/root:/bin/sh\n")
        tree.file("etc/group", b"root:x:0:root\n")
        tree.file("etc/shadow", b"root:!::0:::::\n")
        ii.add_users(tree, [AVAHI])
        passwd = {l.split(":")[0]: l.split(":") for l in tree.read("etc/passwd").splitlines()}
        group = {l.split(":")[0]: l.split(":") for l in tree.read("etc/group").splitlines()}
        self.assertEqual(passwd["avahi"][3], group["avahi"][2])
        self.assertEqual(passwd["avahi"][4:], ["Avahi System User", "/var/lib/avahi", "/sbin/nologin"])
        self.assertIn("avahi:!:", tree.read("etc/shadow"))


if __name__ == "__main__":
    unittest.main()
