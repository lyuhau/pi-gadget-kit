# Build environment for Buildroot (arm64 Linux on Apple Silicon via Colima).
FROM debian:bookworm-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash bc binutils build-essential bzip2 ca-certificates cpio diffutils file findutils \
        git gzip libncurses-dev make patch perl python3 rsync sed tar unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/*
# Several packages' configure scripts refuse to run as root otherwise.
ENV FORCE_UNSAFE_CONFIGURE=1
WORKDIR /work
