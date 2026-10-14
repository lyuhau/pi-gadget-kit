#!/bin/sh
# Apply a gadget plan (made by gadget_plan.py on the Mac) on the Pi. BusyBox sh.
#
#   gadget.sh up [PLAN]    create the gadget and attach it to the USB port,
#                          unbinding whatever gadget held it (e.g. the console)
#   gadget.sh down [NAME]  detach and remove it; rebind what it displaced
#   gadget.sh list         gadgets and the port they're bound to
#   gadget.sh remove NAME  remove a gadget without rebinding anything
#   gadget.sh set FUNCTION ATTR=VALUE...
#                          change attributes of the device that's up, e.g.
#                          "set audio c_srate=96000 p_srate=96000" (see below)
#   gadget.sh plan [PLAN]  print the plan as "up" applies it: with the card's settings
# Defaults: the card's plan (/etc/usb-gadget/device.plan) and its gadget, "device".
# A plan with a "console" operation ([device] console) gets the debug
# console's shell on that function's serial port.
#
# The card's settings (settings.txt; image/settings.sh) go on top of the plan:
# usb.* replace the IDs and strings (written after the plan's, as "write?": a
# value the kernel refuses leaves the plan's), and console = off leaves the
# console function (acm.console) out.
#
# set: a function's attributes can only change while it's in no configuration.
# So: unbind (the host sees an unplug), unlink every function, write the
# attributes, link them all back in the plan's order (links are ordered:
# relinking one alone would move its interfaces to the end), bind again, and
# bring network interfaces up. FUNCTION is the function's id (audio) or its
# directory (uac2.audio). The device is relinked and rebound even when a write
# is refused; then set names it and exits 1.
set -e
R=${USB_TEST_ROOT:-}  # tests: a fake root
CFS=$R/sys/kernel/config/usb_gadget
DISPLACED=$R/run/gadget.displaced
RUNPLAN=$R/run/gadget.plan  # the plan of the device that's up (settings applied), for "set"
PLAN=$R/etc/usb-gadget/device.plan
CONSOLE=$R/etc/usb-gadget/console.sh
SETTING_CONSOLE= SETTING_USB_VENDOR_ID= SETTING_USB_PRODUCT_ID= SETTING_USB_MANUFACTURER= SETTING_USB_PRODUCT= SETTING_USB_SERIAL=
[ -f "$R/etc/usb-gadget/settings.defaults" ] && . "$R/etc/usb-gadget/settings.defaults"
[ -f "$R/run/usb-gadget/settings.env" ] && . "$R/run/usb-gadget/settings.env"

die() { echo "gadget.sh: $*" >&2; exit 1; }


# Bring network functions' interfaces up, right after every bind, before the
# host can send. dwc2 shares one receive FIFO between all OUT endpoints and,
# in DMA mode, only drains it into a queued request: a packet for a network
# function whose interface is down (no receive requests yet) sits at the head
# of the FIFO and stalls every OUT transfer behind it, control requests
# included. Audio, serial and network from the host all go dead.
net_up() {
	for f in "$1"/functions/*/; do
		[ -f "$f/ifname" ] || continue
		ifname=$(cat "$f/ifname")
		ip link set dev "$ifname" up || echo "note: couldn't bring $ifname up"
	done
}

# PLAN with the card's settings applied, on stdout
effective() {
	S_CONSOLE=$SETTING_CONSOLE S_VID=$SETTING_USB_VENDOR_ID S_PID=$SETTING_USB_PRODUCT_ID \
	S_MFR=$SETTING_USB_MANUFACTURER S_PRODUCT=$SETTING_USB_PRODUCT S_SERIAL=$SETTING_USB_SERIAL awk '
	BEGIN {
		n = split("idVendor S_VID idProduct S_PID strings/0x409/manufacturer S_MFR " \
			"strings/0x409/product S_PRODUCT strings/0x409/serialnumber S_SERIAL", a, " ")
		for (i = 1; i < n; i += 2) if (ENVIRON[a[i + 1]] != "") over[a[i]] = ENVIRON[a[i + 1]]
	}
	NR == FNR { if ($1 == "write") planned[$2] = 1; next }
	ENVIRON["S_CONSOLE"] == "off" && $2 ~ /^functions\/acm\.console(\/|$)/ { next }
	{ print }
	$1 == "write" && ($2 in over) { print "write? " $2 " " over[$2] }
	$1 == "mkdir" && $2 == "strings/0x409" {
		for (k in over) if (k ~ /^strings/ && !(k in planned)) print "write? " k " " over[k]
	}' "$1" "$1"
}

remove() {
	g="$CFS/$1"
	[ -d "$g" ] || return 0
	echo > "$g/UDC" 2>/dev/null || true
	# A process holding an acm function's serial port open (a debug console
	# shell) makes removing the function hang. Other gadgets' ports stay.
	for f in "$g"/functions/acm.*; do
		[ -d "$f" ] && sh "$CONSOLE" stop "ttyGS$(cat "$f/port_num")"
	done
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
	mkdir -p "$(dirname "$RUNPLAN")"
	effective "$plan" > "$RUNPLAN.new"
	plan=$RUNPLAN.new
	name=$(sed -n 's/^name //p' "$plan")
	[ -n "$name" ] || die "$plan has no 'name' line"
	modprobe libcomposite
	mountpoint -q "$R/sys/kernel/config" || mount -t configfs none /sys/kernel/config
	remove "$name"
	g="$CFS/$name"
	mkdir "$g"
	console=
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
		console) console=$path ;;
		*) die "unknown plan operation: $line" ;;
		esac
	done < "$plan"

	udc=$(ls "$R/sys/class/udc" | head -n 1)
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
	net_up "$g"
	mv "$plan" "$RUNPLAN"
	if [ -n "$console" ]; then
		tty=ttyGS$(cat "$g/$console/port_num")
		sh "$CONSOLE" start "$tty" 9>&-    # the shell must not hold the lock
		echo "debug console on $tty"
	fi
}

set_attrs() {
	fn=$1
	shift
	want=$fn
	[ -f "$RUNPLAN" ] || die "no device is up (gadget.sh up)"
	name=$(sed -n 's/^name //p' "$RUNPLAN")
	g="$CFS/$name"
	[ -d "$g/functions/$fn" ] || fn=$(cd "$g/functions" && ls -d -- *."$fn" 2>/dev/null | head -n 1)
	f="$g/functions/$fn"
	[ -n "$fn" ] && [ -d "$f" ] || die "gadget '$name' has no function $want"
	for a in "$@"; do
		case "$a" in
		?*=*) [ -f "$f/${a%%=*}" ] || die "$fn has no attribute ${a%%=*}" ;;
		*) die "usage: gadget.sh set FUNCTION ATTR=VALUE..." ;;
		esac
	done
	# From the unbind on, carry on whatever fails: the device must come back.
	set +e
	udc=$(cat "$g/UDC")
	[ -z "$udc" ] || echo > "$g/UDC"
	# a console shell holding an acm port makes unlinking it hang (see remove)
	ttys=
	for a in "$g"/functions/acm.*; do
		[ -d "$a" ] || continue
		t=ttyGS$(cat "$a/port_num")
		sh "$CONSOLE" stop "$t"; ttys="$ttys $t"
	done
	for c in "$g"/configs/*/; do
		find "$c" -maxdepth 1 -type l -exec rm {} \;
	done
	failed=
	for a in "$@"; do
		attr=${a%%=*}
		value=${a#*=}
		{ printf '%s\n' "$value" > "$f/$attr"; } 2>/dev/null || {
			echo "gadget.sh: $fn/$attr rejected $value" >&2
			failed=1
		}
	done
	while read -r op from to; do
		case "$op $to" in
		"link configs/"*) ln -s "$g/$from" "$g/$to" || failed=1 ;;
		esac
	done < "$RUNPLAN"
	if [ -n "$udc" ]; then
		# a moment between unbind and bind: dwc2 seemed to need it (audiolink's experience)
		sleep 0.2
		echo "$udc" > "$g/UDC" || failed=1
		net_up "$g"
	fi
	for t in $ttys; do sh "$CONSOLE" start "$t" 9>&-; done
	set -e
	[ -z "$failed" ] || exit 1
	echo "gadget '$name': $fn set${udc:+, rebound on $udc}"
}

down() {
	remove "$1"
	[ "$(sed -n 's/^name //p' "$RUNPLAN" 2>/dev/null)" = "$1" ] && rm -f "$RUNPLAN"
	echo "removed gadget '$1'"
	[ -f "$DISPLACED" ] || return 0
	while read -r o udc; do
		if echo "$udc" > "$CFS/$o/UDC" 2>/dev/null; then
			echo "rebound gadget '$o'"
		fi
	done < "$DISPLACED"
	rm -f "$DISPLACED"
}

# one change at a time (busybox flock, where built in)
if [ "$1" != list ] && [ "$1" != plan ] && command -v flock >/dev/null; then
	exec 9>"$R/run/gadget.lock"
	flock 9
fi

case "$1" in
up) up "${2:-$PLAN}" ;;
down) down "${2:-device}" ;;
list)
	for g in "$CFS"/*/; do
		[ -d "$g" ] && echo "$(basename "$g"): $(cat "$g/UDC")"
	done ;;
remove) [ -n "$2" ] || die "usage: gadget.sh remove NAME"; remove "$2" ;;
set) [ -n "$2" ] || die "usage: gadget.sh set FUNCTION ATTR=VALUE..."; shift; set_attrs "$@" ;;
plan) p=${2:-$PLAN}; [ -f "$p" ] || die "no plan $p"; effective "$p" ;;
*) echo "usage: gadget.sh up [PLAN] | down [NAME] | list | remove NAME | set FUNCTION ATTR=VALUE... | plan [PLAN]" >&2
	exit 2 ;;
esac
