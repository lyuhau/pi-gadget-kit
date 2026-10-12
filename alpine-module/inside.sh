#!/bin/bash
# Runs inside the container: fetch the exact source Alpine built linux-rpi
# 6.18.52 from (verified against the APKBUILD's sha512sums), apply our
# patches, and build modules against linux-rpi-dev.
set -euo pipefail
V=6.18.52 KV=6.18
KDIR=/usr/src/linux-headers-$V-0-rpi
SRC=/cache/src-$V
PATHS="drivers/usb/gadget drivers/usb/dwc2 include/uapi/linux/usb"

if [ ! -d "$SRC" ]; then
    cd /cache
    [ -f linux-$KV.tar.xz ] || curl -fsSLO https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KV.tar.xz
    [ -f patch-$V.patch.xz ] || curl -fsSL -o patch-$V.patch.xz https://cdn.kernel.org/pub/linux/kernel/v6.x/patch-$V.xz
    [ -f rpi-$V-alpine.patch ] || curl -fsSLO https://dev.alpinelinux.org/archive/rpi-patches/rpi-$V-alpine.patch
    sha512sum -c - <<SUMS
88599ffdec96d150c1feb9b261ba93bb0301a9d0e1ad6bef7aeab1f5372cbfc57d8b43c7e902bd8f76921d1dbd8189663c142ea869e51d0e2b483b150ee00fe0  linux-$KV.tar.xz
f1c32303a8eb7f20c7f5c8d592a23140ff880075d0e4151bf96d5b0365ff64b35324753894d8ba822ad797e673b2a878f18d6a2f557dd6d3aca1fcc8d4e5764d  patch-$V.patch.xz
c7eaa3995faf7717d77d262d26c45155a5becd476f86ea8ce4a576acabd444b536fb95614442ec9f28a7b8a898c68cfc4818721b39d06df6e59f674ee37d7d83  rpi-$V-alpine.patch
SUMS
    rm -rf tmp && mkdir tmp
    tar xJf linux-$KV.tar.xz -C tmp $(for p in $PATHS; do echo linux-$KV/$p; done)
    filt() { filterdiff -p1 -i 'drivers/usb/gadget/*' -i 'drivers/usb/dwc2/*' -i 'include/uapi/linux/usb/*'; }
    (cd tmp/linux-$KV && xzcat /cache/patch-$V.patch.xz | filt | patch -p1 -s && filt < /cache/rpi-$V-alpine.patch | patch -p1 -s)
    mv tmp/linux-$KV "$SRC" && rmdir tmp
fi

[ -d "$KDIR" ] || apk add -q --allow-untrusted /cache/apk/linux-rpi-dev-$V-r0.apk
mkdir -p /cache/apk
[ -f /cache/apk/linux-rpi-dev-$V-r0.apk ] || (cd /cache/apk && apk fetch -q linux-rpi-dev=$V-r0) || true

# Fresh copy each build so patches always apply to pristine source.
rm -rf /build/tree && mkdir -p /build/tree && cp -a "$SRC"/. /build/tree/
cd /build/tree
for p in /repo/alpine-module/patches/*.patch; do
    [ -e "$p" ] || continue
    echo "applying $(basename "$p")"
    patch -p1 -s < "$p"
done

# Build whole directories with the kernel's own Makefiles and the Pi kernel's
# config, so multi-file modules (libcomposite, dwc2) and their symbol
# versions come out right. raw_gadget isn't enabled in Alpine's config, so
# switch it on for this build.
J=$(nproc)
# The gadget Makefiles add -I$(srctree)/drivers/usb/gadget{,/function,/udc};
# srctree is the headers package, which doesn't ship those private headers.
# (The container is discarded after each run, so this edit doesn't stick.)
for d in drivers/usb/gadget drivers/usb/gadget/function drivers/usb/gadget/udc; do
    mkdir -p "$KDIR/$d" && cp /build/tree/$d/*.h "$KDIR/$d/"
done
make -s -j"$J" -C "$KDIR" M=/build/tree/drivers/usb/gadget CONFIG_USB_RAW_GADGET=m modules
make -s -j"$J" -C "$KDIR" M=/build/tree/drivers/usb/dwc2 modules

rm -rf /repo/out/modules && mkdir -p /repo/out/modules
find /build/tree -name '*.ko' -exec cp {} /repo/out/modules/ \;
ls /repo/out/modules | tr '\n' ' '; echo
m=/repo/out/modules/usb_f_uac1.ko
echo "vermagic: $(strings "$m" | grep -m1 '^vermagic=')"
