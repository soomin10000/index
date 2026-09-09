#!/bin/bash
# v6probe.sh — one IPv6 (+ IPv4 control) path-health sample.
#
# Appends a compact JSON line to ~/v6mon/v6health.jsonl and, whenever anything
# fails (ICMP loss, DNS timeout, default-route flip, prefix renumber), a forensic
# block to ~/v6mon/v6health-detail.log. Deployed to bazza + steve and run every
# 5 min by cron; pollers/v6health.py (on steve) rolls both hosts' files up into
# data/v6health.json for the /v6health card + page. See README.md.
#
# Pure stdlib / iputils / bind-dnsutils — no secrets, safe to run unprivileged.
set -u

DIR="${V6MON_DIR:-$HOME/v6mon}"
mkdir -p "$DIR"
JSONL="$DIR/v6health.jsonl"
DETAIL="$DIR/v6health-detail.log"
LAST_ROUTE="$DIR/.last_route6"
LAST_GUA="$DIR/.last_gua"

N=10                       # pings per target (0.2s apart = ~2s/target)
HOST=$(hostname -s 2>/dev/null || hostname)
TS=$(date +%s)

IFACE=$(ip -6 route show default 2>/dev/null | grep -oP 'dev \K\S+' | head -1)
GW=$(ip -6 route show default 2>/dev/null | grep -oP 'via \K\S+' | head -1)
[ -z "$IFACE" ] && IFACE="none"

# current global v6 address + SLAAC lifetimes (first non-deprecated one)
_addrline=$(ip -6 -o addr show dev "$IFACE" scope global 2>/dev/null | grep -v deprecated | head -1)
GUA=$(printf '%s' "$_addrline" | grep -oP 'inet6 \K[0-9a-f:]+')
VALID=$(printf '%s' "$_addrline" | grep -oP 'valid_lft \K\d+')
PREF=$(printf '%s' "$_addrline" | grep -oP 'preferred_lft \K\d+')
GUA=${GUA:-none}; VALID=${VALID:-0}; PREF=${PREF:-0}   # 0 = forever / unknown

ROUTE6="${GW:-none}"
LASTR=$(cat "$LAST_ROUTE" 2>/dev/null || true)
ROUTE_CHANGED=false
[ -n "$LASTR" ] && [ "$LASTR" != "$ROUTE6" ] && ROUTE_CHANGED=true
printf '%s' "$ROUTE6" > "$LAST_ROUTE"

LASTG=$(cat "$LAST_GUA" 2>/dev/null || true)
GUA_CHANGED=false
[ -n "$LASTG" ] && [ "$LASTG" != "$GUA" ] && GUA_CHANGED=true
printf '%s' "$GUA" > "$LAST_GUA"

RA_ROUTERS=$(ip -6 neigh show 2>/dev/null | grep -c ' router')

sn() { grep -m1 "^$1 " /proc/net/snmp6 2>/dev/null | awk '{print $2}'; }
IN_DU=$(sn Icmp6InDestUnreachs);  OUT_TE=$(sn Icmp6OutTimeExcds)
IN_ERR=$(sn Icmp6InErrors);       REASM=$(sn Ip6ReasmFails)
IN_RA=$(sn Icmp6InRouterAdvertisements); OUT_RA=$(sn Icmp6OutRouterAdvertisements)

PING=ping
ping -6 -c1 -W1 ::1 >/dev/null 2>&1 || PING=ping6

# name -> ip / family.  gw_* are first-hop reachability only (no DNS test).
ORDER="gw_v6 cf_v6 goog_v6 quad9_v6 root_v6 gw_v4 cf_v4 goog_v4"
declare -A T_IP=(
  [gw_v6]="$GW" [cf_v6]="2606:4700:4700::1111" [goog_v6]="2001:4860:4860::8888"
  [quad9_v6]="2620:fe::fe" [root_v6]="2001:7fd::1"
  [gw_v4]="192.168.1.1" [cf_v4]="1.1.1.1" [goog_v4]="8.8.8.8"
)
declare -A T_FAM=(
  [gw_v6]=6 [cf_v6]=6 [goog_v6]=6 [quad9_v6]=6 [root_v6]=6
  [gw_v4]=4 [cf_v4]=4 [goog_v4]=4
)
RESOLVERS=" cf_v6 goog_v6 quad9_v6 root_v6 cf_v4 goog_v4 "   # get a DNS probe
BIGTEST=" cf_v6 goog_v6 quad9_v6 root_v6 "                    # + a large-response probe

# link-local targets need a zone id (fe80::x%iface) for ICMP / tracepath
zoned() { case "$1" in fe80:*) echo "$1%$IFACE";; *) echo "$1";; esac; }

icmp() {   # ip family -> "loss rttavg rttmax mdev"
  local ip out loss stats
  ip=$(zoned "$1"); local fam=$2
  out=$($PING -"$fam" -n -q -c "$N" -i 0.2 -W 1 "$ip" 2>/dev/null)
  loss=$(printf '%s\n' "$out" | grep -oP '\d+(?=% packet loss)' | head -1)
  stats=$(printf '%s\n' "$out" | grep -oP '= \K[0-9.]+/[0-9.]+/[0-9.]+/[0-9.]+' | awk -F/ '{print $2, $3, $4}')
  echo "${loss:-100} ${stats:-null null null}"
}

dns() {    # ip family -> "okcount lastms" ( . NS, no recursion — every resolver answers )
  local ip=$1 fam=$2 ok=0 ms=null i t
  for i in 1 2 3; do
    t=$(dig -"$fam" +tries=1 +time=2 +norecurse "@$ip" . NS +noall +stats 2>/dev/null \
        | grep -oP 'Query time: \K\d+')
    [ -n "$t" ] && { ok=$((ok+1)); ms=$t; }
  done
  echo "$ok $ms"
}

bigdns() { # ip family -> "ms size"  ( . DNSKEY +dnssec ~1.1KB — fragmentation canary )
  local ip=$1 fam=$2 r
  r=$(dig -"$fam" +tries=1 +time=3 +dnssec "@$ip" . DNSKEY +noall +stats 2>/dev/null)
  echo "$(printf '%s\n' "$r" | grep -oP 'Query time: \K\d+' || echo null) \
$(printf '%s\n' "$r" | grep -oP 'MSG SIZE\s+rcvd: \K\d+' || echo null)"
}

TOBJ=""; FAILS=""; V6FAIL=0; V4FAIL=0
for name in $ORDER; do
  ip=${T_IP[$name]}; fam=${T_FAM[$name]}
  [ -z "$ip" ] && continue
  read -r loss rtt rmax mdev <<<"$(icmp "$ip" "$fam")"
  dok=null; dms=null; bms=null; bsz=null
  if [[ "$RESOLVERS" == *" $name "* ]]; then
    read -r dok dms <<<"$(dns "$ip" "$fam")"
    [[ "$BIGTEST" == *" $name "* ]] && read -r bms bsz <<<"$(bigdns "$ip" "$fam")"
  fi
  fail=0
  [ "${loss:-100}" -gt 0 ] 2>/dev/null && fail=1
  { [[ "$RESOLVERS" == *" $name "* ]] && [ "${dok:-0}" -lt 3 ]; } && fail=1
  if [ "$fail" -eq 1 ]; then
    FAILS="$FAILS $name"
    [ "$fam" = 6 ] && V6FAIL=1 || V4FAIL=1
  fi
  TOBJ="$TOBJ,\"$name\":{\"ip\":\"$ip\",\"fam\":$fam,\"loss\":${loss:-100},\"rtt\":${rtt:-null},\"rtt_max\":${rmax:-null},\"mdev\":${mdev:-null},\"dns_ok\":${dok:-null},\"dns_ms\":${dms:-null},\"big_ms\":${bms:-null},\"big_size\":${bsz:-null}}"
done
TOBJ="{${TOBJ#,}}"

FK=""
if   [ "$V6FAIL" = 1 ] && [ "$V4FAIL" = 1 ]; then FK="both"
elif [ "$V6FAIL" = 1 ]; then FK="v6"
elif [ "$V4FAIL" = 1 ]; then FK="v4"; fi
FJSON=$(printf '%s' "$FAILS" | awk '{for(i=1;i<=NF;i++)printf "%s\"%s\"",(i>1?",":""),$i}')

LINE="{\"ts\":$TS,\"host\":\"$HOST\",\"iface\":\"$IFACE\",\"gw\":\"${GW:-none}\",\"gua\":\"$GUA\",\"valid_lft\":$VALID,\"pref_lft\":$PREF,\"route6\":\"$ROUTE6\",\"route6_changed\":$ROUTE_CHANGED,\"gua_changed\":$GUA_CHANGED,\"ra_routers\":${RA_ROUTERS:-0},\"snmp6\":{\"in_dstunreach\":${IN_DU:-0},\"out_timeexcd\":${OUT_TE:-0},\"in_err\":${IN_ERR:-0},\"reasm_fail\":${REASM:-0},\"in_ra\":${IN_RA:-0},\"out_ra\":${OUT_RA:-0}},\"targets\":$TOBJ,\"fail\":[${FJSON}],\"fail_kind\":\"$FK\"}"

echo "$LINE"
echo "$LINE" >> "$JSONL"
tail -n 3000 "$JSONL" > "$JSONL.tmp" 2>/dev/null && mv "$JSONL.tmp" "$JSONL"

if [ -n "$FAILS" ] || [ "$ROUTE_CHANGED" = true ] || [ "$GUA_CHANGED" = true ]; then
  {
    echo "===== $(date -Is) $HOST  fail:[${FAILS# }]  route_changed:$ROUTE_CHANGED  gua_changed:$GUA_CHANGED"
    echo "-- ip -6 addr (global) --"; ip -6 -o addr show dev "$IFACE" scope global 2>&1
    echo "-- ip -6 route --";         ip -6 route show 2>&1
    echo "-- ip -6 neigh --";         ip -6 neigh show 2>&1
    for t in $FAILS; do
      [ "${T_FAM[$t]}" = 6 ] || continue
      echo "-- path to $t (${T_IP[$t]}) --"
      _z=$(zoned "${T_IP[$t]}")
      if   command -v tracepath   >/dev/null; then tracepath -6 -m 12 "$_z" 2>&1
      elif command -v traceroute6 >/dev/null; then traceroute6 -q1 -w1 -m12 "$_z" 2>&1
      elif command -v mtr         >/dev/null; then mtr -6 -r -c 10 "$_z" 2>&1
      else echo "   (no tracepath/traceroute6/mtr installed)"; fi
    done
    if command -v rdisc6 >/dev/null; then
      echo "-- RA seen on $IFACE (rdisc6) --"; timeout 8 rdisc6 -1 -w 5000 "$IFACE" 2>&1
    fi
    echo
  } >> "$DETAIL"
  tail -n 1200 "$DETAIL" > "$DETAIL.tmp" 2>/dev/null && mv "$DETAIL.tmp" "$DETAIL"
fi
