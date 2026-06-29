"""Writes pihole.json for the web dashboard. Run directly or imported by poller."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pihole_client import PiholeClient

OUT = Path(__file__).parent / "pihole.json"


def fetch_and_write():
    ph = PiholeClient()

    summary    = ph.summary()
    history    = ph.history()
    t_domains  = ph.top_domains(10)
    t_blocked  = ph.top_blocked(10)
    t_clients  = ph.top_clients(200)
    bl_clients = ph.blocked_clients(200)
    upstreams  = ph.upstreams()

    q = summary.get("queries", {})

    # blocked count keyed by IP only — name fallback causes false matches
    blocked_by_ip = {c["ip"]: c["count"] for c in bl_clients}

    clients_detail = []
    for c in t_clients:
        ip    = c["ip"]
        name  = c["name"] or ""
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
        "ts": int(time.time()),
        "summary": {
            "total":           q.get("total", 0),
            "blocked":         q.get("blocked", 0),
            "percent_blocked": round(q.get("percent_blocked", 0), 1),
            "cached":          q.get("cached", 0),
            "forwarded":       q.get("forwarded", 0),
            "unique_domains":  q.get("unique_domains", 0),
            "frequency":       round(q.get("frequency", 0), 2),
            "types":           q.get("types", {}),
            "status":          q.get("status", {}),
        },
        "history":        history,
        "top_domains":    t_domains,
        "top_blocked":    t_blocked,
        "top_clients":    t_clients[:10],
        "clients_detail": clients_detail,
        "upstreams":      real_upstreams,
    }

    OUT.write_text(json.dumps(data, indent=2))
    print(f"Saved {OUT} — {q.get('total',0):,} total queries, "
          f"{q.get('percent_blocked',0):.1f}% blocked, "
          f"{len(clients_detail)} clients profiled")


if __name__ == "__main__":
    fetch_and_write()
