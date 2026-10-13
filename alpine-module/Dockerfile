# Out-of-tree kernel module builds for the Pi's Alpine kernel (linux-rpi).
# Versions come from kernel.env via build.sh.
ARG ALPINE_BRANCH
FROM alpine:${ALPINE_BRANCH}
ARG KERNEL
ARG PKGREL
# linux-rpi-dev must match the Pi's kernel exactly. If the mirror has moved on,
# inside.sh installs the copy cached in the volume (/cache/apk) instead.
RUN apk add --no-cache bash build-base curl patch patchutils xz \
    && (apk add --no-cache linux-rpi-dev=${KERNEL}-${PKGREL} || true)
WORKDIR /build
