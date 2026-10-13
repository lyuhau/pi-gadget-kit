#!/bin/bash
# Build patched gadget modules for the Pi's Alpine kernel -> out/modules/*.ko
# The kernel version and source checksums are in kernel.env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/kernel.env"; set +a
docker build -q -t usb-gadget-kmod --build-arg ALPINE_BRANCH --build-arg KERNEL --build-arg PKGREL \
    "$HERE" >/dev/null
docker volume create usb-gadget-kmod >/dev/null
exec docker run --rm --env-file "$HERE/kernel.env" -v usb-gadget-kmod:/cache -v "$(dirname "$HERE")":/repo \
    usb-gadget-kmod /repo/alpine-module/inside.sh
