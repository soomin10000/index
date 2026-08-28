"""Parser unit tests against captured remote-command output. jeff.py and bazza.py
share these byte-for-byte, so testing one covers both until they're de-duped."""
import jeff


def test_parse_mem():
    block = "MemTotal:        8058400 kB\nMemAvailable:    6046800 kB\nMemFree:  100 kB\n"
    m = jeff._parse_mem(block)
    assert m["total_mb"] == round(8058400 / 1024, 1)
    assert m["percent"] == round((8058400 - 6046800) / 8058400 * 100, 1)


def test_parse_mem_zero_total():
    assert jeff._parse_mem("MemFree: 0 kB\n")["percent"] == 0.0


def test_parse_disk():
    # df --output=size,used -B1, last line is the mount of interest
    block = "     Size     Used\n 250000000000 100000000000\n"
    d = jeff._parse_disk(block)
    assert d["total_gb"] == 250.0 and d["used_gb"] == 100.0 and d["percent"] == 40.0


def test_parse_load():
    d = jeff._parse_load("0.52 0.48 0.40 1/523 12345\n", "4\n")
    assert d == {"1m": 0.52, "5m": 0.48, "15m": 0.4, "cpus": 4}


def test_parse_procs():
    block = "1234 python3 12.5 3.2\n5678 chrome 4.0 8.1\n9 kworker 0.0 0.0\n"
    procs = jeff._parse_procs(block, n=2)
    assert len(procs) == 2
    assert procs[0] == {"pid": "1234", "name": "python3", "cpu": 12.5, "mem": 3.2}


def test_parse_failed():
    assert jeff._parse_failed("  smb.service   loaded failed\nfoo.timer x y\n") == [
        "smb.service",
        "foo.timer",
    ]
    assert jeff._parse_failed("   \n") == []


def test_parse_temp_vcgencmd():
    assert jeff._parse_temp("temp=47.2'C\n") == 47.2


def test_parse_temp_millidegrees():
    assert jeff._parse_temp("48234\n") == 48.2


def test_parse_temp_empty():
    assert jeff._parse_temp("   ") is None


def test_parse_throttled_clear():
    r = jeff._parse_throttled("throttled=0x0\n")
    assert r["available"] is True and r["flags"] == []


def test_parse_throttled_undervoltage_now():
    r = jeff._parse_throttled("throttled=0x1\n")
    assert r["value"] == 1
    assert [f["bit"] for f in r["flags"]] == [0]
    assert r["flags"][0]["when"] == "now" and r["flags"][0]["level"] == "critical"


def test_parse_throttled_sticky_since_boot():
    # 0x50000 -> bits 16 and 18 set
    r = jeff._parse_throttled("throttled=0x50000\n")
    assert sorted(f["bit"] for f in r["flags"]) == [16, 18]
    assert all(f["when"] == "past" for f in r["flags"])


def test_parse_throttled_unavailable():
    r = jeff._parse_throttled("bash: vcgencmd: command not found\n")
    assert r == {"available": False, "raw": None, "flags": []}


def test_sections_split():
    raw = "===loadavg===\n0.1 0.2 0.3\n===nproc===\n4\n"
    s = jeff._sections(raw)
    assert s["loadavg"].strip() == "0.1 0.2 0.3"
    assert s["nproc"].strip() == "4"
