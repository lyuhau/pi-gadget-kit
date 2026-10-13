#!/bin/bash
# Runs inside the container. /work is a Docker volume (fast, case-sensitive);
# /repo is this repository.
set -euo pipefail
BR_VERSION=2026.08
BR=/work/buildroot-$BR_VERSION
OUT=/work/output
if [ ! -d "$BR" ]; then
    git clone --depth 1 --branch "$BR_VERSION" https://gitlab.com/buildroot.org/buildroot.git "$BR"
fi
export BR2_DL_DIR=/work/dl
if [ ! -f "$OUT/.config" ] || [ "${RECONFIGURE:-0}" = 1 ]; then
    make -C "$BR" O="$OUT" BR2_EXTERNAL=/repo/buildroot zero2w_usb_gadget_defconfig
fi
make -C "$BR" O="$OUT" "$@"
mkdir -p /repo/out
cp "$OUT/images/sdcard.img" /repo/out/usb-gadget-buildroot.img
ls -lh /repo/out/usb-gadget-buildroot.img
