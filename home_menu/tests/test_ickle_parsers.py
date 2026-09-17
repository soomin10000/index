"""Unit tests for pollers/ickle.py's macOS-specific parsers (sysctl/vm_stat/df,
not /proc — ickle is a Mac mini, so it can't reuse hostlib's Linux parsers)."""
import ickle


def test_parse_load():
    d = ickle._parse_load("{ 1.20 1.35 1.40 }\n", "8\n")
    assert d == {"1m": 1.2, "5m": 1.35, "15m": 1.4, "cpus": 8}


def test_parse_mem():
    vmstat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1000.\n"
        "Pages active:                             2000.\n"
        "Pages inactive:                            500.\n"
        "Pages wired down:                         1000.\n"
        "Pages occupied by compressor:               500.\n"
    )
    m = ickle._parse_mem("1073741824", "16384", vmstat)
    used = (2000 + 1000 + 500) * 16384
    assert m["total_mb"] == round(1073741824 / 1024 / 1024, 1)
    assert m["used_mb"] == round(used / 1024 / 1024, 1)
    assert m["percent"] == round(used / 1073741824 * 100, 1)


def test_parse_disk():
    d = ickle._parse_disk("/dev/disk3s1s1  250000000 100000000 140000000  42%  123 456  0%   /")
    assert d["total_gb"] == round(250000000 * 1024 / 1e9, 1)
    assert d["used_gb"] == round(100000000 * 1024 / 1e9, 1)
    assert d["percent"] == round(100000000 / 250000000 * 100, 1)


def test_parse_uptime():
    now = 1_700_003_600
    assert ickle._parse_uptime("{ sec = 1700000000, usec = 0 } Tue Nov 14 22:13:20 2023\n", now) == 3600.0


def test_parse_procs_handles_sshd_session_comm_with_embedded_space():
    """macOS labels SSH login sessions `sshd-session: <user>` — a comm value
    containing a space, which is why comm has to be the trailing column."""
    block = " 1161  14.7   0.1 sshd-session: simon\n  228   2.0   0.1 nehelper\n"
    procs = ickle._parse_procs(block)
    assert procs[0] == {"pid": "1161", "name": "sshd-session: simon", "cpu": 14.7, "mem": 0.1}
    assert procs[1] == {"pid": "228", "name": "nehelper", "cpu": 2.0, "mem": 0.1}
