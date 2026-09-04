"""
UniFi poller — long-running poll loop.

Run with:
    UNIFI_API_KEY=... python3 poller.py
    UNIFI_API_KEY=... python3 poller.py --interval 120
"""

import argparse
import functools
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "ubuntu-sender"))

from unifi_client import UnifiClient, UnifiAuthError
from checks import check_congestion
import topology
import dashboard
from db import (open_db, log_poll, last_flagged_congestion,
                check_new_devices, log_speedtest, log_event, get_known_devices,
                log_radio_util, prune_radio_util)

try:
    from notify_sender import notify as _notify_send
    def _notify(title, message, **kwargs):
        def _send():
            results = _notify_send(title, message, **kwargs)
            for host, status in results.items():
                if status != "ok":
                    log.warning("Notification to %s failed: %s", host, status)
        threading.Thread(target=_send, daemon=True).start()
except ImportError:
    def _notify(title, message, **kwargs):
        log.warning("notify_sender unavailable — would have sent: [%s] %s", title, message)

LOG_FILE    = Path(__file__).resolve().parents[2] / "logs" / "unifi.log"
DEVICES_OUT = Path(__file__).resolve().parents[2] / "data" / "unifi" / "devices.json"

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


# ── Vendor lookup & device type guessing ─────────────────────────────────────

_mac_lookup = None

def _vendor(mac):
    global _mac_lookup
    if _mac_lookup is None:
        from mac_vendor_lookup import MacLookup
        _mac_lookup = MacLookup()
    return _vendor_cached(mac)


# Randomized-MAC clients (most phones, per-SSID) mean this cache would otherwise
# grow forever — one-off entries that are never looked up again. Bound it with an
# LRU so stale random MACs get evicted instead of accumulating for the process's
# entire lifetime.
@functools.lru_cache(maxsize=4096)
def _vendor_cached(mac):
    try:
        return _mac_lookup.lookup(mac)
    except Exception:
        return ""


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


def _display_name(mac, hostname="", known_devs=None):
    """Same fallback chain as write_devices_json's per-station display_name:
    hostname → guessed device type from vendor → raw vendor string → mac.
    Needed for events (e.g. device_left) where we only have a bare mac and no
    live station record to pull a hostname from."""
    if hostname:
        return hostname
    kd = (known_devs or {}).get(mac, {})
    hostname = kd.get("hostname") or ""
    if hostname:
        return hostname
    vendor = kd.get("vendor") or _vendor(mac)
    guess = _guess_device_type(vendor)
    return guess or vendor or mac


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

def write_devices_json(devices, stations, db=None, weak_flags=None):
    try:
        weak_macs = {f["mac"] for f in (weak_flags or []) if f.get("mac")}

        dev_by_mac  = {d["mac"]: d for d in devices}
        ap_by_mac   = {d["mac"]: d.get("name", d["mac"]) for d in devices if d.get("type") == "uap"}
        known_devs  = get_known_devices(db) if db else {}

        out = []
        for sta in stations:
            mac      = sta.get("mac", "")
            hostname = sta.get("hostname", "")
            vendor   = _vendor(mac)
            flagged  = mac in weak_macs
            ap_mac   = sta.get("ap_mac", "")

            if hostname:
                display_name = hostname
                guessed      = False
            else:
                guess        = _guess_device_type(vendor)
                display_name = guess or vendor or mac
                guessed      = bool(guess or vendor)

            kd = known_devs.get(mac, {})
            ipv6 = [a for a in sta.get("ipv6_addresses", []) or sta.get("last_ipv6", [])
                    if not a.startswith("fe80:")]  # drop link-local, keep routable/privacy addresses
            out.append({
                "mac":          mac,
                "hostname":     hostname,
                "display_name": display_name,
                "guessed":      guessed,
                "ip":           sta.get("ip", ""),
                "ipv6":         ipv6,
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

def _regenerate_visuals(devices, stations, wlans):
    try:
        topology.render(devices, stations, wlans)
        log.info("Regenerated topology")
    except Exception as e:
        log.warning("Failed to regenerate topology: %s", e)

    try:
        dashboard.render()
        log.info("Regenerated dashboard")
    except Exception as e:
        log.warning("Failed to regenerate dashboard: %s", e)

    # pollers/pihole.py now runs on its own cron schedule (needs PIHOLE_PASSWORD,
    # which unifi-poller.service's systemd environment doesn't have and we can't
    # add without an interactive sudo password to edit the unit file).


# ── Poll helpers ──────────────────────────────────────────────────────────────

def _congestion_key(f): return f"{f['ap']}:{f['radio']}"


def _radio_samples(devices):
    """Every AP radio's current cu_total/num_sta/channel, unconditionally —
    the continuous feed check_congestion()'s threshold filtering can't give us."""
    samples = []
    for dev in devices:
        radio_stats = dev.get("radio_table_stats")
        if not radio_stats:
            continue
        ap_name = dev.get("name", dev.get("mac", "unknown"))
        for radio in radio_stats:
            samples.append({
                "ap":       ap_name,
                "radio":    radio.get("name", radio.get("radio", "unknown")),
                "cu_total": radio.get("cu_total"),
                "num_sta":  radio.get("num_sta"),
                "channel":  radio.get("channel"),
            })
    return samples


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(interval, client):
    db = open_db()
    prev_congestion = last_flagged_congestion(db, within_seconds=interval * 2)
    if prev_congestion:
        log.info("Resuming — suppressing re-notification for: %s", prev_congestion)

    # Speed test: once per hour
    speedtest_every = max(1, 3600 // interval)
    # Prune old radio_util_log rows: once per day
    prune_every = max(1, 86400 // interval)
    # Regenerate the topology / dashboard PNGs at most every 15 min, not every
    # poll — matplotlib re-rendering two large figures on a 5-min loop was the
    # bulk of this process's CPU and drove a slow RSS ratchet via heap
    # fragmentation. The visuals don't need 5-min freshness.
    visuals_every = max(1, 900 // interval)
    poll_count = 0

    # Seeded on the first poll below (not here) so a process restart doesn't
    # read "everyone just joined" / "every radio just changed channel" off an
    # empty baseline.
    prev_active_macs = None
    prev_channels = None

    log.info("Poll loop started — interval %ds, speedtest every %d polls, visuals every %d polls",
             interval, speedtest_every, visuals_every)

    while True:
        try:
            devices  = client.get_devices()
            stations = client.get_clients()
            wlans    = client.get_wlans()
        except UnifiAuthError as e:
            log.error("Auth error: %s", e)
            time.sleep(interval)
            continue
        except Exception as e:
            log.error("Poll failed: %s", e)
            time.sleep(interval)
            continue

        congestion      = check_congestion(devices)
        weak_clients    = []  # weak-client flagging disabled 2026-08-30 — home
                              # setup, a weak signal here isn't worth acting on.
                              # check_weak_clients() kept in checks.py if ever wanted.
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

        # Continuous utilization/channel logging — every radio, every poll,
        # unlike congestion_log which only gets a row once cu_total > threshold.
        # Powers the dashboard's time-of-day trend view.
        radio_samples = _radio_samples(devices)
        log_radio_util(db, radio_samples)

        # Device join/leave — separate from check_new_devices() above, which
        # only fires once per MAC ever. This tracks online/offline transitions
        # for already-known devices, for correlating against congestion spikes.
        try:
            curr_active_macs = {s["mac"] for s in stations if s.get("mac")}
            if prev_active_macs is not None:
                known_devs = get_known_devices(db)
                for mac in curr_active_macs - prev_active_macs:
                    sta = next((s for s in stations if s.get("mac") == mac), {})
                    name = _display_name(mac, sta.get("hostname", ""), known_devs)
                    log_event(db, "device_joined", "Device joined", name)
                for mac in prev_active_macs - curr_active_macs:
                    name = _display_name(mac, "", known_devs)
                    log_event(db, "device_left", "Device left", name)
            prev_active_macs = curr_active_macs
        except Exception as e:
            log.warning("Join/leave tracking failed: %s", e)

        # Channel changes — the real UniFi event/alarm API (EVT_AP_RadarDetected
        # etc.) isn't reachable with this console's API-key auth (confirmed 404).
        # An "auto" radio's channel changing is a practical proxy: APs switch
        # channel automatically on DFS radar detection, so this catches those
        # along with any other forced channel move worth correlating.
        try:
            curr_channels = {(s["ap"], s["radio"]): s["channel"] for s in radio_samples}
            if prev_channels is not None:
                for key, channel in curr_channels.items():
                    prev = prev_channels.get(key)
                    if prev is not None and prev != channel:
                        ap, radio = key
                        msg = f"{ap} {radio}: channel {prev} → {channel}"
                        log.info("Channel change: %s", msg)
                        log_event(db, "channel_changed", "Channel changed", msg)
            prev_channels = curr_channels
        except Exception as e:
            log.warning("Channel-change tracking failed: %s", e)

        log_poll(db, congestion, weak_clients)
        write_devices_json(devices, stations, db=db, weak_flags=weak_clients)

        poll_count += 1
        if poll_count % speedtest_every == 0:
            trigger_speedtest(client)
        if poll_count % prune_every == 0:
            prune_radio_util(db)
        sync_speedtest(db, client)

        # First poll always renders (so a restart refreshes the PNGs promptly),
        # then only every visuals_every-th poll.
        if poll_count == 1 or poll_count % visuals_every == 0:
            _regenerate_visuals(devices, stations, wlans)

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
