"""
UniFi poller — long-running poll loop.

Run with:
    UNIFI_API_KEY=... python3 poller.py
    UNIFI_API_KEY=... python3 poller.py --interval 120
"""

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "ubuntu-sender"))

from unifi_client import UnifiClient, UnifiAuthError
from checks import check_congestion
from db import (open_db, log_poll, last_flagged_congestion,
                check_new_devices, log_speedtest, log_event, get_known_devices)

try:
    from notify_sender import notify as _notify_send
    def _notify(title, message, **kwargs):
        results = _notify_send(title, message, **kwargs)
        for host, status in results.items():
            if status != "ok":
                log.warning("Notification to %s failed: %s", host, status)
except ImportError:
    def _notify(title, message, **kwargs):
        log.warning("notify_sender unavailable — would have sent: [%s] %s", title, message)

LOG_FILE    = Path(__file__).parent / "poller.log"
DEVICES_OUT = Path(__file__).parent / "devices.json"

log = logging.getLogger("unifi_poller")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh  = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)

_POLLER_DIR = Path(__file__).parent
_ENV = {**os.environ, "UNIFI_API_KEY": os.environ.get("UNIFI_API_KEY", "")}


# ── Vendor lookup & device type guessing ─────────────────────────────────────

_vendor_cache = {}
_mac_lookup = None

def _vendor(mac):
    global _mac_lookup
    if mac in _vendor_cache:
        return _vendor_cache[mac]
    try:
        if _mac_lookup is None:
            from mac_vendor_lookup import MacLookup
            _mac_lookup = MacLookup()
        v = _mac_lookup.lookup(mac)
    except Exception:
        v = ""
    _vendor_cache[mac] = v
    return v


# Ordered list of (keywords, label) — first match wins
_VENDOR_TYPES = [
    (["apple"],                   "Apple Device"),
    (["samsung"],                 "Samsung Device"),
    (["raspberry pi"],            "Raspberry Pi"),
    (["amazon"],                  "Amazon Device"),
    (["ring"],                    "Ring Device"),
    (["nintendo"],                "Nintendo Console"),
    (["sony"],                    "Sony Device"),
    (["playstation"],             "PlayStation"),
    (["google"],                  "Google Device"),
    (["nest labs", "nest"],       "Nest Device"),
    (["roku"],                    "Roku"),
    (["intel"],                   "PC / Laptop"),
    (["dell"],                    "Dell PC"),
    (["hewlett", " hp,", "hp "],  "HP Device"),
    (["canon"],                   "Canon Printer"),
    (["epson"],                   "Epson Printer"),
    (["brother"],                 "Brother Printer"),
    (["netgear"],                 "NETGEAR Device"),
    (["tp-link", "tp link"],      "TP-Link Device"),
    (["ubiquiti", "unifi"],       "UniFi Device"),
    (["eero"],                    "Eero Router"),
    (["philips"],                 "Philips Device"),
    (["sonos"],                   "Sonos Speaker"),
    (["bose"],                    "Bose Speaker"),
    (["xbox"],                    "Xbox"),
    (["microsoft"],               "Microsoft Device"),
    (["lenovo"],                  "Lenovo PC"),
    (["asus"],                    "ASUS Device"),
    (["acer"],                    "Acer Device"),
    (["lg electronics",
       "lg innotek"],             "LG Device"),
    (["xiaomi"],                  "Xiaomi Device"),
    (["huawei"],                  "Huawei Device"),
    (["synology"],                "Synology NAS"),
    (["qnap"],                    "QNAP NAS"),
    (["western digital",
       "wd my cloud"],            "WD Storage"),
    (["seagate"],                 "Seagate Storage"),
    (["espressif"],               "Smart Device"),
    (["shelly"],                  "Shelly Device"),
    (["ikea"],                    "IKEA Smart Home"),
    (["tp link", "tplink"],       "TP-Link Device"),
]

def _guess_device_type(vendor: str) -> str:
    """Return a human-friendly device type from vendor string, or empty string."""
    v = vendor.lower()
    for keywords, label in _VENDOR_TYPES:
        if any(kw in v for kw in keywords):
            return label
    return ""


# ── Speedtest ─────────────────────────────────────────────────────────────────

def sync_speedtest(db, client):
    """Fetch gateway speedtest history and log any results newer than last recorded."""
    try:
        results = client.get_speedtest_history()
        if not results:
            return
        # Find our latest recorded timestamp
        row = db.execute("SELECT MAX(ts) FROM speedtest_log").fetchone()
        last_ts = row[0] or 0

        new_count = 0
        for r in sorted(results, key=lambda x: x["ts"]):
            if r["ts"] > last_ts:
                log_speedtest(db, r["ping_ms"], r["download_mbps"], r["upload_mbps"], ts=r["ts"])
                new_count += 1

        if new_count:
            latest = sorted(results, key=lambda x: x["ts"])[-1]
            log.info("Synced %d new gateway speedtest(s) — latest: %.0f/%.0f Mbps %.0f ms",
                     new_count, latest["download_mbps"], latest["upload_mbps"], latest["ping_ms"])
    except Exception as e:
        log.warning("Gateway speedtest sync failed: %s", e)


def trigger_speedtest(client):
    """Ask the gateway to kick off an on-demand speed test."""
    try:
        client.trigger_speedtest()
        log.info("Gateway speedtest triggered")
    except Exception as e:
        log.warning("Could not trigger gateway speedtest: %s", e)


# ── Devices JSON ──────────────────────────────────────────────────────────────

def write_devices_json(client, weak_flags, db=None):
    try:
        stations = client.get_clients()
        devices  = client.get_devices()
        weak_macs = {f.get("mac", "") for f in weak_flags}
        weak_hosts = {f["hostname"] for f in weak_flags}

        dev_by_mac  = {d["mac"]: d for d in devices}
        ap_by_mac   = {d["mac"]: d.get("name", d["mac"]) for d in devices if d.get("type") == "uap"}
        known_devs  = get_known_devices(db) if db else {}

        out = []
        for sta in stations:
            mac      = sta.get("mac", "")
            hostname = sta.get("hostname", "")
            vendor   = _vendor(mac)
            flagged  = mac in weak_macs or (hostname and hostname in weak_hosts)
            ap_mac   = sta.get("ap_mac", "")

            if hostname:
                display_name = hostname
                guessed      = False
            else:
                guess        = _guess_device_type(vendor)
                display_name = guess or vendor or mac
                guessed      = bool(guess or vendor)

            kd = known_devs.get(mac, {})
            out.append({
                "mac":          mac,
                "hostname":     hostname,
                "display_name": display_name,
                "guessed":      guessed,
                "ip":           sta.get("ip", ""),
                "vendor":       vendor,
                "ssid":         sta.get("essid", ""),
                "ap":           ap_by_mac.get(ap_mac, ap_mac),
                "signal":       sta.get("signal"),
                "retry_pct":    sta.get("wifi_tx_retries_percentage"),
                "is_wired":     sta.get("is_wired", False),
                "flagged":      flagged,
                "first_seen":   kd.get("first_seen"),
                "last_seen":    kd.get("last_seen", int(time.time())),
            })

        out.sort(key=lambda d: (not d["flagged"], d["display_name"].lower()))
        DEVICES_OUT.write_text(json.dumps({"ts": int(time.time()), "devices": out}, indent=2))
        log.info("Wrote devices.json (%d devices)", len(out))
    except Exception as e:
        log.warning("Failed to write devices.json: %s", e)


# ── Visuals ───────────────────────────────────────────────────────────────────

def _regenerate_visuals():
    for script in ("topology.py", "dashboard.py",
                   str(Path.home() / "projects" / "pihole_poller.py")):
        try:
            subprocess.run(
                [sys.executable, str(_POLLER_DIR / script)],
                env=_ENV, timeout=60, capture_output=True, check=True,
            )
            log.info("Regenerated %s", script)
        except subprocess.CalledProcessError as e:
            log.warning("Failed to regenerate %s: %s", script, e.stderr.decode().strip())
        except Exception as e:
            log.warning("Failed to regenerate %s: %s", script, e)


# ── Poll helpers ──────────────────────────────────────────────────────────────

def _congestion_key(f): return f"{f['ap']}:{f['radio']}"


def poll_once(client):
    return check_congestion(client)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(interval, client):
    db = open_db()
    prev_congestion = last_flagged_congestion(db, within_seconds=interval * 2)
    if prev_congestion:
        log.info("Resuming — suppressing re-notification for: %s", prev_congestion)

    # Speed test: once per hour
    speedtest_every = max(1, 3600 // interval)
    poll_count = 0

    log.info("Poll loop started — interval %ds, speedtest every %d polls", interval, speedtest_every)

    while True:
        try:
            congestion = poll_once(client)
        except UnifiAuthError as e:
            log.error("Auth error: %s", e)
            time.sleep(interval)
            continue
        except Exception as e:
            log.error("Poll failed: %s", e)
            time.sleep(interval)
            continue

        curr_congestion = {_congestion_key(f): f for f in congestion}

        # Congestion alerts
        for key, f in curr_congestion.items():
            if key not in prev_congestion:
                msg = f"{f['ap']} {f['radio']} — {f['cu_total']}% utilisation, {f['num_sta']} clients"
                log.warning("NEW congestion: %s", msg)
                _notify("UniFi: Channel congestion", msg, sound=False)
                log_event(db, "congestion", "Channel congestion", msg)
        for key in prev_congestion - curr_congestion.keys():
            log.info("Congestion resolved: %s", key)
            _notify("UniFi: Congestion resolved", key, sound=False)
            log_event(db, "resolved", "Congestion resolved", key)

        # New device detection
        try:
            stations = client.get_clients()
            devices  = client.get_devices()
            all_seen = [
                {"mac": s["mac"], "hostname": s.get("hostname", ""), "vendor": _vendor(s["mac"])}
                for s in stations + devices
            ]
            new_devs = check_new_devices(db, all_seen)
            for d in new_devs:
                msg = f"{d.get('hostname') or d['mac']} — {d.get('vendor') or 'unknown vendor'} ({d['mac']})"
                log.warning("NEW device: %s", msg)
                _notify("UniFi: New device", msg, sound=False)
                log_event(db, "new_device", "New device", msg)
        except Exception as e:
            log.warning("Device check failed: %s", e)

        log_poll(db, congestion, [])
        write_devices_json(client, [], db=db)

        poll_count += 1
        if poll_count % speedtest_every == 0:
            trigger_speedtest(client)
        sync_speedtest(db, client)

        _regenerate_visuals()

        prev_congestion = set(curr_congestion.keys())

        log.info("Poll complete — %d congestion flags", len(curr_congestion))
        time.sleep(interval)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--url",      default="https://192.168.1.1")
    parser.add_argument("--site",     default="default")
    args = parser.parse_args()

    api_key = os.environ.get("UNIFI_API_KEY")
    if not api_key:
        print("Set UNIFI_API_KEY env var.", file=sys.stderr)
        sys.exit(1)

    client = UnifiClient(args.url, api_key, site=args.site)
    run(args.interval, client)


if __name__ == "__main__":
    main()
