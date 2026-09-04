#!/usr/bin/env bash
# readsb-watchdog -- fire readsb-recover when jeff's SDR has gone deaf.
#
# Run every 2 min by readsb-watchdog.timer (as root). Deployed to
# /usr/local/bin/readsb-watchdog (no extension). Detects the wedge signature
# (readsb up but no IQ samples / no messages / frozen feed), waits for a second
# consecutive deaf check so a restart blip is ridden out, honours a 15-min
# cooldown, then hands off to readsb-recover.
set -u

STATS=/run/readsb/stats.json
AIRCRAFT=/run/readsb/aircraft.json
TICK=/run/readsb-watchdog.tick          # consecutive-deaf counter (tmpfs, clears on boot)
STATE=/var/lib/readsb-watchdog/state.json
LOG=/var/log/readsb-watchdog.log
COOLDOWN=900                            # seconds between recovery attempts
MIN_MSGS_PER_MIN=600                    # ~10 msg/s; healthy jeff runs 6000-18000/min
STALE_SEC=120                           # aircraft.json older than this => readsb hung

log() { printf '%s watchdog %s\n' "$(date -Is)" "$*" >> "$LOG"; }

deaf=0; why=""
if ! systemctl is-active --quiet readsb; then
    deaf=1; why="readsb.service not active"
else
    samples=$(jq -r '.last1min.local.samples_processed // 0' "$STATS" 2>/dev/null || echo 0)
    msgs=$(jq -r '.last1min.messages // 0' "$STATS" 2>/dev/null || echo 0)
    if [ -f "$AIRCRAFT" ]; then
        age=$(( $(date +%s) - $(stat -c %Y "$AIRCRAFT") ))
    else
        age=999
    fi
    if ! [ "${samples:-0}" -gt 0 ] 2>/dev/null; then
        deaf=1; why="samples_processed=${samples:-0}"
    elif [ "${msgs:-0}" -lt "$MIN_MSGS_PER_MIN" ] 2>/dev/null; then
        deaf=1; why="messages=${msgs}/min (<${MIN_MSGS_PER_MIN})"
    elif [ "$age" -gt "$STALE_SEC" ]; then
        deaf=1; why="aircraft.json ${age}s stale"
    fi
fi

if [ "$deaf" -eq 0 ]; then
    [ -f "$TICK" ] && rm -f "$TICK"
    exit 0
fi

n=0; [ -f "$TICK" ] && n=$(cat "$TICK" 2>/dev/null || echo 0)
n=$((n + 1)); echo "$n" > "$TICK"
log "deaf ($why), consecutive=$n"

if [ "$n" -lt 2 ]; then
    log "waiting for a 2nd consecutive deaf check before acting"
    exit 0
fi

if [ -f "$STATE" ]; then
    last=$(jq -r '.last_fire // 0' "$STATE" 2>/dev/null || echo 0)
    since=$(( $(date +%s) - ${last:-0} ))
    if [ "$since" -lt "$COOLDOWN" ]; then
        log "deaf but in cooldown (${since}s since last fire, need ${COOLDOWN}s) -- dongle may be dead"
        exit 0
    fi
fi

log "firing readsb-recover auto"
rm -f "$TICK"
exec /usr/local/bin/readsb-recover auto
