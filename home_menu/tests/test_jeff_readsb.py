"""Tests for the readsb / ADS-B deaf detection + ntfy push in pollers/jeff.py."""
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


# ── notify_readsb: debounce + recovery ────────────────────────────────────
class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, priority=4, tags=("satellite",)):
        self.calls.append({"title": title, "message": message, "priority": priority})


def _deaf_alert(ts):
    return [{"id": "readsb_deaf", "ts": ts, "level": "critical",
             "header": "readsb is hearing nothing", "text": "..."}]


def test_notify_pushes_once_outage_sustained(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(jeff, "_ntfy", spy)
    now = 10_000
    onset = now - 400  # deaf longer than NOTIFY_MIN_ACTIVE_SEC

    st1 = {}
    jeff.notify_readsb(_deaf_alert(onset), now, {"readsb_notified": False}, st1)
    assert len(spy.calls) == 1 and st1["readsb_notified"] is True

    # next poll, still deaf — must NOT push again
    st2 = {}
    jeff.notify_readsb(_deaf_alert(onset), now + 120, st1, st2)
    assert len(spy.calls) == 1 and st2["readsb_notified"] is True


def test_notify_rides_out_a_brief_blip(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(jeff, "_ntfy", spy)
    now = 10_000
    # deaf for only 60s (e.g. a deploy restart) — under the threshold
    jeff.notify_readsb(_deaf_alert(now - 60), now, {"readsb_notified": False}, {})
    assert spy.calls == []


def test_notify_seeds_silently_on_first_run(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(jeff, "_ntfy", spy)
    now = 10_000
    st = {}
    jeff.notify_readsb(_deaf_alert(now - 999), now, None, st)
    assert spy.calls == [] and st["readsb_notified"] is True


def test_notify_recovery_push(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(jeff, "_ntfy", spy)
    st = {}
    jeff.notify_readsb([], 10_000, {"readsb_notified": True}, st)
    assert len(spy.calls) == 1
    assert "recovered" in spy.calls[0]["title"]
    assert st["readsb_notified"] is False


def test_notify_quiet_when_healthy_and_was_healthy(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(jeff, "_ntfy", spy)
    st = {}
    jeff.notify_readsb([], 10_000, {"readsb_notified": False}, st)
    assert spy.calls == [] and st["readsb_notified"] is False


def test_notify_never_raises_when_ntfy_down(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(jeff, "_ntfy", boom)
    st = {}
    jeff.notify_readsb(_deaf_alert(0), 10_000, {"readsb_notified": False}, st)
    # push failed, so we did not latch — next poll retries
    assert st["readsb_notified"] is False
