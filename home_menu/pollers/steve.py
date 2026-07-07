"""Writes steve.json for the web dashboard — CPU/mem/disk/uptime + home-stack service health.

Also logs metrics history to steve_history.db and notifies (via notify_sender) on
service down/resolved/crash-restart transitions, using steve_state.json to remember
what was already reported across cron runs.
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / "ubuntu-sender"))

try:
    from notify_sender import notify as _notify
except ImportError:
    def _notify(title, message, **kwargs):
        print(f"notify_sender unavailable — would have sent: [{title}] {message}")

DATA = Path(__file__).resolve().parent.parent / "data"
LOGS = Path(__file__).resolve().parent.parent / "logs"

OUT        = DATA / "steve.json"
STATE_FILE = DATA / "steve_state.json"
DB_FILE    = DATA / "steve_history.db"
HISTORY_RETENTION_SECONDS = 48 * 3600

# The home-stack services running on steve — not the generic OS units.
SERVICES = [
    "bad-parents", "bbc-spoofer", "bettercap", "darren",
    "eufy-listener", "eufy-security-ws", "home-menu", "kismet",
    "next-train", "unifi-poller", "weather",
]

# ── Alert thresholds ──
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
DISK_GROWTH_PCT_PER_HOUR = 5      # flags e.g. a runaway log filling the disk
MEM_WARN_PCT = 90
LOAD_RATIO_WARN = 2.0             # load-1m / cpu count
CRASH_RESTART_THRESHOLD = 3       # total NRestarts before it's "repeated", not a one-off
LOG_GROWTH_MB_PER_HOUR = 20
LOG_SIZE_WARN_MB = 300
LOG_SIZE_CRIT_MB = 800
# Logs most likely to fill the disk: syslog catches driver/kernel spam (see the
# rtl88XXau adapter issue), the rest are this project's own unrotated poller logs.
WATCHED_LOGS = [
    "/var/log/syslog",
    "/var/log/kern.log",
    str(LOGS / "kismet.log"),
    str(LOGS / "pihole.log"),
    str(LOGS / "steve.log"),
    str(LOGS / "unifi.log"),
]


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def _mem():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.strip().split()[0])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def _disk():
    import shutil
    du = shutil.disk_usage("/")
    return {
        "total_gb": round(du.total / 1e9, 1),
        "used_gb": round(du.used / 1e9, 1),
        "percent": round(du.used / du.total * 100, 1),
    }


def _load():
    import os
    one, five, fifteen = os.getloadavg()
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2), "cpus": os.cpu_count()}


def _uptime_seconds():
    return float(Path("/proc/uptime").read_text().split()[0])


def _services():
    units = [f"{s}.service" for s in SERVICES]
    out = _run([
        "systemctl", "show", *units,
        "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
        "-p", "ActiveEnterTimestamp", "-p", "LoadState",
    ])
    blocks = out.strip("\n").split("\n\n")
    results = []
    for name, block in zip(SERVICES, blocks):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        results.append({
            "name": name,
            "active": props.get("ActiveState", "unknown"),
            "sub": props.get("SubState", "unknown"),
            "restarts": int(props.get("NRestarts", 0) or 0),
            "since": props.get("ActiveEnterTimestamp", ""),
            "loaded": props.get("LoadState") == "loaded",
        })
    return results


def _other_failed(watched):
    out = _run(["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--plain"])
    watched_units = {f"{s}.service" for s in watched}
    failed = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if unit not in watched_units:
            failed.append(unit)
    return failed


def _top_processes(sort_key, n=5):
    out = _run(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=" + sort_key, "--no-headers"])
    procs = []
    for line in out.splitlines()[:n]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, comm, cpu, mem = parts
        procs.append({"pid": pid, "name": comm, "cpu": float(cpu), "mem": float(mem)})
    return procs


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


def _save_state(down, restarts, active_alerts, log_sizes):
    STATE_FILE.write_text(json.dumps({
        "down": sorted(down),
        "restarts": restarts,
        "active_alerts": active_alerts,
        "log_sizes": log_sizes,
    }))


def _handle_notifications(services, prev):
    curr_down = {s["name"] for s in services if s["active"] != "active"}
    curr_restarts = {s["name"]: s["restarts"] for s in services}

    if prev is None:
        # First run — seed the baseline without notifying, to avoid a burst
        # of alerts for pre-existing state at deploy time.
        return

    prev_down = set(prev.get("down", []))
    prev_restarts = prev.get("restarts", {})

    for name in curr_down - prev_down:
        _notify("steve: service down", f"{name} is not active", sound=True)
    for name in prev_down - curr_down:
        _notify("steve: service recovered", f"{name} is back up", sound=False)
    for name, count in curr_restarts.items():
        if name not in curr_down and count > prev_restarts.get(name, count):
            _notify("steve: service restarted", f"{name} restarted (NRestarts {prev_restarts.get(name, 0)} -> {count})", sound=True)


def _disk_pct_hour_ago(now):
    if not DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute(
            "SELECT disk_pct FROM metrics_log WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (now - 3300,),  # ~55 min, tolerant of the 2-minute poll cadence
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _log_growth(now, prev_sizes):
    """Size + growth rate (MB/hr) for each watched log, vs. its size last run."""
    results = []
    new_sizes = {}
    for path in WATCHED_LOGS:
        try:
            size = Path(path).stat().st_size
        except OSError:
            continue
        new_sizes[path] = {"size": size, "ts": now}
        prev = prev_sizes.get(path)
        rate = 0.0
        if prev and now > prev["ts"]:
            hours = (now - prev["ts"]) / 3600
            rate = max(0.0, (size - prev["size"]) / 1e6 / hours)
        results.append((path, size, rate))
    return results, new_sizes


def _build_alerts(data, now, prev):
    prev_active = (prev or {}).get("active_alerts", {})
    prev_log_sizes = (prev or {}).get("log_sizes", {})

    candidates = {}  # key -> (level, header, text)

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

    for s in data["services"]:
        if s["restarts"] >= CRASH_RESTART_THRESHOLD:
            candidates[f"crash_{s['name']}"] = ("critical", "Repeated crashes",
                f"{s['name']} has restarted {s['restarts']} times")

    for unit in data["other_failed"]:
        candidates[f"failed_{unit}"] = ("warn", "Unmonitored unit failed", f"{unit} is in a failed state")

    log_growth, new_log_sizes = _log_growth(now, prev_log_sizes)
    for path, size, rate in log_growth:
        size_mb = size / 1e6
        if size_mb >= LOG_SIZE_CRIT_MB:
            level = "critical"
        elif size_mb >= LOG_SIZE_WARN_MB or rate >= LOG_GROWTH_MB_PER_HOUR:
            level = "warn"
        else:
            continue
        candidates[f"log_{path}"] = (level, "Log file filling",
            f"{path} is {size_mb:.0f} MB" + (f", growing ~{rate:.0f} MB/hr" if rate > 0 else ""))

    active_alerts = {}
    alerts = []
    for key, (level, header, text) in candidates.items():
        onset = prev_active.get(key, {}).get("ts", now)
        active_alerts[key] = {"ts": onset, "level": level, "header": header, "text": text}
        alerts.append({"id": key, "ts": onset, "level": level, "header": header, "text": text})
    alerts.sort(key=lambda a: a["ts"], reverse=True)

    return alerts, active_alerts, new_log_sizes


def fetch_and_write():
    services = _services()
    down = [s for s in services if s["active"] != "active"]
    restarted = [s for s in services if s["active"] == "active" and s["restarts"] > 0]

    load = _load()
    mem = _mem()
    disk = _disk()
    ts = int(time.time())

    data = {
        "ts": ts,
        "hostname": "steve",
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": _uptime_seconds(),
        "services": services,
        "other_failed": _other_failed(SERVICES),
        "top_cpu": _top_processes("-%cpu"),
        "top_mem": _top_processes("-%mem"),
    }

    prev = _load_state()
    alerts, active_alerts, log_sizes = _build_alerts(data, ts, prev)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    _log_history(ts, load["1m"], mem["percent"], disk["percent"])
    _handle_notifications(services, prev)
    _save_state({s["name"] for s in down}, {s["name"]: s["restarts"] for s in services}, active_alerts, log_sizes)

    status = f"{len(down)} down" if down else "all up"
    print(f"Saved {OUT} — {status}, {len(restarted)} with past restarts, {len(alerts)} alerts, "
          f"load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
