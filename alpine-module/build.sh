#!/bin/bash
# Build patched gadget modules for the Pi's Alpine kernel -> out/modules/*.ko
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build -q -t usb-gadget-kmod "$HERE" >/dev/null
docker volume create usb-gadget-kmod >/dev/null
exec docker run --rm -v usb-gadget-kmod:/cache -v "$(dirname "$HERE")":/repo usb-gadget-kmod \
    /repo/alpine-module/inside.sh
