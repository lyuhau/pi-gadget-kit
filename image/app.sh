#!/bin/sh
# The platform's side of an app ([app] in the image config). BusyBox sh.
# Started once the device is up and the card is mounted, before Wi-Fi: by
# image/init (initramfs) or /etc/local.d/90-app.start (diskless).
#
#   app.sh start       state folder, logs on the card, [app] kmods, then [app] run
#   app.sh save-logs   copy /var/log to the card's logs/last now
#   app.sh stop        save the logs, release the state folder (at shutdown)
#
# [app] run gets app.env's variables in its environment, and its output in
# $APP_LOG. It's started once, in the background, in its own session: keeping
# it running (restarting it) is the app's job.
set -a
. /etc/usb-gadget/app.env
set +a

# The card, writable. initramfs: init mounted it rw already; diskless: Alpine
# mounted it read-only. No card (booted over USB): nothing.
card_rw() {
	mountpoint -q "$CARD" || return 1
	grep -q " $CARD vfat rw[, ]" /proc/mounts || mount -o remount,rw "$CARD"
}

save_logs() {
	[ -n "$KEEP_LOGS" ] && [ -d "$APP_LOGS/last" ] || return 0
	for f in /var/log/*; do
		[ -f "$f" ] && cp "$f" "$APP_LOGS/last/"
	done
	sync
}

case "$1" in
start)
	mkdir -p "$APP_STATE"
	if [ -n "$STATE_ON_CARD$KEEP_LOGS" ] && card_rw; then
		if [ -n "$STATE_ON_CARD" ]; then
			mkdir -p "$CARD/$APP_NAME" && mount --bind "$CARD/$APP_NAME" "$APP_STATE"
		fi
		if [ -n "$KEEP_LOGS" ]; then
			# logs/last: this boot (refreshed every 10 min, and at shutdown); logs/prev: the one before
			rm -rf "$APP_LOGS/prev"
			[ -d "$APP_LOGS/last" ] && mv "$APP_LOGS/last" "$APP_LOGS/prev"
			mkdir -p "$APP_LOGS/last"
			setsid sh -c 'sleep 60; while :; do sh /etc/usb-gadget/app.sh save-logs; sleep 600; done' \
				</dev/null >/dev/null 2>&1 &
		fi
	fi
	for m in $APP_KMODS; do
		modprobe "$m" || echo "app.sh: couldn't load $m" >&2
	done
	if [ -n "$APP_RUN" ]; then
		setsid sh -c "$APP_RUN" </dev/null >>"$APP_LOG" 2>&1 &
		echo "app.sh: started $APP_NAME"
	fi
	;;
save-logs) save_logs ;;
stop)
	save_logs
	mountpoint -q "$APP_STATE" && umount "$APP_STATE"
	sync
	;;
*) echo "usage: app.sh start | save-logs | stop" >&2; exit 2 ;;
esac
exit 0
