#!/bin/sh
# Download the prebuilt patched modules for kernel.env's kernel -> out/modules/
# (instead of building them with build.sh, which needs Docker). They're the
# release modules-KERNEL-PKGREL of this repo, built by build.sh from patches/,
# and must match MODULES_SHA256 in kernel.env.
#   alpine-module/fetch.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT=$(dirname "$HERE")/out
. "$HERE/kernel.env"
NAME=gadget-modules-$KERNEL-$PKGREL.tar.gz
URL=${MODULES_URL:-https://github.com/lyuhau/pi-gadget-kit/releases/download/modules-$KERNEL-$PKGREL}/$NAME
mkdir -p "$OUT"
TMP=$(mktemp -d "$OUT/.fetch.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
echo "downloading $URL"
curl -fsSL -o "$TMP/$NAME" "$URL"
if command -v sha256sum >/dev/null; then SUM=$(sha256sum "$TMP/$NAME"); else SUM=$(shasum -a 256 "$TMP/$NAME"); fi
[ "${SUM%% *}" = "$MODULES_SHA256" ] || {
    echo "$NAME: sha256 ${SUM%% *}, but kernel.env's MODULES_SHA256 is $MODULES_SHA256" >&2; exit 1; }
mkdir "$TMP/modules"
tar -xzf "$TMP/$NAME" -C "$TMP/modules"
rm -rf "$OUT/modules"
mv "$TMP/modules" "$OUT/modules"
echo "out/modules/: $(ls "$OUT/modules" | wc -l | tr -d ' ') modules for linux-rpi $KERNEL-$PKGREL"
