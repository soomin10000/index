"""Triggers a Pi-hole gravity update via the API (no SSH to bazza needed).

Cron on steve runs this Sunday 04:00 (needs PIHOLE_PASSWORD, set in crontab).
Pi-hole v6 swaps the gravity DB atomically, so DNS keeps serving during the run.
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pollers"))
from pihole_client import PiholeClient


def main():
    print(time.strftime("%F %T"), "starting gravity update")
    ph = PiholeClient()
    try:
        out = ph.update_gravity()
    finally:
        ph.logout()
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", out)  # strip TTY progress codes
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    print("\n".join(tail))
    print(time.strftime("%F %T"), "gravity update finished")


if __name__ == "__main__":
    main()
