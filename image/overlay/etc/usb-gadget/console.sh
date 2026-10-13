#!/bin/sh
# The debug console's root shell on a gadget serial port, for the console
# gadget (/etc/local.d/10-usb-console.start) and a device that includes it
# ([device] console, started by gadget.sh). The Mac sees /dev/cu.usbmodem*.
#
#   console.sh start ttyGSN   keep a shell on /dev/ttyGSN (respawned when it exits)
#   console.sh stop ttyGSN    end it: an open port blocks removing its acm function
tty=$2
pidfile=/run/usb-console-$tty.pid
case "$1" in
start)
	[ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null && exit 0
	# no login prompt (busybox login refuses root on ttyGS*); own session, so
	# it outlives an ssh session that started it
	setsid sh -c "echo \$\$ > $pidfile; while :; do /sbin/getty -n -l /bin/sh -L 115200 $tty vt100; sleep 1; done" \
		</dev/null >/dev/null 2>&1 &
	;;
stop)
	if [ -f "$pidfile" ]; then
		kill "$(cat "$pidfile")" 2>/dev/null
		rm -f "$pidfile"
	fi
	fuser -k "/dev/$tty" >/dev/null 2>&1
	;;
*) echo "usage: console.sh start|stop ttyGSN" >&2; exit 2 ;;
esac
exit 0
