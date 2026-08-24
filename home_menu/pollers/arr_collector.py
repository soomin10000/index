#!/usr/bin/env python3
"""Polls the 'arr' media/download stack on noob (Sonarr/Radarr/Lidarr/Readarr/
Prowlarr/SABnzbd/qBittorrent) directly over Tailscale and writes status.json.

Unlike every other poller in this repo, this script's cron job lives on wacky
itself (not steve) — noob became a direct Tailscale peer of wacky, so wacky can
reach each app's own published API without steve acting as a relay. steve's
pollers/arr.py pulls this file's output via one `ssh wacky "cat ..."` call, the
same pull-based pattern pollers/smokeping_rrd.py uses to mirror files off noob.

This script is deployed by hand (scp) to ~/arr-collector/collect.py on wacky —
there's no automated deploy path for a remote-hosted poller in this repo yet.
Secrets (API keys / qBittorrent password) are NOT tracked anywhere: same
convention as steve's crontab (see e.g. pollers/pihole.py's PIHOLE2_PASSWORD)
— set as VAR=value lines at the top of wacky's own crontab, which cron
exports into every job's environment, read here via os.environ.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NOOB_HOST = "100.83.240.27"  # noob's Tailscale IP (noob.rohu-barb.ts.net)
TIMEOUT = 5

OUT = Path.home() / "arr-collector" / "status.json"

# Sonarr/Radarr/Lidarr/Readarr/Prowlarr all share the same underlying Servarr
# API shape (system/status + history), just at different API versions/ports.
ARR_SERVICES = {
    "sonarr":   {"port": 8989, "api": "v3", "key_env": "SONARR_API_KEY",   "history": True},
    "radarr":   {"port": 7878, "api": "v3", "key_env": "RADARR_API_KEY",   "history": True},
    "lidarr":   {"port": 8686, "api": "v1", "key_env": "LIDARR_API_KEY",   "history": True},
    "readarr":  {"port": 8787, "api": "v1", "key_env": "READARR_API_KEY",  "history": True},
    "prowlarr": {"port": 9696, "api": "v1", "key_env": "PROWLARR_API_KEY", "history": False},
}
SABNZBD_PORT = 8080
QBIT_PORT = 8081


def _parse_history_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _check_arr(name, cfg, key):
    """Returns (status_dict, latest_import_or_None). Raises on unreachable/auth failure."""
    base = f"http://{NOOB_HOST}:{cfg['port']}/api/{cfg['api']}"
    req = urllib.request.Request(f"{base}/system/status", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")

    latest = None
    if cfg["history"]:
        hist_url = f"{base}/history?pageSize=25&sortKey=date&sortDirection=descending"
        req2 = urllib.request.Request(hist_url, headers={"X-Api-Key": key})
        with urllib.request.urlopen(req2, timeout=TIMEOUT) as r2:
            hist = json.loads(r2.read())
        records = hist.get("records", hist if isinstance(hist, list) else [])
        # eventType naming varies slightly across the Servarr family
        # (downloadFolderImported / trackFileImported / bookFileImported / ...)
        # — "imported" as a substring is the stable common thread.
        for rec in records:
            if "imported" in (rec.get("eventType") or "").lower():
                when = _parse_history_date(rec.get("date"))
                latest = {
                    "title": rec.get("sourceTitle") or rec.get("title") or "Unknown",
                    "source": name,
                    "at": rec.get("date"),
                    "_when": when,
                }
                break

    return {"ok": True, "message": "ok"}, latest


def _check_sabnzbd(key):
    url = f"http://{NOOB_HOST}:{SABNZBD_PORT}/api?mode=queue&output=json&apikey={urllib.parse.quote(key)}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        data = json.loads(r.read())
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return {"ok": True, "message": "ok"}


def _check_qbittorrent(user, password):
    base = f"http://{NOOB_HOST}:{QBIT_PORT}"
    body = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        f"{base}/api/v2/auth/login", data=body,
        headers={"Referer": base, "Origin": base,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        ok_body = r.read().decode().strip()
        cookie = r.headers.get("Set-Cookie", "")
    if ok_body != "Ok.":
        raise RuntimeError("login rejected")
    m = re.search(r"SID=([^;]+)", cookie)
    if not m:
        raise RuntimeError("no session cookie returned")
    req2 = urllib.request.Request(f"{base}/api/v2/app/version", headers={"Cookie": f"SID={m.group(1)}"})
    with urllib.request.urlopen(req2, timeout=TIMEOUT) as r2:
        if r2.status != 200:
            raise RuntimeError(f"HTTP {r2.status}")
    return {"ok": True, "message": "ok"}


def _error_status(e):
    if isinstance(e, urllib.error.HTTPError):
        return {"ok": False, "message": f"HTTP {e.code}"}
    if isinstance(e, urllib.error.URLError):
        return {"ok": False, "message": "unreachable"}
    return {"ok": False, "message": str(e) or type(e).__name__}


def collect():
    services = {}
    candidates = []

    for name, cfg in ARR_SERVICES.items():
        key = os.environ.get(cfg["key_env"], "")
        if not key:
            services[name] = {"ok": False, "message": f"{cfg['key_env']} not configured"}
            continue
        try:
            status, latest = _check_arr(name, cfg, key)
            services[name] = status
            if latest:
                candidates.append(latest)
        except Exception as e:
            services[name] = _error_status(e)

    sab_key = os.environ.get("SABNZBD_API_KEY", "")
    if not sab_key:
        services["sabnzbd"] = {"ok": False, "message": "SABNZBD_API_KEY not configured"}
    else:
        try:
            services["sabnzbd"] = _check_sabnzbd(sab_key)
        except Exception as e:
            services["sabnzbd"] = _error_status(e)

    qbit_user, qbit_pass = os.environ.get("QBIT_USER", ""), os.environ.get("QBIT_PASS", "")
    if not qbit_user or not qbit_pass:
        services["qbittorrent"] = {"ok": False, "message": "QBIT_USER/QBIT_PASS not configured"}
    else:
        try:
            services["qbittorrent"] = _check_qbittorrent(qbit_user, qbit_pass)
        except Exception as e:
            services["qbittorrent"] = _error_status(e)

    latest_download = None
    dated = [c for c in candidates if c["_when"] is not None]
    if dated:
        best = max(dated, key=lambda c: c["_when"])
        latest_download = {"title": best["title"], "source": best["source"], "at": best["at"]}

    return {
        "ts": int(time.time()),
        "collector": "wacky",
        "target": "noob",
        "services": services,
        "latest_download": latest_download,
    }


def main():
    data = collect()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(OUT)
    down = [n for n, s in data["services"].items() if not s["ok"]]
    print(f"Saved {OUT} — {len(data['services']) - len(down)}/{len(data['services'])} ok"
          + (f", down: {', '.join(down)}" if down else "")
          + (f", latest: {data['latest_download']['title']}" if data["latest_download"] else ""))


if __name__ == "__main__":
    main()
