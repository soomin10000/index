"""Replace the v6health RIPE Atlas ping/traceroute measurements so their probe
sets are IPv6-known-good.

Some of the probes Atlas picked when the measurements were first created have no
working IPv6 to the resolvers (flat 100% loss for the whole window). One dead
probe in a 4-probe set is 25% "loss" that never moves — enough to red the
"Community Fibre" / "v6 internet" segments of the /v6health fault chain even
though our own probes see 0% loss.

The API key can create and stop measurements but NOT edit the probe set of a
running one (participation-requests -> 403), so the fix is stop + recreate: the
new measurements select probes with `probe_spec(..., ['system-ipv6-works'])`
(see v6atlas_bootstrap.py). New measurement ids land in data/v6atlas_state.json;
pollers/v6health.py picks them up on its next run and also drops any
still-fully-dead probe from its own aggregates as a backstop.

    python3 scripts/v6atlas_fix_probes.py --dry-run
    python3 scripts/v6atlas_fix_probes.py            # stop the 6, then recreate

Needs RIPE_ATLAS_KEY in the env (with-secrets / crontab). Run by hand.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pollers"))
sys.path.insert(0, str(ROOT / "scripts"))
from atlas_client import AtlasClient  # noqa: E402
import v6atlas_bootstrap  # noqa: E402

STATE = ROOT / "data" / "v6atlas_state.json"
DRY = "--dry-run" in sys.argv

# the measurements whose probe pool includes the ISP-AS / worldwide selections;
# the inbound traceroute (in:bazza) is left alone — a 0% reached there is by
# design, not a dead probe.
RECREATE = ("ping:cf_v6:isp", "ping:cf_v6:ww", "trace:cf_v6:isp",
            "ping:quad9_v6:isp", "ping:quad9_v6:ww", "trace:quad9_v6:isp")


def main():
    state = json.loads(STATE.read_text())
    msms = state.get("measurements", {})
    at = AtlasClient()
    print(f"probe {at.probe_id}   credits {at.credits()}\n")

    stopped = []
    for key in RECREATE:
        mid = msms.get(key)
        if not mid:
            print(f"  {key}: not in state, will just be created")
            continue
        if DRY:
            print(f"  WOULD stop {key}  (msm {mid})")
            continue
        at.stop_measurement(mid)
        stopped.append(key)
        print(f"  stopped {key}  (msm {mid})")

    if DRY:
        print("\ndry run — nothing stopped; recreate step not run")
        return

    # drop the stopped ones so bootstrap treats them as missing and recreates
    for key in stopped:
        msms.pop(key, None)
    STATE.write_text(json.dumps(state, indent=2))
    print(f"\ncleared {len(stopped)} ids from {STATE.name}; recreating...\n")

    # let Atlas settle the stops before create re-checks list_measurements
    time.sleep(5)
    v6atlas_bootstrap.main()
    print("\ndone — fresh probe sets report within a few rounds "
          "(ping 30 min / traceroute 2 h)")


if __name__ == "__main__":
    main()
