#!/bin/sh
# Image slots on the card (initramfs images): each image is a folder (a/, b/, ...)
# and the card's config.txt says which one boots (os_prefix=SLOT, include
# SLOTconfig.txt). A new image goes in a slot that isn't running, boots once on
# trial (the firmware's tryboot: tryboot.txt instead of config.txt, for one boot),
# and stays only if it's committed: otherwise the next boot is the old slot again.
# /init runs `slot.sh guard` on a trial boot: no commit within TRIAL_SECONDS
# (app.env; default 300) reboots it. Who commits: the app (app.env TRIAL_COMMIT=app,
# when it knows it's healthy), or /init once the app has started.
#
#   slot.sh status          current=a/ trial=0|1 committed=0|1 slots="a/ b/"
#   slot.sh try SLOT        boot SLOT once, on trial (reboots now)
#   slot.sh commit          a trial boot: keep the running slot (else nothing to do)
#   slot.sh switch SLOT     boot SLOT from the next boot on, without a trial
#   slot.sh guard [SECONDS] (/init) a trial boot: reboot unless committed in time;
#                           any other boot: an old trial's tryboot.txt goes, and
#                           its slot goes in /run/usb-gadget/trial-failed (a commit
#                           removes tryboot.txt: one left means that trial wasn't kept)
CARD=/media/mmcblk0p1
DT=/proc/device-tree/chosen

die() { echo "slot.sh: $*" >&2; exit 1; }

current() { tr -d '\0' < $DT/os_prefix 2>/dev/null; }

trial() {   # the firmware's flag: 4 bytes, big-endian
    [ "$(od -An -tu1 $DT/bootloader/tryboot 2>/dev/null | awk '{print $NF}')" = 1 ]
}

booted() {  # the slot config.txt selects
    sed -n 's/^os_prefix=//p' $CARD/config.txt 2>/dev/null | head -n 1
}

slot_config() {
    printf '# The image in %s boots: its own settings are in %sconfig.txt\nos_prefix=%s\ninclude %sconfig.txt\n' \
        "$1" "$1" "$1" "$1"
}

check_slot() {
    case "$1" in */) ;; *) die "a slot is a folder name ending in /, e.g. b/" ;; esac
    case "$1" in */*/|/*|.*) die "a slot is a folder at the top of the card, e.g. b/" ;; esac
    [ -f "$CARD/$1config.txt" ] || die "no image in $1 (no ${1}config.txt)"
}

# write FILE: stdin to FILE, so a power cut leaves the old file or the new one (the
# card read-write meanwhile: it's mounted read-only for apps that keep nothing there)
write() {
    ro=; grep -q " $CARD vfat ro[,ro ]" /proc/mounts && { mount -o remount,rw $CARD || return 1; ro=1; }
    cat > "$1.new" && sync && mv "$1.new" "$1" && sync; s=$?
    [ -n "$ro" ] && mount -o remount,ro $CARD
    return $s
}

drop() {    # rm FILE, the same way
    [ -e "$1" ] || return 0
    ro=; grep -q " $CARD vfat ro[,ro ]" /proc/mounts && { mount -o remount,rw $CARD || return 1; ro=1; }
    rm -f "$1"; sync
    [ -n "$ro" ] && mount -o remount,ro $CARD
    return 0
}

tryboot() {
    # The firmware's one-boot flag goes with the reboot itself ("0 tryboot"), which
    # busybox's reboot can't pass: the reboot syscall with an argument.
    command -v python3 >/dev/null || die "trying a slot needs python3 on the Pi (the reboot syscall's argument)"
    case "$(uname -m)" in aarch64) nr=142 ;; arm*) nr=88 ;; *) die "unknown CPU $(uname -m)" ;; esac
    kill -TERM -1 2>/dev/null; sleep 1
    sync; mount -o remount,ro $CARD 2>/dev/null; sync
    exec python3 -c "import ctypes; ctypes.CDLL(None).syscall($nr, 0xfee1dead, 672274793, 0xA1B2C3D4, b'0 tryboot')"
}

case "$1" in
status)
    t=0; trial && t=1
    c=0; [ "$(booted)" = "$(current)" ] && c=1
    slots=$(cd $CARD && for d in */; do [ -f "${d}config.txt" ] && printf '%s ' "$d"; done)
    echo "current=$(current) trial=$t committed=$c slots=\"${slots% }\"" ;;
try)
    [ -n "$2" ] || die "usage: slot.sh try SLOT"
    check_slot "$2"
    [ "$2" != "$(current)" ] || die "$2 is running already"
    slot_config "$2" | write $CARD/tryboot.txt || die "can't write tryboot.txt"
    logger -t slot "trying $2 (from $(current))"
    tryboot ;;
commit)
    trial || { echo "not a trial boot: nothing to commit"; exit 0; }
    s=$(current)
    if [ "$(booted)" != "$s" ]; then
        slot_config "$s" | write $CARD/config.txt || die "can't write config.txt"
    fi
    drop $CARD/tryboot.txt
    logger -t slot "committed $s"
    echo "committed $s" ;;
switch)
    [ -n "$2" ] || die "usage: slot.sh switch SLOT"
    check_slot "$2"
    slot_config "$2" | write $CARD/config.txt || die "can't write config.txt"
    drop $CARD/tryboot.txt
    logger -t slot "switched to $2 (from the next boot)"
    echo "switched to $2 (from the next boot)" ;;
guard)
    if ! trial; then                    # a trial that came back: done with it
        failed=$(sed -n 's/^os_prefix=//p' $CARD/tryboot.txt 2>/dev/null | head -n 1)
        if [ -n "$failed" ]; then
            mkdir -p /run/usb-gadget && echo "$failed" > /run/usb-gadget/trial-failed
            logger -t slot "the trial of $failed wasn't kept: back on $(current)"
        fi
        drop $CARD/tryboot.txt
        exit 0
    fi
    sleep "${2:-300}"
    if [ "$(booted)" != "$(current)" ]; then
        logger -t slot "$(current) wasn't committed in ${2:-300}s: rebooting into $(booted)"
        reboot
    fi ;;
*)
    sed -n '2,/^CARD=/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
    exit 2 ;;
esac
