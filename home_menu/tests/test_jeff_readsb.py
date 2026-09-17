"""Tests for the readsb / ADS-B deaf detection in pollers/jeff.py."""
import jeff


# ── _parse_adsb: freshness / staleness ─────────────────────────────────────
def _adsb_block(total=12, pos=8, file_ts=1000):
    return '{"total":%d,"pos":%d,"file_ts":%d}' % (total, pos, file_ts)


_STATS_BLOCK = '{"msgs_last_min":9000,"max_dist_m":120000}'


def test_parse_adsb_fresh():
    a = jeff._parse_adsb(_adsb_block(file_ts=1000), _STATS_BLOCK, "1010")
    assert a["aircraft"] == 12 and a["positions"] == 8
    assert a["msgs_per_sec"] == 150.0
    assert a["feed_age"] == 10 and a["stale"] is False


def test_parse_adsb_stale_feed():
    a = jeff._parse_adsb(_adsb_block(total=40, file_ts=1000), _STATS_BLOCK, "1400")
    assert a["feed_age"] == 400 and a["stale"] is True


def test_parse_adsb_none_when_readsb_absent():
    assert jeff._parse_adsb("{}", "{}", "1000") is None


def test_parse_adsb_no_remote_now():
    a = jeff._parse_adsb(_adsb_block(), _STATS_BLOCK, "")
    assert a["feed_age"] is None and a["stale"] is False


def test_parse_adsb_flights_absent_is_empty_list():
    a = jeff._parse_adsb(_adsb_block(), _STATS_BLOCK, "1010")
    assert a["flights"] == []


def test_parse_adsb_flights_parsed_and_trimmed():
    block = ('{"total":3,"pos":2,"file_ts":1000,"flights":['
             '{"cs":"BAW123 ","hex":"400a1b","alt":37000,"gs":450,"trk":270,"lat":51.4,"lon":-0.4},'
             '{"cs":"  ","hex":"xxx","lat":1,"lon":2},'
             '{"cs":"RYR4KP","alt":"ground","lat":53.4,"lon":-2.2}]}')
    a = jeff._parse_adsb(block, _STATS_BLOCK, "1010")
    assert [f["cs"] for f in a["flights"]] == ["BAW123", "RYR4KP"]   # blank dropped, trimmed
    assert a["flights"][0]["track"] == 270 and a["flights"][0]["alt"] == 37000
    assert a["flights"][1]["alt"] == "ground"


# ── _extra_alerts: the three deaf shapes ──────────────────────────────────
def _data(services=("readsb.service", "piaware.service"), dongle=True, adsb=None):
    return {
        "cpu_temp": 45.0,
        "throttled": {"flags": []},
        "other_failed": [],
        "sdr": {"dongle_present": dongle, "dvb_driver_loaded": False,
                "sdr_driver_loaded": True, "services": list(services)},
        "adsb": adsb,
    }


def test_alert_zero_traffic():
    d = _data(adsb={"aircraft": 0, "msgs_per_sec": 0, "stale": False, "feed_age": 5})
    a = jeff._extra_alerts(d)
    assert a["readsb_deaf"][0] == "critical"
    assert "0 aircraft" in a["readsb_deaf"][2]


def test_alert_wedged_dongle_still_dribbling_frames():
    # half-wedged: a few frames sneak through, a stale track or two on the map —
    # still deaf, and the old aircraft==0 and msgs==0 check used to miss this
    d = _data(adsb={"aircraft": 2, "msgs_per_sec": 3.0, "stale": False, "feed_age": 4})
    a = jeff._extra_alerts(d)
    assert a["readsb_deaf"][0] == "critical"


def test_no_alert_when_traffic_light_but_real():
    # genuinely quiet sky, dongle fine — one aircraft alone clears the msg floor
    d = _data(adsb={"aircraft": 3, "msgs_per_sec": 25.0, "stale": False, "feed_age": 5})
    assert "readsb_deaf" not in jeff._extra_alerts(d)


def test_alert_frozen_feed_even_with_stale_counts():
    # readsb hung: aircraft.json is old but its last message counts look healthy
    d = _data(adsb={"aircraft": 30, "msgs_per_sec": 120.0, "stale": True, "feed_age": 300})
    a = jeff._extra_alerts(d)
    assert a["readsb_deaf"][1] == "readsb feed is frozen"
    assert "300s stale" in a["readsb_deaf"][2]


def test_alert_readsb_process_gone():
    d = _data(services=("piaware.service",),
              adsb=None)  # jq found nothing because readsb isn't running
    a = jeff._extra_alerts(d)
    assert a["readsb_deaf"][1] == "readsb is down"


def test_no_alert_when_healthy():
    d = _data(adsb={"aircraft": 25, "msgs_per_sec": 140.0, "stale": False, "feed_age": 3})
    assert "readsb_deaf" not in jeff._extra_alerts(d)


def test_no_alert_when_readsb_intentionally_absent():
    # no dongle, no piaware — jeff isn't feeding on purpose, stay quiet
    d = _data(services=(), dongle=False, adsb=None)
    assert "readsb_deaf" not in jeff._extra_alerts(d)


# ── _parse_watchdog: readsb-recover state file ───────────────────────────
def test_parse_watchdog_valid():
    block = ('{"last_fire":1000,"last_result":"recovered",'
             '"last_trigger":"auto","fires_24h":2}')
    w = jeff._parse_watchdog(block, "1180")
    assert w["installed"] is True
    assert w["last_fire"] == 1000 and w["last_fire_age"] == 180
    assert w["fires_24h"] == 2
    assert w["last_result"] == "recovered" and w["last_trigger"] == "auto"


def test_parse_watchdog_empty_means_not_installed():
    w = jeff._parse_watchdog("{}", "1180")
    assert w["installed"] is False
    assert w["last_fire"] is None and w["fires_24h"] == 0
    assert w["last_fire_age"] is None


def test_parse_watchdog_malformed_never_raises():
    for block in ("", "not json", "{oops", '{"fires_24h":"lots"}', "null"):
        w = jeff._parse_watchdog(block, "1180")
        assert w["fires_24h"] == 0 and w["installed"] is False


def test_parse_watchdog_no_remote_now():
    w = jeff._parse_watchdog('{"last_fire":1000,"fires_24h":1}', "")
    assert w["installed"] is True and w["last_fire_age"] is None


def test_parse_watchdog_clock_skew_clamps_age_to_zero():
    w = jeff._parse_watchdog('{"last_fire":2000,"fires_24h":1}', "1900")
    assert w["last_fire_age"] == 0


# ── _parse_watchdog: last-48h failure timeline (fires epoch log) ────────
def test_parse_watchdog_fails_by_bucket():
    now = 500_000
    win = jeff.WATCHDOG_FAIL_WINDOW_H          # 48
    nb = jeff.WATCHDOG_FAIL_BUCKETS            # 24  -> 2h per bucket
    # fires at ~0.5h, ~2.5h and ~3.5h ago (both in the 2h..4h bucket), plus one
    # just outside the 48h window
    fires = "\n".join(str(now - s) for s in (1800, 9000, 12600, win * 3600 + 60))
    w = jeff._parse_watchdog("{}", str(now), fires)
    assert w["fail_window_h"] == win and w["fail_bucket_h"] == win // nb
    assert w["fails_recent"] == 3                     # the out-of-window one dropped
    assert w["fires_48h"] == 3                        # falls back to fails_recent
    assert len(w["fails_by_bucket"]) == nb
    assert w["fails_by_bucket"][-1] == 1              # 0h..2h ago
    assert w["fails_by_bucket"][-2] == 2              # 2h..4h ago
    assert sum(w["fails_by_bucket"]) == 3
    # the individual usbresets, newest first, as ages in seconds
    assert w["recent_fires"] == [1800, 9000, 12600]
    # per-bucket absolute epochs drive the hover tooltip ("time of reset")
    assert len(w["bucket_epochs"]) == nb
    assert w["bucket_epochs"][-1] == [now - 1800]
    assert w["bucket_epochs"][-2] == [now - 12600, now - 9000]     # sorted ascending
    assert [len(b) for b in w["bucket_epochs"]] == w["fails_by_bucket"]


def test_parse_watchdog_recent_fires_capped_and_windowed():
    now = 1_000_000
    older = now - jeff.WATCHDOG_FAIL_WINDOW_H * 3600 - 5      # outside the 48h window
    fires = "\n".join(str(now - 60 * i) for i in range(1, 20)) + f"\n{older}\n"
    w = jeff._parse_watchdog("{}", str(now), fires)
    assert len(w["recent_fires"]) == jeff.RECENT_FIRE_MAX     # capped
    assert w["recent_fires"] == sorted(w["recent_fires"])     # newest (smallest age) first
    assert w["recent_fires"][0] == 60
    assert w["fails_recent"] == 19                            # the out-of-window one excluded


def test_parse_watchdog_fires_48h_from_state_preferred():
    w = jeff._parse_watchdog('{"fires_24h":2,"fires_48h":7}', "100000", "")
    assert w["fires_24h"] == 2 and w["fires_48h"] == 7


def test_parse_watchdog_fails_timeline_none_without_remote_now():
    w = jeff._parse_watchdog("{}", "", "123\n456")
    assert w["fails_by_bucket"] is None and w["fails_recent"] is None
    assert w["recent_fires"] is None and w["bucket_epochs"] is None


def test_parse_watchdog_fails_timeline_ignores_junk_lines():
    now = 500_000
    w = jeff._parse_watchdog("{}", str(now), f"\n\n{now - 60}\ngarbage\n{now - 120}\n")
    assert w["fails_recent"] == 2


# ── _extra_alerts: readsb_flapping (auto-heal firing too often) ───────────
def _data_wd(fires_24h):
    d = _data(adsb={"aircraft": 25, "msgs_per_sec": 140.0, "stale": False, "feed_age": 3})
    d["readsb_watchdog"] = {"installed": True, "fires_24h": fires_24h,
                            "last_fire": 1, "last_fire_age": 60,
                            "last_result": "recovered", "last_trigger": "auto"}
    return d


def test_flapping_alert_fires_at_threshold():
    a = jeff._extra_alerts(_data_wd(jeff.WATCHDOG_FLAP_24H))
    assert a["readsb_flapping"][0] == "warn"
    assert str(jeff.WATCHDOG_FLAP_24H) in a["readsb_flapping"][2]


def test_flapping_alert_quiet_below_threshold():
    assert "readsb_flapping" not in jeff._extra_alerts(_data_wd(jeff.WATCHDOG_FLAP_24H - 1))


def test_flapping_alert_absent_when_watchdog_missing():
    d = _data(adsb={"aircraft": 25, "msgs_per_sec": 140.0, "stale": False, "feed_age": 3})
    assert "readsb_flapping" not in jeff._extra_alerts(d)


def test_deaf_and_flapping_can_coexist_deaf_first():
    # a genuinely dying dongle: still deaf right now AND healed a lot today
    d = _data(adsb={"aircraft": 0, "msgs_per_sec": 0, "stale": False, "feed_age": 5})
    d["readsb_watchdog"] = {"installed": True, "fires_24h": 6, "last_fire": 1,
                            "last_fire_age": 60, "last_result": "still-deaf",
                            "last_trigger": "auto"}
    a = jeff._extra_alerts(d)
    ids = list(a)
    assert "readsb_deaf" in ids and "readsb_flapping" in ids
    assert ids.index("readsb_deaf") < ids.index("readsb_flapping")

