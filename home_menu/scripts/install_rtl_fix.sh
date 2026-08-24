#!/bin/bash
# Install the rebuilt rtl88XXau (88XXau.ko) with the syslog-flood fix.
#
# The fix changes the rtw_rf.c:225 WARN_ON (hit on every monitor-mode channel
# hop, ~44k times/2 days) to WARN_ON_ONCE, stopping ~840MB/day of kernel stack
# traces to /var/log/{syslog,kern.log}. Source: ~/rtl8812au/core/rtw_rf.c.
#
# Run with: sudo bash ~/projects/install_rtl_fix.sh
set -euo pipefail

# Absolute path — under `sudo`, $HOME resolves to /root, not the invoking user.
KO_SRC="/home/simon/rtl8812au/88XXau.ko"
KREL="$(uname -r)"
KO_DST="/lib/modules/$KREL/kernel/drivers/net/wireless/88XXau.ko"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root: sudo bash $0" >&2
    exit 1
fi

echo "New module srcversion: $(modinfo "$KO_SRC" | awk '/^srcversion/{print $2}')"
echo "Old module srcversion: $(modinfo "$KO_DST" | awk '/^srcversion/{print $2}')"

echo "[1/6] Stopping kismet (frees the capture adapter)..."
systemctl stop kismet

echo "[2/6] Backing up and installing new module..."
# A kernel upgrade can put us on a kernel that never had 88XXau installed
# yet (weekly_update.sh's srcversion check only sees what's *currently
# loaded*, from the pre-reboot kernel — it can't detect this case), so
# there may be nothing to back up.
if [[ -f "$KO_DST" ]]; then
    cp -a "$KO_DST" "${KO_DST}.bak.$(date +%s)"
fi
mkdir -p "$(dirname "$KO_DST")"
cp "$KO_SRC" "$KO_DST"

echo "[3/6] depmod..."
depmod -a "$KREL"

echo "[4/6] Reloading driver..."
if lsmod | grep -q '^88XXau'; then
    modprobe -r 88XXau || { echo "rmmod failed — is the adapter still in use? Aborting before it's half-installed."; systemctl start kismet; exit 1; }
fi
modprobe 88XXau

echo "[5/6] Restarting kismet (poller re-adds the datasource on its next run)..."
systemctl start kismet

echo "[6/6] Done. Verifying loaded module:"
sleep 2
echo "  loaded srcversion: $(cat /sys/module/88XXau/srcversion 2>/dev/null || echo '?')  (want BD79D617C3C5F1491C7C408)"
echo
echo "Watch syslog for ~a minute to confirm the flood is gone:"
echo "  sudo journalctl -k -f | grep rtw_get_scch"
