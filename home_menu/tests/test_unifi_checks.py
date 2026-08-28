import sys
from pathlib import Path

_UNIFI = Path(__file__).resolve().parent.parent / "pollers" / "unifi"
if str(_UNIFI) not in sys.path:
    sys.path.insert(0, str(_UNIFI))

from checks import check_weak_clients


def _sta(**kw):
    base = {"mac": "aa:bb:cc:dd:ee:ff", "hostname": "phone", "is_wired": False,
            "signal": -55, "wifi_tx_retries_percentage": 2, "essid": "couldbe"}
    base.update(kw)
    return base


def test_healthy_client_not_flagged():
    assert check_weak_clients([_sta()]) == []


def test_wired_client_skipped():
    assert check_weak_clients([_sta(is_wired=True, signal=-90)]) == []


def test_missing_signal_skipped():
    assert check_weak_clients([_sta(signal=None, wifi_tx_retries_percentage=None)]) == []


def test_weak_by_signal():
    flags = check_weak_clients([_sta(signal=-80)])
    assert len(flags) == 1
    assert flags[0]["signal"] == -80
    assert flags[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert set(flags[0]) == {"mac", "hostname", "signal", "retry_pct", "essid"}


def test_signal_floor_is_inclusive():
    assert len(check_weak_clients([_sta(signal=-74)])) == 1
    assert check_weak_clients([_sta(signal=-73)]) == []


def test_weak_by_retry_rate():
    flags = check_weak_clients([_sta(signal=-50, wifi_tx_retries_percentage=20)])
    assert len(flags) == 1
    assert flags[0]["retry_pct"] == 20


def test_hostname_falls_back_to_mac():
    flags = check_weak_clients([_sta(hostname="", signal=-85)])
    assert flags[0]["hostname"] == "aa:bb:cc:dd:ee:ff"
