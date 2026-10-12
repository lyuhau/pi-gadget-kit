# Out-of-tree kernel module builds for the Pi's Alpine kernel (linux-rpi).
FROM alpine:3.24
# linux-rpi-dev must match the Pi's kernel exactly. If the mirror has moved on,
# inside.sh installs the copy cached in the volume (/cache/apk) instead.
RUN apk add --no-cache bash build-base curl patch patchutils xz \
    && (apk add --no-cache linux-rpi-dev=6.18.52-r0 || true)
WORKDIR /build
