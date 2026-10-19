#!/bin/sh
# Download the prebuilt patched modules for kernel.env's kernel -> out/modules/
# (instead of building them with build.sh, which needs Docker). They're the
# release modules-KERNEL-PKGREL of this repo, built by build.sh from patches/,
# and must match MODULES_SHA256 in kernel.env. Also the kernel package itself
# -> out/kernel/ (KERNEL_APK_SHA256), for when Alpine's repositories have moved on.
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
# The kernel package too (the build uses it if the repositories no longer have it)
APK=linux-rpi-$KERNEL-$PKGREL.apk
if [ -n "${KERNEL_APK_SHA256:-}" ]; then
    echo "downloading ${URL%/*}/$APK"
    curl -fsSL -o "$TMP/$APK" "${URL%/*}/$APK"
    if command -v sha256sum >/dev/null; then SUM=$(sha256sum "$TMP/$APK"); else SUM=$(shasum -a 256 "$TMP/$APK"); fi
    [ "${SUM%% *}" = "$KERNEL_APK_SHA256" ] || {
        echo "$APK: sha256 ${SUM%% *}, but kernel.env's KERNEL_APK_SHA256 is $KERNEL_APK_SHA256" >&2; exit 1; }
    mkdir -p "$OUT/kernel"
    mv "$TMP/$APK" "$OUT/kernel/$APK"
    echo "out/kernel/: $APK"
fi
