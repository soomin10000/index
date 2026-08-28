"""Shared host-monitor plumbing for the steve / wacky / jeff / bazza pollers.

Each of those writes data/<host>.json + data/<host>_history.db for its detail
card. They were copy-paste forks; the parsing, the SSH fetch, the metrics-history
DB and the common alert rules live here now. Each poller keeps only its
distinctive part (steve: local /proc + systemd service health + notify; wacky:
fail2ban/geoip; jeff: SDR/ADS-B; bazza: DNS panel).

Imported as a bare module (`from hostlib import ...`) after the poller puts its
own directory on sys.path — same convention as pihole_client / atlas_client.
"""

import json
import re
import sqlite3
import subprocess

HISTORY_RETENTION_SECONDS = 48 * 3600
SSH_TIMEOUT = 15

# ── Alert thresholds (shared by all four host cards) ──
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
DISK_GROWTH_PCT_PER_HOUR = 5      # flags e.g. a runaway log filling the disk
MEM_WARN_PCT = 90
LOAD_RATIO_WARN = 2.0             # load-1m / cpu count

# `vcgencmd get_throttled` bitfield. Low bits = happening right now, bits 16-19 =
# has-occurred-since-boot (sticky until reboot). bazza overrides bit 0's detail
# string (SD-card risk vs SDR-capture corruption) by passing its own dict.
THROTTLE_BITS = {
    0:  ("now",  "critical", "Under-voltage detected", "the PSU can't hold 5V under load — SDR captures will be corrupt"),
    1:  ("now",  "warn",     "ARM frequency capped",   "the CPU is being held below its rated clock"),
    2:  ("now",  "critical", "Currently throttled",    "the SoC is actively throttling"),
    3:  ("now",  "warn",     "Soft temperature limit", "the soft thermal limit is active"),
    16: ("past", "warn",     "Under-voltage since boot", "at least one brown-out has happened since the last reboot"),
    17: ("past", "warn",     "Frequency capping since boot", "the CPU has been clock-capped at some point since boot"),
    18: ("past", "warn",     "Throttling since boot",  "the SoC has throttled at some point since boot"),
    19: ("past", "warn",     "Temp limit hit since boot", "the soft temperature limit has been reached since boot"),
}


# ── remote command output ────────────────────────────────────────────────────
def sections(raw):
    parts = re.split(r"===(\w+)===\n", raw)[1:]  # drop leading empty chunk
    return {name: body for name, body in zip(parts[0::2], parts[1::2])}


def fetch_remote(host, script, timeout=SSH_TIMEOUT):
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, script],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh {host} failed: {out.stderr.strip()}")
    return sections(out.stdout)


# ── parsers (fed either SSH section text or, on steve, local file text) ───────
def parse_mem(block):
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


def parse_disk(block):
    line = block.strip().splitlines()[-1]
    total, used = (int(x) for x in line.split())
    return {
        "total_gb": round(total / 1e9, 1),
        "used_gb": round(used / 1e9, 1),
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def parse_load(loadavg_block, nproc_block):
    one, five, fifteen = (float(x) for x in loadavg_block.split()[:3])
    cpus = int(nproc_block.strip())
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2), "cpus": cpus}


def parse_procs(block, n=5):
    procs = []
    for line in block.strip().splitlines()[:n]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, comm, cpu, mem = parts
        procs.append({"pid": pid, "name": comm, "cpu": float(cpu), "mem": float(mem)})
    return procs


def parse_failed(block):
    failed = []
    for line in block.strip().splitlines():
        parts = line.split()
        if parts:
            failed.append(parts[0])
    return failed


def parse_temp(block):
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


def parse_throttled(block, bits=THROTTLE_BITS):
    s = block.strip()
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", s)
    if not m:
        return {"available": False, "raw": None, "flags": []}
    value = int(m.group(1), 16)
    flags = []
    for bit, (when, level, header, detail) in bits.items():
        if value & (1 << bit):
            flags.append({"bit": bit, "when": when, "level": level,
                          "header": header, "detail": detail})
    return {"available": True, "raw": m.group(1), "value": value, "flags": flags}


# ── per-run state file ──────────────────────────────────────────────────────
def load_state(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_state(path, payload):
    path.write_text(json.dumps(payload))


# ── metrics history DB ──────────────────────────────────────────────────────
_NO_TEMP = object()


def log_history(db_path, ts, load1, mem_pct, disk_pct, cpu_temp=_NO_TEMP):
    """Append one row to `metrics_log` and prune past the retention window.
    Omit `cpu_temp` for the 4-column schema (steve, wacky); pass it (even as
    None) for the 5-column schema (jeff, bazza)."""
    with_temp = cpu_temp is not _NO_TEMP
    conn = sqlite3.connect(db_path)
    if with_temp:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics_log "
            "(ts INTEGER, load1 REAL, mem_pct REAL, disk_pct REAL, cpu_temp REAL)"
        )
        conn.execute(
            "INSERT INTO metrics_log (ts, load1, mem_pct, disk_pct, cpu_temp) VALUES (?, ?, ?, ?, ?)",
            (ts, load1, mem_pct, disk_pct, cpu_temp),
        )
    else:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics_log "
            "(ts INTEGER, load1 REAL, mem_pct REAL, disk_pct REAL)"
        )
        conn.execute(
            "INSERT INTO metrics_log (ts, load1, mem_pct, disk_pct) VALUES (?, ?, ?, ?)",
            (ts, load1, mem_pct, disk_pct),
        )
    conn.execute("DELETE FROM metrics_log WHERE ts < ?", (ts - HISTORY_RETENTION_SECONDS,))
    conn.commit()
    conn.close()


def disk_pct_hour_ago(db_path, now):
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT disk_pct FROM metrics_log WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (now - 3300,),  # ~55 min, tolerant of the 2-minute poll cadence
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ── alerts ──────────────────────────────────────────────────────────────────
def failed_unit_alerts(units, header="Unit failed"):
    """{key: candidate} for each systemd unit in a failed state. Kept a separate
    helper (not folded into build_alerts) so each poller controls where these sit
    in its candidate order relative to its own extras — the alert list is a stable
    sort, so insertion order is the tiebreak for same-onset alerts."""
    return {f"failed_{u}": ("warn", header, f"{u} is in a failed state") for u in units}


def build_alerts(data, now, prev, db_path, extra=None):
    """The disk / disk-growth / memory / load rules every host card shares, then a
    per-host `extra` dict of {key: (level, header, text)} (which the caller has
    already merged its failed-unit + host-specific candidates into, in order).
    Returns (alerts, active_alerts) with onset timestamps carried forward from
    `prev`."""
    prev_active = (prev or {}).get("active_alerts", {})
    candidates = {}  # key -> (level, header, text)

    disk, mem, load = data["disk"], data["mem"], data["load"]
    if disk["percent"] >= DISK_CRIT_PCT:
        candidates["disk_high"] = ("critical", "Disk almost full",
            f"/ is {disk['percent']}% full ({disk['used_gb']}/{disk['total_gb']} GB)")
    elif disk["percent"] >= DISK_WARN_PCT:
        candidates["disk_high"] = ("warn", "Disk usage high",
            f"/ is {disk['percent']}% full ({disk['used_gb']}/{disk['total_gb']} GB)")

    hour_ago = disk_pct_hour_ago(db_path, now)
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

    if extra:
        candidates.update(extra)

    active_alerts = {}
    alerts = []
    for key, (level, header, text) in candidates.items():
        onset = prev_active.get(key, {}).get("ts", now)
        active_alerts[key] = {"ts": onset, "level": level, "header": header, "text": text}
        alerts.append({"id": key, "ts": onset, "level": level, "header": header, "text": text})
    alerts.sort(key=lambda a: a["ts"], reverse=True)

    return alerts, active_alerts
