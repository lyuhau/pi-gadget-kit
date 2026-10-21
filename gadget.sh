#!/bin/sh
# Apply a gadget plan (made by gadget_plan.py) on the Pi. BusyBox sh.
#
#   gadget.sh up [PLAN]    create the gadget and attach it to the USB port,
#                          unbinding whatever gadget held it (e.g. the console)
#   gadget.sh use FLAVOR   plan that flavor and present it instead, live
#   gadget.sh down [NAME]  detach and remove it; rebind what it displaced
#   gadget.sh list         gadgets and the port they're bound to
#   gadget.sh remove NAME  remove a gadget without rebinding anything
#   gadget.sh set FUNCTION ATTR=VALUE...
#                          change attributes of the device that's up, e.g.
#                          "set audio c_srate=96000 p_srate=96000" (see below)
#   gadget.sh plan [PLAN]  print the plan as "up" applies it: with the card's settings
# Defaults: the device's plan (below) and its gadget, "device".
# A plan with a "console" operation ([device] console) gets the debug
# console's shell on that function's serial port.
#
# Flavors (README: Flavors), in images that have them (flavors.env) and
# python3: "up" without a PLAN plans the flavor in use (the last "use"), or
# else the card's setting "flavor" (default: the image's default flavor). A
# flavor is the card's flavors/NAME.toml (or .json), or else a built-in one
# (/etc/usb-gadget/flavors/). It's planned into /run/usb-gadget/device.plan
# and functions (the list apps read), with [device]'s options. Whatever
# fails (no such flavor, it doesn't plan, the kernel refuses it), the
# default flavor comes up instead, or else the image's prebuilt plan
# (/etc/usb-gadget/device.plan, the only one in images without flavors).
# Why: /var/log/flavor.log. A typo never leaves the Pi without a device.
#
# use: plans FLAVOR first (if it doesn't plan, the device stays as it is:
# exit 1), then replaces the device with it (the host sees an unplug and a
# new device) and loads the kernel modules it needs. Exit 0: FLAVOR is up;
# 3: the kernel refused it, the default flavor (or the prebuilt plan) is up
# instead; 1: no device is up. Not kept across reboots: that's the card's
# setting. Every use unbinds and binds the USB controller, which has wedged
# when rebound several times in a row at high rates: callers keep a gap
# between switches (gadget.sh doesn't).
#
# The card's settings (settings.txt; image/settings.sh) go on top of the plan:
# console = off leaves the console function (acm.console) out.
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
ETC=$R/etc/usb-gadget
PLAN=$ETC/device.plan       # prebuilt: the default flavor's
CONSOLE=$ETC/console.sh
RUN=$R/run/usb-gadget       # flavors: device.plan, functions, flavor (its name)
CARD_FLAVORS=$R/media/mmcblk0p1/flavors
PYTHON=$R/usr/bin/python3
PLANNER=$R/usr/lib/usb-gadget/gadget_plan.py
FLOG=$R/var/log/flavor.log
SETTING_CONSOLE= SETTING_FLAVOR= FLAVOR_DEFAULT= FLAVOR_OPTIONS=
[ -f "$ETC/settings.defaults" ] && . "$ETC/settings.defaults"
[ -f "$RUN/settings.env" ] && . "$RUN/settings.env"
[ -f "$ETC/flavors.env" ] && . "$ETC/flavors.env"

die() { echo "gadget.sh: $*" >&2; exit 1; }

# why a flavor didn't come up: to the caller and the log
log() {
	echo "gadget.sh: $*" >&2
	mkdir -p "$(dirname "$FLOG")"
	echo "$*" >> "$FLOG"
}


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
	S_CONSOLE=$SETTING_CONSOLE awk '
	ENVIRON["S_CONSOLE"] == "off" && $2 ~ /^functions\/acm\.console(\/|$)/ { next }
	{ print }' "$1"
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
	# the function modules (already loaded at boot, unless a flavor needs another)
	for k in $(sed -n 's,^link functions/\([a-z0-9]*\)\..*,\1,p' "$plan" | sort -u); do
		[ -d "$R/sys/module/usb_f_$k" ] || modprobe "usb_f_$k" || echo "note: couldn't load usb_f_$k"
	done
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

# ---- flavors

# flavors in use: the image has them, and python3 to plan them
flavors() { [ -n "$FLAVOR_DEFAULT" ] && [ -x "$PYTHON" ] && [ -f "$PLANNER" ]; }

# NAME -> its description: the card's, else the built-in one
find_flavor() {
	for f in "$CARD_FLAVORS/$1.toml" "$CARD_FLAVORS/$1.json" "$ETC/flavors/$1.toml" "$ETC/flavors/$1.json"; do
		[ -f "$f" ] && echo "$f" && return 0
	done
	return 1
}

# NAME -> $RUN/next.plan and next.functions; status 1, and why, if it can't
plan_flavor() {
	case "$1" in
	[a-z0-9]*) case "$1" in *[!a-z0-9_-]*) log "flavor '$1': not a flavor's name"; return 1 ;; esac ;;
	*) log "flavor '$1': not a flavor's name"; return 1 ;;
	esac
	f=$(find_flavor "$1") || { log "flavor $1: not on the card or built in"; return 1; }
	mkdir -p "$RUN"
	# shellcheck disable=SC2086  # options are words
	if ! out=$("$PYTHON" "$PLANNER" "$f" -o "$RUN/next.plan" --functions "$RUN/next.functions" \
			$FLAVOR_OPTIONS 2>&1); then
		log "flavor $1 ($f) doesn't plan: $(printf '%s\n' "$out" | tail -n 1)"
		return 1
	fi
	printf '%s\n' "$out" | sed -n "s/^  note: /flavor $1: note: /p"
}

# PLAN up, carrying on if it fails -> st (0: up)
present() {
	set +e
	(set -e; up "$1")
	st=$?
	set -e
}

# The planned flavor NAME up; if the kernel refuses it, the default flavor,
# then the prebuilt plan -> UP: what's up (a name, "prebuilt" or "")
start() {
	name=$1
	while :; do
		present "$RUN/next.plan"
		if [ "$st" = 0 ]; then
			mv "$RUN/next.plan" "$RUN/device.plan"
			mv "$RUN/next.functions" "$RUN/functions"
			echo "$name" > "$RUN/flavor"
			UP=$name
			return 0
		fi
		log "flavor $name: refused by the kernel"
		[ "$name" != "$FLAVOR_DEFAULT" ] || break
		name=$FLAVOR_DEFAULT
		log "the default flavor instead: $name"
		plan_flavor "$name" || break
	done
	prebuilt
}

prebuilt() {
	log "the prebuilt device instead ($PLAN)"
	mkdir -p "$RUN"
	present "$PLAN"
	UP=
	if [ "$st" != 0 ]; then
		log "that failed too: no device"
		down "$(sed -n 's/^name //p' "$PLAN")" >/dev/null 2>&1 || true  # the debug console back, if it was up
		return 0
	fi
	cp "$PLAN" "$RUN/device.plan"
	cp "$ETC/functions" "$RUN/functions"
	echo "$FLAVOR_DEFAULT" > "$RUN/flavor"
	UP=prebuilt
}

# "up" without a PLAN, with flavors: the flavor in use, or the card's choice
up_flavor() {
	want=$(cat "$RUN/flavor" 2>/dev/null) || want=
	[ -n "$want" ] || want=${SETTING_FLAVOR:-$FLAVOR_DEFAULT}
	if plan_flavor "$want"; then
		start "$want"
	elif [ "$want" != "$FLAVOR_DEFAULT" ] && log "the default flavor instead: $FLAVOR_DEFAULT" \
			&& plan_flavor "$FLAVOR_DEFAULT"; then
		start "$FLAVOR_DEFAULT"
	else
		prebuilt
	fi
	[ -n "$UP" ] || exit 1
}

use() {
	flavors || die "this image has no flavors to switch between (or no python3)"
	plan_flavor "$1" || exit 1
	start "$1"
	case "$UP" in
	"$1") echo "flavor $1 up" ;;
	"") exit 1 ;;
	*) echo "gadget.sh: flavor $1 refused; up instead: $UP" >&2; exit 3 ;;
	esac
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
up)
	if [ -n "$2" ]; then up "$2"
	elif flavors; then up_flavor
	else
		[ -z "$FLAVOR_DEFAULT" ] || log "no python3: the prebuilt device"
		up "$PLAN"
	fi ;;
use) [ -n "$2" ] || die "usage: gadget.sh use FLAVOR"; use "$2" ;;
down) down "${2:-device}" ;;
list)
	for g in "$CFS"/*/; do
		[ -d "$g" ] && echo "$(basename "$g"): $(cat "$g/UDC")"
	done ;;
remove) [ -n "$2" ] || die "usage: gadget.sh remove NAME"; remove "$2" ;;
set) [ -n "$2" ] || die "usage: gadget.sh set FUNCTION ATTR=VALUE..."; shift; set_attrs "$@" ;;
plan)
	p=${2:-$PLAN}
	[ -n "$2" ] || ! flavors || [ ! -f "$RUN/device.plan" ] || p=$RUN/device.plan
	[ -f "$p" ] || die "no plan $p"
	effective "$p" ;;
*) echo "usage: gadget.sh up [PLAN] | use FLAVOR | down [NAME] | list | remove NAME | set FUNCTION ATTR=VALUE..." \
	"| plan [PLAN]" >&2
	exit 2 ;;
esac
