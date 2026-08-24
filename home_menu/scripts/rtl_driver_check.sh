#!/bin/bash
# Ensures the patched 88XXau driver (syslog-flood fix) is loaded for whatever
# kernel is currently running. Runs at every boot, ordered before kismet.service
# via rtl88xxau-check.service — catches the case where a kernel upgrade lands
# a kernel that never had this out-of-tree module built for it.
#
# weekly_update.sh's own driver check can't catch this: it inspects the
# *currently loaded* module before the reboot, i.e. still against the old
# kernel, so it sees a matching srcversion and does nothing. The gap only
# shows up after the reboot, once kismet tries to open an interface that no
# longer exists because the module for the new kernel was never built.
set -uo pipefail

LOG_DIR="/home/simon/projects/home_menu/logs"
LOG="$LOG_DIR/rtl_driver_check.log"
RTL_SRC="/home/simon/rtl8812au"
RTL_FIX="/home/simon/projects/home_menu/scripts/install_rtl_fix.sh"
GOOD_SRCVERSION="BD79D617C3C5F1491C7C408"
NTFY_URL="http://localhost:8197"
NTFY_TOPIC="steve_updates"

mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1
echo "===== $(date -Is) ====="

notify() {
    # $1=title $2=message $3=priority (default 3)
    curl -s -m 5 -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"topic": sys.argv[1], "title": sys.argv[2], "message": sys.argv[3], "priority": int(sys.argv[4])}))' \
            "$NTFY_TOPIC" "$1" "$2" "${3:-3}")" \
        "$NTFY_URL" >/dev/null || echo "ntfy push failed"
}

modprobe 88XXau 2>/dev/null
LOADED_SRCVER="$(cat /sys/module/88XXau/srcversion 2>/dev/null || echo none)"

if [[ "$LOADED_SRCVER" == "$GOOD_SRCVERSION" ]]; then
    echo "driver ok ($LOADED_SRCVER) for $(uname -r)"
    exit 0
fi

echo "rtl88XXau mismatch on boot: loaded=$LOADED_SRCVER want=$GOOD_SRCVERSION kernel=$(uname -r) — rebuilding"
if (cd "$RTL_SRC" && make clean && make); then
    if bash "$RTL_FIX"; then
        notify "steve rtl88XXau" "Kernel $(uname -r): driver was missing/stale after boot, rebuilt and reloaded automatically." 3
    else
        notify "steve rtl88XXau" "Kernel $(uname -r): driver rebuild OK but install FAILED — needs manual attention (see rtl_driver_check.log)." 4
    fi
else
    notify "steve rtl88XXau" "Kernel $(uname -r): driver rebuild FAILED — needs manual attention (see rtl_driver_check.log)." 4
fi
echo "===== done ====="
