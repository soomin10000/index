"""Writes jeff.json for the web dashboard — CPU/mem/disk/uptime for the jeff host
(a Raspberry Pi earmarked for SDR work), gathered over SSH.

On top of the shared host-metrics shape (see hostlib.py) this also reports
Pi-specific health (SoC temperature, under-voltage / throttling flags) and a
best-effort survey of the RTL-SDR setup (dongle present on USB, whether the DVB
kernel driver is squatting it, which rtl_* tools and SDR services exist).

Metrics history goes to jeff_history.db (5-column schema, with cpu_temp).
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import adsb_routes
import hostlib

DATA = Path(__file__).resolve().parent.parent / "data"

OUT            = DATA / "jeff.json"
STATE_FILE     = DATA / "jeff_state.json"
DB_FILE        = DATA / "jeff_history.db"
ROUTES_CACHE   = DATA / "jeff_routes.json"
AIRCRAFT_CACHE = DATA / "jeff_aircraft.json"

TEMP_WARN_C = 70.0
TEMP_CRIT_C = 80.0

# readsb (ADS-B) has wedged repeatedly on a flaky USB/dongle link (2026-09-01/02/04).
# The `/jeff` card shows a "deaf" alert for this — no phone push (removed 2026-09-17
# along with every other ntfy push in home_menu, per Simon).
#
# readsb's feed files count as stale (i.e. readsb hung / crashed) past this age.
ADSB_STALE_SEC = 120
# A half-wedged dongle doesn't go silent — it dribbles the odd frame, enough to
# put 1-3 aircraft on the map for a poll. Anything under this message rate is
# "hearing (almost) nothing"; healthy jeff runs 100-300 msg/s, so a single
# genuinely-received aircraft still clears it comfortably.
DEAF_MSGS_PER_SEC = 10

# RTL2832U-based dongles (incl. the R820T2 stick jeff is for) enumerate under
# Realtek vendor 0bda, product 2832 or 2838.
RTL_USB_RE = re.compile(r"0bda:28(32|38)|RTL28(32|38)|Realtek.*(283[28]|DVB-T)", re.I)

REMOTE_SCRIPT = r"""
echo '===LOADAVG==='; cat /proc/loadavg
echo '===NPROC==='; nproc
echo '===MEMINFO==='; cat /proc/meminfo
echo '===DISK==='; df -B1 --output=size,used / | tail -1
echo '===UPTIME==='; cat /proc/uptime
echo '===TOPCPU==='; ps -eo pid,comm,%cpu,%mem --sort=-%cpu --no-headers | head -5
echo '===TOPMEM==='; ps -eo pid,comm,%cpu,%mem --sort=-%mem --no-headers | head -5
echo '===FAILED==='; systemctl list-units --type=service --state=failed --no-legend --plain
echo '===MODEL==='; (cat /proc/device-tree/model 2>/dev/null | tr -d '\0'; echo)
echo '===TEMP==='; if command -v vcgencmd >/dev/null 2>&1; then vcgencmd measure_temp; else cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; fi
echo '===THROTTLED==='; (command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled) || echo n/a
echo '===USB==='; lsusb 2>/dev/null
echo '===RTLMODS==='; lsmod 2>/dev/null | awk '{print $1}' | grep -E '^(rtl2832|rtl2838|dvb_usb_rtl28xxu|rtl8xxxu)$' || true
echo '===RTLTOOLS==='; for t in rtl_test rtl_sdr rtl_fm rtl_tcp rtl_power rtl_433 rtl_adsb; do command -v $t >/dev/null 2>&1 && echo $t; done
echo '===SDRSVC==='; systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -iE 'rtl|sdr|dump1090|readsb|acars|dumpvdl|dump978|satdump|spyserver|soapy|gqrx|piaware|fr24feed|rbfeeder|adsb|webrx' || true
echo '===NOW==='; date +%s
echo '===ADSB==='; jq -c '{total:(.aircraft|length), pos:([.aircraft[]|select(.lat!=null)]|length), file_ts:(.now//0), flights:([.aircraft[]|select((.flight//""|gsub("^ +| +$";""))!="" and .lat!=null)|{cs:(.flight|gsub("^ +| +$";"")), hex, alt:.alt_baro, gs, trk:.track, rssi, lat, lon}]|sort_by(.rssi//-100)|reverse|.[:8])}' /run/readsb/aircraft.json 2>/dev/null || echo '{}'
echo '===ADSBSTATS==='; jq -c '{msgs_last_min:(.last1min.messages//0), max_dist_m:(.last1min.max_distance//0)}' /run/readsb/stats.json 2>/dev/null || echo '{}'
echo '===WATCHDOG==='; cat /var/lib/readsb-watchdog/state.json 2>/dev/null || echo '{}'
echo '===FIRES==='; cat /var/lib/readsb-watchdog/fires 2>/dev/null || true
"""


def _parse_sdr(usb_block, mods_block, tools_block, svc_block):
    usb_lines = [ln for ln in usb_block.splitlines() if ln.strip()]
    dongle_line = next((ln for ln in usb_lines if RTL_USB_RE.search(ln)), None)
    dongle_name = None
    if dongle_line:
        # lsusb: "Bus 001 Device 004: ID 0bda:2838 Realtek ... RTL2838 DVB-T"
        parts = dongle_line.split("ID ", 1)
        dongle_name = parts[1].strip() if len(parts) > 1 else dongle_line.strip()

    mods = [m for m in mods_block.split() if m]
    return {
        "dongle_present": dongle_line is not None,
        "dongle_name": dongle_name,
        # The DVB-T driver grabs an RTL2832 stick on plug-in and blocks librtlsdr
        # until it's blacklisted — the classic "SDR doesn't work" gotcha.
        "dvb_driver_loaded": "dvb_usb_rtl28xxu" in mods,
        "sdr_driver_loaded": any(m in ("rtl2832", "rtl2838") for m in mods),
        "tools": [t for t in tools_block.split() if t],
        "services": [s for s in svc_block.split() if s],
    }


def _parse_adsb(adsb_block, stats_block, remote_now=None):
    """Live readsb numbers off /run/readsb/{aircraft,stats}.json (via jq on the
    remote). Returns None when readsb isn't running / jq is missing / the files
    aren't there — the block is just `{}` in that case.

    `feed_age` is how many seconds behind wall-clock aircraft.json's own
    timestamp is; `stale` means readsb has stopped updating it (hung / crashed)
    even though the file — and its last message counts — are still on disk.

    `flights` is the strongest-signal handful of aircraft with a callsign and a
    position — {cs, hex, alt, gs, track, lat, lon} each — for the panel's flight
    list; routes get attached later in fetch_and_write."""
    try:
        a = json.loads(adsb_block.strip() or "{}")
    except Exception:
        a = {}
    try:
        s = json.loads(stats_block.strip() or "{}")
    except Exception:
        s = {}
    if a.get("total") is None:
        return None
    max_dist_m = s.get("max_dist_m") or 0
    file_ts = a.get("file_ts") or 0
    try:
        feed_age = int(float(remote_now) - float(file_ts)) if remote_now and file_ts else None
    except (TypeError, ValueError):
        feed_age = None

    flights = []
    for f in a.get("flights") or []:
        if not isinstance(f, dict):
            continue
        cs = str(f.get("cs") or "").strip()
        if not cs:
            continue
        flights.append({
            "cs": cs,
            "hex": f.get("hex"),
            "alt": f.get("alt"),
            "gs": f.get("gs"),
            "track": f.get("trk"),
            "lat": f.get("lat"),
            "lon": f.get("lon"),
        })

    return {
        "aircraft": a.get("total", 0),
        "positions": a.get("pos", 0),
        "msgs_per_sec": round((s.get("msgs_last_min") or 0) / 60, 1),
        "max_range_km": round(max_dist_m / 1000, 1) if max_dist_m else None,
        "feed_age": feed_age,
        "stale": feed_age is not None and feed_age > ADSB_STALE_SEC,
        "flights": flights,
    }


# The /jeff SDR panel reports the last 48h of auto-heal fires as a strip of
# WATCHDOG_FAIL_BUCKETS equal columns (48 / 24 = 2h each), plus the age of the
# most recent RECENT_FIRE_MAX usbresets as a list.
WATCHDOG_FAIL_WINDOW_H = 48
WATCHDOG_FAIL_BUCKETS = 24
RECENT_FIRE_MAX = 8


def _parse_watchdog(block, now=None, fires_block=""):
    """State written by jeff's `readsb-recover` script (pollers/jeff/) each time
    it runs — the auto-heal that usbresets the wedged dongle and restarts readsb.
    `{}` when the watchdog has never fired (or isn't installed). None-safe; never
    raises — the poll's real job is writing jeff.json.

    `fires_block` is the append-only epoch-per-fire log
    (`/var/lib/readsb-watchdog/fires`, one line per auto-heal, kept 72h by
    readsb-recover); its last 48h are bucketed here into `WATCHDOG_FAIL_BUCKETS`
    equal columns for the panel strip."""
    try:
        w = json.loads(block.strip() or "{}")
    except Exception:
        w = {}
    if not isinstance(w, dict):  # json.loads("null") -> None, "[]" -> list, etc.
        w = {}
    last_fire = w.get("last_fire")
    try:
        last_fire = int(last_fire) if last_fire is not None else None
    except (TypeError, ValueError):
        last_fire = None
    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    fires_24h = _int(w.get("fires_24h"))
    fires_48h = _int(w.get("fires_48h")) or None
    try:
        now_i = int(float(now)) if now not in (None, "") else None
    except (TypeError, ValueError):
        now_i = None
    age = None
    if last_fire is not None and now_i is not None:
        age = max(now_i - last_fire, 0)

    # Last-48h failure timeline, oldest bucket first — bucket i spans
    # (window - i*step) .. (window - (i+1)*step) hours ago; the final bucket ends
    # "now". None when we have no remote clock to bucket against.
    window_h = WATCHDOG_FAIL_WINDOW_H
    n_buckets = WATCHDOG_FAIL_BUCKETS
    bucket_s = window_h * 3600 // n_buckets      # seconds per column (2h)
    fails_recent = None
    fails_by_bucket = None
    bucket_epochs = None                         # per-bucket list of usbreset epochs (s)
    recent_fires = None                          # age (s) of each usbreset, newest first
    if now_i is not None:
        cutoff = now_i - window_h * 3600
        epochs = []
        by_bucket = [[] for _ in range(n_buckets)]
        for tok in (fires_block or "").split():
            try:
                e = int(tok)
            except ValueError:
                continue
            if not (cutoff < e <= now_i):
                continue
            epochs.append(e)
            idx = n_buckets - 1 - (now_i - e) // bucket_s
            if 0 <= idx < n_buckets:
                by_bucket[idx].append(e)
        epochs.sort()
        for b in by_bucket:
            b.sort()
        fails_recent = len(epochs)
        fails_by_bucket = [len(b) for b in by_bucket]
        bucket_epochs = by_bucket
        recent_fires = [now_i - e for e in epochs[::-1][:RECENT_FIRE_MAX]]

    return {
        "installed": last_fire is not None,
        "last_fire": last_fire,
        "last_fire_age": age,
        "fires_24h": fires_24h,
        "fires_48h": fires_48h if fires_48h is not None else fails_recent,
        "last_result": w.get("last_result"),
        "last_trigger": w.get("last_trigger"),
        "fail_window_h": window_h,
        "fail_bucket_h": bucket_s // 3600,
        "fails_recent": fails_recent,
        "fails_by_bucket": fails_by_bucket,
        "bucket_epochs": bucket_epochs,
        "recent_fires": recent_fires,
    }


# readsb-recover fires >= this many times in 24h => the dongle is genuinely on
# its way out, not just an occasional soft wedge the usbreset clears.
WATCHDOG_FLAP_24H = 4


def _other_sdr_mode_active(svcs):
    """True if jeff's dongle is intentionally running a non-planes SDR mode right
    now (rtl_433/ISM, acarsdec/ACARS, or openwebrx/waterfall — see the /sdr mode
    switcher in server.py, which stops readsb entirely for these). readsb being
    down in that case is expected, not a fault — the deaf/down alert must not
    fire just because the mode switcher parked it on purpose."""
    return any(any(x in s for x in ("rtl_433", "acarsdec", "openwebrx")) for s in svcs)


def _extra_alerts(data):
    """jeff-specific alert candidates, in the same insertion order as before:
    SoC temp, throttle flags, failed units, DVB squat, deaf readsb."""
    extra = {}

    temp = data.get("cpu_temp")
    if temp is not None and temp >= TEMP_CRIT_C:
        extra["temp_high"] = ("critical", "SoC running hot",
            f"CPU temperature is {temp} °C — the Pi throttles hard around 80-85 °C")
    elif temp is not None and temp >= TEMP_WARN_C:
        extra["temp_high"] = ("warn", "SoC warm", f"CPU temperature is {temp} °C")

    for f in data["throttled"]["flags"]:
        extra[f"throttle_{f['bit']}"] = (f["level"], f["header"], f["detail"])

    extra.update(hostlib.failed_unit_alerts(data["other_failed"]))

    if data["sdr"]["dongle_present"] and data["sdr"]["dvb_driver_loaded"] \
            and not data["sdr"]["sdr_driver_loaded"]:
        extra["dvb_squat"] = ("warn", "DVB driver has the SDR dongle",
            "dvb_usb_rtl28xxu is loaded — blacklist it so librtlsdr can claim the stick")

    adsb = data.get("adsb")
    svcs = data["sdr"]["services"]
    readsb_up = any("readsb" in s for s in svcs)
    piaware_up = any("piaware" in s for s in svcs)
    dongle = data["sdr"]["dongle_present"]
    wedge_hint = ("the USB/dongle link most likely wedged (see the 2026-09-01 "
                  "incidents) — usbreset 0bda:2838 then restart readsb; a bare "
                  "service restart alone did not clear it last time")
    # readsb being down/quiet is expected whenever the /sdr mode switcher has
    # intentionally parked it for ISM/ACARS/waterfall — not a fault to alert on.
    if not _other_sdr_mode_active(svcs):
        if readsb_up and adsb and adsb.get("stale"):
            extra["readsb_deaf"] = ("critical", "readsb feed is frozen",
                f"readsb is up but aircraft.json is {adsb['feed_age']}s stale — {wedge_hint}")
        elif readsb_up and adsb and adsb["msgs_per_sec"] < DEAF_MSGS_PER_SEC:
            extra["readsb_deaf"] = ("critical", "readsb is hearing (almost) nothing",
                f"readsb is running but only {adsb['msgs_per_sec']} msg/s and "
                f"{adsb['aircraft']} aircraft — {wedge_hint}")
        elif dongle and piaware_up and not readsb_up:
            extra["readsb_deaf"] = ("critical", "readsb is down",
                "the dongle is present and piaware is up but readsb isn't running — "
                f"it may be crash-looping on a wedged USB device; {wedge_hint}")

    wd = data.get("readsb_watchdog") or {}
    if wd.get("fires_24h", 0) >= WATCHDOG_FLAP_24H:
        extra["readsb_flapping"] = ("warn", "SDR auto-heal firing repeatedly",
            f"readsb-recover has usbreset the dongle {wd['fires_24h']}× in the last "
            "24h — reseat or swap it if this keeps happening")

    return extra


def fetch_and_write():
    ts = int(time.time())
    try:
        s = hostlib.fetch_remote("jeff", REMOTE_SCRIPT)
        load = hostlib.parse_load(s["LOADAVG"], s["NPROC"])
        mem = hostlib.parse_mem(s["MEMINFO"])
        disk = hostlib.parse_disk(s["DISK"])
        uptime_seconds = float(s["UPTIME"].split()[0])
        top_cpu = hostlib.parse_procs(s["TOPCPU"])
        top_mem = hostlib.parse_procs(s["TOPMEM"])
        other_failed = hostlib.parse_failed(s["FAILED"])
        model = s["MODEL"].strip() or "Raspberry Pi"
        cpu_temp = hostlib.parse_temp(s["TEMP"])
        throttled = hostlib.parse_throttled(s["THROTTLED"])
        sdr = _parse_sdr(s["USB"], s["RTLMODS"], s["RTLTOOLS"], s["SDRSVC"])
        remote_now = s.get("NOW", "").strip()
        adsb = _parse_adsb(s.get("ADSB", ""), s.get("ADSBSTATS", ""), remote_now)
        watchdog = _parse_watchdog(s.get("WATCHDOG", ""), remote_now, s.get("FIRES", ""))
    except Exception as e:
        OUT.write_text(json.dumps({"ts": ts, "error": str(e)}))
        print(f"jeff poll failed: {e}")
        return

    # Best-effort origin/destination for the aircraft overhead (adsbdb.com, cached).
    # Isolated so a route-API blip can't fail the poll or stale the card.
    if adsb and adsb.get("flights"):
        try:
            cache = adsb_routes.load_cache(ROUTES_CACHE)
            routes, dirty = adsb_routes.resolve(adsb["flights"], cache, ts)
            adsb_routes.annotate(adsb["flights"], routes)
            if dirty:
                adsb_routes.save_cache(ROUTES_CACHE, cache)
        except Exception as e:
            print(f"jeff route lookup skipped: {e}")

        # Registration/owner (adsbdb again, by hex this time) — most useful for
        # exactly the flights the route lookup above came up empty for: private/GA
        # aircraft fly under an owner's own callsign rather than a scheduled route.
        try:
            ac_cache = adsb_routes.load_cache(AIRCRAFT_CACHE)
            aircraft, ac_dirty = adsb_routes.resolve_aircraft(adsb["flights"], ac_cache, ts)
            adsb_routes.annotate_aircraft(adsb["flights"], aircraft)
            if ac_dirty:
                adsb_routes.save_cache(AIRCRAFT_CACHE, ac_cache, prune_after=adsb_routes.AIRCRAFT_PRUNE_AFTER)
        except Exception as e:
            print(f"jeff aircraft lookup skipped: {e}")

    data = {
        "ts": ts,
        "hostname": "jeff",
        "model": model,
        "load": load,
        "mem": mem,
        "disk": disk,
        "uptime_seconds": uptime_seconds,
        "cpu_temp": cpu_temp,
        "throttled": throttled,
        "sdr": sdr,
        "adsb": adsb,
        "readsb_watchdog": watchdog,
        "other_failed": other_failed,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
    }

    prev = hostlib.load_state(STATE_FILE)
    alerts, active_alerts = hostlib.build_alerts(data, ts, prev, DB_FILE, _extra_alerts(data))
    data["alerts"] = alerts

    new_state = {"active_alerts": active_alerts}

    OUT.write_text(json.dumps(data, indent=2))
    hostlib.log_history(DB_FILE, ts, load["1m"], mem["percent"], disk["percent"], cpu_temp)
    hostlib.save_state(STATE_FILE, new_state)

    adsb_note = f", {adsb['aircraft']} aircraft" if adsb else ""
    print(f"Saved {OUT} — {len(alerts)} alerts, load {load['1m']}, mem {mem['percent']}%, "
          f"disk {disk['percent']}%, temp {cpu_temp} °C, "
          f"sdr {'present' if sdr['dongle_present'] else 'absent'}{adsb_note}")


if __name__ == "__main__":
    fetch_and_write()
