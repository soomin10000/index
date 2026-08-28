"""Writes bazza.json for the web dashboard — CPU/mem/disk/uptime for the bazza
host (the Raspberry Pi that runs the primary Pi-hole), gathered over SSH like
jeff.py / wacky.py.

On top of the shared host-metrics shape this also reports Pi-specific health
(SoC temperature, under-voltage / throttling flags from `vcgencmd get_throttled`)
and a DNS panel: the pihole-FTL / unbound service state comes over SSH, the query
/ blocking / gravity figures are lifted from data/pihole.json (already refreshed
every 5 min by pollers/pihole.py — no second Pi-hole API session from here).

Metrics history goes to bazza_history.db, same schema as jeff_history.db so the
trend-chart code is shared.
"""

import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "bazza.json"
STATE_FILE = DATA / "bazza_state.json"
DB_FILE    = DATA / "bazza_history.db"
PIHOLE_JSON = DATA / "pihole.json"
HISTORY_RETENTION_SECONDS = 48 * 3600

SSH_TIMEOUT = 15

# ── Alert thresholds (disk/mem/load match steve.py, wacky.py & jeff.py) ──
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
DISK_GROWTH_PCT_PER_HOUR = 5
MEM_WARN_PCT = 90
LOAD_RATIO_WARN = 2.0
TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0
# pihole.json is written by pollers/pihole.py on a */5 cron — anything past ~15 min
# means that poller (or bazza) is in trouble and the DNS figures below are stale.
DNS_STALE_SECONDS = 15 * 60
# Gravity is normally refreshed by the Sunday 04:00 cron; flag if it's well past.
GRAVITY_STALE_SECONDS = 10 * 24 * 3600

REMOTE_SCRIPT = r"""
echo '===LOADAVG==='; cat /proc/loadavg
echo '===NPROC==='; nproc
echo '===MEMINFO==='; cat /proc/meminfo
echo '===DISK==='; df -B1 --output=size,used / | tail -1
echo '===UPTIME==='; cat /proc/uptime
echo '===TOPCPU==='; ps -eo pid,comm,%cpu,%mem --sort=-%cpu --no-headers | head -5
echo '===TOPMEM==='; ps -eo pid,comm,%cpu,%mem --sort=-%mem --no-headers | head -5
echo '===FAILED==='; systemctl list-units --type=service --state=failed --no-legend --plain
echo '===MODEL==='; (cat /proc/device-tree/model 2>/dev/null | tr -d '\0'; echo)
echo '===TEMP==='; if command -v vcgencmd >/dev/null 2>&1; then vcgencmd measure_temp; else cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; fi
echo '===THROTTLED==='; (command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled) || echo n/a
echo '===FTL==='; systemctl is-active pihole-FTL 2>/dev/null || true
echo '===UNBOUND==='; echo "$(systemctl is-active unbound 2>/dev/null) $(systemctl is-enabled unbound 2>/dev/null)"
"""


def _sections(raw):
    parts = re.split(r"===(\w+)===\n", raw)[1:]  # drop leading empty chunk
    return {name: body for name, body in zip(parts[0::2], parts[1::2])}


def _fetch_remote():
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "bazza", REMOTE_SCRIPT],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh bazza failed: {out.stderr.strip()}")
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


def _parse_temp(block):
    """vcgencmd prints `temp=47.2'C`; the thermal_zone fallback prints raw
    millidegrees (`47234`)."""
    s = block.strip()
    if not s:
        return None
    m = re.search(r"temp=([\d.]+)", s)
    if m:
        return round(float(m.group(1)), 1)
    try:
        raw = int(s.splitlines()[0])
        return round(raw / 1000, 1)
    except (ValueError, IndexError):
        return None


# `vcgencmd get_throttled` bitfield. Low bits = happening right now, bits 16-19 =
# has-occurred-since-boot (sticky until reboot).
THROTTLE_BITS = {
    0:  ("now",  "critical", "Under-voltage detected", "the PSU can't hold 5V under load — DNS will still work but the SD card is at risk"),
    1:  ("now",  "warn",     "ARM frequency capped",   "the CPU is being held below its rated clock"),
    2:  ("now",  "critical", "Currently throttled",    "the SoC is actively throttling"),
    3:  ("now",  "warn",     "Soft temperature limit", "the soft thermal limit is active"),
    16: ("past", "warn",     "Under-voltage since boot", "at least one brown-out has happened since the last reboot"),
    17: ("past", "warn",     "Frequency capping since boot", "the CPU has been clock-capped at some point since boot"),
    18: ("past", "warn",     "Throttling since boot",  "the SoC has throttled at some point since boot"),
    19: ("past", "warn",     "Temp limit hit since boot", "the soft temperature limit has been reached since boot"),
}


def _parse_throttled(block):
    s = block.strip()
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", s)
    if not m:
        return {"available": False, "raw": None, "flags": []}
    value = int(m.group(1), 16)
    flags = []
    for bit, (when, level, header, detail) in THROTTLE_BITS.items():
        if value & (1 << bit):
            flags.append({"bit": bit, "when": when, "level": level,
                          "header": header, "detail": detail})
    return {"available": True, "raw": m.group(1), "value": value, "flags": flags}


def _parse_dns(ftl_block, unbound_block, now):
    """DNS panel data. Service state (pihole-FTL, unbound) comes from the remote
    `systemctl is-active`; the query / blocking / gravity numbers are lifted from
    data/pihole.json, which pollers/pihole.py keeps fresh on its own cron."""
    ftl_active = ftl_block.strip() == "active"

    ub_parts = unbound_block.split()
    ub_active = bool(ub_parts) and ub_parts[0] == "active"
    ub_enabled = ub_parts[1] if len(ub_parts) > 1 else "unknown"
    # Only meaningful to alert on unbound if it's actually a managed unit here.
    ub_known = ub_enabled in ("enabled", "static", "disabled", "indirect")

    dns = {
        "ftl_active": ftl_active,
        "unbound_active": ub_active,
        "unbound_known": ub_known,
        "stale": True,
        "age_s": None,
    }

    try:
        p = json.loads(PIHOLE_JSON.read_text())
    except Exception:
        return dns

    age = now - p.get("ts", 0)
    dns["age_s"] = age
    dns["stale"] = age > DNS_STALE_SECONDS

    s = p.get("summary", {}) or {}
    dns.update({
        "total": s.get("total"),
        "blocked": s.get("blocked"),
        "percent_blocked": s.get("percent_blocked"),
        "cached": s.get("cached"),
        "forwarded": s.get("forwarded"),
        "unique_domains": s.get("unique_domains"),
    })
    g = s.get("gravity", {}) or {}
    dns["gravity_domains"] = g.get("domains")
    dns["gravity_last_update"] = g.get("last_update")

    tb = p.get("top_blocked") or []
    dns["top_blocked"] = {"domain": tb[0]["domain"], "count": tb[0]["count"]} if tb else None
    tc = p.get("top_clients") or []
    dns["top_client"] = {"name": tc[0].get("name") or tc[0].get("ip"), "count": tc[0]["count"]} if tc else None

    return dns


def _log_history(ts, load1, mem_pct, disk_pct, cpu_temp):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metrics_log "
        "(ts INTEGER, load1 REAL, mem_pct REAL, disk_pct REAL, cpu_temp REAL)"
    )
    conn.execute(
        "INSERT INTO metrics_log (ts, load1, mem_pct, disk_pct, cpu_temp) VALUES (?, ?, ?, ?, ?)",
        (ts, load1, mem_pct, disk_pct, cpu_temp),
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

    temp = data.get("cpu_temp")
    if temp is not None and temp >= TEMP_CRIT_C:
        candidates["temp_high"] = ("critical", "SoC running hot",
            f"CPU temperature is {temp} °C — the Pi throttles hard around 80-85 °C")
    elif temp is not None and temp >= TEMP_WARN_C:
        candidates["temp_high"] = ("warn", "SoC warm",
            f"CPU temperature is {temp} °C")

    for f in data["throttled"]["flags"]:
        key = f"throttle_{f['bit']}"
        candidates[key] = (f["level"], f["header"], f["detail"])

    for unit in data["other_failed"]:
        candidates[f"failed_{unit}"] = ("warn", "Unit failed", f"{unit} is in a failed state")

    dns = data["dns"]
    if not dns.get("ftl_active"):
        candidates["ftl_down"] = ("critical", "Pi-hole FTL not running",
            "pihole-FTL is not active — DNS resolution and ad-blocking are down for the whole LAN")
    if dns.get("unbound_known") and not dns.get("unbound_active"):
        candidates["unbound_down"] = ("warn", "unbound not running",
            "the recursive resolver unbound is inactive — Pi-hole has lost its private upstream")

    if dns.get("age_s") is not None and dns["age_s"] > DNS_STALE_SECONDS:
        candidates["dns_stale"] = ("warn", "Pi-hole poller stale",
            f"pihole.json hasn't refreshed in {round(dns['age_s'] / 60)} min — the DNS figures may be old")

    glu = dns.get("gravity_last_update")
    if glu and now - glu > GRAVITY_STALE_SECONDS:
        candidates["gravity_stale"] = ("warn", "Gravity list is old",
            f"the blocklist was last rebuilt {round((now - glu) / 86400)} days ago")

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
        s = _fetch_remote()
        load = _parse_load(s["LOADAVG"], s["NPROC"])
        mem = _parse_mem(s["MEMINFO"])
        disk = _parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = _parse_procs(s["TOPCPU"])
        top_mem = _parse_procs(s["TOPMEM"])
        other_failed = _parse_failed(s["FAILED"])
        model = s["MODEL"].strip() or "Raspberry Pi"
        cpu_temp = _parse_temp(s["TEMP"])
        throttled = _parse_throttled(s["THROTTLED"])
        dns = _parse_dns(s.get("FTL", ""), s.get("UNBOUND", ""), ts)
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"bazza poll failed: {e}")
        return

    data = {
        "ts": ts,
        "hostname": "bazza",
        "model": model,
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "cpu_temp": cpu_temp,
        "throttled": throttled,
        "dns": dns,
        "other_failed": other_failed,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = _load_state()
    alerts, active_alerts = _build_alerts(data, ts, prev)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    _log_history(ts, load["1m"], mem["percent"], disk["percent"], cpu_temp)
    _save_state(active_alerts)

    blk = f"{dns['percent_blocked']}% blocked" if dns.get("percent_blocked") is not None else "no dns data"
    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C, "
          f"FTL {'up' if dns.get('ftl_active') else 'DOWN'}, {blk}")


if __name__ == "__main__":
    fetch_and_write()
