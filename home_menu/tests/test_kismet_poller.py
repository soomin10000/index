"""Tests for the pure classification/grouping logic in pollers/kismet.py —
noise detection, SSID grouping — that back the /kismet decluttering."""
import os

os.environ.setdefault('KISMET_USER', 'test')
os.environ.setdefault('KISMET_PASS', 'test')

import kismet


def _client(mac, known=False, ssid='', probes=None, signal=-50):
    return {'mac': mac, 'type': 'Wi-Fi Client', 'known': known, 'ssid': ssid,
            'probes': probes or [], 'signal': signal}


def _ap(ssid, mac, signal=-50, crypt='WPA2 WPA2-PSK AES-CCMP',
        manuf='Ubiquiti Inc', last_time=100):
    return {'type': 'Wi-Fi AP', 'mac': mac, 'ssid': ssid, 'signal': signal,
            'crypt': crypt, 'manuf': manuf, 'last_time': last_time}


# ── _mac_is_random ───────────────────────────────────────────────────────────
def test_mac_is_random_true_for_locally_administered_bit():
    assert kismet._mac_is_random('2A:11:22:33:44:55') is True


def test_mac_is_random_false_for_vendor_mac():
    assert kismet._mac_is_random('00:1A:2B:33:44:55') is False


def test_mac_is_random_handles_garbage():
    assert kismet._mac_is_random('') is False
    assert kismet._mac_is_random(None) is False


# ── _is_noise ─────────────────────────────────────────────────────────────────
def test_is_noise_true_for_unresolved_randomized_silent_client():
    assert kismet._is_noise(_client('2A:11:22:33:44:55')) is True


def test_is_noise_false_when_known():
    assert kismet._is_noise(_client('2A:11:22:33:44:55', known=True)) is False


def test_is_noise_false_when_vendor_mac():
    assert kismet._is_noise(_client('00:1A:2B:33:44:55')) is False


def test_is_noise_false_when_ssid_present():
    assert kismet._is_noise(_client('2A:11:22:33:44:55', ssid='SomeNetwork')) is False


def test_is_noise_false_when_probes_present():
    assert kismet._is_noise(_client('2A:11:22:33:44:55', probes=['HomeWifi'])) is False


# ── _group_networks ────────────────────────────────────────────────────────
def test_group_networks_collapses_same_ssid():
    devices = [
        _ap('couldbe', 'AA:AA:AA:AA:AA:01', signal=-60),
        _ap('couldbe', 'AA:AA:AA:AA:AA:02', signal=-40),
        _ap('Neighbour', 'BB:BB:BB:BB:BB:01', signal=-70),
    ]
    nets = kismet._group_networks(devices)
    assert len(nets) == 2
    couldbe = next(n for n in nets if n['ssid'] == 'couldbe')
    assert couldbe['bssid_count'] == 2
    assert couldbe['strongest_signal'] == -40


def test_group_networks_sorted_strongest_first():
    devices = [
        _ap('Weak', 'AA:AA:AA:AA:AA:01', signal=-80),
        _ap('Strong', 'BB:BB:BB:BB:BB:01', signal=-30),
    ]
    nets = kismet._group_networks(devices)
    assert [n['ssid'] for n in nets] == ['Strong', 'Weak']


def test_group_networks_skips_hidden_ssid_aps():
    assert kismet._group_networks([_ap('', 'AA:AA:AA:AA:AA:01')]) == []


def test_group_networks_skips_client_devices():
    assert kismet._group_networks([_client('AA:AA:AA:AA:AA:01', ssid='irrelevant')]) == []
