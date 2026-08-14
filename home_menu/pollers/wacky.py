"""Writes wacky.json for the web dashboard — CPU/mem/disk/uptime for the wacky host,
gathered over SSH (unlike steve.py, which reads /proc directly since it runs on steve).

Logs metrics history to wacky_history.db, same shape as steve_history.db.
"""

import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

OUT         = DATA / "wacky.json"
STATE_FILE  = DATA / "wacky_state.json"
DB_FILE     = DATA / "wacky_history.db"
GEOIP_CACHE = DATA / "wacky_geoip_cache.json"
HISTORY_RETENTION_SECONDS = 48 * 3600

SSH_TIMEOUT = 15
GEOIP_TIMEOUT = 4

# ── Alert thresholds (same values as steve.py) ──
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
DISK_GROWTH_PCT_PER_HOUR = 5
MEM_WARN_PCT = 90
LOAD_RATIO_WARN = 2.0

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
"""


def _sections(raw):
    parts = re.split(r"===(\w+)===\n", raw)[1:]  # drop leading empty chunk
    return {name: body for name, body in zip(parts[0::2], parts[1::2])}


def _fetch_remote():
    out = subprocess.run(
        ["ssh", "wacky", REMOTE_SCRIPT],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh wacky failed: {out.stderr.strip()}")
    return _sections(out.stdout)


def _parse_mem(block):
    info = {}
    for line in block.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            info[key] = int(rest.strip().split()[0])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def _parse_disk(block):
    line = block.strip().splitlines()[-1]
    total, used = (int(x) for x in line.split())
    return {
        "total_gb": round(total / 1e9, 1),
        "used_gb": round(used / 1e9, 1),
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def _parse_load(loadavg_block, nproc_block):
    one, five, fifteen = (float(x) for x in loadavg_block.split()[:3])
    cpus = int(nproc_block.strip())
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2), "cpus": cpus}


def _parse_procs(block, n=5):
    procs = []
    for line in block.strip().splitlines()[:n]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, comm, cpu, mem = parts
        procs.append({"pid": pid, "name": comm, "cpu": float(cpu), "mem": float(mem)})
    return procs


def _parse_failed(block):
    failed = []
    for line in block.strip().splitlines():
        parts = line.split()
        if parts:
            failed.append(parts[0])
    return failed


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


def _log_history(ts, load1, mem_pct, disk_pct):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metrics_log "
        "(ts INTEGER, load1 REAL, mem_pct REAL, disk_pct REAL)"
    )
    conn.execute(
        "INSERT INTO metrics_log (ts, load1, mem_pct, disk_pct) VALUES (?, ?, ?, ?)",
        (ts, load1, mem_pct, disk_pct),
    )
    cutoff = ts - HISTORY_RETENTION_SECONDS
    conn.execute("DELETE FROM metrics_log WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()


def _load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _save_state(active_alerts):
    STATE_FILE.write_text(json.dumps({"active_alerts": active_alerts}))


def _disk_pct_hour_ago(now):
    if not DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute(
            "SELECT disk_pct FROM metrics_log WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (now - 3300,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _build_alerts(data, now, prev):
    prev_active = (prev or {}).get("active_alerts", {})
    candidates = {}

    disk, mem, load = data["disk"], data["mem"], data["load"]
    if disk["percent"] >= DISK_CRIT_PCT:
        candidates["disk_high"] = ("critical", "Disk almost full",
            f"/ is {disk['percent']}% full ({disk['used_gb']}/{disk['total_gb']} GB)")
    elif disk["percent"] >= DISK_WARN_PCT:
        candidates["disk_high"] = ("warn", "Disk usage high",
            f"/ is {disk['percent']}% full ({disk['used_gb']}/{disk['total_gb']} GB)")

    hour_ago = _disk_pct_hour_ago(now)
    if hour_ago is not None and disk["percent"] - hour_ago >= DISK_GROWTH_PCT_PER_HOUR:
        candidates["disk_growth"] = ("warn", "Unusual disk activity",
            f"disk usage rose {disk['percent'] - hour_ago:.1f} pts in the last hour "
            f"({hour_ago:.1f}% → {disk['percent']}%)")

    if mem["percent"] >= MEM_WARN_PCT:
        candidates["mem_high"] = ("warn", "Memory pressure",
            f"{mem['percent']}% RAM in use ({mem['used_mb']:.0f}/{mem['total_mb']:.0f} MB)")

    cpus = load.get("cpus") or 1
    if load["1m"] / cpus >= LOAD_RATIO_WARN:
        candidates["load_high"] = ("warn", "Load average high",
            f"1m load {load['1m']} across {cpus} CPU(s)")

    for unit in data["other_failed"]:
        candidates[f"failed_{unit}"] = ("warn", "Unit failed", f"{unit} is in a failed state")

    active_alerts = {}
    alerts = []
    for key, (level, header, text) in candidates.items():
        onset = prev_active.get(key, {}).get("ts", now)
        active_alerts[key] = {"ts": onset, "level": level, "header": header, "text": text}
        alerts.append({"id": key, "ts": onset, "level": level, "header": header, "text": text})
    alerts.sort(key=lambda a: a["ts"], reverse=True)

    return alerts, active_alerts


def fetch_and_write():
    ts = int(time.time())
    try:
        sections = _fetch_remote()
        load = _parse_load(sections["LOADAVG"], sections["NPROC"])
        mem = _parse_mem(sections["MEMINFO"])
        disk = _parse_disk(sections["DISK"])
        uptime_seconds = float(sections["UPTIME"].split()[0])
        top_cpu = _parse_procs(sections["TOPCPU"])
        top_mem = _parse_procs(sections["TOPMEM"])
        other_failed = _parse_failed(sections["FAILED"])
        fail2ban = _parse_fail2ban(sections["FAIL2BAN"])
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
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = _load_state()
    alerts, active_alerts = _build_alerts(data, ts, prev)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    _log_history(ts, load["1m"], mem["percent"], disk["percent"])
    _save_state(active_alerts)

    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
