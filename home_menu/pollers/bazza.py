"""Writes bazza.json for the web dashboard — CPU/mem/disk/uptime for the bazza
host (the Raspberry Pi that runs the primary Pi-hole), gathered over SSH.

On top of the shared host-metrics shape (see hostlib.py) this also reports
Pi-specific health (SoC temperature, under-voltage / throttling flags) and a DNS
panel: the pihole-FTL / unbound service state comes over SSH, the query /
blocking / gravity figures are lifted from data/pihole.json (already refreshed
every 5 min by pollers/pihole.py — no second Pi-hole API session from here).

Metrics history goes to bazza_history.db (5-column schema, with cpu_temp).
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "bazza.json"
STATE_FILE = DATA / "bazza_state.json"
DB_FILE    = DATA / "bazza_history.db"
PIHOLE_JSON = DATA / "pihole.json"

TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0
# pihole.json is written by pollers/pihole.py on a */5 cron — anything past ~15 min
# means that poller (or bazza) is in trouble and the DNS figures below are stale.
DNS_STALE_SECONDS = 15 * 60
# Gravity is normally refreshed by the Sunday 04:00 cron; flag if it's well past.
GRAVITY_STALE_SECONDS = 10 * 24 * 3600

# bazza's take on throttle bit 0 (SD-card risk, not SDR-capture corruption).
BAZZA_THROTTLE_BITS = dict(hostlib.THROTTLE_BITS)
BAZZA_THROTTLE_BITS[0] = ("now", "critical", "Under-voltage detected",
    "the PSU can't hold 5V under load — DNS will still work but the SD card is at risk")

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


def _extra_alerts(data, now):
    """bazza-specific alert candidates, in the same insertion order as before:
    SoC temp, throttle flags, failed units, FTL/unbound/DNS-staleness/gravity."""
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

    dns = data["dns"]
    if not dns.get("ftl_active"):
        extra["ftl_down"] = ("critical", "Pi-hole FTL not running",
            "pihole-FTL is not active — DNS resolution and ad-blocking are down for the whole LAN")
    if dns.get("unbound_known") and not dns.get("unbound_active"):
        extra["unbound_down"] = ("warn", "unbound not running",
            "the recursive resolver unbound is inactive — Pi-hole has lost its private upstream")

    if dns.get("age_s") is not None and dns["age_s"] > DNS_STALE_SECONDS:
        extra["dns_stale"] = ("warn", "Pi-hole poller stale",
            f"pihole.json hasn't refreshed in {round(dns['age_s'] / 60)} min — the DNS figures may be old")

    glu = dns.get("gravity_last_update")
    if glu and now - glu > GRAVITY_STALE_SECONDS:
        extra["gravity_stale"] = ("warn", "Gravity list is old",
            f"the blocklist was last rebuilt {round((now - glu) / 86400)} days ago")

    return extra


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("bazza", REMOTE_SCRIPT)
        load = hostlib.parse_load(s["LOADAVG"], s["NPROC"])
        mem = hostlib.parse_mem(s["MEMINFO"])
        disk = hostlib.parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = hostlib.parse_procs(s["TOPCPU"])
        top_mem = hostlib.parse_procs(s["TOPMEM"])
        other_failed = hostlib.parse_failed(s["FAILED"])
        model = s["MODEL"].strip() or "Raspberry Pi"
        cpu_temp = hostlib.parse_temp(s["TEMP"])
        throttled = hostlib.parse_throttled(s["THROTTLED"], BAZZA_THROTTLE_BITS)
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

    prev = hostlib.load_state(STATE_FILE)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE, _extra_alerts(data, ts))
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"], cpu_temp)
    hostlib.save_state(STATE_FILE, {"active_alerts": active_alerts})

    blk = f"{dns['percent_blocked']}% blocked" if dns.get("percent_blocked") is not None else "no dns data"
    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C, "
          f"FTL {'up' if dns.get('ftl_active') else 'DOWN'}, {blk}")


if __name__ == "__main__":
    fetch_and_write()
