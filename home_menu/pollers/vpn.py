"""Writes vpn.json for the web dashboard — health of the site-to-site VPN between
the home LAN (192.168.1.0/24, UCG-Max) and the remote site (192.168.10.0/24, base
UDM behind CGNAT), running over Site Magic / SD-WAN mesh.

The console-side status API is a known liar for this tunnel type (reports the
tunnel down / zero bytes while it's demonstrably passing traffic — see
pollers/unifi/SITE_TO_SITE_VPN.md), so health here is measured by **real traffic**:
an ICMP ping to the far gateway plus an authenticated HTTPS call to the far
console's local API *through* the tunnel. The far console's own `stat/health`
payload then gives some bonus remote-site detail (ISP, WAN IP, gateway load).

History (rtt + loss) goes to vpn_history.db for the trend chart. Cron every 5 min.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DATA = Path(__file__).resolve().parent.parent / "data"
OUT        = DATA / "vpn.json"
STATE_FILE = DATA / "vpn_state.json"
DB_FILE    = DATA / "vpn_history.db"
HISTORY_RETENTION_SECONDS = 7 * 24 * 3600

HOME_LAN     = "192.168.1.0/24"
HOME_CONSOLE = "192.168.1.1"
FAR_LAN      = os.environ.get("VPN_FAREND_LAN", "192.168.10.0/24")
FAR_IP       = os.environ.get("VPN_FAREND_IP", "192.168.10.1")
TRANSPORT    = "Site Magic (SD-WAN mesh)"

HOME_API_KEY = os.environ.get("UNIFI_API_KEY")
FAR_API_KEY  = os.environ.get("UNIFI_FAREND_API_KEY")

PING_COUNT   = 5
API_TIMEOUT  = 8

# home <-> remote is a short hop (~15-40 ms seen at build time); these are
# generous ceilings before the card starts complaining.
RTT_WARN_MS  = 120.0
RTT_CRIT_MS  = 400.0


def _ping(host, count=PING_COUNT):
    try:
        out = subprocess.run(
            ["ping", "-n", "-c", str(count), "-i", "0.3", "-W", "2", host],
            capture_output=True, text=True, timeout=count * 2 + 8,
        ).stdout
    except Exception as e:
        return {"sent": count, "recv": 0, "loss_pct": 100.0, "error": str(e)}

    recv = 0
    m = re.search(r"(\d+) received", out)
    if m:
        recv = int(m.group(1))
    loss = 100.0
    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        loss = float(m.group(1))
    res = {"sent": count, "recv": recv, "loss_pct": loss}
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    if m:
        res["rtt_min"] = float(m.group(1))
        res["rtt_avg"] = float(m.group(2))
        res["rtt_max"] = float(m.group(3))
        res["rtt_mdev"] = float(m.group(4))
    return res


def _health(console_ip, api_key):
    """GET the console's stat/health. Returns (subsystems_by_name, elapsed_ms) or
    (None, elapsed_ms) on any failure."""
    t0 = time.monotonic()
    try:
        r = requests.get(
            f"https://{console_ip}/proxy/network/api/s/default/stat/health",
            headers={"X-API-KEY": api_key}, verify=False, timeout=API_TIMEOUT,
        )
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code != 200:
            return None, ms, f"HTTP {r.status_code}"
        by_name = {s.get("subsystem"): s for s in r.json().get("data", [])}
        return by_name, ms, None
    except Exception as e:
        return None, round((time.monotonic() - t0) * 1000, 1), str(e)


def _far_detail(wan):
    """Pull the interesting bits out of the far console's `wan` health subsystem."""
    if not wan:
        return {}
    gw = wan.get("gw_system-stats", {}) or {}
    detail = {
        "gw_name": wan.get("gw_name"),
        "isp": wan.get("isp_name"),
        "wan_ip": wan.get("wan_ip"),
        "wan_status": wan.get("status"),
        "gw_cpu": _num(gw.get("cpu")),
        "gw_mem": _num(gw.get("mem")),
        "gw_uptime_s": _num(gw.get("uptime")),
        "gw_version": wan.get("gw_version"),
    }
    mons = (wan.get("uptime_stats", {}).get("WAN", {}) or {}).get("alerting_monitors", [])
    icmp = [m for m in mons if m.get("type") == "icmp"]
    if icmp:
        detail["wan_availability"] = round(sum(m.get("availability", 0) for m in icmp) / len(icmp), 1)
        lat = [m["latency_average"] for m in icmp if m.get("latency_average")]
        if lat:
            detail["wan_latency_ms"] = round(sum(lat) / len(lat), 1)
    return detail


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _console_reported(home_subs):
    """What the home console *thinks* of the site-to-site tunnel — kept only to
    show the discrepancy, since this reading is a known stale liar for Site Magic
    tunnels."""
    vpn = (home_subs or {}).get("vpn")
    if not vpn:
        return {"attempted": bool(home_subs is not None), "available": False}
    return {
        "attempted": True,
        "available": True,
        "status": vpn.get("status"),
        "site_to_site_active": vpn.get("site_to_site_num_active"),
        "note": "UniFi's own reading — known to under-report Site Magic tunnels; the ping/API checks above are authoritative",
    }


def _load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _save_state(active_alerts):
    STATE_FILE.write_text(json.dumps({"active_alerts": active_alerts}))


def _log_history(ts, rtt_ms, loss_pct, api_ms, up):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metrics_log "
        "(ts INTEGER, rtt_ms REAL, loss_pct REAL, api_ms REAL, up INTEGER)"
    )
    conn.execute(
        "INSERT INTO metrics_log (ts, rtt_ms, loss_pct, api_ms, up) VALUES (?, ?, ?, ?, ?)",
        (ts, rtt_ms, loss_pct, api_ms, 1 if up else 0),
    )
    conn.execute("DELETE FROM metrics_log WHERE ts < ?", (ts - HISTORY_RETENTION_SECONDS,))
    conn.commit()
    conn.close()


def _build_alerts(data, now, prev):
    prev_active = (prev or {}).get("active_alerts", {})
    candidates = {}

    ping = data["ping"]
    api = data["api"]
    rtt = ping.get("rtt_avg")

    if ping["loss_pct"] >= 100:
        candidates["tunnel_down"] = ("critical", "Tunnel down",
            f"100% packet loss to the far gateway ({FAR_IP}) — no traffic is crossing the VPN")
    elif ping["loss_pct"] > 0:
        candidates["tunnel_degraded"] = ("warn", "Packet loss across the tunnel",
            f"{ping['loss_pct']:.0f}% loss to {FAR_IP} ({ping['recv']}/{ping['sent']} replies)")

    if rtt is not None and rtt >= RTT_CRIT_MS:
        candidates["high_latency"] = ("critical", "Tunnel latency very high",
            f"round trip to the far gateway is {rtt:.0f} ms")
    elif rtt is not None and rtt >= RTT_WARN_MS:
        candidates["high_latency"] = ("warn", "Tunnel latency high",
            f"round trip to the far gateway is {rtt:.0f} ms (usually 15-40 ms)")

    if api["attempted"] and not api["ok"] and ping["loss_pct"] < 100:
        candidates["api_unreachable"] = ("warn", "Far console unreachable through the tunnel",
            f"ping succeeds but the authenticated API call to {FAR_IP} failed"
            + (f" — {api['error']}" if api.get("error") else ""))

    far = data["far"]
    if far.get("wan_status") and far["wan_status"] != "ok":
        candidates["far_wan"] = ("warn", "Remote site WAN not OK",
            f"the far console reports its internet as '{far['wan_status']}'")

    active_alerts, alerts = {}, []
    for key, (level, header, text) in candidates.items():
        onset = prev_active.get(key, {}).get("ts", now)
        active_alerts[key] = {"ts": onset, "level": level, "header": header, "text": text}
        alerts.append({"id": key, "ts": onset, "level": level, "header": header, "text": text})
    alerts.sort(key=lambda a: a["ts"], reverse=True)
    return alerts, active_alerts


def fetch_and_write():
    ts = int(time.time())

    ping = _ping(FAR_IP)

    far_subs = far_ms = far_err = None
    if FAR_API_KEY:
        far_subs, far_ms, far_err = _health(FAR_IP, FAR_API_KEY)
    api = {
        "attempted": bool(FAR_API_KEY),
        "ok": far_subs is not None,
        "ms": far_ms,
        "error": far_err,
    }
    far = _far_detail((far_subs or {}).get("wan")) if far_subs else {}

    home_subs = None
    if HOME_API_KEY:
        home_subs, _, _ = _health(HOME_CONSOLE, HOME_API_KEY)
    console_reported = _console_reported(home_subs)

    reachable = ping["loss_pct"] < 100
    rtt = ping.get("rtt_avg")
    if not reachable:
        status = "down"
    elif ping["loss_pct"] > 0 or (api["attempted"] and not api["ok"]) \
            or (rtt is not None and rtt >= RTT_WARN_MS) \
            or (far.get("wan_status") and far["wan_status"] != "ok"):
        status = "degraded"
    else:
        status = "up"

    data = {
        "ts": ts,
        "status": status,
        "transport": TRANSPORT,
        "home": {"lan": HOME_LAN, "console": HOME_CONSOLE},
        "far": {"lan": FAR_LAN, "ip": FAR_IP, **far},
        "ping": ping,
        "api": api,
        "console_reported": console_reported,
    }

    prev = _load_state()
    alerts, active_alerts = _build_alerts(data, ts, prev)
    data["alerts"] = alerts

    OUT.write_text(json.dumps(data, indent=2))
    _log_history(ts, rtt, ping["loss_pct"], api["ms"], status != "down")
    _save_state(active_alerts)

    print(f"Saved {OUT} — status {status}, loss {ping['loss_pct']:.0f}%, "
          f"rtt {rtt if rtt is not None else '—'} ms, "
          f"api {'ok' if api['ok'] else ('n/a' if not api['attempted'] else 'FAIL')}, "
          f"{len(alerts)} alerts")


if __name__ == "__main__":
    fetch_and_write()
