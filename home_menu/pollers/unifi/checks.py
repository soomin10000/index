"""
Threshold checks against live UniFi data.

These operate on the data returned by UnifiClient.get_devices() / get_clients(),
confirmed against real responses from the console at 192.168.1.1 on 2026-06-29.

Notes from real data inspection:
- get_devices() returns switches AND APs. Only AP entries carry
  "radio_table_stats" — a switch like the USW Flex 2.5G will not have it,
  so check_congestion() naturally yields nothing for switch-only devices.
  This is expected, not a bug.
- get_clients() entries carry "rssi" only for wireless clients; wired clients
  won't have a meaningful rssi value and are skipped automatically since we
  guard on `rssi is not None`.
"""

import logging
import time

logger = logging.getLogger(__name__)


def check_congestion(devices, cu_threshold=70):
    """
    Flags AP radios with channel utilization above cu_threshold (%).
    Returns a list of dicts: {ap, radio, cu_total, num_sta}
    """
    flags = []
    for dev in devices:
        radio_stats = dev.get("radio_table_stats")
        if not radio_stats:
            # Switches and other non-AP devices won't have this field. Skip silently.
            continue
        for radio in radio_stats:
            cu_total = radio.get("cu_total", 0)
            if cu_total > cu_threshold:
                flags.append({
                    "ap": dev.get("name", dev.get("mac", "unknown")),
                    "radio": radio.get("name", radio.get("radio", "unknown")),
                    "cu_total": cu_total,
                    "num_sta": radio.get("num_sta"),
                })
    return flags


def check_weak_clients(stations, signal_floor=-74, retry_ceiling=15):
    """
    Flags wireless clients with a weak signal or a high TX-retry rate.

    Returns a list of dicts: {mac, hostname, signal, retry_pct, essid}. Wired
    clients and any station without a signal reading are skipped. The defaults
    match the page's own bands (see signalCell() in pages/unifi.html) so the
    "flagged" dots and the drawer's signal history agree with the live table.
    """
    flags = []
    for sta in stations:
        if sta.get("is_wired"):
            continue
        signal = sta.get("signal")
        if signal is None:
            continue
        retry = sta.get("wifi_tx_retries_percentage")
        if signal <= signal_floor or (retry is not None and retry >= retry_ceiling):
            flags.append({
                "mac":       sta.get("mac", ""),
                "hostname":  sta.get("hostname") or sta.get("mac", ""),
                "signal":    signal,
                "retry_pct": retry,
                "essid":     sta.get("essid", ""),
            })
    return flags


def check_port_flapping(devices, baseline, delta_threshold=5):
    """
    Flags switch ports whose link_down_count has risen by at least
    delta_threshold since the baseline sample (see db.port_flap_baseline()).

    link_down_count/stp_state_change_count are lifetime-cumulative on the
    device (only reset on reboot), so this only fires on the *rate of
    increase* over the baseline window, not the raw value — a port with a
    normal history of occasional reconnects would otherwise trip permanently.
    A port not yet covered by a full baseline window (just started polling,
    or a brand-new/moved port) is skipped rather than flagged — same
    cold-start tradeoff as check_congestion's threshold crossing.

    Returns a list of dicts: {sw_name, sw_mac, port_idx, port_name, delta,
    window_min, connected_mac}
    """
    flags = []
    for dev in devices:
        sw_mac = dev.get("mac")
        sw_name = dev.get("name", sw_mac or "unknown")
        for p in dev.get("port_table", []):
            if not p.get("up"):
                continue
            key = (sw_mac, p.get("port_idx"))
            base = baseline.get(key)
            if not base:
                continue
            base_count, base_ts = base
            curr_count = p.get("link_down_count")
            if curr_count is None:
                continue
            delta = curr_count - base_count
            if delta >= delta_threshold:
                window_min = max(1, round((time.time() - base_ts) / 60))
                flags.append({
                    "sw_name":      sw_name,
                    "sw_mac":       sw_mac,
                    "port_idx":     p.get("port_idx"),
                    "port_name":    p.get("name", f"Port {p.get('port_idx')}"),
                    "delta":        delta,
                    "window_min":   window_min,
                    "connected_mac": (p.get("last_connection") or {}).get("mac"),
                })
    return flags


if __name__ == "__main__":
    # Manual smoke test against the real console.
    # Run with: UNIFI_API_KEY=... python3 checks.py
    import os
    import sys
    import json
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO)

    sys.path.insert(0, os.path.dirname(__file__))
    from unifi_client import UnifiClient

    api_key = os.environ.get("UNIFI_API_KEY")
    if not api_key:
        print("Set UNIFI_API_KEY env var before running this smoke test.", file=sys.stderr)
        sys.exit(1)

    client = UnifiClient("https://192.168.1.1", api_key)

    print("--- Congestion check (cu_threshold=70) ---")
    congestion = check_congestion(client.get_devices())
    print(json.dumps(congestion, indent=2) if congestion else "No congestion flags.")

    print("\n--- Weak-client check (signal_floor=-74, retry_ceiling=15) ---")
    weak = check_weak_clients(client.get_clients())
    print(json.dumps(weak, indent=2) if weak else "No weak clients.")

    devices  = client.get_devices()
    stations = client.get_clients()
    print(f"\nTotal devices seen: {len(devices)}")
    print(f"Total clients seen: {len(stations)}")
