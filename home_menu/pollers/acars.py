"""Writes acars.json for the /acars dashboard page — a live ACARS message feed
and per-aircraft summary built from jeff's acarsdec service (131.525/131.725/
131.825/131.850/131.950 MHz, the standard European primary ACARS channel set).

Same one-dongle constraint as rtl433.py/jeff.py: acarsdec only runs when the
/sdr page has switched jeff into 'acars' mode. acarsdec emits one JSON object
per decoded message to its own stdout/journal (mixed with plain-text startup
banners), so — same as rtl433.py — this pulls new journal lines since the last
poll and folds them into a persistent per-aircraft catalog + a bounded recent
feed, since journald will eventually vacuum the raw history.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "acars.json"
STATE_FILE = DATA / "acars_state.json"

SSH_TIMEOUT = 20
RECENT_MAX = 150
FLIGHT_PRUNE_AFTER = 24 * 3600   # aircraft aren't stable identities day to day like ISM sensors


def _remote_script(since_ts):
    return (
        "echo '===ACTIVE==='; systemctl is-active acarsdec 2>&1\n"
        "echo '===NOW==='; date +%s\n"
        "echo '===JOURNAL==='; journalctl -u acarsdec --no-pager -o cat "
        f"--since '@{since_ts}' 2>/dev/null\n"
    )


def _sections(raw):
    parts = re.split(r"===(\w+)===\n", raw)[1:]
    return {name: body for name, body in zip(parts[0::2], parts[1::2])}


def _fetch(since_ts):
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "jeff",
         _remote_script(since_ts)],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh jeff failed: {out.stderr.strip()}")
    return _sections(out.stdout)


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"cursor_ts": 0, "flights": {}, "recent": []}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def _parse_journal(text):
    messages = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "label" in obj:
            messages.append(obj)
    return messages


def fetch_and_write():
    ts = int(time.time())
    state = _load_state()
    since_ts = state.get("cursor_ts") or (ts - 300)

    try:
        s = _fetch(since_ts)
        active = s.get("ACTIVE", "").strip()
        remote_now = s.get("NOW", "").strip()
        messages = _parse_journal(s.get("JOURNAL", ""))
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"acars poll failed: {e}")
        return

    flights = state.get("flights", {})
    recent = state.get("recent", [])

    for m in messages:
        tail = (m.get("tail") or "").strip()
        key = tail or (m.get("flight") or "").strip() or "unknown"
        rec = flights.get(key)
        if rec is None:
            rec = {"tail": tail or None, "flight": None, "first_seen": ts,
                    "last_seen": ts, "count": 0, "last_label": None, "last_text": None,
                    "depa": None, "dsta": None}
            flights[key] = rec
        rec["last_seen"] = ts
        rec["count"] += 1
        if m.get("flight"):
            rec["flight"] = m["flight"].strip()
        if m.get("label"):
            rec["last_label"] = m["label"]
        if m.get("text"):
            rec["last_text"] = m["text"].strip()
        if m.get("depa"):
            rec["depa"] = m["depa"]
        if m.get("dsta"):
            rec["dsta"] = m["dsta"]

        recent.append({
            "ts": ts, "tail": tail or None, "flight": m.get("flight"),
            "label": m.get("label"), "text": (m.get("text") or "").strip() or None,
            "depa": m.get("depa"), "dsta": m.get("dsta"), "freq": m.get("freq"),
        })

    recent = recent[-RECENT_MAX:]
    flights = {k: v for k, v in flights.items() if ts - v["last_seen"] <= FLIGHT_PRUNE_AFTER}

    new_cursor = int(float(remote_now)) if remote_now else ts
    _save_state({"cursor_ts": new_cursor, "flights": flights, "recent": recent})

    flight_list = sorted(flights.values(), key=lambda r: r["last_seen"], reverse=True)
    data = {
        "ts": ts,
        "service_active": active == "active",
        "flight_count": len(flight_list),
        "new_messages": len(messages),
        "flights": flight_list,
        "recent": list(reversed(recent)),
    }
    OUT.write_text(json.dumps(data, indent=2))
    print(f"Saved {OUT} — {len(flight_list)} aircraft, {len(messages)} new messages, "
          f"service {'active' if data['service_active'] else 'inactive'}")


if __name__ == "__main__":
    fetch_and_write()
