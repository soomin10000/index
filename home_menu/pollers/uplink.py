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

# Exposure check: external probes send a DNS query AT our public IP hourly.
# Everything should time out — any response means an open resolver or a
# router forwarding UDP/53. ~720 credits/day.
EXPOSURE_DESC = "home-exposure-check"
EXPOSURE_PROBES = 3
EXPOSURE_INTERVAL = 3600


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


def ensure_exposure_msm(state, ip):
    """Keep one recurring DNS measurement aimed at our own public IP.

    If the IP moved, stop the stale measurement first — it must never keep
    probing an address that is no longer ours. Needs RIPE_ATLAS_KEY (from
    the crontab env); without it, leaves whatever exists untouched.
    """
    exp = state.setdefault("exposure", {})
    if not ip or (exp.get("target") == ip and exp.get("msm_id")):
        return exp
    key = os.environ.get("RIPE_ATLAS_KEY", "")
    if not key:
        return exp
    s = requests.Session()
    s.headers["Authorization"] = f"Key {key}"
    if exp.get("msm_id"):
        s.delete(f"{BASE}/measurements/{exp['msm_id']}/", timeout=30)
    r = s.post(f"{BASE}/measurements/", timeout=30, json={
        "definitions": [{
            "type": "dns", "af": 4, "query_class": "IN", "query_type": "A",
            "query_argument": "example.com", "target": ip,
            "use_probe_resolver": False, "set_rd_bit": True,
            "description": EXPOSURE_DESC, "interval": EXPOSURE_INTERVAL,
        }],
        "probes": [{"type": "area", "value": "WW", "requested": EXPOSURE_PROBES}],
        "is_oneoff": False,
    })
    if r.ok:
        exp.update(msm_id=r.json()["measurements"][0], target=ip, created=int(time.time()))
        print(f"exposure measurement -> msm {exp['msm_id']} at {ip}")
    else:
        print(f"exposure measurement create failed ({r.status_code}): {r.text[:200]}")
    return exp


def check_exposure(session, state, ip):
    exp = ensure_exposure_msm(state, ip)
    if not exp.get("msm_id"):
        return {"status": "unmanaged", "note": "no measurement (key unavailable?)"}
    r = session.get(f"{BASE}/measurements/{exp['msm_id']}/latest/", timeout=60)
    r.raise_for_status()
    results = r.json()
    # a real DNS response always carries rt/abuf; timeouts and errors don't
    responded = [res["prb_id"] for res in results
                 if isinstance(res.get("result"), dict)
                 and ("rt" in res["result"] or "abuf" in res["result"])]
    if not results:
        return {"status": "pending", "target": exp["target"], "msm_id": exp["msm_id"],
                "probes": 0, "responded": []}
    return {
        "status": "open" if responded else "closed",
        "target": exp["target"],
        "msm_id": exp["msm_id"],
        "probes": len(results),
        "responded": responded,
    }


def _asn_for_ip(session, ip, cache):
    """IP -> origin ASN via RIPEstat, memoised in the state cache."""
    if ip in cache:
        return cache[ip]
    try:
        d = session.get(f"{STAT}/network-info/data.json",
                        params={"resource": ip}, timeout=20).json()["data"]
        asn = int(d["asns"][0]) if d.get("asns") else None
    except Exception:
        asn = None
    cache[ip] = asn
    return asn


def _is_private(ip):
    o = ip.split(".")
    return (o[0] == "10" or (o[0] == "172" and 16 <= int(o[1]) <= 31)
            or (o[0] == "192" and o[1] == "168")) if len(o) == 4 else False


def check_reverse_path(session, state, ip):
    """How the world routes back to us: the transit ASNs of the last few
    responsive hops before our (silent) address, aggregated across probes.
    Alerts when that near-us AS set changes — the reverse of the BGP origin
    watch, which only sees who *announces* us."""
    rev = state.setdefault("reverse", {})
    if not rev.get("msm_id"):
        return {"status": "unmanaged", "note": "no measurement"}
    r = session.get(f"{BASE}/measurements/{rev['msm_id']}/latest/", timeout=60)
    r.raise_for_status()
    results = r.json()
    cache = state.setdefault("asn_cache", {})

    hop_counts, transit = [], {}
    for res in results:
        hops = res.get("result", [])
        responsive = [h for h in hops
                      if any(p.get("from") for p in h.get("result", []))]
        if responsive:
            hop_counts.append(responsive[-1].get("hop", len(hops)))
        # the last two responsive hops = ISP border + its upstream
        for h in responsive[-2:]:
            for p in h.get("result", []):
                fip = p.get("from")
                if not fip or _is_private(fip):
                    continue
                asn = _asn_for_ip(session, fip, cache)
                if asn and asn != EXPECTED_ASN:
                    transit.setdefault(asn, set()).add(res.get("prb_id"))

    as_set = sorted(transit)
    fp = ",".join(str(a) for a in as_set)
    prev_fp = rev.get("fingerprint")
    changed = prev_fp is not None and fp != prev_fp
    rev["fingerprint"] = fp
    names = state.setdefault("asn_names", {})
    return {
        "status": "changed" if changed else "ok",
        "msm_id": rev["msm_id"],
        "probes": len(results),
        "median_hops": int(statistics.median(hop_counts)) if hop_counts else None,
        "transit": [{"asn": a, "probes": len(transit[a]),
                     "holder": _asn_holder(session, a, names)} for a in as_set],
        "prev_fingerprint": prev_fp if changed else None,
    }


def _asn_holder(session, asn, cache):
    key = str(asn)
    if key in cache:
        return cache[key]
    try:
        d = session.get(f"{STAT}/as-overview/data.json",
                        params={"resource": f"AS{asn}"}, timeout=20).json()["data"]
        holder = (d.get("holder") or "").split(",")[0][:40] or None
    except Exception:
        holder = None
    cache[key] = holder
    return holder


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
    try:
        exposure = check_exposure(s, state, probe.get("address_v4"))
    except Exception as e:
        exposure = {"status": "unknown", "note": f"fetch failed: {e}"}

    try:
        reverse = check_reverse_path(s, state, probe.get("address_v4"))
    except Exception as e:
        reverse = {"status": "unknown", "note": f"fetch failed: {e}"}

    # notify once per transition into a bad state, like the probe events
    if bgp["status"] == "alert" and state.get("bgp_last_status") != "alert":
        _notify("Uplink BGP", "; ".join(bgp["notes"]))
    state["bgp_last_status"] = bgp["status"]
    if exposure["status"] == "open" and state.get("exposure_last_status") != "open":
        _notify("Uplink exposure", f"UDP/53 on {exposure.get('target')} ANSWERED "
                                   f"external probes {exposure.get('responded')} — "
                                   "open resolver / port forward?")
    state["exposure_last_status"] = exposure["status"]
    if reverse["status"] == "changed":
        _notify("Uplink reverse-path", f"transit AS set changed: was "
                                       f"[{reverse.get('prev_fingerprint')}] now "
                                       f"[{','.join(str(t['asn']) for t in reverse['transit'])}]")
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
        "exposure": exposure,
        "reverse": reverse,
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
