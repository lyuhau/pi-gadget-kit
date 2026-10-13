#!/bin/sh
# Apply a gadget plan (made by gadget_plan.py on the Mac) on the Pi. BusyBox sh.
#
#   gadget.sh up [PLAN]    create the gadget and attach it to the USB port,
#                          unbinding whatever gadget held it (e.g. the console)
#   gadget.sh down [NAME]  detach and remove it; rebind what it displaced
#   gadget.sh list         gadgets and the port they're bound to
#   gadget.sh remove NAME  remove a gadget without rebinding anything
# Defaults: the card's plan (/etc/usb-gadget/device.plan) and its gadget, "device".
set -e
CFS=/sys/kernel/config/usb_gadget
DISPLACED=/run/gadget.displaced
PLAN=/etc/usb-gadget/device.plan

die() { echo "gadget.sh: $*" >&2; exit 1; }

release_ttys() {
	# A process holding a gadget serial port open (the debug console's shell)
	# makes removing its acm function hang. Each step tolerates "already gone".
	if [ -f /run/usb-console.pid ]; then
		kill "$(cat /run/usb-console.pid)" 2>/dev/null || true
		rm -f /run/usb-console.pid
	fi
	for t in /dev/ttyGS*; do
		[ -e "$t" ] && { fuser -k "$t" >/dev/null 2>&1 || true; }
	done
	return 0
}

remove() {
	g="$CFS/$1"
	[ -d "$g" ] || return 0
	echo > "$g/UDC" 2>/dev/null || true
	for f in "$g"/functions/acm.*; do [ -d "$f" ] && release_ttys; done
	for c in "$g"/configs/*/; do
		[ -d "$c" ] || continue
		find "$c" -maxdepth 1 -type l -exec rm {} \;
		for s in "$c"strings/*/; do [ -d "$s" ] && rmdir "$s"; done
		rmdir "$c"
	done
	for d in "$g"/strings/*/*/; do [ -d "$d" ] && rmdir "$d"; done  # custom strings
	for d in "$g"/functions/*/ "$g"/strings/*/; do [ -d "$d" ] && rmdir "$d"; done
	rmdir "$g"
}

up() {
	plan=$1
	[ -f "$plan" ] || die "no plan $plan"
	name=$(sed -n 's/^name //p' "$plan")
	[ -n "$name" ] || die "$plan has no 'name' line"
	modprobe libcomposite
	mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
	remove "$name"
	g="$CFS/$name"
	mkdir "$g"
	while IFS= read -r line; do
		op=${line%% *}
		rest=${line#"$op"}; rest=${rest# }
		path=${rest%% *}
		value=${rest#"$path"}; value=${value# }
		case "$op" in
		""|"#"*|name) ;;
		mkdir) mkdir -p "$g/$path" ;;
		write) printf '%s\n' "$value" > "$g/$path" || die "write $path = $value failed" ;;
		"write?")
			if [ -e "$g/$path" ]; then
				printf '%s\n' "$value" > "$g/$path" 2>/dev/null || echo "note: $path rejected $value"
			else
				echo "note: kernel has no $path"
			fi ;;
		hex) printf '%s' "$value" | xxd -r -p > "$g/$path" || die "hex $path failed" ;;
		link) ln -s "$g/$path" "$g/$value" ;;
		*) die "unknown plan operation: $line" ;;
		esac
	done < "$plan"

	udc=$(ls /sys/class/udc | head -n 1)
	[ -n "$udc" ] || die "no USB device controller (is dwc2 loaded in peripheral mode?)"
	# One gadget per controller: unbind whichever holds it, remember it for "down".
	for other in "$CFS"/*/UDC; do
		o=$(basename "$(dirname "$other")")
		[ "$o" != "$name" ] && [ "$(cat "$other")" = "$udc" ] || continue
		echo > "$other"
		echo "$o $udc" >> "$DISPLACED"
		echo "unbound gadget '$o' (gadget.sh down $name rebinds it)"
	done
	echo "$udc" > "$g/UDC"
	echo "gadget '$name' up on $udc"
}

down() {
	remove "$1"
	echo "removed gadget '$1'"
	[ -f "$DISPLACED" ] || return 0
	while read -r o udc; do
		if echo "$udc" > "$CFS/$o/UDC" 2>/dev/null; then
			echo "rebound gadget '$o'"
		fi
	done < "$DISPLACED"
	rm -f "$DISPLACED"
}

case "$1" in
up) up "${2:-$PLAN}" ;;
down) down "${2:-device}" ;;
list)
	for g in "$CFS"/*/; do
		[ -d "$g" ] && echo "$(basename "$g"): $(cat "$g/UDC")"
	done ;;
remove) [ -n "$2" ] || die "usage: gadget.sh remove NAME"; remove "$2" ;;
*) echo "usage: gadget.sh up [PLAN] | down [NAME] | list | remove NAME" >&2; exit 2 ;;
esac
