"""Polls Kismet REST API and writes kismet.json for the web dashboard."""

import json
import os
import time
import urllib.request
import urllib.parse
import base64
from pathlib import Path

KISMET_URL   = os.environ.get('KISMET_URL', 'http://localhost:2501')
KISMET_USER  = os.environ.get('KISMET_USER')
KISMET_PASS  = os.environ.get('KISMET_PASS')
KISMET_IFACE = os.environ.get('KISMET_IFACE', 'wlx00c0cabaea2c')

if not KISMET_USER or not KISMET_PASS:
    raise SystemExit('Set KISMET_USER and KISMET_PASS env vars before running kismet_poller.py')

OUT = Path(__file__).parent / 'kismet.json'
UNIFI_DEVICES = Path(__file__).parent / 'unifi_poller' / 'devices.json'

DEVICE_FIELDS = [
    'kismet.device.base.macaddr',
    'kismet.device.base.type',
    'kismet.device.base.name',
    'kismet.device.base.last_time',
    'kismet.device.base.first_time',
    'kismet.device.base.signal/kismet.common.signal.last_signal',
    'kismet.device.base.channel',
    'kismet.device.base.manuf',
    'dot11.device/dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ssid',
    'dot11.device/dot11.device.probed_ssid_map',
]


def _auth_header():
    token = base64.b64encode(f'{KISMET_USER}:{KISMET_PASS}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def _get(path):
    req = urllib.request.Request(f'{KISMET_URL}{path}', headers=_auth_header())
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _post(path, data):
    body = urllib.parse.urlencode({'json': json.dumps(data)}).encode()
    req = urllib.request.Request(
        f'{KISMET_URL}{path}', data=body,
        headers={**_auth_header(), 'Content-Type': 'application/x-www-form-urlencoded'}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def ensure_datasource():
    """Kismet forgets its datasource on every restart (site config is root-owned
    and we don't have a password for sudo to persist it there), so re-add the
    capture interface here if it's missing. Idempotent — no-op if already present.
    """
    sources = _get('/datasource/all_sources.json')
    if sources:
        return
    definition = f'{KISMET_IFACE}:name=alfa5,type=linuxwifi'
    _post('/datasource/add_source.cmd', {'definition': definition})
    print(f'No datasource found — added {definition}')
    time.sleep(5)  # give the capture helper a moment to spin up


def fetch_and_write():
    now = int(time.time())

    ensure_datasource()

    # System status
    status = _get('/system/status.json')
    device_count = status.get('kismet.system.devices.count', 0)
    start_sec    = status.get('kismet.system.timestamp.start_sec', now)

    # All devices with field filter
    raw = _post('/devices/views/all/devices.json', {'fields': DEVICE_FIELDS})

    devices = []
    for d in raw:
        mac      = d.get('kismet.device.base.macaddr', '')
        dev_type = d.get('kismet.device.base.type', '')
        name     = d.get('kismet.device.base.name', mac)
        ssid     = d.get('dot11.advertisedssid.ssid', '')
        probes   = d.get('dot11.device.probed_ssid_map', 0)
        probe_list = list(probes.keys()) if isinstance(probes, dict) else []

        devices.append({
            'mac':        mac,
            'type':       dev_type,
            'name':       name,
            'ssid':       ssid,
            'probes':     probe_list,
            'signal':     d.get('kismet.common.signal.last_signal', 0),
            'channel':    d.get('kismet.device.base.channel', ''),
            'manuf':      d.get('kismet.device.base.manuf', ''),
            'first_time': d.get('kismet.device.base.first_time', 0),
            'last_time':  d.get('kismet.device.base.last_time', 0),
        })

    # Sort: APs first, then by signal descending
    devices.sort(key=lambda d: (0 if 'AP' in d['type'] else 1, -(d['signal'] or -100)))

    # Alerts
    raw_alerts = _get('/alerts/all_alerts.json')
    alerts = []
    for a in raw_alerts:
        header = a.get('kismet.alert.header', '')
        if header == 'ROOTUSER':
            continue  # suppress the known root warning
        alerts.append({
            'header':    header,
            'class':     a.get('kismet.alert.class', ''),
            'severity':  a.get('kismet.alert.severity', 0),
            'ts':        int(a.get('kismet.alert.timestamp', 0)),
            'text':      a.get('kismet.alert.text', ''),
            'source_mac': a.get('kismet.alert.source_mac', ''),
        })
    alerts.sort(key=lambda a: -a['ts'])

    # Cross-reference: flag MACs not seen in UniFi
    known_macs = set()
    if UNIFI_DEVICES.exists():
        try:
            ud = json.loads(UNIFI_DEVICES.read_text())
            known_macs = {d.get('mac', '').lower() for d in ud.get('devices', [])}
        except Exception:
            pass

    for d in devices:
        d['known'] = d['mac'].lower() in known_macs

    unknown = [d for d in devices if not d['known'] and 'Client' in d['type']]

    data = {
        'ts':           now,
        'uptime_sec':   now - start_sec,
        'device_count': device_count,
        'devices':      devices,
        'alerts':       alerts,
        'unknown_clients': unknown,
    }

    OUT.write_text(json.dumps(data, indent=2))
    aps      = sum(1 for d in devices if 'AP' in d['type'])
    clients  = sum(1 for d in devices if 'Client' in d['type'])
    print(f'Saved {OUT} — {device_count} devices ({aps} APs, {clients} clients), '
          f'{len(alerts)} alerts, {len(unknown)} unknown clients')


if __name__ == '__main__':
    fetch_and_write()
