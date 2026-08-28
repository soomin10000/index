"""Writes wacky.json for the web dashboard — CPU/mem/disk/uptime for the wacky
host (a public VPS), gathered over SSH.

On top of the shared host-metrics shape (see hostlib.py) this reports a fail2ban
panel: currently-banned SSH IPs with country flags (geoip memoised in
wacky_geoip_cache.json) and repeat offenders tracked in a ban_history table.

Metrics history goes to wacky_history.db (4-column schema).
"""

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT         = DATA / "wacky.json"
STATE_FILE  = DATA / "wacky_state.json"
DB_FILE     = DATA / "wacky_history.db"
GEOIP_CACHE = DATA / "wacky_geoip_cache.json"
BAN_HISTORY_RETENTION_SECONDS = 180 * 86400

GEOIP_TIMEOUT = 4
REPEAT_OFFENDER_MIN = 2
REPEAT_OFFENDER_TOP_N = 10

REMOTE_SCRIPT = r"""
echo '===LOADAVG==='; cat /proc/loadavg
echo '===NPROC==='; nproc
echo '===MEMINFO==='; cat /proc/meminfo
echo '===DISK==='; df -B1 --output=size,used / | tail -1
echo '===UPTIME==='; cat /proc/uptime
echo '===TOPCPU==='; ps -eo pid,comm,%cpu,%mem --sort=-%cpu --no-headers | head -5
echo '===TOPMEM==='; ps -eo pid,comm,%cpu,%mem --sort=-%mem --no-headers | head -5
echo '===FAILED==='; systemctl list-units --type=service --state=failed --no-legend --plain
echo '===FAIL2BAN==='; sudo -n /usr/bin/fail2ban-client status sshd
echo '===LANPIHOLE==='; curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 5 http://192.168.1.246/admin/ || echo '000 0'
"""


def _parse_lan_check(block):
    parts = block.strip().split()
    code = int(parts[0]) if parts else 0
    latency_ms = round(float(parts[1]) * 1000, 1) if len(parts) > 1 else None
    return {"reachable": code in (200, 301, 302, 401, 403), "http_code": code, "latency_ms": latency_ms}


def _parse_fail2ban(block):
    currently = total = 0
    ips = []
    for line in block.splitlines():
        if "Currently banned:" in line:
            currently = int(line.rsplit(":", 1)[1].strip() or 0)
        elif "Total banned:" in line:
            total = int(line.rsplit(":", 1)[1].strip() or 0)
        elif "Banned IP list:" in line:
            ips = line.split(":", 1)[1].split()
    return {"currently_banned": currently, "total_banned": total,
            "banned_ips": _annotate_banned_ips(ips)}


def _country_flag(cc):
    if not cc or len(cc) != 2:
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def _lookup_country(ip):
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,countryCode"
        with urllib.request.urlopen(url, timeout=GEOIP_TIMEOUT) as r:
            data = json.loads(r.read())
        if data.get("status") == "success":
            return data.get("countryCode")
    except Exception:
        pass
    return None


def _annotate_banned_ips(ips):
    """Country lookups are memoised forever in GEOIP_CACHE, keyed by IP — an
    IP's country essentially never changes, and the ban list is short, so this
    keeps steady-state polling from hitting ip-api.com at all. Failed lookups
    are NOT cached, so a transient failure just retries next poll."""
    try:
        cache = json.loads(GEOIP_CACHE.read_text()) if GEOIP_CACHE.exists() else {}
    except Exception:
        cache = {}
    changed = False
    out = []
    for ip in ips:
        cc = cache.get(ip)
        if cc is None:
            cc = _lookup_country(ip)
            if cc:
                cache[ip] = cc
                changed = True
        out.append({"ip": ip, "cc": cc, "flag": _country_flag(cc)})
    if changed:
        GEOIP_CACHE.write_text(json.dumps(cache))
    return out


def _log_ban_events(ts, current, prev_ips):
    """One row per ban *episode* — logged only when an IP transitions from
    not-banned to banned, not on every poll it stays banned. Repeat offenders
    are IPs with multiple episodes (banned, bantime expired, banned again),
    not just ones that happen to still be banned right now."""
    new_ips = [e for e in current if e["ip"] not in prev_ips]
    if not new_ips:
        return
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS ban_history (ip TEXT, cc TEXT, ts INTEGER)")
    conn.executemany(
        "INSERT INTO ban_history (ip, cc, ts) VALUES (?, ?, ?)",
        [(e["ip"], e["cc"], ts) for e in new_ips],
    )
    cutoff = ts - BAN_HISTORY_RETENTION_SECONDS
    conn.execute("DELETE FROM ban_history WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()


def _repeat_offenders():
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("CREATE TABLE IF NOT EXISTS ban_history (ip TEXT, cc TEXT, ts INTEGER)")
        rows = conn.execute(
            "SELECT ip, cc, COUNT(*), MIN(ts), MAX(ts) FROM ban_history "
            "GROUP BY ip HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC, MAX(ts) DESC LIMIT ?",
            (REPEAT_OFFENDER_MIN, REPEAT_OFFENDER_TOP_N),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    return [{"ip": ip, "cc": cc, "flag": _country_flag(cc), "count": count,
             "first_ts": first, "last_ts": last} for ip, cc, count, first, last in rows]


def _extra_alerts(data):
    """wacky-specific alert candidates, same insertion order as before: failed
    units, then the tailnet Pi-hole reachability check."""
    extra = dict(hostlib.failed_unit_alerts(data["other_failed"]))
    if not data["lan"]["pihole"]["reachable"]:
        extra["lan_pihole"] = ("warn", "Pi-hole unreachable from wacky",
            f"http {data['lan']['pihole']['http_code']} via the tailnet subnet route")
    return extra


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("wacky", REMOTE_SCRIPT)
        load = hostlib.parse_load(s["LOADAVG"], s["NPROC"])
        mem = hostlib.parse_mem(s["MEMINFO"])
        disk = hostlib.parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = hostlib.parse_procs(s["TOPCPU"])
        top_mem = hostlib.parse_procs(s["TOPMEM"])
        other_failed = hostlib.parse_failed(s["FAILED"])
        fail2ban = _parse_fail2ban(s["FAIL2BAN"])
        lan = {"pihole": _parse_lan_check(s["LANPIHOLE"])}
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"wacky poll failed: {e}")
        return

    data = {
        "ts": ts,
        "hostname": "wacky",
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "other_failed": other_failed,
        "fail2ban": fail2ban,
        "lan": lan,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = hostlib.load_state(STATE_FILE)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE, _extra_alerts(data))
    data["alerts"] = alerts

    prev_banned_ips = set((prev or {}).get("banned_ips", []))
    _log_ban_events(ts, fail2ban["banned_ips"], prev_banned_ips)
    fail2ban["repeat_offenders"] = _repeat_offenders()

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"])
    hostlib.save_state(STATE_FILE, {"active_alerts": active_alerts,
                                    "banned_ips": [e["ip"] for e in fail2ban["banned_ips"]]})

    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
