"""Writes pihole.json for the web dashboard. Run directly or imported by poller."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pihole_client import PiholeClient

OUT = Path(__file__).resolve().parent.parent / "data" / "pihole.json"
UNIFI_DEVICES = OUT.parent / "unifi" / "devices.json"
UNIFI_MAX_AGE_SEC = 900  # a stale name is worse than no name — devices come and go

# Secondary (redundant) Pi-hole — see project_secondary_pihole memory. Steve's own
# instance, kept in sync from the primary hourly via nebula-sync. Self-signed cert
# (Caddy-fronted), so verify=False.
PIHOLE2_URL      = os.environ.get("PIHOLE2_URL", "https://192.168.1.183")
PIHOLE2_PASSWORD = os.environ.get("PIHOLE2_PASSWORD", "")


def _summary_block(summary_resp):
    q = summary_resp.get("queries", {})
    return {
        "total":           q.get("total", 0),
        "blocked":         q.get("blocked", 0),
        "percent_blocked": round(q.get("percent_blocked", 0), 1),
        "cached":          q.get("cached", 0),
        "forwarded":       q.get("forwarded", 0),
        "unique_domains":  q.get("unique_domains", 0),
        "frequency":       round(q.get("frequency", 0), 2),
        "types":           q.get("types", {}),
        "status":          q.get("status", {}),
        "gravity": {
            "domains":     summary_resp.get("gravity", {}).get("domains_being_blocked", 0),
            "last_update": summary_resp.get("gravity", {}).get("last_update", 0),
        },
    }


def _fetch_secondary():
    """Steve's redundant instance — a much lighter poll than the primary (no
    clients_detail/ip_mac join, nothing else consumes it). Never raises: a
    down/unreachable secondary is itself worth showing on the dashboard."""
    host = PIHOLE2_URL.split("//", 1)[-1]
    if not PIHOLE2_PASSWORD:
        return {"reachable": False, "label": "steve", "host": host, "error": "PIHOLE2_PASSWORD not configured"}
    try:
        ph2 = PiholeClient(url=PIHOLE2_URL, password=PIHOLE2_PASSWORD, verify=False)
        try:
            summary2 = ph2.summary()
            history2 = ph2.history()
        finally:
            ph2.logout()
        return {
            "reachable": True,
            "label":     "steve",
            "host":      host,
            "summary":   _summary_block(summary2),
            "history":   history2,
        }
    except Exception as e:
        return {"reachable": False, "label": "steve", "host": host, "error": str(e)}


def _unifi_names():
    """mac -> display name from the UniFi poller, for clients whose hostname never
    reaches Pi-hole (iOS private WiFi addresses, rotating IPv6 privacy addresses)."""
    try:
        d = json.loads(UNIFI_DEVICES.read_text())
    except (OSError, ValueError):
        return {}
    if time.time() - d.get("ts", 0) > UNIFI_MAX_AGE_SEC:
        return {}
    return {e["mac"].lower(): e.get("display_name") or e.get("hostname") or ""
            for e in d.get("devices", []) if e.get("mac")}


def fetch_and_write():
    ph = PiholeClient()

    summary    = ph.summary()
    history    = ph.history()
    t_domains  = ph.top_domains(10)
    t_blocked  = ph.top_blocked(10)
    t_clients  = ph.top_clients(200)
    bl_clients = ph.blocked_clients(200)
    upstreams  = ph.upstreams()
    net_devs   = ph.network_devices()
    ph.logout()

    secondary = _fetch_secondary()

    # ip -> mac from Pi-hole's network table, so consumers can join a device's
    # rotating IPv6 privacy addresses back to the same hardware as its IPv4.
    # Pi-hole invents "ip-x.x.x.x" pseudo-MACs for off-subnet clients — skip those.
    ip_mac = {}
    mac_name = {}    # any hostname Pi-hole knows for the same hardware
    mac_vendor = {}
    for nd in net_devs:
        mac = (nd.get("hwaddr") or "").lower()
        if ":" not in mac:
            continue
        if nd.get("macVendor"):
            mac_vendor[mac] = nd["macVendor"]
        for a in nd.get("ips", []):
            ip_mac[a["ip"]] = mac
            if a.get("name") and mac not in mac_name:
                mac_name[mac] = a["name"]

    # blocked count keyed by IP only — name fallback causes false matches
    blocked_by_ip = {c["ip"]: c["count"] for c in bl_clients}

    unifi_names = _unifi_names()

    clients_detail = []
    for c in t_clients:
        ip    = c["ip"]
        name  = c["name"] or ""
        if not name:
            # fill via the MAC join: Pi-hole's name for a sibling address of the
            # same device, then UniFi's name, then plain vendor as a last resort
            mac  = ip_mac.get(ip, "")
            name = mac_name.get(mac) or unifi_names.get(mac) or mac_vendor.get(mac, "")
        total = c["count"]
        blocked = blocked_by_ip.get(ip, 0)
        pct     = round(blocked / total * 100, 1) if total else 0.0
        clients_detail.append({
            "ip":          ip,
            "name":        name,
            "total":       total,
            "blocked":     blocked,
            "blocked_pct": pct,
        })

    real_upstreams = [u for u in upstreams if u["ip"] not in ("blocklist", "cache")]

    data = {
        "ts":             int(time.time()),
        "summary":        _summary_block(summary),
        "history":        history,
        "top_domains":    t_domains,
        "top_blocked":    t_blocked,
        "top_clients":    t_clients[:10],
        "clients_detail": clients_detail,
        "ip_mac":         ip_mac,
        "upstreams":      real_upstreams,
        "secondary":      secondary,
    }

    OUT.write_text(json.dumps(data, indent=2))
    sec_note = (f"secondary reachable, {secondary['summary']['gravity']['domains']:,} gravity domains"
                if secondary.get("reachable") else f"secondary UNREACHABLE ({secondary.get('error')})")
    print(f"Saved {OUT} — {data['summary']['total']:,} total queries, "
          f"{data['summary']['percent_blocked']:.1f}% blocked, "
          f"{len(clients_detail)} clients profiled, {sec_note}")


if __name__ == "__main__":
    fetch_and_write()
