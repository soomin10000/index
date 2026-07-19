"""Writes atlas.json — outside-in DNS integrity check.

Compares what RIPE Atlas probes around the world get for each canary domain
(via a public resolver) against what Pi-hole answers a LAN client. Confirms
the intentional blocks are live and flags divergence that could mean DNS
tampering. Cron every 15 min; fetching Atlas results costs no credits.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "ubuntu-sender"))
from atlas_client import AtlasClient, ATLAS_DOMAINS

try:
    from notify_sender import notify as _notify
except ImportError:
    def _notify(title, message, **kwargs):
        print(f"notify_sender unavailable — would have sent: [{title}] {message}")

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "atlas.json"
STATE_FILE = DATA / "atlas_state.json"

PIHOLE_IP = "192.168.1.246"
BLOCK_SENTINELS = {"0.0.0.0", "::"}
# statuses that warrant a notification when a domain transitions into them
ALERT_STATUSES = {"divergent", "own_path_divergent", "unexpected_block", "unblocked"}


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def pihole_ips(domain):
    """A-record answer Pi-hole gives a LAN client (IPs only, CNAMEs dropped).

    Returns None when Pi-hole didn't answer at all (down/unreachable) — an
    empty *answer* looks identical to a block, so it must not be conflated
    with a failed *query*.
    """
    try:
        r = subprocess.run(["dig", f"@{PIHOLE_IP}", domain, "A", "+short", "+time=3"],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return [l for l in r.stdout.split()
            if l.count(".") == 3 and l.replace(".", "").isdigit()]


def _slash24(ip):
    return ip.rsplit(".", 1)[0]


def classify(group, ph_ips, external_ips, own_ips):
    """(status, note) for one domain.

    external_ips: union of answers from Atlas probes out in the world.
    own_ips: our own probe's answer via the same public resolver.
    """
    ph_blocked = not ph_ips or all(ip in BLOCK_SENTINELS for ip in ph_ips)
    ext_nets = {_slash24(ip) for ip in external_ips}
    scattered = len(ext_nets) > 2  # CDN spreading answers across networks

    if ph_blocked:
        if group == "block":
            return "blocked", "nulled by Pi-hole, resolves elsewhere"
        return "unexpected_block", "integrity canary is being blocked by Pi-hole"

    if group == "block":
        return "unblocked", "block canary resolves to real IPs via Pi-hole"

    # own probe reaches the public resolver through our uplink — it can be tampered
    # with even while Pi-hole's own upstream (unbound, direct) stays clean
    own_nets = {_slash24(ip) for ip in own_ips}
    own_diverges = (own_ips and external_ips and not scattered
                    and not (own_nets & ext_nets))
    if own_diverges:
        return "own_path_divergent", "our probe's public-resolver answer disagrees with the world"

    ph_nets = {_slash24(ip) for ip in ph_ips}
    if set(ph_ips) & set(external_ips) or ph_nets & ext_nets:
        return "agree", ""

    if scattered:
        return "cdn_variance", f"global answers span {len(ext_nets)} networks — likely CDN"

    if external_ips:
        return "divergent", "Pi-hole answer disjoint from tight global consensus"

    return "no_data", "no external results yet"


def fetch_and_write():
    state = _load_state()
    msms = state.get("measurements", {})
    if not msms:
        raise SystemExit("No measurements in atlas_state.json — run scripts/atlas_bootstrap.py first")

    at = AtlasClient()
    domains = []
    for domain, group in ATLAS_DOMAINS.items():
        msm_id = msms.get(domain)
        if not msm_id:
            domains.append({"domain": domain, "group": group, "status": "no_measurement",
                            "pihole_ips": [], "global_ips": [], "own_ip": [],
                            "note": "not bootstrapped — re-run scripts/atlas_bootstrap.py"})
            continue
        try:
            results = at.latest_results(msm_id)
        except Exception as e:
            results = []
            fetch_err = str(e)
        else:
            fetch_err = ""
        own = [ip for r in results if r["prb_id"] == at.probe_id for ip in r["ips"]]
        external = sorted({ip for r in results if r["prb_id"] != at.probe_id for ip in r["ips"]})
        ph = pihole_ips(domain)
        if ph is None:
            ph = []
            status, note = "pihole_down", f"Pi-hole at {PIHOLE_IP} not answering — comparison skipped"
        else:
            status, note = classify(group, ph, external, own)
        if fetch_err:
            status, note = "no_data", f"Atlas fetch failed: {fetch_err}"
        domains.append({"domain": domain, "group": group, "status": status,
                        "pihole_ips": ph, "global_ips": external, "own_ip": own,
                        "note": note, "msm_id": msm_id})

    data = {
        "ts": int(time.time()),
        "credits": at.credits(),
        "probe": at.probe_status(),
        "domains": domains,
    }
    OUT.write_text(json.dumps(data, indent=2))

    # Pi-hole unreachable is one incident, not seven — a single push on the
    # down transition, never the per-domain tampering alerts below
    pihole_down = any(d["status"] == "pihole_down" for d in domains)
    if pihole_down and not state.get("pihole_down_alerted"):
        _notify("DNS cross-check", f"Pi-hole at {PIHOLE_IP} is not answering DNS — cross-check paused")
    state["pihole_down_alerted"] = pihole_down

    # notify on transition *into* an alerting status, once per status per domain
    alerted = state.get("alerted", {})
    for d in domains:
        prev, cur = alerted.get(d["domain"]), d["status"]
        if cur in ALERT_STATUSES and prev != cur:
            _notify("DNS cross-check", f"{d['domain']}: {cur} — {d['note']}")
        alerted[d["domain"]] = cur
    state["alerted"] = alerted
    STATE_FILE.write_text(json.dumps(state, indent=2))

    flagged = sum(1 for d in domains if d["status"] in ALERT_STATUSES)
    print(f"Saved {OUT} — {len(domains)} domains, {flagged} flagged, "
          f"credits {data['credits'].get('balance')}")


if __name__ == "__main__":
    fetch_and_write()
