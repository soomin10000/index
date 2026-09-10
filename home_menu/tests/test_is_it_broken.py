import json
import time

import pytest

import server


@pytest.fixture
def broken_env(tmp_path, monkeypatch):
    """Point _is_it_broken_status at a scratch data dir with fresh, healthy
    uplink + devices files, and a healthy speedtest. Tests tweak the bits they
    care about via the returned mutable dicts + a re-write helper."""
    now = int(time.time())
    uplink = {'ts': now, 'probe': {'connected': True}, 'bgp': {'status': 'ok'}}
    devices = {'ts': now, 'devices': [{'name': 'a'}, {'name': 'b'}]}

    up_path = tmp_path / 'uplink.json'
    dev_path = tmp_path / 'devices.json'

    def write():
        up_path.write_text(json.dumps(uplink))
        dev_path.write_text(json.dumps(devices))

    write()
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DEVICES_JSON', dev_path)
    monkeypatch.setattr(server, '_unifi_status', lambda: {
        'speedtest': {'ts': now, 'download_mbps': 900.0, 'upload_mbps': 900.0},
    })
    return uplink, devices, write


def test_all_healthy(broken_env):
    assert server._is_it_broken_status()['overall'] == 'ok'


def test_bgp_unknown_does_not_warn(broken_env):
    """RIPEstat 500s leave bgp.status == 'unknown' — that's a failed *check*,
    not a routing fault, so the kids' page must stay green."""
    uplink, _devices, write = broken_env
    uplink['bgp'] = {'status': 'unknown', 'notes': ['RIPEstat fetch failed: 500']}
    write()
    status = server._is_it_broken_status()
    assert status['checks']['internet']['state'] == 'ok'
    assert status['overall'] == 'ok'


def test_bgp_alert_warns(broken_env):
    uplink, _devices, write = broken_env
    uplink['bgp'] = {'status': 'alert', 'notes': ['prefix not announced']}
    write()
    status = server._is_it_broken_status()
    assert status['checks']['internet']['state'] == 'warn'
    assert status['overall'] == 'warn'


def test_probe_disconnected_is_down(broken_env):
    uplink, _devices, write = broken_env
    uplink['probe'] = {'connected': False}
    write()
    status = server._is_it_broken_status()
    assert status['checks']['internet']['state'] == 'down'
    assert status['overall'] == 'down'


def test_stale_uplink_is_down(broken_env):
    uplink, _devices, write = broken_env
    uplink['ts'] = int(time.time()) - server.STALE_SECONDS - 60
    write()
    assert server._is_it_broken_status()['checks']['internet']['state'] == 'down'
