import time

import server


def test_freshness_fresh(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"ts": %d, "v": 1}' % int(time.time()))
    data, fresh = server._freshness(f)
    assert fresh is True
    assert data["v"] == 1


def test_freshness_stale(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"ts": %d}' % (int(time.time()) - server.STALE_SECONDS - 60))
    _, fresh = server._freshness(f)
    assert fresh is False


def test_freshness_missing(tmp_path):
    data, fresh = server._freshness(tmp_path / "nope.json")
    assert data is None and fresh is False


def test_freshness_no_ts(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"events": []}')
    _, fresh = server._freshness(f)
    assert fresh is False


def test_annotate_fresh():
    d = server._annotate_freshness({"ts": time.time()})
    assert d["_stale"] is False
    assert d["_age_s"] < 5


def test_annotate_stale_by_age():
    d = server._annotate_freshness({"ts": time.time() - server.STALE_SECONDS - 1})
    assert d["_stale"] is True


def test_annotate_stale_by_error():
    d = server._annotate_freshness({"ts": time.time(), "error": "ssh jeff failed"})
    assert d["_stale"] is True


def test_annotate_no_ts_untouched():
    d = server._annotate_freshness({"events": [1, 2]})
    assert "_stale" not in d and "_age_s" not in d


def test_annotate_no_ts_with_error_is_stale():
    d = server._annotate_freshness({"error": "boom"})
    assert d["_stale"] is True


def test_fresh_json_bytes_missing_is_200_stub(tmp_path):
    import json
    out = json.loads(server._fresh_json_bytes(tmp_path / "gone.json"))
    assert out["_stale"] is True and "error" in out


def test_fresh_json_bytes_annotates(tmp_path):
    import json
    f = tmp_path / "steve.json"
    f.write_text('{"ts": %d, "hostname": "steve"}' % int(time.time()))
    out = json.loads(server._fresh_json_bytes(f))
    assert out["hostname"] == "steve"
    assert out["_stale"] is False and "_age_s" in out
