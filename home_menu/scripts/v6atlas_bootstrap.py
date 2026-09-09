"""Idempotent setup for the v6health external (RIPE Atlas) vantage point.

bazza and steve both sit behind one UCG-Max, on one Community Fibre line, sharing one
delegated /64 — so the monitor's best classification, "both-hosts", still can't
separate *our line* from *our ISP's network* from *the resolver*. These recurring
Atlas measurements watch the same v6 resolvers v6probe.sh tests, but from three
tiers of vantage point:

  mine  - our own probe 64460 (on the LAN, same uplink as bazza/steve)
  isp   - other probes on our ISP's AS (Community Fibre, 201838)  -> isolates our line / CPE
  ww    - worldwide                            -> isolates the resolver / v6 internet

plus an inbound traceroute from worldwide probes to bazza's GUA (is our prefix
reachable from outside at all?).

Measurement ids are recorded in data/v6atlas_state.json for pollers/v6health.py,
which only *reads* results (free). Re-runnable: creates what's missing, never
duplicates. Needs RIPE_ATLAS_KEY in the env (crontab / with-secrets). Run by
hand, not from cron.
"""

import json
import sys
from pathlib import Path

DRY = "--dry-run" in sys.argv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pollers"))
from atlas_client import AtlasClient

STATE = ROOT / "data" / "v6atlas_state.json"

ISP_ASN = 201838          # Community Fibre - origin AS of 2a02:6b60::/28 and our v4 212.132.192.0/18
OWN_PROBE = 64460
TARGETS = {
    "cf_v6":    "2606:4700:4700::1111",
    "quad9_v6": "2620:fe::fe",
}
# bazza's GUA is EUI-64 from MAC 88:a2:9e:76:65:68 and stable while the /48 holds;
# if the ISP renumbers, the inbound measurement goes dark (itself a signal) and
# this needs re-running with the new address.
BAZZA_GUA = "2a02:6b67:d7e0:2500:8aa2:9eff:fe76:6568"

PING_INTERVAL = 1800
TRACE_INTERVAL = 7200


def main():
    at = AtlasClient()
    print(f"probe {at.probe_id}   credits {at.credits()}")

    isp = at.probe_spec([("probes", OWN_PROBE, 1), ("asn", ISP_ASN, 5)])
    ww = at.probe_spec([("area", "WW", 6)])

    # suffix -> zero-arg callable that creates it (description filled in below)
    want = {}
    for name, ip in TARGETS.items():
        want[f"ping:{name}:isp"] = lambda d, ip=ip: at.create_ping(d, ip, isp, interval=PING_INTERVAL)
        want[f"ping:{name}:ww"] = lambda d, ip=ip: at.create_ping(d, ip, ww, interval=PING_INTERVAL)
        want[f"trace:{name}:isp"] = lambda d, ip=ip: at.create_traceroute(d, ip, isp, interval=TRACE_INTERVAL)
    want["in:bazza"] = lambda d: at.create_traceroute(d, BAZZA_GUA, ww, interval=TRACE_INTERVAL)

    existing = at.list_measurements("v6health:")
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    msms = state.setdefault("measurements", {})
    state.update(targets=TARGETS, bazza_gua=BAZZA_GUA,
                 isp_asn=ISP_ASN, own_probe=OWN_PROBE)

    for suffix, mk in want.items():
        if suffix in existing:
            msms[suffix] = existing[suffix]
            print(f"  exists  {suffix}  (msm {existing[suffix]})")
        elif suffix in msms:
            print(f"  exists  {suffix}  (msm {msms[suffix]}, from state file)")
        elif DRY:
            print(f"  WOULD create v6health:{suffix}")
        else:
            mid = mk(f"v6health:{suffix}")
            msms[suffix] = mid
            print(f"  created {suffix}  (msm {mid})")

    if DRY:
        print("dry run — nothing created, state file untouched")
        return
    STATE.write_text(json.dumps(state, indent=2))
    print(f"wrote {STATE}  ({len(msms)} measurements)")


if __name__ == "__main__":
    main()
