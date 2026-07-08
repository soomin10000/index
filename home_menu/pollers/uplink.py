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

# World latency map: recurring traceroutes AT our public IP from probes spread
# across the continents. Our WAN drops ICMP, so per probe we read the RTT of
# the last responsive hop (the ISP border, same trick as the reverse-path
# watch); when a reply does come from our IP we mark it "reached". ~26 probes
# every 30 min — spendy in credits, deliberately so.
WORLDMAP_DESC = "home-worldmap"
WORLDMAP_INTERVAL = 1800
WORLDMAP_SPREAD = [  # country -> probes, aiming for even continent coverage
    ("US", 3), ("CA", 1), ("BR", 2), ("AR", 1), ("CL", 1),
    ("GB", 1), ("DE", 1), ("ES", 1), ("PL", 1), ("SE", 1),
    ("ZA", 2), ("KE", 1), ("AE", 1), ("IL", 1),
    ("IN", 2), ("JP", 2), ("SG", 1), ("KR", 1),
    ("AU", 2), ("NZ", 1),
]

# NTP integrity: query the same public NTP servers from our probe and a handful
# of world-wide probes. Our probe's measured offset to a server should track the
# world consensus; if it drifts off on its own, something is rewriting NTP on
# our path. Atlas reports NTP offset in seconds; we convert to ms for display.
NTP_SERVERS = {"162.159.200.123": "time.cloudflare.com",
               "216.239.35.0": "time.google.com"}
NTP_DELTA_MS = 100.0     # our offset may diverge from the world by this much
NTP_ABS_MS = 1000.0      # an absolute offset this large is alarming by itself


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
            or (o[0] == "192" and o[1] == "168")
            or (o[0] == "100" and 64 <= int(o[1]) <= 127)) if len(o) == 4 else False


def check_reverse_path(session, state, ip):
    """How the world routes back to us: the transit ASNs of the last few
    responsive hops before our (silent) address, aggregated across probes.
    Tracks changes to that near-us AS set (informational only, no alerting)
    — the reverse of the BGP origin watch, which only sees who *announces* us."""
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
    # only a change between two non-empty sets is real; empty->populated is the
    # first baseline, and populated->empty is a transient resolution miss
    changed = bool(prev_fp) and bool(fp) and fp != prev_fp
    # don't overwrite a known-good baseline with a momentary empty read
    if fp:
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


def ensure_worldmap_msm(state, ip):
    """Keep one recurring world-spread traceroute aimed at our public IP,
    recreated (never left running) when the IP moves. Same contract as the
    exposure measurement: needs RIPE_ATLAS_KEY, otherwise hands-off."""
    wm = state.setdefault("worldmap", {})
    if not ip or (wm.get("target") == ip and wm.get("msm_id")):
        return wm
    key = os.environ.get("RIPE_ATLAS_KEY", "")
    if not key:
        return wm
    s = requests.Session()
    s.headers["Authorization"] = f"Key {key}"
    if wm.get("msm_id"):
        s.delete(f"{BASE}/measurements/{wm['msm_id']}/", timeout=30)
    r = s.post(f"{BASE}/measurements/", timeout=30, json={
        "definitions": [{
            "type": "traceroute", "af": 4, "protocol": "ICMP",
            "target": ip, "description": WORLDMAP_DESC,
            "interval": WORLDMAP_INTERVAL, "max_hops": 32, "packets": 3,
        }],
        "probes": [{"type": "country", "value": cc, "requested": n}
                   for cc, n in WORLDMAP_SPREAD],
        "is_oneoff": False,
    })
    if r.ok:
        wm.update(msm_id=r.json()["measurements"][0], target=ip, created=int(time.time()))
        print(f"worldmap measurement -> msm {wm['msm_id']} at {ip}")
    else:
        print(f"worldmap measurement create failed ({r.status_code}): {r.text[:200]}")
    return wm


def _probe_geo(session, prb_id, cache):
    """Probe -> (country, lat, lon), memoised in state (probes rarely move)."""
    key = str(prb_id)
    if key in cache:
        return cache[key]
    try:
        p = session.get(f"{BASE}/probes/{prb_id}/", timeout=20).json()
        lon, lat = (p.get("geometry") or {}).get("coordinates", (None, None))
        geo = {"cc": p.get("country_code"), "lat": lat, "lon": lon}
    except Exception:
        geo = {"cc": None, "lat": None, "lon": None}
    cache[key] = geo
    return geo


def check_worldmap(session, state, ip):
    wm = ensure_worldmap_msm(state, ip)
    if not wm.get("msm_id"):
        return {"status": "unmanaged", "note": "no measurement (key unavailable?)"}
    r = session.get(f"{BASE}/measurements/{wm['msm_id']}/latest/", timeout=60)
    r.raise_for_status()
    results = r.json()
    geo_cache = state.setdefault("probe_geo", {})
    home = _probe_geo(session, PROBE_ID, geo_cache)
    if not results:
        return {"status": "pending", "msm_id": wm["msm_id"], "home": home, "points": []}

    # Our IP never answers (ping-tested: all echo dropped), so every trace
    # ends at whatever hop last replied. Real paths converge on a shared
    # funnel of border IPs just upstream of us (Level3/NTT/LINX in London);
    # a last hop seen by only one probe, or in private/CGNAT space, means
    # the path died near the *probe* and its RTT would poison the map.
    raw = []
    for res in results:
        hops = res.get("result", [])
        last_ip, last_rtts, reached = None, [], False
        for h in hops:
            replies = h.get("result", [])
            rtts = [p["rtt"] for p in replies if "rtt" in p]
            if rtts:
                last_ip = next((p["from"] for p in replies if p.get("from")), None)
                last_rtts = rtts
                reached = any(p.get("from") == res.get("dst_addr") for p in replies)
        raw.append((res, last_ip, last_rtts, reached))

    seen = {}
    for _, ip, rtts, _ in raw:
        if ip and rtts:
            seen[ip] = seen.get(ip, 0) + 1

    points, truncated = [], 0
    for res, ip, rtts, reached in raw:
        if not rtts or not ip:
            truncated += 1
            continue
        if not reached and (_is_private(ip) or seen[ip] < 2):
            truncated += 1
            continue
        geo = _probe_geo(session, res.get("prb_id"), geo_cache)
        points.append({
            "prb_id": res.get("prb_id"),
            "cc": geo["cc"], "lat": geo["lat"], "lon": geo["lon"],
            "rtt": round(statistics.median(rtts), 1),
            "reached": reached,
        })
    points.sort(key=lambda p: p["rtt"])
    return {"status": "ok" if points else "pending", "msm_id": wm["msm_id"],
            "home": home, "probes": len(results), "truncated": truncated,
            "points": points}


def _probe_ntp(res):
    """One probe's view of a server: median offset (ms), stratum, reachability.
    Atlas reports NTP offset in seconds -> convert to ms. Successful samples
    carry an "offset"; timeouts carry "x" instead."""
    offs = [r["offset"] * 1000 for r in res.get("result", [])
            if isinstance(r, dict) and "offset" in r]
    return {
        "prb_id": res.get("prb_id"),
        "offset": round(statistics.median(offs), 2) if offs else None,
        "stratum": res.get("stratum"),
        "reachable": bool(offs),
    }


def fetch_ntp(session, state):
    """Cross-check our probe's clock offset to public NTP servers against a
    world-wide consensus. Each server is queried from our probe and a few WW
    probes; if our offset to a server is an outlier while the rest of the world
    agrees, something is rewriting NTP on our path (or our clock has slipped)."""
    servers_cfg = state.get("ntp", {}).get("servers", {})
    if not servers_cfg:
        return {"status": "unmanaged", "note": "no measurements"}

    out, alert = [], False
    for host, msm_id in servers_cfg.items():
        row = {"host": host, "name": NTP_SERVERS.get(host, host), "msm_id": msm_id}
        try:
            results = session.get(f"{BASE}/measurements/{msm_id}/latest/",
                                  timeout=60).json()
        except Exception as e:
            out.append(row | {"status": "unknown", "notes": [str(e)]})
            continue

        views = [_probe_ntp(r) for r in results]
        ours = next((v for v in views if v["prb_id"] == PROBE_ID), None)
        others = [v["offset"] for v in views
                  if v["prb_id"] != PROBE_ID and v["offset"] is not None]
        consensus = round(statistics.median(others), 2) if others else None

        notes, delta = [], None
        if not results:
            status = "pending"
        elif ours is None or not ours["reachable"]:
            status = "unreachable"
            notes.append("our probe got no NTP reply")
        else:
            if consensus is not None:
                delta = round(ours["offset"] - consensus, 2)
                if abs(delta) > NTP_DELTA_MS:
                    notes.append(f"our offset {ours['offset']}ms vs world "
                                 f"{consensus}ms (Δ{delta}ms)")
            if abs(ours["offset"]) > NTP_ABS_MS:
                notes.append(f"absolute offset {ours['offset']}ms")
            st = ours["stratum"]
            if st in (0, None) or (st and st > 4):
                notes.append(f"stratum {st}")
            status = "alert" if notes else "ok"

        if status == "alert":
            alert = True
        out.append(row | {
            "status": status,
            "our_offset": ours["offset"] if ours else None,
            "consensus": consensus, "delta": delta,
            "stratum": ours["stratum"] if ours else None,
            "peers": len(others), "notes": notes,
        })

    if alert:
        overall = "alert"
    elif out and all(s["status"] == "pending" for s in out):
        overall = "pending"
    else:
        overall = "ok"
    return {"status": overall, "servers": out}


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

    try:
        ntp = fetch_ntp(s, state)
    except Exception as e:
        ntp = {"status": "unknown", "note": f"fetch failed: {e}"}

    try:
        worldmap = check_worldmap(s, state, probe.get("address_v4"))
    except Exception as e:
        worldmap = {"status": "unknown", "note": f"fetch failed: {e}", "points": []}

    # notify once per transition into a bad state, like the probe events
    if bgp["status"] == "alert" and state.get("bgp_last_status") != "alert":
        _notify("Uplink BGP", "; ".join(bgp["notes"]))
    state["bgp_last_status"] = bgp["status"]
    if exposure["status"] == "open" and state.get("exposure_last_status") != "open":
        _notify("Uplink exposure", f"UDP/53 on {exposure.get('target')} ANSWERED "
                                   f"external probes {exposure.get('responded')} — "
                                   "open resolver / port forward?")
    state["exposure_last_status"] = exposure["status"]
    if ntp["status"] == "alert" and state.get("ntp_last_status") != "alert":
        bad = "; ".join(f"{s['name']}: {', '.join(s['notes'])}"
                        for s in ntp["servers"] if s["status"] == "alert")
        _notify("Uplink NTP", f"clock/NTP integrity: {bad}")
    state["ntp_last_status"] = ntp["status"]
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
        "ntp": ntp,
        "worldmap": worldmap,
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
