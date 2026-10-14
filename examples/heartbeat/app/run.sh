#!/bin/sh
# heartbeat: counts boots in its state folder (on the card), says so in its
# log ($APP_LOG, kept on the card), and blinks the activity LED every
# heartbeat.blink seconds (the card's settings.txt; checking it is the app's job).
# Changing the device at runtime would be e.g.:
#   sh "$GADGET_SH" set audio c_srate=44100 p_srate=44100
n=$(( $(cat "$APP_STATE/boots" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$APP_STATE/boots"
sync
echo "$(date) boot $n, state in $APP_STATE, gadget $GADGET"
led=/sys/class/leds/ACT
[ -d $led ] || led=/sys/class/leds/led0
case "$APP_SETTING_BLINK" in
[1-9]|10) every=$APP_SETTING_BLINK ;;
*) every=1; echo "heartbeat.blink = $APP_SETTING_BLINK isn't 1 to 10; blinking every second" ;;
esac
echo none > $led/trigger
while :; do
	echo 1 > $led/brightness; sleep 0.1
	echo 0 > $led/brightness; sleep $((every - 1)).9
done
