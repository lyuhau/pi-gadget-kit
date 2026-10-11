#!/bin/bash
# Build the Buildroot image in Docker. Extra args go to make, e.g.:
#   buildroot/build.sh                    # full build -> out/usbpi-buildroot.img
#   buildroot/build.sh linux-rebuild all  # after changing kernel patches
#   RECONFIGURE=1 buildroot/build.sh      # after editing the defconfig
#   buildroot/build.sh linux-menuconfig   # interactive kernel config
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
python3 "$HERE/make_local_overlay.py"
docker build -q -t usbpi-buildroot "$HERE" >/dev/null
docker volume create usbpi-buildroot >/dev/null
TTY=; [ -t 0 ] && TTY=-it
exec docker run --rm $TTY -e RECONFIGURE="${RECONFIGURE:-0}" \
    -v usbpi-buildroot:/work -v "$REPO":/repo usbpi-buildroot \
    /repo/buildroot/inside.sh "$@"
