"""Writes ickle.json for the web dashboard — CPU/mem/disk/uptime for ickle (a
Mac mini project box on the LAN), gathered over SSH.

macOS has none of /proc, systemd or nproc, so this can't reuse hostlib's Linux
parsers the way steve/wacky/jeff/bazza do — it pulls the same shape (load/mem/
disk/uptime/top-procs) out of sysctl/vm_stat/df/ps instead, then hands off to
the shared hostlib alerting + history-DB plumbing, which only cares about the
resulting dict shape, not where the numbers came from.

Metrics history goes to ickle_history.db (4-column schema, no cpu_temp — Macs
don't expose an SoC temperature the way a Pi's vcgencmd does).
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "ickle.json"
STATE_FILE = DATA / "ickle_state.json"
DB_FILE    = DATA / "ickle_history.db"

REMOTE_SCRIPT = r"""
echo '===LOADAVG==='; sysctl -n vm.loadavg
echo '===NPROC==='; sysctl -n hw.ncpu
echo '===MEMSIZE==='; sysctl -n hw.memsize
echo '===PAGESIZE==='; sysctl -n hw.pagesize
echo '===VMSTAT==='; vm_stat
echo '===DISK==='; df -k / | tail -1
echo '===BOOTTIME==='; sysctl -n kern.boottime
echo '===TOPCPU==='; ps -Aceo pid,pcpu,pmem,comm -r | tail -n +2 | head -5
echo '===TOPMEM==='; ps -Aceo pid,pcpu,pmem,comm -m | tail -n +2 | head -5
echo '===MODEL==='; sysctl -n hw.model
echo '===OSVER==='; sw_vers -productVersion
"""


def _parse_load(loadavg_block, nproc_block):
    """`sysctl -n vm.loadavg` prints `{ 1.20 1.35 1.40 }` — no /proc/loadavg here."""
    nums = re.findall(r"[\d.]+", loadavg_block)
    one, five, fifteen = (float(x) for x in nums[:3])
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2),
             "cpus": int(nproc_block.strip())}


def _parse_mem(memsize_block, pagesize_block, vmstat_block):
    """No MemAvailable on macOS — approximate "used" the same way Activity
    Monitor's memory pressure roughly does: active + wired + compressed pages.
    Free/inactive/speculative pages are reclaimable and don't count as used."""
    total = int(memsize_block.strip())
    pagesize = int(pagesize_block.strip())
    stats = {}
    for line in vmstat_block.splitlines():
        m = re.match(r"^(Pages [a-zA-Z ]+|.*compressor):\s+([\d.]+)", line)
        if m:
            stats[m.group(1).strip()] = int(float(m.group(2)))
    used = (stats.get("Pages active", 0) + stats.get("Pages wired down", 0)
             + stats.get("Pages occupied by compressor", 0)) * pagesize
    return {
        "total_mb": round(total / 1024 / 1024, 1),
        "used_mb": round(used / 1024 / 1024, 1),
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def _parse_disk(block):
    parts = block.split()
    total_kb, used_kb = int(parts[1]), int(parts[2])
    return {
        "total_gb": round(total_kb * 1024 / 1e9, 1),
        "used_gb": round(used_kb * 1024 / 1e9, 1),
        "percent": round(used_kb / total_kb * 100, 1) if total_kb else 0.0,
    }


def _parse_uptime(boottime_block, now):
    m = re.search(r"sec\s*=\s*(\d+)", boottime_block)
    return float(now - int(m.group(1))) if m else 0.0


def _parse_procs(block, n=5):
    """Can't reuse hostlib.parse_procs' `pid comm cpu mem` column order — macOS
    labels SSH login sessions `sshd-session: <user>`, a comm value with an
    embedded space, so comm has to be the trailing (whitespace-tolerant) field
    here instead of hostlib's Linux ordering, which assumes only the last
    column (mem) can contain spaces."""
    procs = []
    for line in block.strip().splitlines()[:n]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, mem, comm = parts
        procs.append({"pid": pid, "name": comm.strip(), "cpu": float(cpu), "mem": float(mem)})
    return procs


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("ickle", REMOTE_SCRIPT)
        load = _parse_load(s["LOADAVG"], s["NPROC"])
        mem = _parse_mem(s["MEMSIZE"], s["PAGESIZE"], s["VMSTAT"])
        disk = _parse_disk(s["DISK"])
        uptime_seconds = _parse_uptime(s["BOOTTIME"], ts)
        top_cpu = _parse_procs(s["TOPCPU"])
        top_mem = _parse_procs(s["TOPMEM"])
        model = s["MODEL"].strip() or "Mac mini"
        os_ver = s["OSVER"].strip()
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"ickle poll failed: {e}")
        return

    data = {
        "ts": ts,
        "hostname": "ickle",
        "model": model,
        "os_version": os_ver,
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = hostlib.load_state(STATE_FILE)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"])
    hostlib.save_state(STATE_FILE, {"active_alerts": active_alerts})

    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, disk {disk['percent']}%")


if __name__ == "__main__":
    fetch_and_write()
