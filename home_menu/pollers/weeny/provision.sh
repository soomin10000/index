#!/usr/bin/env bash
# weeny -- first-boot provisioning for the Pi Zero W OpenCanary honeypot.
#
# Runs as root from cloud-init `runcmd:` on the fresh Raspberry Pi OS Lite
# (Trixie, 32-bit) image, after network-online.target. Rebuilds the box that
# locked us out on 2026-09-06:
#
#   * cheap-NAS OpenCanary persona on 192.168.1.5 (FTP/telnet/HTTP/SSH)
#   * real OpenSSH moved to Port 2222 on the WILDCARD address -- no ListenAddress
#     anywhere, so nothing to race on cold boot (that race was the lockout)
#   * nft drop of 192.168.1.5:2222 so the bait IP never shows a real sshd
#   * comitup for headless Wi-Fi re-provisioning, decoupled from the honeypot
#   * unattended security upgrades (no auto-reboot)
#
# Idempotent: every stage checks its own state; a marker (/var/lib/weeny-
# provisioned) short-circuits a re-run. Safe to re-run over SSH:
#
#   ssh weeny 'sudo systemd-run --unit weeny-provision --collect \
#       bash /boot/firmware/weeny/provision.sh --force --no-reboot'
#   ssh weeny journalctl -fu weeny-provision
#
# Flags:
#   --force       run even if the marker exists
#   --no-reboot   don't reboot at the end (use for SSH re-runs)
#
# NOT `set -e`: a non-fatal stage failure (e.g. unattended-upgrades) must not
# abort the run, and the lockout-risky stages (sshd, nft, comitup) must always
# get a chance to run last.
set -u

FORCE=0
REBOOT=1
for a in "$@"; do
    case "$a" in
        --force)     FORCE=1 ;;
        --no-reboot) REBOOT=0 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

# ---- paths ------------------------------------------------------------------
if [ -d /boot/firmware ]; then BOOT=/boot/firmware; else BOOT=/boot; fi
SRC="$BOOT/weeny"
LOG="$SRC/provision.log"
MARK=/var/lib/weeny-provisioned
HP_IP=192.168.1.5
MGMT_HINT=192.168.1.        # weeny's management address is a DHCP reservation in .1.0/24

[ -d "$SRC" ] || { echo "no $SRC -- is the weeny/ folder on the boot partition?" >&2; exit 1; }
mkdir -p "$(dirname "$MARK")"

# Everything from here is teed to the boot partition so it's readable on the Mac
# even if the network never comes up.
exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n%s === %s\n' "$(date -Is)" "$*"; }
info() { printf '%s     %s\n'   "$(date -Is)" "$*"; }

# ---- secrets --------------------------------------------------------------
# secrets.env sits next to this script on the boot partition, never in git.
if [ -r "$SRC/secrets.env" ]; then
    set -a; . "$SRC/secrets.env"; set +a
fi
NTFY_URL="${WEENY_NTFY_URL:-http://192.168.1.183:8197}"
NTFY_TOPIC="${WEENY_NTFY_TOPIC:-steve_updates}"

# ---- stage runner ------------------------------------------------------------
declare -A RESULT
run_stage() {
    local name="$1"; shift
    say "stage: $name"
    if "$@"; then
        RESULT[$name]=OK;   info "stage $name: OK"
    else
        RESULT[$name]=FAIL; info "stage $name: FAIL (rc=$?)"
    fi
}

retry() {  # retry <n> <sleep> -- <cmd...>
    local n="$1" s="$2"; shift 2; [ "$1" = "--" ] && shift
    local i
    for i in $(seq 1 "$n"); do
        "$@" && return 0
        info "  attempt $i/$n failed; sleeping ${s}s"
        sleep "$s"
    done
    return 1
}

ntfy() {  # ntfy <title> <message> <priority>
    local body
    body=$(python3 -c 'import json,sys; print(json.dumps({"topic":sys.argv[1],"title":sys.argv[2],"message":sys.argv[3],"priority":int(sys.argv[4])}))' \
        "$NTFY_TOPIC" "$1" "$2" "${3:-3}" 2>/dev/null) || return 0
    curl -sf -m 10 -H 'Content-Type: application/json' -d "$body" "$NTFY_URL" >/dev/null 2>&1 \
        || info "ntfy push failed (non-fatal)"
}

# ===========================================================================
# stage 0 -- preflight
# ===========================================================================
st_preflight() {
    [ "$(id -u)" -eq 0 ] || { info "must run as root"; return 1; }
    info "$(uname -a)"
    info "$(. /etc/os-release; echo "$PRETTY_NAME")"
    if [ -e "$MARK" ] && [ "$FORCE" -eq 0 ]; then
        info "already provisioned ($(cat "$MARK" 2>/dev/null | head -1)); use --force to re-run"
        exit 0
    fi
    # Imager's runcmd also does this; harmless to repeat. Explicit .service so we
    # don't get socket activation here.
    systemctl enable --now ssh.service >/dev/null 2>&1 || true
    return 0
}

# ===========================================================================
# stage 1 -- wait for LAN + a real clock (Zero W has no RTC)
# ===========================================================================
st_wait_net_time() {
    local i
    info "waiting for wlan0 on ${MGMT_HINT}0/24 + gateway + DNS (<=15 min)"
    for i in $(seq 1 90); do
        if ip -4 addr show wlan0 2>/dev/null | grep -q " inet ${MGMT_HINT//./\\.}" \
           && ping -c1 -W2 192.168.1.1 >/dev/null 2>&1 \
           && getent hosts deb.debian.org >/dev/null 2>&1; then
            info "  network up after $((i*10))s"
            break
        fi
        sleep 10
    done
    ip -4 -br addr show wlan0 | sed 's/^/     /'

    info "waiting for NTP sync (<=3 min) -- fake-hwclock restores the image build date"
    timedatectl set-ntp true >/dev/null 2>&1 || true
    for i in $(seq 1 18); do
        [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ] && break
        [ "$i" -eq 6 ] && systemctl restart systemd-timesyncd >/dev/null 2>&1 || true
        sleep 10
    done
    info "  clock: $(date -Is)  NTPSynchronized=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
    return 0   # proceed regardless; a bad clock will surface as an apt error
}

# ===========================================================================
# stage 2 -- sysctl (bind-before-address)
# ===========================================================================
st_sysctl() {
    install -m0644 "$SRC/sysctl-90-weeny.conf" /etc/sysctl.d/90-weeny.conf || return 1
    sysctl --system >/dev/null
    sysctl net.ipv4.ip_nonlocal_bind
}

# ===========================================================================
# stage 3 -- bait address 192.168.1.5/32 on wlan0
# ===========================================================================
st_bait_ip() {
    install -m0755 "$SRC/50-honeypot-ip" /etc/NetworkManager/dispatcher.d/50-honeypot-ip || return 1

    local con
    con=$(nmcli -t -g GENERAL.CONNECTION device show wlan0 2>/dev/null)
    [ -z "$con" ] && con=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: '$2=="wlan0"{print $1; exit}')
    if [ -n "$con" ]; then
        info "wlan0 connection profile: $con"
        if nmcli -g ipv4.addresses con show "$con" 2>/dev/null | tr ',' '\n' | grep -q "^ *${HP_IP}/"; then
            info "  ${HP_IP} already on the profile"
        else
            nmcli con mod "$con" +ipv4.addresses "${HP_IP}/32" \
                && info "  added ${HP_IP}/32 to profile $con (persists across reboot)" \
                || info "  WARNING: could not add ${HP_IP} to profile $con"
        fi
    else
        info "WARNING: no NM profile found for wlan0 -- relying on the dispatcher only"
    fi

    # Bring it up on the live interface now (don't reactivate the connection --
    # that could drop our SSH session mid-provision).
    ip -4 addr show wlan0 | grep -q " inet ${HP_IP}/" || ip addr add "${HP_IP}/32" dev wlan0 || true
    ip -4 -br addr show wlan0 | sed 's/^/     /'
    ip -4 addr show wlan0 | grep -q " inet ${HP_IP}/"
}

# ===========================================================================
# stage 4 -- apt dependencies
# ===========================================================================
st_apt() {
    export DEBIAN_FRONTEND=noninteractive
    retry 3 60 -- apt-get -o DPkg::Lock::Timeout=600 update || return 1
    retry 3 60 -- apt-get -o DPkg::Lock::Timeout=600 -y install \
        python3-dev python3-pip python3-venv \
        libssl-dev libffi-dev libpcap-dev build-essential \
        nftables unattended-upgrades curl logrotate ca-certificates jq
}

# ===========================================================================
# stage 5 -- canary service account + directories
# ===========================================================================
st_canary_user() {
    id canary >/dev/null 2>&1 \
        || useradd --system --home /var/lib/opencanary --shell /usr/sbin/nologin canary || return 1
    install -d -o canary -g canary -m0750 /var/lib/opencanary
    install -d -o canary -g canary -m0750 /var/log/opencanary
    install -d -m0755 /etc/opencanaryd
    # steve's collector tails the log over SSH as `simon`; group membership lets it.
    id simon >/dev/null 2>&1 && usermod -aG canary simon || true
    return 0
}

# ===========================================================================
# stage 6 -- OpenCanary in a venv (piwheels armv6 wheels for the heavy deps)
# ===========================================================================
st_venv() {
    if [ -x /opt/opencanary/bin/pip ] \
       && /opt/opencanary/bin/pip show opencanary 2>/dev/null | grep -q '^Version: 0\.9\.9'; then
        info "opencanary 0.9.9 already in /opt/opencanary"
        return 0
    fi
    python3 -m venv /opt/opencanary || return 1
    # --only-binary on the two slow ones: if piwheels ever lacks the armv6 wheel,
    # fail fast here instead of compiling Rust/C for hours on one core.
    retry 3 60 -- /opt/opencanary/bin/pip install --prefer-binary \
        --only-binary=cryptography,bcrypt 'opencanary==0.9.9' || return 1
    test -f /opt/opencanary/bin/opencanary.tac || { info "opencanary.tac missing"; return 1; }
    /opt/opencanary/bin/python -c 'import opencanary; print("import opencanary OK")'
}

# ===========================================================================
# stage 7 -- persona config + logrotate
# ===========================================================================
st_opencanary_conf() {
    python3 -m json.tool "$SRC/opencanary.conf" >/dev/null || { info "opencanary.conf is not valid JSON"; return 1; }
    install -o root -g canary -m0640 "$SRC/opencanary.conf" /etc/opencanaryd/opencanary.conf || return 1
    install -m0644 "$SRC/opencanary.logrotate" /etc/logrotate.d/opencanary || return 1
    logrotate -d /etc/logrotate.d/opencanary >/dev/null 2>&1 || info "  logrotate -d warned (non-fatal)"
    return 0
}

# ===========================================================================
# stage 8 -- opencanary unit
# ===========================================================================
st_opencanary_unit() {
    install -m0644 "$SRC/opencanary.service" /etc/systemd/system/opencanary.service || return 1
    systemctl daemon-reload
    systemctl enable --now opencanary.service || return 1
    # armv6 takes ~40s just to import twisted; poll up to 90s for all four ports.
    local ok=0 p i
    for i in $(seq 1 18); do
        ok=1
        for p in 21 22 23 80; do
            ss -Hltn 2>/dev/null | grep -q "${HP_IP}:${p} " || ok=0
        done
        [ "$ok" -eq 1 ] && break
        sleep 5
    done
    for p in 21 22 23 80; do
        ss -Hltn 2>/dev/null | grep -q "${HP_IP}:${p} " \
            && info "  bound ${HP_IP}:${p}" || info "  NOT bound ${HP_IP}:${p}"
    done
    [ "$ok" -eq 1 ] || { journalctl -u opencanary --no-pager -n 30 | sed 's/^/     /'; return 1; }
    return 0
}

# ===========================================================================
# stage 9 -- unattended security upgrades
# ===========================================================================
st_unattended() {
    install -m0644 "$SRC/apt-20auto-upgrades"    /etc/apt/apt.conf.d/20auto-upgrades || return 1
    install -m0644 "$SRC/apt-52weeny-unattended" /etc/apt/apt.conf.d/52weeny-unattended-upgrades || return 1
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
    systemctl enable unattended-upgrades.service >/dev/null 2>&1 || true
    unattended-upgrades --dry-run 2>&1 | tail -n 5 | sed 's/^/     /' || true
    return 0
}

# ===========================================================================
# stage 10 -- real sshd -> Port 2222, wildcard, key-only  (LAST SSH CHANGE)
# ===========================================================================
# Two-phase so we never lose the way in:
#   (a) listen on BOTH 22 and 2222, prove 2222 works
#   (b) drop 22
# Debian Trixie socket-activates ssh (ssh.socket owns :22 and IGNORES the Port
# directive) -- disable it so sshd_config is authoritative and opencanary can
# own 192.168.1.5:22.
st_sshd_port() {
    local dropin=/etc/ssh/sshd_config.d/10-weeny.conf
    local auth=$'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin no'

    systemctl disable --now ssh.socket >/dev/null 2>&1 || true
    systemctl enable ssh.service       >/dev/null 2>&1 || true

    # phase (a): 22 + 2222
    printf '# weeny two-phase: 22 kept alive until 2222 is proven.\nPort 22\nPort 2222\n%s\n' "$auth" > "$dropin"
    if ! sshd -t; then
        info "  sshd -t failed on phase (a) -- rolling back"; rm -f "$dropin"
        systemctl restart ssh.service; return 1
    fi
    systemctl restart ssh.service
    local i up=0
    for i in $(seq 1 15); do
        ss -Hltn 2>/dev/null | grep -qE '(\*|0\.0\.0\.0|\[::\]):2222 ' && { up=1; break; }
        sleep 1
    done
    [ "$up" -eq 1 ] || { info "  :2222 never came up -- rolling back"; rm -f "$dropin"; systemctl restart ssh.service; return 1; }
    info "  :2222 listening (still on :22 too)"

    # phase (b): 2222 only
    printf '# weeny: real OpenSSH on 2222, wildcard, key-only. NEVER add ListenAddress.\nPort 2222\n%s\n' "$auth" > "$dropin"
    chmod 0644 "$dropin"
    if ! sshd -t; then
        info "  sshd -t failed on phase (b) -- reverting to phase (a) config"
        printf 'Port 22\nPort 2222\n%s\n' "$auth" > "$dropin"; systemctl reload ssh.service; return 1
    fi
    systemctl reload ssh.service
    sleep 2
    ss -Hltn 2>/dev/null | grep -qE '(\*|0\.0\.0\.0|\[::\]):2222 ' || { info "  :2222 gone after phase (b)"; return 1; }
    if ss -Hltn 2>/dev/null | grep -qE '(\*|0\.0\.0\.0|\[::\]):22 '; then
        info "  WARNING: something still on *:22 after phase (b)"
    fi
    info "  real sshd now on 2222 only"

    # self-heal drop-in (needs ssh.service, which we're now on)
    install -Dm0644 "$SRC/ssh-restart.conf" /etc/systemd/system/ssh.service.d/restart.conf
    systemctl daemon-reload
    return 0
}

# ===========================================================================
# stage 11 -- nftables: drop 192.168.1.5:2222
# ===========================================================================
st_nft() {
    install -m0644 "$SRC/nftables.conf" /etc/nftables.conf || return 1
    nft -c -f /etc/nftables.conf || { info "  nft syntax check failed"; return 1; }
    systemctl enable nftables >/dev/null 2>&1 || true
    nft -f /etc/nftables.conf || return 1
    nft list table inet weeny | sed 's/^/     /'
}

# ===========================================================================
# stage 12 -- comitup  (LAST -- most likely to disturb wlan0)
# ===========================================================================
st_comitup() {
    if [ -z "${COMITUP_AP_PASSWORD:-}" ]; then
        info "COMITUP_AP_PASSWORD unset in $SRC/secrets.env -- skipping comitup."
        info "  (WPA2 hotspot was the chosen design; add the key and re-run --force.)"
        return 1
    fi

    local bk="/root/nm-backup-$(date +%s)"
    install -d -m0700 "$bk"
    cp -a /etc/NetworkManager/system-connections/. "$bk"/ 2>/dev/null || true
    info "  NM profiles backed up to $bk"
    local before
    before=$(nmcli -t -f NAME con show 2>/dev/null | sort)

    retry 3 60 -- apt-get -o DPkg::Lock::Timeout=600 -y install comitup || return 1
    install -m0755 "$SRC/comitup-callback" /usr/local/bin/comitup-callback || return 1

    # render /etc/comitup.conf: template minus the ap_password comment, plus the real key
    grep -v '^# *ap_password:' "$SRC/comitup.conf" > /etc/comitup.conf
    printf 'ap_password: %s\n' "$COMITUP_AP_PASSWORD" >> /etc/comitup.conf
    chmod 0600 /etc/comitup.conf

    systemctl enable comitup >/dev/null 2>&1 || true
    systemctl restart comitup
    sleep 15

    local state after
    state=$(nmcli -t -g GENERAL.STATE dev show wlan0 2>/dev/null)
    after=$(nmcli -t -f NAME con show 2>/dev/null | sort)
    info "  wlan0 state: ${state:-unknown}"
    if ! printf '%s' "$state" | grep -q 'connected'; then
        info "  wlan0 not connected after comitup -- restoring NM profiles from $bk"
        cp -a "$bk"/. /etc/NetworkManager/system-connections/ 2>/dev/null || true
        nmcli con reload || true
        sleep 10
        return 1
    fi
    if [ "$before" != "$after" ]; then
        info "  note: NM profile list changed:"; diff <(echo "$before") <(echo "$after") | sed 's/^/     /' || true
    fi
    return 0
}

# ===========================================================================
# run
# ===========================================================================
say "weeny provisioning start (force=$FORCE reboot=$REBOOT)"

run_stage preflight        st_preflight
run_stage wait_net_time    st_wait_net_time
run_stage sysctl           st_sysctl
run_stage bait_ip          st_bait_ip

# Move the real sshd to 2222 BEFORE the honeypot starts: OpenCanary can't bind
# 192.168.1.5:22 while sshd holds the wildcard *:22. No package deps here
# (openssh-server + ss are in the base image), and the two-phase cutover keeps
# :22 alive until :2222 is proven -- so it's safe to do early and fail fast.
run_stage sshd_port        st_sshd_port

run_stage apt              st_apt

# nft needs the nftables package (from apt) and the opencanary unit is
# After=nftables.service, so the firewall goes up here, before the honeypot.
run_stage nft              st_nft

if [ "${RESULT[apt]}" = OK ]; then
    run_stage canary_user      st_canary_user
    run_stage venv             st_venv
    if [ "${RESULT[venv]:-FAIL}" = OK ]; then
        run_stage opencanary_conf  st_opencanary_conf
        run_stage opencanary_unit  st_opencanary_unit
    else
        info "venv failed -- skipping opencanary config/unit stages"
    fi
    run_stage unattended       st_unattended
else
    info "apt failed -- skipping opencanary + unattended stages (box still reachable on 2222)"
fi

run_stage comitup          st_comitup

# ---- finalise -------------------------------------------------------------
say "summary"
summary=""
fail=0
for k in preflight wait_net_time sysctl bait_ip sshd_port apt nft canary_user venv opencanary_conf opencanary_unit unattended comitup; do
    r="${RESULT[$k]:-skipped}"
    printf '     %-18s %s\n' "$k" "$r"
    summary="${summary}${k}=${r} "
    [ "$r" = FAIL ] && fail=1
done

{
    echo "provisioned: $(date -Is)"
    echo "script_sha256: $(sha256sum "$0" 2>/dev/null | awk '{print $1}')"
    echo "stages: $summary"
} > "$MARK"

if [ "$fail" -eq 0 ]; then
    ntfy "weeny provisioned" "All stages OK. $summary" 3
else
    ntfy "weeny provisioning FAILED" "$summary" 4
fi

if [ "$REBOOT" -eq 1 ]; then
    say "rebooting in 1 min to prove cold-boot survival (the 2026-09-06 failure mode)"
    shutdown -r +1 "weeny: provisioning complete, rebooting to prove cold boot" || reboot
fi

say "provisioning done (fail=$fail)"
exit 0
