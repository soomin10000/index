"""Unit tests for pollers/hostlib.py — the shared parsing / alert / history code
behind the steve / wacky / jeff / bazza host cards."""
import sqlite3
from pathlib import Path

import hostlib


# ── remote-command output parsers ───────────────────────────────────────────
def test_parse_mem():
    block = "MemTotal:        8058400 kB\nMemAvailable:    6046800 kB\nMemFree:  100 kB\n"
    m = hostlib.parse_mem(block)
    assert m["total_mb"] == round(8058400 / 1024, 1)
    assert m["percent"] == round((8058400 - 6046800) / 8058400 * 100, 1)


def test_parse_mem_zero_total():
    assert hostlib.parse_mem("MemFree: 0 kB\n")["percent"] == 0.0


def test_parse_disk():
    block = "     Size     Used\n 250000000000 100000000000\n"
    d = hostlib.parse_disk(block)
    assert d["total_gb"] == 250.0 and d["used_gb"] == 100.0 and d["percent"] == 40.0


def test_parse_load():
    d = hostlib.parse_load("0.52 0.48 0.40 1/523 12345\n", "4\n")
    assert d == {"1m": 0.52, "5m": 0.48, "15m": 0.4, "cpus": 4}


def test_parse_procs():
    block = "1234 python3 12.5 3.2\n5678 chrome 4.0 8.1\n9 kworker 0.0 0.0\n"
    procs = hostlib.parse_procs(block, n=2)
    assert len(procs) == 2
    assert procs[0] == {"pid": "1234", "name": "python3", "cpu": 12.5, "mem": 3.2}


def test_parse_failed():
    assert hostlib.parse_failed("  smb.service   loaded failed\nfoo.timer x y\n") == [
        "smb.service",
        "foo.timer",
    ]
    assert hostlib.parse_failed("   \n") == []


def test_parse_temp_vcgencmd():
    assert hostlib.parse_temp("temp=47.2'C\n") == 47.2


def test_parse_temp_millidegrees():
    assert hostlib.parse_temp("48234\n") == 48.2


def test_parse_temp_empty():
    assert hostlib.parse_temp("   ") is None


def test_parse_throttled_clear():
    r = hostlib.parse_throttled("throttled=0x0\n")
    assert r["available"] is True and r["flags"] == []


def test_parse_throttled_undervoltage_now():
    r = hostlib.parse_throttled("throttled=0x1\n")
    assert r["value"] == 1
    assert [f["bit"] for f in r["flags"]] == [0]
    assert r["flags"][0]["when"] == "now" and r["flags"][0]["level"] == "critical"


def test_parse_throttled_sticky_since_boot():
    # 0x50000 -> bits 16 and 18 set
    r = hostlib.parse_throttled("throttled=0x50000\n")
    assert sorted(f["bit"] for f in r["flags"]) == [16, 18]
    assert all(f["when"] == "past" for f in r["flags"])


def test_parse_throttled_custom_bits():
    bits = {0: ("now", "warn", "H", "custom detail")}
    r = hostlib.parse_throttled("throttled=0x1", bits)
    assert r["flags"][0]["detail"] == "custom detail" and r["flags"][0]["level"] == "warn"


def test_parse_throttled_unavailable():
    assert hostlib.parse_throttled("bash: vcgencmd: not found\n") == {
        "available": False, "raw": None, "flags": []}


def test_sections_split():
    s = hostlib.sections("===loadavg===\n0.1 0.2 0.3\n===nproc===\n4\n")
    assert s["loadavg"].strip() == "0.1 0.2 0.3" and s["nproc"].strip() == "4"


# ── metrics history DB ──────────────────────────────────────────────────────
def _schema(db):
    c = sqlite3.connect(db)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='metrics_log'").fetchone()[0]
    rows = c.execute("SELECT * FROM metrics_log").fetchall()
    c.close()
    return sql, rows


def test_log_history_four_col(tmp_path):
    db = tmp_path / "h.db"
    hostlib.log_history(db, 1000, 0.5, 40.0, 20.0)
    sql, rows = _schema(db)
    assert "cpu_temp" not in sql
    assert rows == [(1000, 0.5, 40.0, 20.0)]


def test_log_history_five_col(tmp_path):
    db = tmp_path / "h.db"
    hostlib.log_history(db, 1000, 0.5, 40.0, 20.0, cpu_temp=47.2)
    sql, rows = _schema(db)
    assert "cpu_temp REAL" in sql
    assert rows == [(1000, 0.5, 40.0, 20.0, 47.2)]


def test_log_history_five_col_none_temp(tmp_path):
    db = tmp_path / "h.db"
    hostlib.log_history(db, 1000, 0.5, 40.0, 20.0, cpu_temp=None)
    sql, rows = _schema(db)
    assert "cpu_temp REAL" in sql and rows == [(1000, 0.5, 40.0, 20.0, None)]


def test_log_history_prunes_old(tmp_path):
    db = tmp_path / "h.db"
    now = 10_000_000
    hostlib.log_history(db, now - hostlib.HISTORY_RETENTION_SECONDS - 1, 0.1, 1.0, 1.0)
    hostlib.log_history(db, now, 0.2, 2.0, 2.0)
    _, rows = _schema(db)
    assert [r[0] for r in rows] == [now]


# ── build_alerts core ──────────────────────────────────────────────────────
def _data(disk_pct=10.0, mem_pct=10.0, load1=0.1, cpus=4):
    return {
        "disk": {"percent": disk_pct, "used_gb": 1, "total_gb": 10},
        "mem": {"percent": mem_pct, "used_mb": 100, "total_mb": 1000},
        "load": {"1m": load1, "cpus": cpus},
    }


def test_build_alerts_quiet(tmp_path):
    alerts, active = hostlib.build_alerts(_data(), 1000, None, tmp_path / "x.db")
    assert alerts == [] and active == {}


def test_build_alerts_disk_crit_vs_warn(tmp_path):
    a, _ = hostlib.build_alerts(_data(disk_pct=95.0), 1000, None, tmp_path / "x.db")
    assert a[0]["id"] == "disk_high" and a[0]["level"] == "critical"
    a, _ = hostlib.build_alerts(_data(disk_pct=85.0), 1000, None, tmp_path / "x.db")
    assert a[0]["level"] == "warn"


def test_build_alerts_mem_and_load(tmp_path):
    a, _ = hostlib.build_alerts(_data(mem_pct=95.0, load1=12.0, cpus=4), 1000, None, tmp_path / "x.db")
    ids = {x["id"] for x in a}
    assert ids == {"mem_high", "load_high"}


def test_build_alerts_merges_extra_and_carries_onset(tmp_path):
    extra = {"temp_high": ("warn", "SoC warm", "hot")}
    prev = {"active_alerts": {"temp_high": {"ts": 500}}}
    a, active = hostlib.build_alerts(_data(), 1000, prev, tmp_path / "x.db", extra)
    assert a[0]["id"] == "temp_high"
    assert a[0]["ts"] == 500          # onset carried forward from prev
    assert active["temp_high"]["ts"] == 500


def test_failed_unit_alerts():
    d = hostlib.failed_unit_alerts(["smb.service", "foo.timer"])
    assert set(d) == {"failed_smb.service", "failed_foo.timer"}
    assert d["failed_smb.service"] == ("warn", "Unit failed", "smb.service is in a failed state")
    assert hostlib.failed_unit_alerts(["x"], "Custom")["failed_x"][1] == "Custom"
