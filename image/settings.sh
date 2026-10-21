#!/bin/sh
# Card settings (README: Card settings): settings.txt on the card, which anyone
# can edit on any computer. BusyBox sh.
#
#   settings.sh load [FILE]  the card's settings.txt -> /run/usb-gadget/settings.env
#   settings.sh boot         load, then apply hostname and ssh_authorized_keys
#                            (/etc/local.d/05-settings.start, before the device)
#
# Readers source settings.defaults (the build's values), then settings.env if
# it's there: only the card's valid values, as VAR='value'. The keys, their
# types and variables are in settings.keys (build_card.py). Unknown keys and
# bad values are logged to /var/log/settings.log and ignored: a typo must
# never keep the Pi from booting or presenting its device.
R=${USB_TEST_ROOT:-}  # tests: a fake root
ETC=$R/etc/usb-gadget
OUT=$R/run/usb-gadget/settings.env
CARD=$R/media/mmcblk0p1
LOG=$R/var/log/settings.log

say() { echo "settings: $*"; }

# TYPE VALUE -> the value to use (normalised), or status 1
check() {
	case "$1" in
	hostname) printf '%s' "$2" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?' && printf '%s' "$2" ;;
	flavor) printf '%s' "$2" | grep -Eqx '[a-z0-9][a-z0-9_-]*' && printf '%s' "$2" ;;  # README: Flavors
	onoff)
		case "$2" in
		on|yes|true|1) echo on ;;
		off|no|false|0) echo off ;;
		*) return 1 ;;
		esac ;;
	text) ! printf '%s' "$2" | grep -q '[[:cntrl:]]' && printf '%s' "$2" ;;
	*) return 1 ;;
	esac
}

hint() {
	case "$1" in
	hostname) echo "letters, digits and -" ;;
	flavor) echo "a flavor's name: lowercase letters, digits, - and _" ;;
	onoff) echo "on or off" ;;
	*) echo "one line of text" ;;
	esac
}

load() {
	file=${1:-$CARD/settings.txt}
	mkdir -p "$(dirname "$OUT")"
	: > "$OUT.tmp"
	if [ ! -f "$file" ]; then
		say "no $file: built-in values"
	else
		bom=$(printf '\357\273\277')
		n=0
		# CR: files saved on Windows; the loop reads its own copy of the file
		tr -d '\r' < "$file" > "$OUT.in"
		while IFS= read -r line || [ -n "$line" ]; do
			n=$((n + 1))
			[ $n = 1 ] && line=${line#"$bom"}
			line=$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
			case "$line" in ""|"#"*) continue ;; esac
			case "$line" in
			*=*) ;;
			*) say "line $n: not key = value: $line"; continue ;;
			esac
			key=$(printf '%s' "${line%%=*}" | sed 's/[[:space:]]*$//')
			value=$(printf '%s' "${line#*=}" | sed 's/^[[:space:]]*//')
			case "$value" in \"*\") value=${value#\"}; value=${value%\"} ;; esac
			def=$(awk -v k="$key" '$1 == k { print $2, $3; exit }' "$ETC/settings.keys")
			if [ -z "$def" ]; then
				case "$key" in
				usb.vendor_id|usb.product_id|usb.manufacturer|usb.product|usb.serial)
					say "line $n: $key is no longer a setting: the flavor names the device; ignored" ;;
				*) say "line $n: unknown key $key, ignored" ;;
				esac
				continue
			fi
			[ -n "$value" ] || continue  # empty: the built-in value
			type=${def% *} var=${def#* }
			if v=$(check "$type" "$value"); then
				printf "%s='%s'\n" "$var" "$(printf '%s' "$v" | sed "s/'/'\\\\''/g")" >> "$OUT.tmp"
				say "$key = $v"
			else
				say "line $n: $key = $value isn't $(hint "$type"); using the built-in value"
			fi
		done < "$OUT.in"
		rm -f "$OUT.in"
	fi
	mv "$OUT.tmp" "$OUT"
}

# hostname, and ssh_authorized_keys added to root's keys (if the image has sshd)
apply() {
	. "$ETC/settings.defaults"
	. "$OUT"
	if [ -n "$SETTING_HOSTNAME" ] && [ "$SETTING_HOSTNAME" != "$(cat "$R/etc/hostname" 2>/dev/null)" ]; then
		echo "$SETTING_HOSTNAME" > "$R/etc/hostname"
		printf '127.0.0.1\tlocalhost %s\n::1\t\tlocalhost %s\n' "$SETTING_HOSTNAME" "$SETTING_HOSTNAME" > "$R/etc/hosts"
		[ -n "$R" ] || hostname "$SETTING_HOSTNAME"
		say "hostname $SETTING_HOSTNAME"
	fi
	keys=$CARD/ssh_authorized_keys
	[ -f "$keys" ] || return 0
	if [ ! -x "$R/usr/sbin/sshd" ]; then
		say "$keys: this image has no SSH server; ignored"
		return 0
	fi
	mkdir -p "$R/root/.ssh"
	chmod 700 "$R/root/.ssh"
	tr -d '\r' < "$keys" | while IFS= read -r k || [ -n "$k" ]; do
		case "$k" in
		""|"#"*) ;;
		ssh-*" "*|ecdsa-*" "*|sk-*" "*) printf '%s\n' "$k" >> "$R/root/.ssh/authorized_keys" ;;
		*) say "ssh_authorized_keys: a line that isn't a public key (ssh-..., ecdsa-..., sk-...), ignored" ;;
		esac
	done
	chmod 600 "$R/root/.ssh/authorized_keys" 2>/dev/null
	say "ssh keys from $keys added"
	# diskless: sshd is in a runlevel only if the image has a key of its own
	if [ -z "$R" ] && [ -x /etc/init.d/sshd ] && [ ! -e /etc/runlevels/default/sshd ]; then
		rc-update add sshd default
	fi
}

case "$1" in
load) load "$2" ;;
boot) mkdir -p "$(dirname "$LOG")"; { load; apply; } >> "$LOG" 2>&1 ;;
*) echo "usage: settings.sh load [FILE] | boot" >&2; exit 2 ;;
esac
exit 0
