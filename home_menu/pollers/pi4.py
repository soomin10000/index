"""Writes pi4.json for the web dashboard — CPU/mem/disk/uptime for pi4 (a
Raspberry Pi 4 Model B being scoped as a possible second SDR host), gathered
over SSH.

No SDR dongle attached yet (see project_pi4_new_host memory) — this is just
the plain host-health card (load/mem/disk/temp/throttling), same shape as
bazza/weeny minus their DNS/honeypot specifics. If pi4 later picks up SDR
duties, fold in jeff.py's SDR survey the same way jeff itself does.

Metrics history goes to pi4_history.db (5-column schema, with cpu_temp).
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "pi4.json"
STATE_FILE = DATA / "pi4_state.json"
DB_FILE    = DATA / "pi4_history.db"

TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0

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
"""


def _extra_alerts(data):
    """pi4-specific alert candidates: SoC temp, throttle flags, failed units."""
    extra = {}

    temp = data.get("cpu_temp")
    if temp is not None and temp >= TEMP_CRIT_C:
        extra["temp_high"] = ("critical", "SoC running hot",
            f"CPU temperature is {temp} °C — the Pi throttles hard around 80-85 °C")
    elif temp is not None and temp >= TEMP_WARN_C:
        extra["temp_high"] = ("warn", "SoC warm", f"CPU temperature is {temp} °C")

    for f in data["throttled"]["flags"]:
        extra[f"throttle_{f['bit']}"] = (f["level"], f["header"], f["detail"])

    extra.update(hostlib.failed_unit_alerts(data["other_failed"]))
    return extra


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("pi4", REMOTE_SCRIPT)
        load = hostlib.parse_load(s["LOADAVG"], s["NPROC"])
        mem = hostlib.parse_mem(s["MEMINFO"])
        disk = hostlib.parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = hostlib.parse_procs(s["TOPCPU"])
        top_mem = hostlib.parse_procs(s["TOPMEM"])
        other_failed = hostlib.parse_failed(s["FAILED"])
        model = s["MODEL"].strip() or "Raspberry Pi 4"
        cpu_temp = hostlib.parse_temp(s["TEMP"])
        throttled = hostlib.parse_throttled(s["THROTTLED"])
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"pi4 poll failed: {e}")
        return

    data = {
        "ts": ts,
        "hostname": "pi4",
        "model": model,
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "cpu_temp": cpu_temp,
        "throttled": throttled,
        "other_failed": other_failed,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = hostlib.load_state(STATE_FILE)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE, _extra_alerts(data))
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"], cpu_temp)
    hostlib.save_state(STATE_FILE, {"active_alerts": active_alerts})

    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C")


if __name__ == "__main__":
    fetch_and_write()
