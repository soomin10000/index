"""Writes jeff.json for the web dashboard — CPU/mem/disk/uptime for the jeff host
(a Raspberry Pi earmarked for SDR work), gathered over SSH.

On top of the shared host-metrics shape (see hostlib.py) this also reports
Pi-specific health (SoC temperature, under-voltage / throttling flags) and a
best-effort survey of the RTL-SDR setup (dongle present on USB, whether the DVB
kernel driver is squatting it, which rtl_* tools and SDR services exist).

Metrics history goes to jeff_history.db (5-column schema, with cpu_temp).
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "jeff.json"
STATE_FILE = DATA / "jeff_state.json"
DB_FILE    = DATA / "jeff_history.db"

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
echo '===ADSB==='; jq -c '{total:(.aircraft|length), pos:([.aircraft[]|select(.lat!=null)]|length)}' /run/readsb/aircraft.json 2>/dev/null || echo '{}'
echo '===ADSBSTATS==='; jq -c '{msgs_last_min:(.last1min.messages//0), max_dist_m:(.last1min.max_distance//0)}' /run/readsb/stats.json 2>/dev/null || echo '{}'
"""


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


def _parse_adsb(adsb_block, stats_block):
    """Live readsb numbers off /run/readsb/{aircraft,stats}.json (via jq on the
    remote). Returns None when readsb isn't running / jq is missing / the files
    aren't there — the block is just `{}` in that case."""
    try:
        a = json.loads(adsb_block.strip() or "{}")
    except Exception:
        a = {}
    try:
        s = json.loads(stats_block.strip() or "{}")
    except Exception:
        s = {}
    if a.get("total") is None:
        return None
    max_dist_m = s.get("max_dist_m") or 0
    return {
        "aircraft": a.get("total", 0),
        "positions": a.get("pos", 0),
        "msgs_per_sec": round((s.get("msgs_last_min") or 0) / 60, 1),
        "max_range_km": round(max_dist_m / 1000, 1) if max_dist_m else None,
    }


def _extra_alerts(data):
    """jeff-specific alert candidates, in the same insertion order as before:
    SoC temp, throttle flags, failed units, DVB squat, deaf readsb."""
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

    if data["sdr"]["dongle_present"] and data["sdr"]["dvb_driver_loaded"] \
            and not data["sdr"]["sdr_driver_loaded"]:
        extra["dvb_squat"] = ("warn", "DVB driver has the SDR dongle",
            "dvb_usb_rtl28xxu is loaded — blacklist it so librtlsdr can claim the stick")

    adsb = data.get("adsb")
    readsb_up = any("readsb" in s for s in data["sdr"]["services"])
    if readsb_up and adsb and adsb["aircraft"] == 0 and adsb["msgs_per_sec"] == 0:
        extra["readsb_deaf"] = ("warn", "readsb is hearing nothing",
            "readsb is running but 0 aircraft and 0 msg/s — check the antenna and coax")

    return extra


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("jeff", REMOTE_SCRIPT)
        load = hostlib.parse_load(s["LOADAVG"], s["NPROC"])
        mem = hostlib.parse_mem(s["MEMINFO"])
        disk = hostlib.parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = hostlib.parse_procs(s["TOPCPU"])
        top_mem = hostlib.parse_procs(s["TOPMEM"])
        other_failed = hostlib.parse_failed(s["FAILED"])
        model = s["MODEL"].strip() or "Raspberry Pi"
        cpu_temp = hostlib.parse_temp(s["TEMP"])
        throttled = hostlib.parse_throttled(s["THROTTLED"])
        sdr = _parse_sdr(s["USB"], s["RTLMODS"], s["RTLTOOLS"], s["SDRSVC"])
        adsb = _parse_adsb(s.get("ADSB", ""), s.get("ADSBSTATS", ""))
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
        "adsb": adsb,
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

    adsb_note = f", {adsb['aircraft']} aircraft" if adsb else ""
    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C, "
          f"sdr {'present' if sdr['dongle_present'] else 'absent'}{adsb_note}")


if __name__ == "__main__":
    fetch_and_write()
