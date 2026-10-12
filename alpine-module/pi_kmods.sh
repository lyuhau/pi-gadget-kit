#!/bin/sh
# Load the patched USB gadget modules (alpine-module/build.sh output) in place
# of Alpine's stock ones. Loading ours first means configfs never asks
# modprobe for the stock modules from modloop.
#
#   pi_kmods.sh load [DIR]   at boot, before any gadget exists
#   pi_kmods.sh swap [DIR]   at runtime: removes every gadget first
#   pi_kmods.sh status       which gadget modules are loaded, and whose
set -e
DIR=${2:-/media/mmcblk0p1/kmods}
CFS=/sys/kernel/config/usb_gadget
# dependency order: libcomposite first, functions last
MODS="libcomposite u_audio u_serial usb_f_acm usb_f_uac1 usb_f_hid"

stop_console_shell() {
	# A process holding ttyGS0 open makes removing the acm function hang.
	# (each step tolerates "already gone": under set -e a failed kill would
	# silently abort the whole swap)
	if [ -f /run/usb-console.pid ]; then
		kill "$(cat /run/usb-console.pid)" 2>/dev/null || true
		rm -f /run/usb-console.pid
	fi
	for t in /dev/ttyGS*; do
		[ -e "$t" ] && { fuser -k "$t" 2>/dev/null || true; }
	done
	return 0
}

teardown_all() {
	stop_console_shell
	for g in "$CFS"/*/; do
		[ -d "$g" ] || continue
		echo > "$g/UDC" 2>/dev/null || true
		for c in "$g"configs/*/; do
			[ -d "$c" ] || continue
			find "$c" -maxdepth 1 -type l -exec rm {} \;
			for s in "$c"strings/*/; do [ -d "$s" ] && rmdir "$s"; done
			rmdir "$c"
		done
		for d in "$g"functions/*/ "$g"strings/*/; do [ -d "$d" ] && rmdir "$d"; done
		rmdir "$g"
		echo "removed gadget $(basename "$g")"
	done
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
	swap) teardown_all; unload_all; load_all; status ;;
	status) status ;;
	*) echo "usage: $0 load|swap|status [DIR]" >&2; exit 2 ;;
esac
