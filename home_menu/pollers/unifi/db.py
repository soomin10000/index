"""SQLite logging for UniFi poller."""

import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path.home() / "unifi_poller.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weak_client_log (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    hostname    TEXT    NOT NULL,
    signal      INTEGER NOT NULL,
    retry_pct   REAL    NOT NULL,
    essid       TEXT
);

CREATE TABLE IF NOT EXISTS congestion_log (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    ap       TEXT    NOT NULL,
    radio    TEXT    NOT NULL,
    cu_total INTEGER NOT NULL,
    num_sta  INTEGER
);

CREATE TABLE IF NOT EXISTS known_devices (
    mac        TEXT    PRIMARY KEY,
    hostname   TEXT,
    vendor     TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS speedtest_log (
    id           INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,
    ping_ms      REAL,
    download_mbps REAL,
    upload_mbps  REAL
);

CREATE TABLE IF NOT EXISTS events_log (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    type    TEXT    NOT NULL,
    title   TEXT    NOT NULL,
    message TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS weak_client_log_ts  ON weak_client_log (ts);
CREATE INDEX IF NOT EXISTS congestion_log_ts   ON congestion_log (ts);
CREATE INDEX IF NOT EXISTS speedtest_log_ts    ON speedtest_log (ts);
CREATE INDEX IF NOT EXISTS events_log_ts       ON events_log (ts);
"""


def open_db(path=DEFAULT_DB):
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def last_flagged_clients(conn, within_seconds=600):
    cutoff = int(time.time()) - within_seconds
    rows = conn.execute(
        "SELECT DISTINCT hostname FROM weak_client_log WHERE ts >= ?", (cutoff,)
    ).fetchall()
    return {r[0] for r in rows}


def last_flagged_congestion(conn, within_seconds=600):
    cutoff = int(time.time()) - within_seconds
    rows = conn.execute(
        "SELECT DISTINCT ap, radio FROM congestion_log WHERE ts >= ?", (cutoff,)
    ).fetchall()
    return {f"{r[0]}:{r[1]}" for r in rows}


def log_poll(conn, congestion_flags, weak_flags, ts=None):
    if ts is None:
        ts = int(time.time())
    with conn:
        for f in weak_flags:
            conn.execute(
                "INSERT INTO weak_client_log (ts, hostname, signal, retry_pct, essid) VALUES (?,?,?,?,?)",
                (ts, f["hostname"], f["signal"], f["retry_pct"], f.get("essid")),
            )
        for f in congestion_flags:
            conn.execute(
                "INSERT INTO congestion_log (ts, ap, radio, cu_total, num_sta) VALUES (?,?,?,?,?)",
                (ts, f["ap"], f["radio"], f["cu_total"], f.get("num_sta")),
            )


def check_new_devices(conn, all_macs_info, ts=None):
    """
    Compare seen MACs against known_devices table.
    Returns list of new device dicts. Updates last_seen for all known ones.
    all_macs_info: list of {"mac", "hostname", "vendor"} dicts.
    """
    if ts is None:
        ts = int(time.time())

    known = {r[0] for r in conn.execute("SELECT mac FROM known_devices").fetchall()}
    new_devices = []

    with conn:
        for dev in all_macs_info:
            mac = dev["mac"]
            if mac not in known:
                conn.execute(
                    "INSERT INTO known_devices (mac, hostname, vendor, first_seen, last_seen) VALUES (?,?,?,?,?)",
                    (mac, dev.get("hostname", ""), dev.get("vendor", ""), ts, ts),
                )
                new_devices.append(dev)
            else:
                conn.execute(
                    "UPDATE known_devices SET last_seen=?, hostname=? WHERE mac=?",
                    (ts, dev.get("hostname", ""), mac),
                )

    return new_devices


def log_event(conn, type_, title, message, ts=None):
    if ts is None:
        ts = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO events_log (ts, type, title, message) VALUES (?,?,?,?)",
            (ts, type_, title, message),
        )


def get_events(conn, limit=200):
    rows = conn.execute(
        "SELECT ts, type, title, message FROM events_log ORDER BY ts DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [{"ts": r[0], "type": r[1], "title": r[2], "message": r[3]} for r in rows]


def get_known_devices(conn):
    rows = conn.execute(
        "SELECT mac, first_seen, last_seen FROM known_devices"
    ).fetchall()
    return {r[0]: {"first_seen": r[1], "last_seen": r[2]} for r in rows}


def get_latest_speedtest(conn):
    row = conn.execute(
        "SELECT ts, ping_ms, download_mbps, upload_mbps FROM speedtest_log ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        return {"ts": row[0], "ping_ms": row[1], "download_mbps": row[2], "upload_mbps": row[3]}
    return None


def log_speedtest(conn, ping_ms, download_mbps, upload_mbps, ts=None):
    if ts is None:
        ts = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO speedtest_log (ts, ping_ms, download_mbps, upload_mbps) VALUES (?,?,?,?)",
            (ts, ping_ms, download_mbps, upload_mbps),
        )
