#!/bin/sh
# Load the patched USB gadget modules (alpine-module/build.sh output) in place
# of Alpine's stock ones. Loading ours first means configfs never asks
# modprobe for the stock modules from modloop.
#
#   pi_kmods.sh load [DIR]   at boot, before any gadget exists
#   pi_kmods.sh swap [DIR]   at runtime: removes every gadget, reloads the
#                            modules, then brings the debug console back
#   pi_kmods.sh status       which gadget modules are loaded, and whose
set -e
HERE=$(dirname "$0")
DIR=${2:-/media/mmcblk0p1/kmods}
CFS=/sys/kernel/config/usb_gadget
# dependency order: libcomposite first, functions last (written by build_card.py)
MODS=$(cat "$HERE/kmods.list")

teardown_all() {
	for g in "$CFS"/*/; do
		[ -d "$g" ] || continue
		sh "$HERE/gadget.sh" remove "$(basename "$g")"
		echo "removed gadget $(basename "$g")"
	done
	rm -f /run/gadget.displaced
}

unload_all() {
	for m in $(echo $MODS | tr ' ' '\n' | sed -n '1!G;h;$p'); do  # reverse order
		if lsmod | grep -q "^$m "; then
			rmmod "$m"
		fi
	done
}

load_all() {
	for m in $MODS; do
		lsmod | grep -q "^$m " && continue
		if [ -f "$DIR/$m.ko" ]; then
			# insmod doesn't resolve dependencies; stock ones (snd_pcm...) via modprobe
			for d in $(modinfo -F depends "$DIR/$m.ko" | tr ',' ' '); do
				lsmod | grep -q "^$d " || modprobe "$d"
			done
			insmod "$DIR/$m.ko"
		else
			modprobe "$m"
		fi
	done
}

status() {
	for m in $MODS; do
		if [ -d "/sys/module/$m" ]; then
			t=$(cat "/sys/module/$m/taint" 2>/dev/null)
			case "$t" in *O*) who="patched";; *) who="stock";; esac
			echo "$m: $who"
		else
			echo "$m: not loaded"
		fi
	done
}

mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
case "$1" in
	load) load_all; status ;;
	swap)
		teardown_all; unload_all; load_all; status
		console=/etc/local.d/10-usb-console.start
		if [ -f "$console" ]; then sh "$console" && echo "debug console restored"; fi ;;
	status) status ;;
	*) echo "usage: $0 load|swap|status [DIR]" >&2; exit 2 ;;
esac
