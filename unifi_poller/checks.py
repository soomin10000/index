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

    devices  = client.get_devices()
    stations = client.get_clients()
    print(f"\nTotal devices seen: {len(devices)}")
    print(f"Total clients seen: {len(stations)}")
