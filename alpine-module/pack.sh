#!/bin/sh
# Maintainers: package the patched modules build_card.py uses (its KMODS, from
# out/modules/, i.e. build.sh's output) for a GitHub release that fetch.sh
# downloads. Deterministic: the same modules give the same bytes.
#   alpine-module/pack.sh   # -> out/gadget-modules-KERNEL-PKGREL.tar.gz and its sha256
# Then: MODULES_SHA256 in kernel.env, and a release tagged modules-KERNEL-PKGREL
# with the tarball and SHA256SUMS (README: Changing the Alpine release).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO=$(dirname "$HERE")
. "$HERE/kernel.env"
NAME=gadget-modules-$KERNEL-$PKGREL.tar.gz
cd "$REPO"
python3 - "out/$NAME" <<'PY'
import gzip, io, os, re, sys, tarfile
src = open("build_card.py").read()
kmods = re.findall(r'"(\w+)"', re.search(r"^KMODS = \[(.*?)\]", src, re.S | re.M).group(1))
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
    for m in sorted(kmods):
        data = open(os.path.join("out", "modules", m + ".ko"), "rb").read()
        info = tarfile.TarInfo(m + ".ko")
        info.size, info.mode, info.mtime = len(data), 0o644, 946684800
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        t.addfile(info, io.BytesIO(data))
with open(sys.argv[1], "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as g:
    g.write(buf.getvalue())
print(f"{len(kmods)} modules")
PY
cd out
if command -v sha256sum >/dev/null; then sha256sum "$NAME"; else shasum -a 256 "$NAME"; fi
