"""One-time (idempotent) setup for the Atlas DNS cross-check.

Creates one recurring DNS measurement per canary domain, skipping any that
already exist on the account, and records domain -> measurement id in
data/atlas_state.json for the poller. Safe to re-run after editing
ATLAS_DOMAINS: creates what's missing, never duplicates, leaves the rest.

Needs RIPE_ATLAS_KEY in the environment. Run by hand, not cron.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pollers"))
from atlas_client import AtlasClient, ATLAS_DOMAINS, EXTERNAL_PROBES, INTERVAL

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "atlas_state.json"


def main():
    per_domain_daily = (1 + EXTERNAL_PROBES) * (86400 // INTERVAL) * 10
    print(f"{len(ATLAS_DOMAINS)} domains × {1 + EXTERNAL_PROBES} probes, every {INTERVAL}s "
          f"→ ~{per_domain_daily * len(ATLAS_DOMAINS):,} credits/day")

    at = AtlasClient()
    print(f"probe: {at.probe_id}, credits: {at.credits()}")

    existing = at.list_my_measurements()
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    msms = state.setdefault("measurements", {})

    for domain in ATLAS_DOMAINS:
        if domain in existing:
            msms[domain] = existing[domain]
            print(f"  exists  {domain} (msm {existing[domain]})")
        elif domain in msms:
            # Account listing may be unavailable (403); trust the state file.
            print(f"  exists  {domain} (msm {msms[domain]}, from state file)")
        else:
            msm_id = at.create_dns_measurement(domain)
            msms[domain] = msm_id
            print(f"  created {domain} (msm {msm_id})")

    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"Wrote {STATE_FILE}")


if __name__ == "__main__":
    main()
