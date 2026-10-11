#!/bin/bash
# Build patched gadget modules for the Pi's Alpine kernel -> out/modules/*.ko
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build -q -t usbpi-alpine-kmod "$HERE" >/dev/null
docker volume create usbpi-alpine-kmod >/dev/null
exec docker run --rm -v usbpi-alpine-kmod:/cache -v "$(dirname "$HERE")":/repo usbpi-alpine-kmod \
    /repo/alpine-module/inside.sh
