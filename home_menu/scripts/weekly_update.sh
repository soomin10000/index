#!/bin/bash
# Weekly system update for steve. Runs as root via a systemd system service
# (weekly-update.service/.timer), so it never needs a sudo password prompt.
#
# apt's own unattended-upgrades already applies security updates daily
# (including kernel packages — confirmed via /var/log/apt/history.log), so
# this is a supplementary full-upgrade pass (catches held-back packages
# unattended-upgrades' conservative default skips) plus cleanup.
#
# The rtl88XXau syslog-flood fix (see install_rtl_fix.sh) only lives in the
# *currently loaded* module, not in any package — a kernel upgrade + reboot
# silently reverts to the stock (flooding) driver. Rather than guessing when
# that happened, this checks the loaded module's srcversion every run and
# rebuilds/reinstalls whenever it doesn't match the known-good patched build.
set -uo pipefail

# Runs as root (weekly-update.service). Keep the log under /var/log, not
# home_menu/logs/ — that dir's logrotate stanza runs `su simon simon` and chokes
# on root-owned files, failing the whole logrotate.service run.
LOG_DIR="/var/log"
LOG="$LOG_DIR/weekly_update.log"
RTL_SRC="/home/simon/rtl8812au"
RTL_FIX="/home/simon/projects/home_menu/scripts/install_rtl_fix.sh"
GOOD_SRCVERSION="BD79D617C3C5F1491C7C408"
NTFY_URL="http://localhost:8197"
NTFY_TOPIC="steve_updates"

mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1
echo "===== $(date -Is) ====="

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root" >&2
    exit 1
fi

notify() {
    # $1=title $2=message $3=priority (default 3)
    curl -s -m 5 -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"topic": sys.argv[1], "title": sys.argv[2], "message": sys.argv[3], "priority": int(sys.argv[4])}))' \
            "$NTFY_TOPIC" "$1" "$2" "${3:-3}")" \
        "$NTFY_URL" >/dev/null || echo "ntfy push failed"
}

BEFORE_KERNEL="$(uname -r)"

export DEBIAN_FRONTEND=noninteractive
apt-get update
UPGRADED="$(apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade 2>&1 | tee /dev/stderr | grep -c '^Setting up ')"
apt-get -y autoremove --purge

REBOOT_NOTE=""
if [[ -f /var/run/reboot-required ]]; then
    PKGS="$(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"
    REBOOT_NOTE=" Reboot required ($PKGS)."
fi

DRIVER_NOTE=""
LOADED_SRCVER="$(cat /sys/module/88XXau/srcversion 2>/dev/null || echo none)"
if [[ "$LOADED_SRCVER" != "$GOOD_SRCVERSION" ]]; then
    echo "rtl88XXau driver mismatch: loaded=$LOADED_SRCVER want=$GOOD_SRCVERSION — rebuilding for $(uname -r)"
    if (cd "$RTL_SRC" && make clean && make); then
        if bash "$RTL_FIX"; then
            DRIVER_NOTE=" rtl88XXau syslog-flood fix was reapplied automatically after a kernel change."
        else
            DRIVER_NOTE=" WARNING: rtl88XXau fix install FAILED — needs manual attention (see /var/log/weekly_update.log)."
        fi
    else
        DRIVER_NOTE=" WARNING: rtl88XXau rebuild FAILED — needs manual attention (see /var/log/weekly_update.log)."
    fi
fi

AFTER_KERNEL="$(uname -r)"
MSG="Kernel $BEFORE_KERNEL -> $AFTER_KERNEL. $UPGRADED package(s) upgraded.${REBOOT_NOTE}${DRIVER_NOTE}"
echo "$MSG"

PRIORITY=3
[[ -n "$DRIVER_NOTE" ]] && PRIORITY=4
notify "steve weekly update" "$MSG" "$PRIORITY"

echo "===== done ====="
