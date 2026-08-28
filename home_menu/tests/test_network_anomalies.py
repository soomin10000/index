import server


def test_hostnames_exact():
    assert server._hostnames_match("LivingRoomTV", "livingroomtv") is True


def test_hostnames_empty():
    assert server._hostnames_match("", "anything") is False
    assert server._hostnames_match(None, None) is False


def test_hostnames_short_generic_no_match():
    # both under the 8-char guard -> never matches even with shared prefix
    assert server._hostnames_match("tv", "tv2") is False


def test_hostnames_serial_suffix_extension():
    a = "bosch-dishwasher"
    b = "bosch-dishwasher-68a40e"
    assert server._hostnames_match(a, b) is True
    assert server._hostnames_match(b, a) is True


def test_hostnames_suffix_too_long():
    a = "bosch-dishwasher"
    b = "bosch-dishwasher-0123456789"  # >8 char suffix
    assert server._hostnames_match(a, b) is False


def test_mac_is_random():
    assert server._mac_is_random("f2:3c:91:aa:bb:cc") is True   # 0xf2 -> bit1 set
    assert server._mac_is_random("f4:3c:91:aa:bb:cc") is False  # 0xf4 -> bit1 clear
    assert server._mac_is_random("") is False
    assert server._mac_is_random("zz:zz") is False


def test_network_anomalies_evil_twin_weaker_crypto():
    # One of my SSIDs, normally WPA2, also being broadcast Open by an unmanaged BSSID.
    unifi_devices = [{"ssid": "HomeNet", "mac": "aa:aa:aa:aa:aa:aa", "crypt": "WPA2"}]
    ks = {
        "devices": [
            {"ssid": "HomeNet", "mac": "aa:aa:aa:aa:aa:aa", "type": "Wi-Fi AP",
             "crypt": "WPA2", "signal": -40},
            {"ssid": "HomeNet", "mac": "de:ad:be:ef:00:01", "type": "Wi-Fi AP",
             "crypt": "Open", "signal": -55},
        ],
        "unknown_clients": [],
    }
    out = server._network_anomalies(unifi_devices, {}, ks)
    assert "HomeNet" in out["my_ssids"]
    macs = [a.get("mac") for a in out["anomalies"]]
    assert "de:ad:be:ef:00:01" in macs


def test_network_anomalies_clean_when_all_match():
    unifi_devices = [{"ssid": "HomeNet", "mac": "aa:aa:aa:aa:aa:aa", "crypt": "WPA2"}]
    ks = {
        "devices": [
            {"ssid": "HomeNet", "mac": "aa:aa:aa:aa:aa:aa", "type": "Wi-Fi AP",
             "crypt": "WPA2", "signal": -40},
            {"ssid": "HomeNet", "mac": "aa:aa:aa:aa:aa:bb", "type": "Wi-Fi AP",
             "crypt": "WPA2", "signal": -50},
        ],
        "unknown_clients": [],
    }
    out = server._network_anomalies(unifi_devices, {}, ks)
    assert not any(a["kind"] == "evil_twin" for a in out["anomalies"])
