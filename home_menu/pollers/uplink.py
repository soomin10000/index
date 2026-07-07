"""Writes uplink.json — ISP uplink health from our RIPE Atlas probe's built-ins.

Every probe pings the DNS root servers around the clock; fetching those results
is free and needs no API key. Median RTT + packet loss over the last 24h gives
an outside-in view of uplink quality, and probe connect/disconnect transitions
(tracked here, Atlas keeps no public history) form an ISP outage diary that
survives steve being down. Cron every 15 min.
"""

import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path.home() / "ubuntu-sender"))
try:
    from notify_sender import notify as _notify
except ImportError:
    def _notify(title, message, **kwargs):
        print(f"notify_sender unavailable — would have sent: [{title}] {message}")

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "uplink.json"
STATE_FILE = DATA / "uplink_state.json"

BASE = "https://atlas.ripe.net/api/v2"
STAT = "https://stat.ripe.net/data"
PROBE_ID = int(os.environ.get("RIPE_ATLAS_PROBE_ID", "64460"))
EXPECTED_ASN = 201838

# Built-in ping measurements (one per root server, every 240s from every probe).
# Four are plenty for a median — all are anycast so each samples a different path.
ROOT_MSMS = {1001: "k-root", 1004: "f-root", 1009: "a-root", 1013: "e-root"}

WINDOW = 24 * 3600
BUCKET = 900          # aggregate to 15-min points -> 96 per day
MAX_EVENTS = 200


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def fetch_pings(session, start):
    """[(timestamp, avg_rtt|None, sent, rcvd)] across the root measurements."""
    rows = []
    for msm_id in ROOT_MSMS:
        r = session.get(f"{BASE}/measurements/{msm_id}/results/",
                        params={"probe_ids": PROBE_ID, "start": start}, timeout=60)
        r.raise_for_status()
        for res in r.json():
            avg = res.get("avg", -1)
            rows.append((res.get("timestamp", 0),
                         avg if avg and avg > 0 else None,
                         res.get("sent", 0), res.get("rcvd", 0)))
    return rows


def bucket_series(rows, start, now):
    """15-min points: median RTT of the pings in the bucket + loss percent."""
    buckets = {}
    for ts, rtt, sent, rcvd in rows:
        b = buckets.setdefault(ts // BUCKET * BUCKET, {"rtts": [], "sent": 0, "rcvd": 0})
        if rtt is not None:
            b["rtts"].append(rtt)
        b["sent"] += sent
        b["rcvd"] += rcvd
    series = []
    for t in range((start // BUCKET + 1) * BUCKET, now, BUCKET):
        b = buckets.get(t)
        if not b or not b["sent"]:
            # probe sent nothing this bucket — offline counts as total loss
            series.append({"t": t, "rtt": None, "loss": 100.0})
        else:
            series.append({
                "t": t,
                "rtt": round(statistics.median(b["rtts"]), 2) if b["rtts"] else None,
                "loss": round(100 * (1 - b["rcvd"] / b["sent"]), 1),
            })
    return series


def probe_status(session):
    p = session.get(f"{BASE}/probes/{PROBE_ID}/", timeout=30).json()
    return {
        "id": PROBE_ID,
        "status": (p.get("status") or {}).get("name", "?"),
        "connected": (p.get("status") or {}).get("name") == "Connected",
        "since": p.get("status_since"),
        "first_connected": p.get("first_connected"),
        "total_uptime": p.get("total_uptime"),
        "address_v4": p.get("address_v4"),
    }


def fetch_bgp(session, public_ip):
    """RIPEstat watch on the prefix our uplink lives in: origin AS, RPKI,
    more-specific announcements (classic hijack shape), RIS visibility."""

    def stat(endpoint, **params):
        r = session.get(f"{STAT}/{endpoint}/data.json", params=params, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    ni = stat("network-info", resource=public_ip)
    prefix = ni.get("prefix")
    if not prefix:
        return {"status": "unknown", "notes": ["prefix not found for " + public_ip]}

    rs = stat("routing-status", resource=prefix)
    rpki = stat("rpki-validation", resource=EXPECTED_ASN, prefix=prefix)

    origins = sorted(o["origin"] for o in rs.get("origins", []))
    vis = rs.get("visibility", {}).get("v4", {})
    seeing, total = vis.get("ris_peers_seeing", 0), vis.get("total_ris_peers", 0)
    more = [m["prefix"] for m in rs.get("more_specifics", [])]

    notes = []
    if origins != [EXPECTED_ASN]:
        notes.append(f"origin AS {origins} != expected AS{EXPECTED_ASN}")
    if rpki.get("status") != "valid":
        notes.append(f"RPKI {rpki.get('status', '?')}")
    if more:
        notes.append(f"more-specific announced: {', '.join(more[:5])}")
    if total and seeing / total < 0.9:
        notes.append(f"visibility {seeing}/{total} RIS peers")

    return {
        "status": "alert" if notes else "ok",
        "prefix": prefix,
        "origins": origins,
        "expected_asn": EXPECTED_ASN,
        "rpki": rpki.get("status"),
        "visibility": f"{seeing}/{total}",
        "more_specifics": more,
        "notes": notes,
    }


def update_events(state, probe):
    """Append a transition when status_since moves — Atlas keeps no history."""
    events = state.setdefault("events", [])
    last = state.get("last_transition")
    cur = {"t": probe["since"], "event": "connect" if probe["connected"] else "disconnect",
           "status": probe["status"]}
    if probe["since"] and cur != last:
        events.append(cur)
        state["last_transition"] = cur
        state["events"] = events[-MAX_EVENTS:]
        if not probe["connected"]:
            _notify("Uplink", f"Atlas probe {PROBE_ID} {probe['status'].lower()} "
                              f"— ISP uplink may be down")
    return state["events"]


def fetch_and_write():
    now = int(time.time())
    start = now - WINDOW
    s = requests.Session()

    probe = probe_status(s)
    state = _load_state()
    events = update_events(state, probe)

    try:
        bgp = fetch_bgp(s, probe.get("address_v4") or "212.132.242.38")
    except Exception as e:
        bgp = {"status": "unknown", "notes": [f"RIPEstat fetch failed: {e}"]}
    # notify once per transition into alert, like the probe events
    if bgp["status"] == "alert" and state.get("bgp_last_status") != "alert":
        _notify("Uplink BGP", "; ".join(bgp["notes"]))
    state["bgp_last_status"] = bgp["status"]
    STATE_FILE.write_text(json.dumps(state, indent=2))

    series = bucket_series(fetch_pings(s, start), start, now)
    recent = [p for p in series[-4:] if p["rtt"] is not None]
    uptime_pct = None
    if probe["first_connected"] and probe["total_uptime"]:
        uptime_pct = round(100 * probe["total_uptime"] / (now - probe["first_connected"]), 1)

    data = {
        "ts": now,
        "probe": probe | {"uptime_pct": uptime_pct},
        "bgp": bgp,
        "targets": sorted(ROOT_MSMS.values()),
        "series": series,
        "events": events[-50:],
        "last": {
            "rtt": round(statistics.median(r["rtt"] for r in recent), 1) if recent else None,
            "loss": series[-1]["loss"] if series else None,
        },
    }
    OUT.write_text(json.dumps(data, indent=2))
    print(f"Saved {OUT} — {len(series)} points, probe {probe['status']}, "
          f"last rtt {data['last']['rtt']} ms")


if __name__ == "__main__":
    fetch_and_write()
