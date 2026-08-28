"""Mirrors the arr-stack status collected on wacky into data/arr.json.

The actual polling (of Sonarr/Radarr/Lidarr/Readarr/Prowlarr/SABnzbd/
qBittorrent on noob) happens on wacky's own cron, in
~/arr-collector/collect.py (pollers/arr_collector.py in this repo, deployed
there by hand) — noob is a direct Tailscale peer of wacky, so wacky reaches
it without steve as a relay. This poller just pulls that file's output over
SSH, same pull pattern as pollers/smokeping_rrd.py.
"""
import json
import subprocess
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "arr.json"

SSH_TIMEOUT = 15
STALE_AFTER_SECONDS = 15 * 60  # wacky's own cron runs every 5 min


def _fetch_remote():
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "wacky", "cat ~/arr-collector/status.json"],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh wacky failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def fetch_and_write():
    ts = int(time.time())
    try:
        data = _fetch_remote()
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"arr poll failed: {e}")
        return

    data["stale"] = (ts - data.get("ts", 0)) > STALE_AFTER_SECONDS
    OUT.write_text(json.dumps(data, indent=2))

    down = [n for n, s in data.get("services", {}).items() if not s.get("ok")]
    print(f"Saved {OUT} — {len(data.get('services', {})) - len(down)}/{len(data.get('services', {}))} ok"
          + (f", down: {', '.join(down)}" if down else "")
          + (", STALE" if data["stale"] else ""))


if __name__ == "__main__":
    fetch_and_write()
