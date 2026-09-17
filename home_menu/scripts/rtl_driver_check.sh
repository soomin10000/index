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

# Runs as root (rtl88xxau-check.service, User=root). Keep the log under /var/log,
# not home_menu/logs/ — that dir's logrotate stanza runs `su simon simon` and
# chokes on root-owned files, failing the whole logrotate.service run.
LOG_DIR="/var/log"
LOG="$LOG_DIR/rtl_driver_check.log"
RTL_SRC="/home/simon/rtl8812au"
RTL_FIX="/home/simon/projects/home_menu/scripts/install_rtl_fix.sh"
GOOD_SRCVERSION="BD79D617C3C5F1491C7C408"

mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1
echo "===== $(date -Is) ====="

modprobe 88XXau 2>/dev/null
LOADED_SRCVER="$(cat /sys/module/88XXau/srcversion 2>/dev/null || echo none)"

if [[ "$LOADED_SRCVER" == "$GOOD_SRCVERSION" ]]; then
    echo "driver ok ($LOADED_SRCVER) for $(uname -r)"
    exit 0
fi

echo "rtl88XXau mismatch on boot: loaded=$LOADED_SRCVER want=$GOOD_SRCVERSION kernel=$(uname -r) — rebuilding"
if (cd "$RTL_SRC" && make clean && make); then
    if bash "$RTL_FIX"; then
        echo "driver was missing/stale after boot, rebuilt and reloaded automatically"
    else
        echo "WARNING: driver rebuild OK but install FAILED — needs manual attention"
    fi
else
    echo "WARNING: driver rebuild FAILED — needs manual attention"
fi
echo "===== done ====="
