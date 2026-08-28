"""Writes steve.json for the web dashboard — CPU/mem/disk/uptime + home-stack
service health. Unlike the other host pollers this runs ON steve, so it reads
/proc directly instead of over SSH, and it drives notify_sender pushes on
service down/resolved/crash-restart transitions (state kept in steve_state.json).

Shared metrics parsing / alert core / history DB live in hostlib.py.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

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

# The home-stack services running on steve — not the generic OS units.
SERVICES = [
    "bad-parents", "bbc-spoofer", "darren",
    "eufy-listener", "eufy-security-ws", "home-menu", "kismet",
    "next-train", "unifi-poller", "weather",
]

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


def _extra_alerts(data, now, prev):
    """steve-specific alert candidates, same insertion order as before: repeated
    crashes, failed units, filling logs. Also returns the fresh log-size map for
    the state file."""
    extra = {}

    for s in data["services"]:
        if s["restarts"] >= CRASH_RESTART_THRESHOLD:
            extra[f"crash_{s['name']}"] = ("critical", "Repeated crashes",
                f"{s['name']} has restarted {s['restarts']} times")

    extra.update(hostlib.failed_unit_alerts(data["other_failed"], "Unmonitored unit failed"))

    log_growth, new_log_sizes = _log_growth(now, (prev or {}).get("log_sizes", {}))
    for path, size, rate in log_growth:
        size_mb = size / 1e6
        if size_mb >= LOG_SIZE_CRIT_MB:
            level = "critical"
        elif size_mb >= LOG_SIZE_WARN_MB or rate >= LOG_GROWTH_MB_PER_HOUR:
            level = "warn"
        else:
            continue
        extra[f"log_{path}"] = (level, "Log file filling",
            f"{path} is {size_mb:.0f} MB" + (f", growing ~{rate:.0f} MB/hr" if rate > 0 else ""))

    return extra, new_log_sizes


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

    prev = hostlib.load_state(STATE_FILE)
    extra, log_sizes = _extra_alerts(data, ts, prev)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE, extra)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"])
    _handle_notifications(services, prev)
    hostlib.save_state(STATE_FILE, {
        "down": sorted({s["name"] for s in down}),
        "restarts": {s["name"]: s["restarts"] for s in services},
        "active_alerts": active_alerts,
        "log_sizes": log_sizes,
    })

    status = f"{len(down)} down" if down else "all up"
    print(f"Saved {OUT} — {status}, {len(restarted)} with past restarts, {len(alerts)} alerts, "
          f"load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
