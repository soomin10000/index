"""Tests for the server-side alert-acknowledgement store (server._alert_ack_*)."""
import json
import time

import server


def _acks(tmp_path):
    return tmp_path / "alert_acks.json"


def test_add_creates_file_and_row(tmp_path):
    p = _acks(tmp_path)
    now = int(time.time())
    out = server._alert_ack_add("jeff\x1freadsb_flapping\x1f1000",
                                {"header": "SDR flapping", "source": "jeff", "level": "warn"},
                                "simon", now, path=p)
    assert "jeff\x1freadsb_flapping\x1f1000" in out
    row = out["jeff\x1freadsb_flapping\x1f1000"]
    assert row["source"] == "jeff" and row["by"] == "simon" and row["ts"] == now
    # persisted
    assert json.loads(p.read_text()) == out


def test_readd_keeps_original_ack_time(tmp_path):
    p = _acks(tmp_path)
    k = "steve\x1fstatus\x1fabc"
    first = server._alert_ack_add(k, {"source": "steve"}, "a", 1000, path=p)
    again = server._alert_ack_add(k, {"source": "steve"}, "b", 2000, path=p)
    assert again[k]["ts"] == 1000 and again[k]["by"] == "a"
    assert first[k] == again[k]


def test_remove_one_and_many(tmp_path):
    p = _acks(tmp_path)
    now = int(time.time())
    for k in ("a\x1fx\x1f1", "b\x1fx\x1f2", "c\x1fx\x1f3"):
        server._alert_ack_add(k, {"source": k[0]}, "u", now, path=p)
    out = server._alert_ack_remove(["a\x1fx\x1f1", "c\x1fx\x1f3", "missing\x1fk\x1f9"], now, path=p)
    assert list(out) == ["b\x1fx\x1f2"]


def test_prune_drops_expired_and_malformed(tmp_path):
    now = int(time.time())
    raw = {
        "fresh\x1fk\x1f1": {"source": "x", "ts": now - 10},
        "old\x1fk\x1f2": {"source": "x", "ts": now - server.ALERT_ACK_TTL - 1},
        "bad-row": "not a dict",
    }
    pruned = server._prune_alert_acks(raw, now)
    assert list(pruned) == ["fresh\x1fk\x1f1"]


def test_load_missing_and_garbage_return_empty(tmp_path):
    assert server._load_alert_acks(tmp_path / "nope.json") == {}
    p = _acks(tmp_path)
    p.write_text("{ not json")
    assert server._load_alert_acks(p) == {}
    p.write_text("[1, 2, 3]")           # valid json, wrong shape
    assert server._load_alert_acks(p) == {}


def test_add_evicts_oldest_past_the_cap(tmp_path):
    p = _acks(tmp_path)
    for i in range(server.ALERT_ACK_MAX + 5):
        server._alert_ack_add(f"s\x1fk\x1f{i}", {"source": "s"}, "u", 1000 + i, path=p)
    out = server._load_alert_acks(p)
    assert len(out) == server.ALERT_ACK_MAX
    # the five lowest ts values are the ones dropped
    assert "s\x1fk\x1f0" not in out and "s\x1fk\x1f4" not in out
    assert "s\x1fk\x1f5" in out


def test_valid_ack_key():
    assert server._valid_ack_key("a\x1fb\x1fc")
    assert not server._valid_ack_key("")
    assert not server._valid_ack_key(None)
    assert not server._valid_ack_key(123)
    assert not server._valid_ack_key("x" * 301)
