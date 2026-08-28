"""Writes jeff.json for the web dashboard — CPU/mem/disk/uptime for the jeff host
(a Raspberry Pi earmarked for SDR work), gathered over SSH like wacky.py.

On top of the shared host-metrics shape this also reports Pi-specific health
(SoC temperature, under-voltage / throttling flags from `vcgencmd get_throttled`)
and a best-effort survey of the RTL-SDR setup (dongle present on USB, whether the
DVB kernel driver is squatting it, which rtl_* tools and SDR services exist).

Metrics history goes to jeff_history.db, same schema as steve_history.db /
wacky_history.db so the trend chart code is shared.
"""

import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "jeff.json"
STATE_FILE = DATA / "jeff_state.json"
DB_FILE    = DATA / "jeff_history.db"
HISTORY_RETENTION_SECONDS = 48 * 3600

SSH_TIMEOUT = 15

# ── Alert thresholds (disk/mem/load match steve.py & wacky.py) ──
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
DISK_GROWTH_PCT_PER_HOUR = 5
MEM_WARN_PCT = 90
LOAD_RATIO_WARN = 2.0
TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0

# RTL2832U-based dongles (incl. the R820T2 stick jeff is for) enumerate under
# Realtek vendor 0bda, product 2832 or 2838.
RTL_USB_RE = re.compile(r"0bda:28(32|38)|RTL28(32|38)|Realtek.*(283[28]|DVB-T)", re.I)

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
echo '===USB==='; lsusb 2>/dev/null
echo '===RTLMODS==='; lsmod 2>/dev/null | awk '{print $1}' | grep -E '^(rtl2832|rtl2838|dvb_usb_rtl28xxu|rtl8xxxu)$' || true
echo '===RTLTOOLS==='; for t in rtl_test rtl_sdr rtl_fm rtl_tcp rtl_power rtl_433 rtl_adsb; do command -v $t >/dev/null 2>&1 && echo $t; done
echo '===SDRSVC==='; systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -iE 'rtl|sdr|dump1090|readsb|acars|dumpvdl|dump978|satdump|spyserver|soapy|gqrx|piaware|fr24feed|rbfeeder|adsb' || true
"""


def _sections(raw):
    parts = re.split(r"===(\w+)===\n", raw)[1:]  # drop leading empty chunk
    return {name: body for name, body in zip(parts[0::2], parts[1::2])}


def _fetch_remote():
    out = subprocess.run(
        ["ssh", "jeff", REMOTE_SCRIPT],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh jeff failed: {out.stderr.strip()}")
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
    0:  ("now",  "critical", "Under-voltage detected", "the PSU can't hold 5V under load — SDR captures will be corrupt"),
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


def _parse_sdr(usb_block, mods_block, tools_block, svc_block):
    usb_lines = [ln for ln in usb_block.splitlines() if ln.strip()]
    dongle_line = next((ln for ln in usb_lines if RTL_USB_RE.search(ln)), None)
    dongle_name = None
    if dongle_line:
        # lsusb: "Bus 001 Device 004: ID 0bda:2838 Realtek ... RTL2838 DVB-T"
        parts = dongle_line.split("ID ", 1)
        dongle_name = parts[1].strip() if len(parts) > 1 else dongle_line.strip()

    mods = [m for m in mods_block.split() if m]
    return {
        "dongle_present": dongle_line is not None,
        "dongle_name": dongle_name,
        # The DVB-T driver grabs an RTL2832 stick on plug-in and blocks librtlsdr
        # until it's blacklisted — the classic "SDR doesn't work" gotcha.
        "dvb_driver_loaded": "dvb_usb_rtl28xxu" in mods,
        "sdr_driver_loaded": any(m in ("rtl2832", "rtl2838") for m in mods),
        "tools": [t for t in tools_block.split() if t],
        "services": [s for s in svc_block.split() if s],
    }


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

    if data["sdr"]["dongle_present"] and data["sdr"]["dvb_driver_loaded"] \
            and not data["sdr"]["sdr_driver_loaded"]:
        candidates["dvb_squat"] = ("warn", "DVB driver has the SDR dongle",
            "dvb_usb_rtl28xxu is loaded — blacklist it so librtlsdr can claim the stick")

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
        sdr = _parse_sdr(s["USB"], s["RTLMODS"], s["RTLTOOLS"], s["SDRSVC"])
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"jeff poll failed: {e}")
        return

    data = {
        "ts": ts,
        "hostname": "jeff",
        "model": model,
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "cpu_temp": cpu_temp,
        "throttled": throttled,
        "sdr": sdr,
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

    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C, "
          f"sdr {'present' if sdr['dongle_present'] else 'absent'}")


if __name__ == "__main__":
    fetch_and_write()
