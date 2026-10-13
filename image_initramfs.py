"""
The initramfs image type ([image] type = "initramfs"): Alpine's packages are
unpacked on the Mac into one initramfs, which the Pi's firmware loads with the
kernel. Nothing is installed at boot; image/init (PID 1) only starts things.

The card holds the boot firmware, the kernel and the initramfs. All of it
comes from the [alpine] release (pinned by its sha256) and from Alpine's
repositories:
  - packages: the release's own apks/ first, the configured repositories for
    the rest; a repository index is trusted only with a valid Alpine
    signature (keys from the release's alpine-keys), each package only if its
    hashes match the index
  - kernel modules and Wi-Fi firmware: the linux-rpi package of the release's
    kernel, linux-firmware-brcm/-cypress; the patched modules (KMODS) replace
    the stock ones
Downloads are cached in out/cache/apks-<version>-<arch>/.
"""
import base64
import gzip
import hashlib
import io
import lzma
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zlib

import build_card as bc

# What image/init needs besides [system] packages.
INIT_PACKAGES = ["alpine-baselayout", "busybox", "busybox-binsh", "dosfstools", "wpa_supplicant", "wireless-regdb",
                 "openssh-server"]
# Kernel modules in the image (with their dependencies): the boot modules, the
# gadget modules, and the Wi-Fi driver's vendor parts.
MODULES = bc.BOOT_MODULES + bc.KMODS + ["brcmfmac_cyw", "brcmfmac_wcc", "brcmfmac_bca"]
MODULE_DIRS = ["kernel/drivers/usb/gadget/function/"]
# Zero 2 W Wi-Fi firmware (both radio variants), from linux-firmware-*
FIRMWARE = re.compile(r"lib/firmware/(brcm/brcmfmac4343[06]|cypress/cyfmac43430)")
# Boot files for the Zero 2 W, from the release
BOOT_FILES = ["bootcode.bin", "start.elf", "fixup.dat", "bcm2710-rpi-zero-2-w.dtb",
              "overlays/dwc2.dtbo", "overlays/overlay_map.dtb", ".alpine-release"]
KERNEL, INITRD = "boot/vmlinuz-rpi", "boot/initrd.img"


# ---------------------------------------------------------------- apk files

def gzip_streams(data):
    """An apk is concatenated gzip streams (signature, control, data); an
    APKINDEX has the first two. -> [(compressed, decompressed)]"""
    out = []
    while data:
        d = zlib.decompressobj(31)
        plain = d.decompress(data) + d.flush()
        n = len(data) - len(d.unused_data)
        out.append((data[:n], plain))
        data = d.unused_data
    return out


def tar_members(plain):
    """(TarInfo, bytes or None) for each member of an uncompressed tar."""
    with tarfile.open(fileobj=io.BytesIO(plain)) as t:
        for m in t:
            yield m, t.extractfile(m).read() if m.isfile() else None


def rsa_key(pem):
    """(n, e) of a PEM RSA public key (SubjectPublicKeyInfo)."""
    der = base64.b64decode("".join(l for l in pem.decode().splitlines() if not l.startswith("-----")))

    def tlv(i):  # -> start, end of the value at i
        n = der[i + 1]
        i += 2
        if n & 0x80:
            k = n & 0x7F
            n, i = int.from_bytes(der[i:i + k], "big"), i + k
        return i, i + n

    i, _ = tlv(0)       # SubjectPublicKeyInfo
    _, i = tlv(i)       # skip the algorithm
    i, _ = tlv(i)       # BIT STRING
    i, _ = tlv(i + 1)   # RSAPublicKey (after the unused-bits byte)
    a, b = tlv(i)
    c, d = tlv(b)
    return int.from_bytes(der[a:b], "big"), int.from_bytes(der[c:d], "big")


DIGEST_INFO = {"sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
               "sha256": bytes.fromhex("3031300d060960864801650304020105000420")}


def verify_signed(streams, keys, what):
    """Check the signature stream of an APKINDEX against the stream after it
    (RSA PKCS#1 v1.5, as apk-tools does)."""
    signed = streams[1][0]
    for m, sig in tar_members(streams[0][1]):
        alg, _, name = m.name.partition(".SIGN.RSA256.")
        alg = "sha256" if name else "sha1"
        name = name or m.name.partition(".SIGN.RSA.")[2]
        if name not in keys:
            continue
        n, e = keys[name]
        k = (n.bit_length() + 7) // 8
        t = DIGEST_INFO[alg] + hashlib.new(alg, signed).digest()
        if pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big") == b"\0\1" + b"\xff" * (k - len(t) - 3) + b"\0" + t:
            return
    sys.exit(f"{what}: no valid Alpine signature")


def parse_index(plain):
    """APKINDEX text -> list of entries, one dict per package (single-letter keys)."""
    text = dict((m.name, d) for m, d in tar_members(plain))["APKINDEX"].decode()
    entries = []
    for block in text.strip().split("\n\n"):
        e = {}
        for line in block.splitlines():
            if len(line) > 2 and line[1] == ":":
                e[line[0]] = line[2:]
        entries.append(e)
    return entries


def download(url, path):
    print(f"downloading {url}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        urllib.request.urlretrieve(url, path + ".part")
    except OSError as err:
        sys.exit(f"{url}: {err}")
    os.replace(path + ".part", path)


class Repos:
    """The release's apks/ plus the configured repositories, as apk would see
    them, with every download checked."""

    def __init__(self, cfg, release, cache, refetch):
        a = cfg["alpine"]
        self.arch, self.cache = a["arch"], cache
        self.pkgs, self.provides = {}, {}
        local = os.path.join(release, "apks", a["arch"])
        # the release's index is covered by its tarball's sha256
        idx = gzip_streams(open(os.path.join(local, "APKINDEX.tar.gz"), "rb").read())
        self.add_index(parse_index(idx[1][1]), local)
        keys_apk = self.fetch("alpine-keys")[1]  # its etc/apk/keys: this arch's
        self.keys = {os.path.basename(m.name): rsa_key(d) for m, d in tar_members(keys_apk)
                     if d and m.name.startswith("etc/apk/keys/")}
        if refetch:
            shutil.rmtree(cache, ignore_errors=True)
        for repo in cfg["system"]["repositories"]:
            path = os.path.join(cache, "APKINDEX-" + "-".join(repo.rstrip("/").split("/")[-2:]) + ".tar.gz")
            if not os.path.exists(path):
                download(f"{repo}/{self.arch}/APKINDEX.tar.gz", path)
            idx = gzip_streams(open(path, "rb").read())
            verify_signed(idx, self.keys, path)
            self.add_index(parse_index(idx[1][1]), repo)

    def add_index(self, entries, source):
        for e in entries:
            e["source"] = source
            self.pkgs.setdefault(e["P"], e)  # earlier sources win: the release first
            for p in e.get("p", "").split():
                self.provides.setdefault(re.split(r"[<>=~]", p)[0], []).append(e["P"])

    def fetch(self, name):
        """-> (control tar, data tar) of a package, verified against its index."""
        e = self.pkgs[name]
        fname = f"{name}-{e['V']}.apk"
        if e["source"].startswith(("http:", "https:")):
            path = os.path.join(self.cache, fname)
            if not os.path.exists(path):
                download(f"{e['source']}/{self.arch}/{fname}", path)
        else:
            path = os.path.join(e["source"], fname)
        data = open(path, "rb").read()
        streams = gzip_streams(data)
        ok = len(streams) == 3 and \
            "Q1" + base64.b64encode(hashlib.sha1(streams[1][0]).digest()).decode() == e["C"]
        if ok:
            info = dict((m.name, d) for m, d in tar_members(streams[1][1]))[".PKGINFO"].decode()
            ok = f"datahash = {hashlib.sha256(streams[2][0]).hexdigest()}\n" in info
        if not ok:
            sys.exit(f"{path}: doesn't match its repository index (remove it, or --refetch)")
        return streams[1][1], streams[2][1], hashlib.sha256(data).hexdigest(), fname

    def pick(self, dep):
        """A package name for a dependency like 'so:libz.so.1' or 'busybox=1.37.0-r31'."""
        name, op, ver = re.match(r"([^<>=~]+)([<>=~]*)(.*)", dep).groups()
        if name in self.pkgs:
            pick = name
        elif name in self.provides:  # highest provider_priority, then index order
            pick = max(self.provides[name], key=lambda p: int(self.pkgs[p].get("k", 0)))
        else:
            sys.exit(f"no package provides {dep}")
        if op == "=" and self.pkgs[pick]["V"] != ver:
            sys.exit(f"{dep} wanted, but the repositories have {pick} {self.pkgs[pick]['V']}")
        return pick

    def resolve(self, wanted):
        """Packages to install for `wanted`, with dependencies and install_if
        (e.g. -openrc and -pyc subpackages), in a fixed order."""
        chosen, provided = [], set()
        stack = list(reversed(wanted))
        while True:
            while stack:
                dep = stack.pop()
                if dep.startswith("!"):
                    continue
                base = re.split(r"[<>=~]", dep)[0]
                if base in chosen or base in provided:
                    continue
                name = self.pick(dep)
                if name in chosen:
                    continue
                chosen.append(name)
                provided.update(re.split(r"[<>=~]", p)[0] for p in self.pkgs[name].get("p", "").split())
                stack.extend(reversed(self.pkgs[name].get("D", "").split()))
            have = set(chosen) | provided
            stack = [n for n, e in sorted(self.pkgs.items()) if n not in have and e.get("i") and
                     all(re.split(r"[<>=~]", c)[0] in have for c in e["i"].split())]
            if not stack:
                return chosen


# ---------------------------------------------------------------- root filesystem

class Tree:
    """The root filesystem: path -> [mode, data or link target, owner, group]."""

    def __init__(self):
        self.entries = {}

    def _parents(self, path):
        d = os.path.dirname(path)
        while d and d not in self.entries:
            self.entries[d] = [stat.S_IFDIR | 0o755, b"", "root", "root"]
            d = os.path.dirname(d)

    def dir(self, path, mode=0o755, owner="root", group="root"):
        path = path.strip("/")
        self._parents(path)
        if path not in self.entries or stat.S_ISDIR(self.entries[path][0]):
            self.entries[path] = [stat.S_IFDIR | mode, b"", owner, group]

    def file(self, path, data, mode=0o644, owner="root", group="root"):
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = [stat.S_IFREG | mode, data, owner, group]

    def symlink(self, path, target):
        path = path.strip("/")
        self._parents(path)
        self.entries[path] = [stat.S_IFLNK | 0o777, target.encode(), "root", "root"]

    def read(self, path):
        e = self.entries.get(path)
        return e[1].decode() if e else ""

    def add_tar(self, plain, select=None):
        hardlinks = []
        for m, data in tar_members(plain):
            name = m.name[2:] if m.name.startswith("./") else m.name
            if not name or name.startswith(".") or (select and not select(name)):
                continue  # .PKGINFO, .SIGN.*, install scripts
            perm = m.mode & 0o7777
            if m.isdir():
                self.dir(name, perm, m.uname, m.gname)
            elif m.issym():
                self.symlink(name, m.linkname)
            elif m.islnk():
                hardlinks.append((name, m.linkname))
            elif m.isfile():
                self.file(name, data, perm, m.uname, m.gname)
        for name, target in hardlinks:
            self.entries[name] = list(self.entries[target.lstrip("./")])

    def cpio(self):
        """Gzipped newc cpio, reproducible: sorted (directories before their
        contents), fixed mtimes, numeric owners from the tree's own passwd."""
        uids = {l.split(":")[0]: int(l.split(":")[2]) for l in self.read("etc/passwd").splitlines() if l}
        gids = {l.split(":")[0]: int(l.split(":")[2]) for l in self.read("etc/group").splitlines() if l}
        buf = io.BytesIO()

        def entry(ino, mode, uid, gid, nlink, data, name):
            name = name.encode() + b"\0"
            buf.write(("070701" + "".join(f"{v:08X}" for v in (
                ino, mode, uid, gid, nlink, bc.MTIME, len(data), 0, 0, 0, 0, len(name), 0))).encode() + name)
            buf.write(b"\0" * (-(110 + len(name)) % 4) + data + b"\0" * (-len(data) % 4))

        for ino, path in enumerate(sorted(self.entries), 1):
            mode, data, owner, group = self.entries[path]
            isdir = stat.S_ISDIR(mode)
            entry(ino, mode, uids.get(owner, 0), gids.get(group, 0), 2 if isdir else 1,
                  b"" if isdir else data, path)
        entry(0, 0, 0, 0, 1, b"", "TRAILER!!!")
        buf.write(b"\0" * (-buf.tell() % 512))
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write(buf.getvalue())
        return out.getvalue()


def add_users(tree, scripts):
    """The users and groups packages create in their install scripts (which
    aren't run): their addgroup/adduser lines, applied as busybox would."""
    def table(path):
        return [l.split(":") for l in tree.read(path).splitlines() if l]
    passwd, group, shadow = table("etc/passwd"), table("etc/group"), table("etc/shadow")

    def free(rows):
        used = {int(r[2]) for r in rows}
        return next(i for i in range(100, 1000) if i not in used)

    def add_group(name, gid=None):
        if not any(g[0] == name for g in group):
            group.append([name, "x", str(gid if gid is not None else free(group)), ""])

    for script in scripts:
        for line in script.splitlines():
            words = shlex.split(line, comments=True) if re.match(r"\s*(addgroup|adduser)\b", line) else []
            words = words[:next((i for i, w in enumerate(words) if re.match(r"[0-9]*>|\|\||&&|;", w)), None)]
            if not words:
                continue
            opts, args, i = {}, [], 1
            while i < len(words):
                w = words[i]
                if w in ("-g", "-G", "-h", "-s", "-u", "-k"):
                    opts[w], i = words[i + 1], i + 2
                else:
                    (args if not w.startswith("-") else opts.setdefault("flags", [])).append(w)
                    i += 1
            if words[0] == "addgroup":
                if len(args) == 1:
                    add_group(args[0], opts.get("-g"))
                elif len(args) == 2:  # addgroup USER GROUP
                    g = next((g for g in group if g[0] == args[1]), None)
                    if g and args[0] not in g[3].split(","):
                        g[3] = ",".join(filter(None, [g[3], args[0]]))
            elif len(args) == 1 and not any(p[0] == args[0] for p in passwd):
                name = args[0]
                uid = opts.get("-u") or str(free(passwd))
                gname = opts.get("-G", name)
                add_group(gname, None if "-G" in opts else uid)
                gid = next(g[2] for g in group if g[0] == gname)
                passwd.append([name, "x", uid, gid, opts.get("-g", ""), opts.get("-h", "/home/" + name),
                               opts.get("-s", "/bin/sh")])
                shadow.append([name, "!", "", "0", "", "", "", "", ""])

    tree.file("etc/passwd", "".join(":".join(r) + "\n" for r in passwd).encode())
    tree.file("etc/group", "".join(":".join(r) + "\n" for r in group).encode())
    tree.file("etc/shadow", "".join(":".join(r) + "\n" for r in shadow).encode(), 0o640, "root", "shadow")


def busybox_links(tree):
    """The applet links busybox's install script would make."""
    for n, target in (("busybox", "/bin/busybox"), ("busybox-extras", "/bin/busybox-extras")):
        for path in tree.read(f"etc/busybox-paths.d/{n}").split():
            if path not in tree.entries:
                tree.symlink(path, target)


# ---------------------------------------------------------------- kernel modules

def modinfo(ko):
    """key=value pairs from a module's .modinfo ELF section."""
    shoff, = struct.unpack_from("<Q", ko, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", ko, 0x3A)

    def section(i):
        name, _, _, _, off, size = struct.unpack_from("<IIQQQQ", ko, shoff + i * shentsize)
        return name, ko[off:off + size]

    names = section(shstrndx)[1]
    for i in range(shnum):
        name, data = section(i)
        if names[name:names.index(b"\0", name)] == b".modinfo":
            return [s.decode().partition("=")[::2] for s in data.split(b"\0") if b"=" in s]
    return []


def add_modules(tree, kernel_tar, release, patched):
    """The selected modules (uncompressed, so any modprobe loads them) with
    modules.dep/alias/softdep generated from their own modinfo."""
    base = f"lib/modules/{release}/"
    stock, extra = {}, {}
    for m, data in tar_members(kernel_tar):
        if m.name.startswith(base) and m.name.endswith(".ko.xz"):
            stock[os.path.basename(m.name)[:-6].replace("-", "_")] = (m.name[len(base):-3], data)
        elif m.name in (base + "modules.builtin", base + "modules.builtin.modinfo"):
            extra[m.name] = data
    wanted = MODULES + [n for n, (p, _) in stock.items() if p.startswith(tuple(MODULE_DIRS))]
    mods = {}  # name -> (path, bytes, info)
    while wanted:
        n = wanted.pop().replace("-", "_")
        if n in mods:
            continue
        if n not in stock:
            sys.exit(f"module {n} isn't in linux-rpi")
        path, data = stock[n]
        ko = patched.get(n) or lzma.decompress(data)
        mods[n] = (path, ko, modinfo(ko))
        wanted += [d for k, v in mods[n][2] if k == "depends" for d in v.split(",") if d]

    def deps(n, seen):  # dependencies, deepest last (as depmod writes them)
        for k, v in mods[n][2]:
            if k == "depends":
                for d in filter(None, v.split(",")):
                    d = d.replace("-", "_")
                    if d not in seen:
                        seen.append(d)
                        deps(d, seen)
        return seen

    dep, alias, softdep = [], [], []
    for n in sorted(mods):
        path, ko, info = mods[n]
        tree.file(base + path, ko)
        dep.append(f"{path}: " + " ".join(mods[d][0] for d in deps(n, [])))
        alias += [f"alias {v} {n}" for k, v in info if k == "alias"]
        softdep += [f"softdep {n} {v}" for k, v in info if k == "softdep"]
    for name, text in (("modules.dep", dep), ("modules.alias", alias), ("modules.softdep", softdep)):
        tree.file(base + name, "".join(l.rstrip() + "\n" for l in text).encode())
    for name, data in extra.items():
        tree.file(name, data)
    return len(mods)


# ---------------------------------------------------------------- the image

def ssh_host_key(host):
    """One ed25519 host key per hostname, made once and kept in out/, so the
    Pi keeps its identity across reinstalls and builds stay reproducible."""
    d = os.path.join(bc.OUT, f"ssh-host-key-{host}")
    key = os.path.join(d, "ssh_host_ed25519_key")
    if not os.path.exists(key):
        os.makedirs(d, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"root@{host}", "-f", key],
                       check=True)
    return open(key, "rb").read(), open(key + ".pub", "rb").read()


def build(cfg, tarball, card, files, refetch, lock_path):
    """Write the card: boot files, kernel, initramfs. `files` are the config's
    files for the Pi (path -> (mode, bytes))."""
    a, s = cfg["alpine"], cfg["system"]
    cache = os.path.join(bc.OUT, "cache", f"apks-{a['version']}-{a['arch']}")
    with tempfile.TemporaryDirectory(dir=bc.OUT) as release:
        bc.extract(tarball, release)
        rel = bc.kernel_release(release)  # e.g. 6.18.52-0-rpi
        print(f"alpine-rpi {a['version']} {a['arch']}, kernel {rel}")
        bc.step("kernel modules")
        kmods = bc.read_kmods(rel)
        for f in BOOT_FILES:
            os.makedirs(os.path.dirname(os.path.join(card, f)), exist_ok=True)
            shutil.copyfile(os.path.join(release, f), os.path.join(card, f))
        os.makedirs(os.path.join(card, "boot"))
        shutil.copyfile(os.path.join(release, "boot", "vmlinuz-rpi"), os.path.join(card, KERNEL))

        bc.step("packages")
        repos = Repos(cfg, release, cache, refetch)
        names = repos.resolve(INIT_PACKAGES + s["packages"])
        tree, scripts, lock = Tree(), [], []
        for n in names:
            control, data, digest, fname = repos.fetch(n)
            tree.add_tar(data)
            scripts += [d.decode() for m, d in tar_members(control) if m.name in (".pre-install", ".post-install")]
            lock.append(f"{digest}  {fname}")
        print(f"{len(names)} packages")

        bc.step("kernel and firmware")
        ver, pkgrel = rel.split("-")[:2]
        repos.pick(f"linux-rpi={ver}-r{pkgrel}")
        _, kernel, digest, fname = repos.fetch("linux-rpi")
        lock.append(f"{digest}  {fname}")
        vmlinuz = next(d for m, d in tar_members(kernel) if m.name == "boot/vmlinuz-rpi")
        if vmlinuz != open(os.path.join(card, KERNEL), "rb").read():
            sys.exit(f"linux-rpi {ver}-r{pkgrel} isn't the release's kernel")
        n = add_modules(tree, kernel, rel, kmods)
        for pkg in ("linux-firmware-brcm", "linux-firmware-cypress"):
            _, data, digest, fname = repos.fetch(pkg)
            lock.append(f"{digest}  {fname}")
            tree.add_tar(data, lambda p: bool(FIRMWARE.match(p)))
        fw = [p for p in tree.entries if FIRMWARE.match(p)]
        print(f"{n} modules, {len(fw)} Wi-Fi firmware files")

    bc.step("root filesystem")
    add_users(tree, scripts)
    busybox_links(tree)
    for d in ("proc", "sys", "dev", "run", "tmp", "media/mmcblk0p1"):
        tree.dir(d, 0o1777 if d == "tmp" else 0o755)
    tree.file("init", open(os.path.join(bc.REPO, "image", "init"), "rb").read(), 0o755)
    for p, (mode, data) in sorted(files.items()):
        d, missing = os.path.dirname(p), []
        while d and d not in tree.entries:
            missing.append(d)
            d = os.path.dirname(d)
        for d in reversed(missing):  # as in the diskless overlay
            tree.dir(d, 0o700 if d.startswith("root") else 0o755)
        tree.file(p, data, mode)
    if "openssh-server" in names:
        key, pub = ssh_host_key(s["hostname"])
        tree.file("etc/ssh/ssh_host_ed25519_key", key, 0o600)
        tree.file("etc/ssh/ssh_host_ed25519_key.pub", pub)
        tree.file("etc/ssh/sshd_config.d/10-host-key.conf", b"HostKey /etc/ssh/ssh_host_ed25519_key\n")
        blob = base64.b64decode(pub.split()[1])
        fp = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        print(f"ssh host key SHA256:{fp} (kept in out/ssh-host-key-{s['hostname']}/)")
    data = tree.cpio()
    with open(os.path.join(card, INITRD), "wb") as f:
        f.write(data)
    with open(os.path.join(card, "config.txt"), "w") as f:
        f.write("arm_64bit=1\n"
                f"kernel={KERNEL}\n"
                f"initramfs {INITRD} followkernel\n"
                "disable_splash=1\n" + "\n".join(bc.BOOT_CONFIG) + "\n")
    with open(os.path.join(card, "cmdline.txt"), "w") as f:
        f.write("console=tty1 rdinit=/init panic=5 quiet\n")
    print(f"{INITRD}: {len(tree.entries)} entries, {len(data) / 1e6:.1f} MB")

    old = open(lock_path).read() if os.path.exists(lock_path) else None
    text = "".join(l + "\n" for l in sorted(lock, key=lambda l: l[66:]))
    with open(lock_path, "w") as f:
        f.write("# packages unpacked into the initramfs (build_card.py)\n" + text)
    print(f"{len(lock)} packages; {os.path.relpath(lock_path, bc.REPO)} "
          + ("unchanged" if old and old.split("\n", 1)[1] == text else "updated: review the diff"))
