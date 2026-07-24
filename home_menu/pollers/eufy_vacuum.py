#!/usr/bin/env python3
"""Poll the Eufy X8 Pro robovac over local Tuya (v3.5, port 6668) and write
home_menu/data/eufy_vacuum.json for the /eufy dashboard page.

The Tuya local_key rotates whenever the robot reconnects to Eufy's cloud
(observed several times a day), so it's cached in eufy_vacuum_state.json and
only re-fetched via a full cloud login when a local poll fails — avoids
hitting Eufy's login endpoint every cron tick while still self-healing.
"""
import json
import os
import sys
import time
from pathlib import Path

import tinytuya

sys.path.insert(0, str(Path(__file__).parent))
from eufy_vacuum_client import get_vacuum_credentials, EufyAuthError

DATA = Path(__file__).parent.parent / "data"
OUT_JSON = DATA / "eufy_vacuum.json"
STATE_JSON = DATA / "eufy_vacuum_state.json"

EUFY_EMAIL = os.environ.get("EUFY_EMAIL", "")
EUFY_PASSWORD = os.environ.get("EUFY_PASSWORD", "")
VACUUM_IP = os.environ.get("EUFY_VACUUM_IP", "192.168.1.51")

DPS_POWER, DPS_ACTIVATE, DPS_WORK_MODE = "1", "2", "5"
DPS_WORK_STATUS, DPS_RETURN_HOME, DPS_CLEAN_SPEED = "15", "101", "102"
DPS_BATTERY, DPS_ERROR_CODE, DPS_DO_NOT_DISTURB = "104", "106", "107"
DPS_CLEANING_TIME, DPS_CLEANING_AREA = "109", "110"
DPS_BOOST_IQ, DPS_AUTO_RETURN = "118", "135"

ACTIVITY_MAP = {
    "Sleeping": "docked", "Charging": "docked",
    "Running": "cleaning", "Goto": "cleaning",
    "Recharge": "returning",
    "Completed": "idle", "standby": "idle", "Locating": "idle",
}


def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    tmp = STATE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_JSON)


def _write_output(data: dict) -> None:
    tmp = OUT_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(OUT_JSON)


def _poll_dps(device_id: str, local_key: str) -> dict | None:
    d = tinytuya.Device(dev_id=device_id, address=VACUUM_IP, local_key=local_key, version=3.5)
    d.set_socketTimeout(8)
    status = d.status()
    if not status or "dps" not in status:
        return None
    return status["dps"]


def main() -> None:
    if not EUFY_EMAIL or not EUFY_PASSWORD:
        _write_output({"ts": time.time(), "error": "EUFY_EMAIL/EUFY_PASSWORD not set"})
        print("EUFY_EMAIL/EUFY_PASSWORD not set", file=sys.stderr)
        sys.exit(1)

    state = _load_state()
    device_id, local_key, name = state.get("device_id"), state.get("local_key"), state.get("name")

    dps = None
    if device_id and local_key:
        try:
            dps = _poll_dps(device_id, local_key)
        except Exception as e:
            print(f"local poll with cached key failed: {e}", file=sys.stderr)

    if dps is None:
        try:
            creds = get_vacuum_credentials(EUFY_EMAIL, EUFY_PASSWORD)
        except EufyAuthError as e:
            _write_output({"ts": time.time(), "error": str(e)})
            print(f"cloud auth failed: {e}", file=sys.stderr)
            sys.exit(1)
        device_id, local_key, name = creds["device_id"], creds["local_key"], creds["name"]
        _save_state({"device_id": device_id, "local_key": local_key, "name": name, "refreshed_at": time.time()})
        try:
            dps = _poll_dps(device_id, local_key)
        except Exception as e:
            _write_output({"ts": time.time(), "error": f"local poll failed even after key refresh: {e}"})
            print(f"local poll failed even after key refresh: {e}", file=sys.stderr)
            sys.exit(1)

    if dps is None:
        _write_output({"ts": time.time(), "error": "no DPS data in device response"})
        sys.exit(1)

    work_status = dps.get(DPS_WORK_STATUS)
    _write_output({
        "ts": time.time(),
        "name": name,
        "online": True,
        "power": dps.get(DPS_POWER),
        "activating": dps.get(DPS_ACTIVATE),
        "work_mode": dps.get(DPS_WORK_MODE),
        "work_status": work_status,
        "activity": ACTIVITY_MAP.get(work_status, work_status),
        "returning_home": dps.get(DPS_RETURN_HOME),
        "fan_speed": dps.get(DPS_CLEAN_SPEED),
        "battery": dps.get(DPS_BATTERY),
        "error_code": dps.get(DPS_ERROR_CODE),
        "do_not_disturb": dps.get(DPS_DO_NOT_DISTURB),
        "cleaning_time_s": dps.get(DPS_CLEANING_TIME),
        "cleaning_area_m2": dps.get(DPS_CLEANING_AREA),
        "boost_iq": dps.get(DPS_BOOST_IQ),
        "auto_return": dps.get(DPS_AUTO_RETURN),
    })


if __name__ == "__main__":
    main()
