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

OUT        = Path(__file__).parent / "steve.json"
STATE_FILE = Path(__file__).parent / "steve_state.json"
DB_FILE    = Path(__file__).parent / "steve_history.db"
HISTORY_RETENTION_SECONDS = 48 * 3600

# The home-stack services running on steve — not the generic OS units.
SERVICES = [
    "bad-parents", "bbc-spoofer", "bettercap", "darren",
    "eufy-listener", "eufy-security-ws", "home-menu", "kismet",
    "next-train", "unifi-poller", "weather",
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


def _save_state(down, restarts):
    STATE_FILE.write_text(json.dumps({"down": sorted(down), "restarts": restarts}))


def _handle_notifications(services):
    curr_down = {s["name"] for s in services if s["active"] != "active"}
    curr_restarts = {s["name"]: s["restarts"] for s in services}

    prev = _load_state()
    if prev is None:
        # First run — seed the baseline without notifying, to avoid a burst
        # of alerts for pre-existing state at deploy time.
        _save_state(curr_down, curr_restarts)
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

    _save_state(curr_down, curr_restarts)


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

    OUT.write_text(json.dumps(data, indent=2))
    _log_history(ts, load["1m"], mem["percent"], disk["percent"])
    _handle_notifications(services)

    status = f"{len(down)} down" if down else "all up"
    print(f"Saved {OUT} — {status}, {len(restarted)} with past restarts, "
          f"load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
