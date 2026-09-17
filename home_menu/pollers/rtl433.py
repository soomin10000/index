"""Writes rtl433.json for the /rtl433 dashboard card — a device inventory built
from jeff's rtl_433 service, which sniffs the 433/868 MHz ISM band (weather
station sensors, TPMS, doorbells, smart meters, etc.) on the same RTL-SDR
dongle that otherwise feeds the (currently parked) ADS-B setup.

rtl_433 emits one JSON line per decode to its own stdout/journal, not a file,
and journald eventually vacuums old entries — so this poller is the only
durable record. Each run pulls journal lines since the last poll (a cursor
timestamp in rtl433_state.json), folds new decodes into a persistent device
catalog keyed by model+id+channel, and republishes the catalog as rtl433.json.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

OUT        = DATA / "rtl433.json"
STATE_FILE = DATA / "rtl433_state.json"

SSH_TIMEOUT = 20
RECENT_MAX = 100        # bounded ring buffer of raw decodes for the page's live feed
DEVICE_PRUNE_AFTER = 30 * 24 * 3600   # drop devices not heard in 30 days

# Fields that identify a decode, not describe a reading — everything else in
# the JSON line is treated as the sensor's payload and shown as-is.
_META_KEYS = {"time", "model", "id", "channel", "mic"}


def _remote_script(since_ts):
    return (
        "echo '===ACTIVE==='; systemctl is-active rtl_433 2>&1\n"
        "echo '===NOW==='; date +%s\n"
        "echo '===JOURNAL==='; journalctl -u rtl_433 --no-pager -o cat "
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
    return {"cursor_ts": 0, "devices": {}, "recent": []}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def _device_key(rec):
    return f"{rec.get('model')}|{rec.get('id')}|{rec.get('channel')}"


def _parse_journal(text):
    """One JSON object per line; startup banners and warnings are plain text
    (rtl_433 writes those unstructured even with -F json) so silently skip
    anything that doesn't parse as a JSON object."""
    decodes = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "model" in obj:
            decodes.append(obj)
    return decodes


def fetch_and_write():
    ts = int(time.time())
    state = _load_state()
    since_ts = state.get("cursor_ts") or (ts - 300)  # first run: last 5 min only

    try:
        s = _fetch(since_ts)
        active = s.get("ACTIVE", "").strip()
        remote_now = s.get("NOW", "").strip()
        decodes = _parse_journal(s.get("JOURNAL", ""))
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"rtl433 poll failed: {e}")
        return

    devices = state.get("devices", {})
    recent = state.get("recent", [])

    for d in decodes:
        key = _device_key(d)
        fields = {k: v for k, v in d.items() if k not in _META_KEYS}
        rec = devices.get(key)
        if rec is None:
            rec = {
                "model": d.get("model"), "id": d.get("id"), "channel": d.get("channel"),
                "first_seen": ts, "last_seen": ts, "count": 0, "fields": {},
            }
            devices[key] = rec
        rec["last_seen"] = ts
        rec["count"] += 1
        rec["fields"] = fields

        recent.append({"ts": ts, "model": d.get("model"), "id": d.get("id"),
                        "channel": d.get("channel"), "fields": fields})

    recent = recent[-RECENT_MAX:]
    devices = {k: v for k, v in devices.items() if ts - v["last_seen"] <= DEVICE_PRUNE_AFTER}

    new_cursor = int(float(remote_now)) if remote_now else ts
    _save_state({"cursor_ts": new_cursor, "devices": devices, "recent": recent})

    device_list = sorted(devices.values(), key=lambda r: r["last_seen"], reverse=True)
    data = {
        "ts": ts,
        "service_active": active == "active",
        "device_count": len(device_list),
        "new_decodes": len(decodes),
        "devices": device_list,
        "recent": list(reversed(recent)),
    }
    OUT.write_text(json.dumps(data, indent=2))
    print(f"Saved {OUT} — {len(device_list)} devices, {len(decodes)} new decodes, "
          f"service {'active' if data['service_active'] else 'inactive'}")


if __name__ == "__main__":
    fetch_and_write()
