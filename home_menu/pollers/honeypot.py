"""Writes honeypot.json for the dashboard — OpenCanary hits on weeny's honeypot
address (192.168.1.5), pulled over SSH.

weeny logs every probe to /var/log/opencanary/opencanary.log as one JSON object
per line. This poller reads the tail of that file, drops the service's own
start/stop chatter, and summarises the last 24 h into the shape the index card
wants.

**Everything in `logdata` is attacker-controlled** — usernames, passwords and
user-agents are literally whatever the prober typed. Fields are truncated and
stripped of control characters here, and the card escapes them again on render.
Do not relax either half.

Passwords are additionally masked (mask_pw) before they ever reach honeypot.json
— probed passwords can coincide with a real credential of ours, so the raw
value is never written to disk here or shown on the dashboard, only its shape.

Note steve is in weeny's `ip.ignorelist`, so this poller's own SSH traffic and
any nmap run from steve never appear in the log. Probes must come from another
host to show up here.
"""

import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "honeypot.json"

HOST = "weeny"
LOG = "/var/log/opencanary/opencanary.log"
# Bounded read: the card only ever shows a 24 h window, and an unbounded cat of a
# log a scanner has been hammering would be a silly thing to pull over ssh.
TAIL_LINES = 2000
WINDOW = 24 * 3600
RECENT_N = 8
FIELD_MAX = 64

# OpenCanary logtype -> (service, human label). Anything under 2000 is the
# daemon talking about itself, not a probe, and is dropped.
LOGTYPES = {
    2000: ("ftp", "FTP login"),
    2001: ("ftp", "FTP auth"),
    3000: ("http", "HTTP request"),
    3001: ("http", "HTTP login"),
    3002: ("http", "HTTP odd method"),
    4000: ("ssh", "SSH connect"),
    4002: ("ssh", "SSH login"),
    6001: ("telnet", "Telnet login"),
    6002: ("telnet", "Telnet connect"),
}
# Login attempts specifically — these carry credentials and are the high-signal
# ones worth counting separately from a bare connection.
LOGIN_TYPES = {2000, 3001, 4002, 6001}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean(v, limit=FIELD_MAX):
    """Attacker-controlled string -> something safe to store and show."""
    s = _CTRL.sub("", str(v if v is not None else ""))
    return s[:limit] + "…" if len(s) > limit else s


def mask_pw(v):
    """Passwords probed against the honeypot are sometimes real credentials of
    ours reused elsewhere (an attacker's dictionary can contain a leaked one by
    coincidence), so the raw value never leaves weeny's own log — only a shape
    (first/last char + length) survives into honeypot.json and the dashboard.
    The full value stays in /var/log/opencanary/opencanary.log on weeny for
    anyone who deliberately needs to go look."""
    s = clean(v)
    if not s:
        return s
    if len(s) <= 2:
        return "•" * len(s)
    return s[0] + "•" * (len(s) - 2) + s[-1]


def fetch_log():
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST,
         f"tail -n {TAIL_LINES} {LOG}"],
        capture_output=True, text=True, timeout=hostlib.SSH_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ssh failed").strip().splitlines()[-1][:200])
    return r.stdout


def parse(raw, now):
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        lt = e.get("logtype")
        src = e.get("src_host")
        # No src_host => daemon lifecycle noise (LOG_BASE_*), not a probe.
        if lt not in LOGTYPES or not src:
            continue
        try:
            ts = time.mktime(time.strptime(e["utc_time"][:19], "%Y-%m-%d %H:%M:%S"))
            ts -= time.timezone  # utc_time is UTC; mktime read it as local
        except (KeyError, ValueError):
            continue
        if now - ts > WINDOW:
            continue
        service, label = LOGTYPES[lt]
        ld = e.get("logdata") or {}
        events.append({
            "ts": int(ts),
            "src": clean(src, 45),
            "port": e.get("dst_port"),
            "service": service,
            "label": label,
            "login": lt in LOGIN_TYPES,
            "user": clean(ld.get("USERNAME")),
            "pw": mask_pw(ld.get("PASSWORD")),
            "ua": clean(ld.get("USERAGENT"), 90),
        })
    events.sort(key=lambda x: x["ts"])
    return events


def build(events, now):
    logins = [e for e in events if e["login"]]
    sources = Counter(e["src"] for e in events)
    last_ts = events[-1]["ts"] if events else None

    data = {
        "ts": int(now),
        "host": "weeny",
        "listen_ip": "192.168.1.5",
        "attempts_24h": len(events),
        "logins_24h": len(logins),
        "sources_24h": len(sources),
        "by_service": dict(Counter(e["service"] for e in events)),
        "top_sources": [{"ip": ip, "n": n} for ip, n in sources.most_common(3)],
        "last_ts": last_ts,
        "last_age": int(now - last_ts) if last_ts else None,
        "recent": list(reversed(events[-RECENT_N:])),
        "alerts": [],
    }

    # A honeypot has no legitimate traffic, so any hit is worth surfacing — but
    # only recent activity should light the card up, otherwise it stays amber
    # for a full day after one stray scan.
    if last_ts and now - last_ts < 3600:
        sev = "critical" if logins else "warning"
        who = events[-1]["src"]
        data["alerts"].append({
            "key": "honeypot_hit",
            "severity": sev,
            "header": "Honeypot touched",
            "detail": f"{who} hit {events[-1]['label']} on the honeypot "
                      f"{int((now - last_ts) // 60)} min ago",
        })
    return data


def main():
    now = time.time()
    try:
        events = parse(fetch_log(), now)
        data = build(events, now)
    except Exception as e:
        data = {"ts": int(now), "host": "weeny", "error": str(e)[:200],
                "attempts_24h": None, "alerts": [{
                    "key": "honeypot_unreachable", "severity": "warning",
                    "header": "Honeypot log unreadable",
                    "detail": f"could not read {LOG} on weeny: {str(e)[:120]}"}]}
    OUT.write_text(json.dumps(data, indent=1))
    print(f"honeypot: {data.get('attempts_24h')} attempts, "
          f"{data.get('sources_24h')} sources, {len(data.get('alerts', []))} alert(s)")


if __name__ == "__main__":
    main()
