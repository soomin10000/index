#!/usr/bin/env python3
"""Home services menu — serves index.html and proxies service APIs on port 8080."""
import base64
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

BASE   = Path(__file__).parent
PAGES  = BASE / 'pages'
STATIC = BASE / 'static'
DATA   = BASE / 'data'
PORT = 8080

UNIFI_DB   = Path.home() / 'unifi_poller.db'
UNIFI_DATA = DATA / 'unifi'

PROXIES = {
    '/api/harold':  'http://localhost:5000/api/status',
    '/api/train':   'http://localhost:8192/api/departures',
    '/api/darren':  'http://localhost:8193/api/status',
    '/api/weather': 'http://localhost:8186/api/weather',
    '/api/timers':  'http://localhost:8196/api/status',
}

# SmokePing's classic browse windows, as hours.
SMOKEPING_RANGES = {'3h': 3, '30h': 30, '10d': 240, '1y': 8766}
SMOKEPING_RRD_DIR = DATA / 'smokeping_rrd'
# Dead/decommissioned probes — 100% loss / nan RTT since the card was built,
# not worth a tile. Filtered here rather than at the CGI-scrape poller so
# smokeping.py keeps mirroring SmokePing's own target list faithfully.
# DNSProbes.{Download,Upload}-sivel are a menu-scrape mislabeling (the real
# RRDs are SpeedTest throughput probes, not DNS) — permanently 404, and
# speedtest is tracked elsewhere anyway, so just hidden rather than fixed.
SMOKEPING_HIDDEN = {
    'Internal.shed', 'Internal.down_front',
    'DNSProbes.Download-sivel', 'DNSProbes.Upload-sivel',
}
_SP_TARGET_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')
_SP_PING_DS_RE = re.compile(r'ds\[ping(\d+)\]')
_sp_cache = {}
_sp_ping_count_cache = {}  # rrd path -> pings/interval (schema is static, cached indefinitely)
_sp_lock = threading.Lock()


def _smokeping_status():
    try:
        data = json.loads((DATA / 'smokeping.json').read_text())
    except Exception:
        return {'ts': 0, 'ok': False, 'error': 'no data yet', 'sections': [], 'target_count': 0,
                'base': ''}
    sections = []
    for sec in data.get('sections', []):
        targets = [t for t in sec['targets'] if t['id'] not in SMOKEPING_HIDDEN]
        if targets:
            sections.append({**sec, 'targets': targets})
    data['sections'] = sections
    data['target_count'] = sum(len(s['targets']) for s in sections)
    return data


def _smokeping_ping_count(rrd):
    """Pings/interval for one RRD — not a constant across targets. The
    default FPing probe sends 20, but e.g. this instance's DNS-query probes
    (DNSProbes.*) only send 5; hardcoding 20 made DEF:pingN reference a
    nonexistent DS and killed the whole xport for those targets. Schema is
    static for a given file, so cache indefinitely (unlike the data cache)."""
    with _sp_lock:
        hit = _sp_ping_count_cache.get(rrd)
    if hit is not None:
        return hit
    proc = subprocess.run(['rrdtool', 'info', rrd], capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors='replace').strip() or 'rrdtool info failed')
    nums = [int(m) for m in _SP_PING_DS_RE.findall(proc.stdout.decode(errors='replace'))]
    count = max(nums) if nums else 0
    with _sp_lock:
        _sp_ping_count_cache[rrd] = count
    return count


def _smokeping_series(target, rng):
    """Median/loss/smoke-band series for one target/range, read straight off
    the local RRD mirror (pollers/smokeping_rrd.py) with `rrdtool xport`
    instead of proxying a pre-rendered PNG from the NAS's CGI. Cached briefly
    — a page load is ~34 tiles, each a subprocess spawn.

    The "smoke" is real SmokePing's namesake effect: each interval's
    individual ping RTTs, sorted and paired symmetrically from the outside
    in (min+max, 2nd-min+2nd-max, ...), give nested bands. Stacking them
    with equal, low alpha client-side lets ordinary alpha compositing do the
    density gradient — no min/max box, no percentile math needed here."""
    key = (target, rng)
    now = time.time()
    with _sp_lock:
        hit = _sp_cache.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
    section, _, name = target.partition('.')
    rrd = SMOKEPING_RRD_DIR / section / f'{name}.rrd'
    if not rrd.is_file():
        raise FileNotFoundError(f'no RRD mirrored for target {target!r}')
    rrd = str(rrd)
    ping_count = _smokeping_ping_count(rrd)
    ping_defs = [f'DEF:p{i}={rrd}:ping{i}:AVERAGE' for i in range(1, ping_count + 1)]
    ping_xports = [f'XPORT:p{i}:p{i}' for i in range(1, ping_count + 1)]
    cmd = [
        'rrdtool', 'xport', '--json',
        '-s', f'now-{SMOKEPING_RANGES[rng]}h', '-e', 'now',
        f'DEF:med={rrd}:median:AVERAGE', f'DEF:loss={rrd}:loss:AVERAGE',
        *ping_defs, 'XPORT:med:median', 'XPORT:loss:loss', *ping_xports,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors='replace').strip() or 'rrdtool xport failed')
    raw = json.loads(proc.stdout)
    step, start = raw['meta']['step'], raw['meta']['start']
    band_count = ping_count // 2
    points = []
    for i, row in enumerate(raw['data']):
        med, loss = row[0], row[1]
        pings = [p for p in row[2:] if p is not None]
        pings.sort()
        bands = None
        if band_count and len(pings) == ping_count:
            bands = [[round(pings[b] * 1000, 3), round(pings[-1 - b] * 1000, 3)]
                     for b in range(band_count)]
        points.append({
            't': start + i * step,
            'median_ms': None if med is None else round(med * 1000, 3),
            # loss DS counts lost pings out of ping_count sent per interval
            'loss_pct': None if loss is None or not ping_count else round(loss / ping_count * 100, 1),
            'bands': bands,
        })
    result = {'target': target, 'range': rng, 'step': step, 'points': points}
    with _sp_lock:
        _sp_cache[key] = (now, result)
        if len(_sp_cache) > 200:
            del _sp_cache[min(_sp_cache, key=lambda k: _sp_cache[k][0])]
    return result


def _unifi_status():
    if not UNIFI_DB.exists():
        return {'error': 'no db'}
    cutoff = int(time.time()) - 600
    try:
        conn = sqlite3.connect(UNIFI_DB)
        weak = conn.execute(
            'SELECT hostname, MIN(signal) as signal, MAX(retry_pct) as retry_pct '
            'FROM weak_client_log WHERE ts >= ? GROUP BY hostname ORDER BY signal',
            (cutoff,)
        ).fetchall()
        cong = conn.execute(
            'SELECT ap, radio, MAX(cu_total) as cu_total '
            'FROM congestion_log WHERE ts >= ? GROUP BY ap, radio',
            (cutoff,)
        ).fetchall()
        speed_row = conn.execute(
            'SELECT ts, ping_ms, download_mbps, upload_mbps FROM speedtest_log ORDER BY ts DESC LIMIT 1'
        ).fetchone()
        conn.close()
        return {
            'flagged_clients':    [{'hostname': r[0], 'signal': r[1], 'retry_pct': round(r[2], 1)} for r in weak],
            'flagged_congestion': [{'ap': r[0], 'radio': r[1], 'cu_total': r[2]} for r in cong],
            'speedtest': {'ts': speed_row[0], 'ping_ms': speed_row[1],
                          'download_mbps': round(speed_row[2], 1),
                          'upload_mbps': round(speed_row[3], 1)} if speed_row else None,
        }
    except Exception as e:
        return {'error': str(e)}


STALE_SECONDS = 900  # poller data older than this reads as "can't tell / probably down"


def _freshness(path):
    """Returns (data_dict_or_None, is_fresh_bool) for a poller JSON file, keyed off its own 'ts'."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None, False
    ts = data.get('ts')
    fresh = isinstance(ts, (int, float)) and (time.time() - ts) < STALE_SECONDS
    return data, fresh


def _is_it_broken_status():
    checks = {}

    up, up_fresh = _freshness(DATA / 'uplink.json')
    if not up_fresh or not (up or {}).get('probe', {}).get('connected'):
        checks['internet'] = {'state': 'down', 'detail': "Can't reach the internet right now."}
    elif (up.get('bgp') or {}).get('status') != 'ok':
        checks['internet'] = {'state': 'warn', 'detail': "Online, but something's off with the connection."}
    else:
        checks['internet'] = {'state': 'ok', 'detail': 'Internet is up.'}

    wifi, wifi_fresh = _freshness(DEVICES_JSON)
    devices = (wifi or {}).get('devices') or []
    if not wifi_fresh:
        checks['wifi'] = {'state': 'down', 'detail': "Router/WiFi isn't answering."}
    elif not devices:
        checks['wifi'] = {'state': 'warn', 'detail': 'WiFi is up, but no devices are showing as connected.'}
    else:
        n = len(devices)
        checks['wifi'] = {'state': 'ok', 'detail': f"{n} device{'s' if n != 1 else ''} connected."}

    st = _unifi_status().get('speedtest')
    if not st:
        checks['speed'] = {'state': 'warn', 'detail': 'No speed test yet.'}
    else:
        age_min = int((time.time() - st['ts']) / 60)
        age = f'{age_min} min ago' if age_min < 60 else f'{age_min // 60}h ago'
        checks['speed'] = {
            'state': 'ok',
            'detail': f"{st['download_mbps']:.0f}↓ / {st['upload_mbps']:.0f}↑ Mbps · {age}",
        }

    order = {'ok': 0, 'warn': 1, 'down': 2}
    overall = max((c['state'] for c in checks.values()), key=order.get)
    return {'overall': overall, 'checks': checks, 'ts': int(time.time())}


def _device_from_ua(ua):
    ua = ua or ''
    if 'iPad' in ua: return 'iPad'
    if 'iPhone' in ua: return 'iPhone'
    if 'Android' in ua: return 'Android'
    if 'Macintosh' in ua: return 'Mac'
    if 'Windows' in ua: return 'Windows'
    return 'a device'


PIHOLE_JSON  = DATA / 'pihole.json'
KISMET_JSON  = DATA / 'kismet.json'
STEVE_JSON   = DATA / 'steve.json'
EUFY_JSON    = DATA / 'eufy.json'
EUFY_VACUUM_JSON = DATA / 'eufy_vacuum.json'
EUFY_SNAPSHOTS = DATA / 'eufy_snapshots'
_EUFY_SNAPSHOT_RE = re.compile(r'^[A-Za-z0-9_.-]+$')
STEVE_DB     = DATA / 'steve_history.db'
WACKY_JSON   = DATA / 'wacky.json'
ARR_JSON     = DATA / 'arr.json'
WACKY_DB     = DATA / 'wacky_history.db'
JEFF_JSON    = DATA / 'jeff.json'
JEFF_DB      = DATA / 'jeff_history.db'
BAZZA_JSON   = DATA / 'bazza.json'
BAZZA_DB     = DATA / 'bazza_history.db'
DEVICES_JSON = UNIFI_DATA / 'devices.json'
REPORT_HTML  = DATA / 'network_report_2026-07-19.html'  # static analysis report, served at /report
MOISTURE_DB  = Path.home() / 'projects' / 'moisture.db'  # written by moisture_endpoint.py (:8082)
# Retired sensors whose old readings are still in the DB — hidden from the
# dashboard, since "latest per plant" never expires a silent sensor on its own.
MOISTURE_HIDDEN = ('monstera',)

KISMET_URL  = os.environ.get('KISMET_URL', 'http://localhost:2501')
KISMET_USER = os.environ.get('KISMET_USER')
KISMET_PASS = os.environ.get('KISMET_PASS')

PIHOLE_URL      = os.environ.get('PIHOLE_URL', 'http://192.168.1.246')
PIHOLE_PASSWORD = os.environ.get('PIHOLE_PASSWORD')


def _trigger_gravity():
    """Kick off a Pi-hole gravity update. The run takes minutes with big lists,
    far longer than a browser should wait, so the API call happens in a background
    thread and the reply just acks the start — the card's freshness readout (from
    the poller) shows when it actually completed."""
    if not PIHOLE_PASSWORD:
        return {'ok': False, 'error': 'PIHOLE_PASSWORD not configured on server'}

    def run():
        sid = None
        try:
            req = urllib.request.Request(
                f'{PIHOLE_URL}/api/auth',
                data=json.dumps({'password': PIHOLE_PASSWORD}).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as r:
                sid = json.load(r)['session']['sid']
            req = urllib.request.Request(f'{PIHOLE_URL}/api/action/gravity',
                                         data=b'', headers={'X-FTL-SID': sid})
            with urllib.request.urlopen(req, timeout=900) as r:
                r.read()
        except Exception as e:
            print(f'gravity update failed: {e}')
        finally:
            if sid:  # free the API seat — Pi-hole caps concurrent sessions
                try:
                    req = urllib.request.Request(f'{PIHOLE_URL}/api/auth',
                                                 headers={'X-FTL-SID': sid},
                                                 method='DELETE')
                    urllib.request.urlopen(req, timeout=10).close()
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {'ok': True, 'started': True}

# Gates /cross_ref, /api/cross_ref and /api/capture — this page can trigger live packet
# captures, so it needs its own login rather than riding on the rest of the (unauthenticated)
# home dashboard suite. Fails closed: unset creds means the routes refuse all requests.
CROSS_REF_USER = os.environ.get('CROSS_REF_USER')
CROSS_REF_PASS = os.environ.get('CROSS_REF_PASS')

import sys
sys.path.insert(0, str(BASE / 'pollers'))
from steve import SERVICES as STEVE_SERVICES


def _steve_history():
    if not STEVE_DB.exists():
        return {'points': []}
    try:
        conn = sqlite3.connect(STEVE_DB)
        rows = conn.execute(
            'SELECT ts, load1, mem_pct, disk_pct FROM metrics_log ORDER BY ts'
        ).fetchall()
        conn.close()
        return {'points': [{'ts': r[0], 'load1': r[1], 'mem_pct': r[2], 'disk_pct': r[3]} for r in rows]}
    except Exception as e:
        return {'points': [], 'error': str(e)}


def _wacky_history():
    if not WACKY_DB.exists():
        return {'points': []}
    try:
        conn = sqlite3.connect(WACKY_DB)
        rows = conn.execute(
            'SELECT ts, load1, mem_pct, disk_pct FROM metrics_log ORDER BY ts'
        ).fetchall()
        conn.close()
        return {'points': [{'ts': r[0], 'load1': r[1], 'mem_pct': r[2], 'disk_pct': r[3]} for r in rows]}
    except Exception as e:
        return {'points': [], 'error': str(e)}


def _jeff_history():
    if not JEFF_DB.exists():
        return {'points': []}
    try:
        conn = sqlite3.connect(JEFF_DB)
        rows = conn.execute(
            'SELECT ts, load1, mem_pct, disk_pct FROM metrics_log ORDER BY ts'
        ).fetchall()
        conn.close()
        return {'points': [{'ts': r[0], 'load1': r[1], 'mem_pct': r[2], 'disk_pct': r[3]} for r in rows]}
    except Exception as e:
        return {'points': [], 'error': str(e)}


def _bazza_history():
    if not BAZZA_DB.exists():
        return {'points': []}
    try:
        conn = sqlite3.connect(BAZZA_DB)
        rows = conn.execute(
            'SELECT ts, load1, mem_pct, disk_pct FROM metrics_log ORDER BY ts'
        ).fetchall()
        conn.close()
        return {'points': [{'ts': r[0], 'load1': r[1], 'mem_pct': r[2], 'disk_pct': r[3]} for r in rows]}
    except Exception as e:
        return {'points': [], 'error': str(e)}


def _moisture_data(hours=24):
    """Latest reading per plant + bucket-averaged history for the chart. The sensor
    posts every 10s, so raw rows are far denser than a chart can show — average into
    buckets sized to the window (~300 points max) instead of shipping them all."""
    if not MOISTURE_DB.exists():
        return {'plants': [], 'history': {}, 'error': 'no moisture.db yet'}
    bucket = max(120, int(hours * 3600 / 300))
    cutoff = int(time.time()) - hours * 3600
    try:
        hidden = ','.join('?' * len(MOISTURE_HIDDEN))
        conn = sqlite3.connect(MOISTURE_DB)
        latest = conn.execute(
            "SELECT plant, raw, moisture_pct, battery_v, "
            "CAST(strftime('%s', recorded_at) AS INTEGER) "
            'FROM readings WHERE id IN (SELECT MAX(id) FROM readings GROUP BY plant) '
            f'AND plant NOT IN ({hidden}) ORDER BY plant',
            MOISTURE_HIDDEN,
        ).fetchall()
        rows = conn.execute(
            "SELECT plant, (CAST(strftime('%s', recorded_at) AS INTEGER) / ?) * ? AS tb, "
            'ROUND(AVG(moisture_pct), 1), CAST(ROUND(AVG(raw)) AS INTEGER) '
            "FROM readings WHERE CAST(strftime('%s', recorded_at) AS INTEGER) >= ? "
            f'AND plant NOT IN ({hidden}) GROUP BY plant, tb ORDER BY tb',
            (bucket, bucket, cutoff, *MOISTURE_HIDDEN),
        ).fetchall()
        conn.close()
    except Exception as e:
        return {'plants': [], 'history': {}, 'error': str(e)}
    history = {}
    for plant, tb, pct, raw in rows:
        history.setdefault(plant, []).append({'ts': tb, 'pct': pct, 'raw': raw})
    now = int(time.time())
    return {
        'ts': now, 'hours': hours,
        'plants': [{'plant': p, 'raw': r, 'moisture_pct': m, 'battery_v': b,
                    'last_seen': ts, 'age_s': now - ts}
                   for p, r, m, b, ts in latest],
        'history': history,
    }


def _restart_steve_service(name):
    if name not in STEVE_SERVICES:
        return {'ok': False, 'error': 'unknown service'}
    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', f'{name}.service'],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {'ok': False, 'error': result.stderr.strip() or f'exit {result.returncode}'}
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _kismet_bytes():
    """Raw file bytes — avoid parsing+re-serializing JSON we're just forwarding to the client."""
    if not KISMET_JSON.exists():
        return json.dumps({'error': 'no kismet.json — run pollers/kismet.py'}).encode()
    try:
        return KISMET_JSON.read_bytes()
    except Exception as e:
        return json.dumps({'error': str(e)}).encode()


def _hostnames_match(a, b):
    """True if two hostnames belong to the same device — exact match, or one is a
    short numeric-suffixed extension of the other. Seen with e.g. Bosch Home Connect
    appliances, which report a slightly longer serial-suffixed name on one interface
    than another (DHCP vs mDNS/IPv6). Guarded to a >=8-char shared prefix and <=8-char
    suffix so short generic names (e.g. 'tv' / 'tv2') don't false-positive match.
    """
    a, b = (a or '').lower(), (b or '').lower()
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 8:
        return False
    return longer.startswith(shorter) and (len(longer) - len(shorter)) <= 8


def _mac_is_random(mac):
    """True if the locally-administered bit is set — a randomized/private address,
    not the device's real vendor-assigned MAC (common on modern phones per-SSID)."""
    try:
        first_octet = int((mac or '').split(':')[0], 16)
    except ValueError:
        return False
    return bool(first_octet & 0x02)


def _network_anomalies(unifi_devices, result, ks):
    """Cross-layer red flags from UniFi (managed set) + Pi-hole (behaviour) + Kismet (RF)."""
    from collections import Counter, defaultdict
    # dedupe: a MAC can appear in both devices[] and unknown_clients[]
    ks_devs = list({(d.get('mac') or '').lower(): d
                    for d in ks.get('devices', []) + ks.get('unknown_clients', [])}.values())
    my_ssids = {d['ssid'] for d in unifi_devices if d.get('ssid')}
    managed  = {(d.get('mac') or '').lower() for d in unifi_devices if d.get('mac')}

    def strength(crypt):
        c = (crypt or '').upper()
        if 'WPA3' in c: return 3
        if 'WPA2' in c: return 2
        if 'WPA1' in c or 'WEP' in c: return 1
        return 0  # Open / unknown

    # Baseline the crypto each of your SSIDs normally uses, and index every BSSID broadcasting it.
    # (Your UniFi AP fans one SSID across several virtual BSSIDs — all with the *correct* crypto —
    # so an evil-twin stands out by broadcasting your SSID with weaker crypto, not by being "new".)
    ssid_crypt  = defaultdict(Counter)
    ssid_bssids = defaultdict(list)
    for d in ks_devs:
        if 'AP' in str(d.get('type', '')) and d.get('ssid') in my_ssids:
            ssid_crypt[d['ssid']][d.get('crypt') or 'Open'] += 1
            ssid_bssids[d['ssid']].append(d)

    # BSSID -> SSID, so a client's "associated AP" can be shown by name, not just MAC
    bssid_ssid = {(d.get('mac') or '').lower(): d.get('ssid')
                  for d in ks_devs if 'AP' in str(d.get('type', '')) and d.get('ssid')}

    anoms, rf_index = [], {}
    def add(sev, kind, title, detail, dev=None):
        mac = dev.get('mac') if dev else None
        anoms.append({'severity': sev, 'kind': kind, 'title': title, 'detail': detail, 'mac': mac})
        if dev and mac:
            entry = {k: dev.get(k) for k in
                ('signal', 'signal_min', 'signal_max', 'channel', 'crypt', 'type', 'manuf', 'ssid',
                 'first_time', 'last_time', 'known', 'probes', 'assoc_bssid', 'pkt_total', 'pkt_data')}
            entry['mac_random'] = _mac_is_random(mac)
            assoc = (dev.get('assoc_bssid') or '').lower()
            entry['assoc_ssid'] = bssid_ssid.get(assoc) if assoc else None
            rf_index[mac] = entry

    # 1. Evil-twin: a BSSID broadcasting your SSID with weaker crypto than that SSID normally uses
    for ssid, cnt in ssid_crypt.items():
        norm = strength(cnt.most_common(1)[0][0])
        for d in ssid_bssids[ssid]:
            if strength(d.get('crypt')) < norm:
                add('critical', 'evil_twin', f'Possible evil-twin on “{ssid}”',
                    f'{d.get("mac")} broadcasts your SSID as {d.get("crypt") or "Open"} '
                    f'(normally {cnt.most_common(1)[0][0].split()[0]}) · {d.get("signal")} dBm', d)

    # 2. Open / weak-crypto networks in range (neighbours) — strongest few only
    weak = [d for d in ks_devs if 'AP' in str(d.get('type', '')) and d.get('ssid') not in my_ssids
            and ((d.get('crypt') or '') == 'Open' or 'WEP' in (d.get('crypt') or '') or 'WPA1' in (d.get('crypt') or ''))]
    for d in sorted(weak, key=lambda x: -(x.get('signal') or -999))[:6]:
        c = d.get('crypt') or 'Open'
        add('info', 'weak_ap', f'{"Open" if c == "Open" else "Weak-crypto"} network in range',
            f'{d.get("ssid") or "(hidden)"} · {c} · {d.get("manuf", "?")} · {d.get("signal")} dBm', d)

    # 3. Managed clients whose DNS is mostly ads / trackers / malware
    for r in result:
        if (r.get('dns_pct') or 0) >= 25 and (r.get('dns_total') or 0) >= 500:
            add('warn', 'dns_block', f'Heavy ad/tracker DNS · {r.get("display_name") or r.get("ip")}',
                f'{r["dns_pct"]}% of {r["dns_total"]:,} queries blocked', r)

    # 4. Strong, persistent, unmanaged devices parked in RF range (possible lurker)
    lurk = []
    for d in ks_devs:
        if 'AP' in str(d.get('type', '')):
            continue
        if (d.get('mac') or '').lower() in managed or d.get('known'):
            continue  # skip your own gear (UniFi-managed or Kismet-resolved)
        sig = d.get('signal')
        dur = (d.get('last_time', 0) or 0) - (d.get('first_time', 0) or 0)
        if sig is not None and sig >= -55 and dur >= 900:
            lurk.append(d)
    for d in sorted(lurk, key=lambda x: -(x.get('signal') or -999))[:8]:
        mins = ((d.get('last_time', 0) or 0) - (d.get('first_time', 0) or 0)) // 60
        probes = [p for p in (d.get('probes') or []) if p]
        extra = []
        if _mac_is_random(d.get('mac')):
            extra.append('randomized MAC')
        assoc = (d.get('assoc_bssid') or '').lower()
        assoc_ssid = bssid_ssid.get(assoc) if assoc else None
        if assoc_ssid:
            extra.append(f'on “{assoc_ssid}”')
        elif probes:
            shown = ', '.join(probes[:3]) + ('…' if len(probes) > 3 else '')
            extra.append(f'probing for {shown}')
        tail = (' · ' + ' · '.join(extra)) if extra else ''
        add('info', 'lurker', 'Unrecognised device at close range',
            f'{d.get("mac")} · {d.get("manuf") or "unknown vendor"} · {d.get("signal")} dBm · seen {mins}m{tail}', d)

    # 5. Kismet's own IDS alerts (deauth floods, etc.)
    for a in ks.get('alerts', []):
        add('critical', 'kismet_alert', a.get('header', 'Kismet alert'), a.get('text', ''), {'mac': a.get('mac')})

    order = {'critical': 0, 'warn': 1, 'info': 2}
    anoms.sort(key=lambda a: order.get(a['severity'], 3))
    broadcasts = {s: [{'mac': d.get('mac'), 'manuf': d.get('manuf'), 'signal': d.get('signal'),
                       'crypt': d.get('crypt'), 'channel': d.get('channel'), 'known': d.get('known')}
                      for d in sorted(v, key=lambda x: -(x.get('signal') or -999))]
                  for s, v in ssid_bssids.items()}
    return {'anomalies': anoms, 'my_ssids': sorted(my_ssids),
            'ssid_broadcasts': broadcasts, 'rf_index': rf_index}


_cross_ref_cache = {'mtimes': None, 'result': None}


def _cross_ref():
    """Cached wrapper — the underlying files change on their own poll/cron schedules
    (kismet.json every 5min, etc), not on every dashboard refresh, so there's no need
    to rerun the full anomaly scan on every request between updates."""
    mtimes = tuple(p.stat().st_mtime if p.exists() else None
                   for p in (PIHOLE_JSON, DEVICES_JSON, KISMET_JSON))
    if mtimes == _cross_ref_cache['mtimes'] and _cross_ref_cache['result'] is not None:
        return _cross_ref_cache['result']
    result = _compute_cross_ref()
    _cross_ref_cache['mtimes'] = mtimes
    _cross_ref_cache['result'] = result
    return result


def _compute_cross_ref():
    """Join Pi-hole clients_detail with UniFi devices.json + Kismet RF, and surface anomalies."""
    try:
        ph = json.loads(PIHOLE_JSON.read_text()) if PIHOLE_JSON.exists() else {}
        ud = json.loads(DEVICES_JSON.read_text()) if DEVICES_JSON.exists() else {}
        ks = json.loads(KISMET_JSON.read_text()) if KISMET_JSON.exists() else {}
    except Exception as e:
        return {'error': str(e), 'devices': []}

    kismet_by_mac = {d['mac'].lower(): d for d in ks.get('devices', [])}

    unifi_devices = ud.get('devices', [])
    ph_clients    = ph.get('clients_detail', [])
    ip_mac        = ph.get('ip_mac', {})  # Pi-hole network table: ip -> hardware MAC

    # Build lookups: ip→client and name→[clients] (name may be ambiguous)
    by_ip   = {c['ip']: c for c in ph_clients}
    by_name = {}
    for c in ph_clients:
        n = (c['name'] or '').lower()
        if n:
            by_name.setdefault(n, []).append(c)

    result = []
    for dev in unifi_devices:
        ip       = dev.get('ip', '')
        ipv6s    = dev.get('ipv6') or []
        hostname = (dev.get('hostname') or dev.get('display_name') or '').lower()

        # A device can appear under several Pi-hole rows: its IPv4 plus every rotating
        # IPv6 privacy address — merge all of them. Pi-hole's own MAC table catches
        # addresses the device has already rotated away from, which UniFi no longer lists.
        mac_key = (dev.get('mac') or '').lower()
        matched = [c for c in ph_clients if mac_key and ip_mac.get(c['ip']) == mac_key]
        matched += [c for c in (by_ip.get(a) for a in (ip, *ipv6s))
                    if c and c not in matched]
        if not matched and hostname:
            names = by_name.get(hostname, [])
            # Only trust hostname match when it's unique (not "iphone" matching 6 entries)
            if len(names) == 1:
                matched = names

        if matched:
            ph_total, ph_blocked = sum(c['total'] for c in matched), sum(c['blocked'] for c in matched)
            ph_pct, ph_ip = (round(ph_blocked / ph_total * 100, 1) if ph_total else 0.0), matched[0]['ip']
        else:
            ph_total = ph_blocked = ph_pct = ph_ip = None

        ks_dev  = kismet_by_mac.get(mac_key, {})
        result.append({
            **dev,
            'dns_total':     ph_total,
            'dns_blocked':   ph_blocked,
            'dns_pct':       ph_pct,
            'dns_ip':        ph_ip,
            'rf_signal':     ks_dev.get('signal'),
            'rf_channel':    ks_dev.get('channel'),
            'rf_manuf':      ks_dev.get('manuf'),
            'rf_last_seen':  ks_dev.get('last_time'),
            'rf_crypt':      ks_dev.get('crypt'),
            'rf_type':       ks_dev.get('type'),
            'rf_known':      ks_dev.get('known'),
        })

    # Also include Pi-hole-only clients — no UniFi/RF match, so link type is unknown
    # (NOT necessarily wired: it just means we have no wireless data for it, e.g. it's
    # offline right now). A single device can also show up under several Pi-hole IPs
    # since iOS/Android rotate IPv6 privacy addresses — merge same-named entries into one row.
    unifi_ips = set()
    for d in unifi_devices:
        if d.get('ip'):
            unifi_ips.add(d['ip'])
        unifi_ips.update(d.get('ipv6') or [])
    unifi_names = [(d.get('hostname') or '').lower() for d in unifi_devices if d.get('hostname')]
    unifi_macs  = {(d.get('mac') or '').lower() for d in unifi_devices if d.get('mac')}
    unmatched = [c for c in ph_clients
                 if c['ip'] not in unifi_ips
                 and ip_mac.get(c['ip']) not in unifi_macs
                 and not any(_hostnames_match(c['name'], n) for n in unifi_names)]

    # Cluster unmatched clients — same MAC (Pi-hole network table) is the strongest
    # signal, then (fuzzy) name — so a device doesn't fragment into several rows
    # across its rotating IPv6 privacy addresses or slight name differences.
    groups = []      # [(name_key_or_ip, [clients])]
    mac_members = {} # mac -> members list (aliases entries in groups)
    for c in unmatched:
        mac = ip_mac.get(c['ip'])
        if mac and mac in mac_members:
            mac_members[mac].append(c)
            continue
        name = (c['name'] or '').lower()
        members = None
        if name:
            for key, ms in groups:
                if key and _hostnames_match(name, key):
                    members = ms
                    ms.append(c)
                    break
        if members is None:
            members = [c]
            # unnamed clients keyed by IP so they never fuzzy-match a name
            groups.append((name or c['ip'], members))
        if mac:
            mac_members[mac] = members

    for _, group in groups:
        ipv4 = [c for c in group if ':' not in c['ip']]
        primary = max(ipv4 or group, key=lambda c: c['total'])  # prefer a stable IPv4 over a rotating IPv6
        name    = primary['name'] or next((c['name'] for c in group if c['name']), '')
        total   = sum(c['total'] for c in group)
        blocked = sum(c['blocked'] for c in group)
        other_ips = sorted({c['ip'] for c in group} - {primary['ip']})
        result.append({
            'mac': ip_mac.get(primary['ip']), 'hostname': name, 'display_name': name or primary['ip'],
            'ip': primary['ip'], 'ip_alt': other_ips,
            'vendor': '', 'ssid': '', 'ap': '', 'signal': None,
            'retry_pct': None, 'is_wired': None, 'flagged': False,
            'first_seen': None, 'last_seen': None, 'guessed': False,
            'dns_total': total, 'dns_blocked': blocked,
            'dns_pct': round(blocked / total * 100, 1) if total else 0, 'dns_ip': primary['ip'],
        })

    result.sort(key=lambda d: -(d['dns_total'] or 0))
    intel = _network_anomalies(unifi_devices, result, ks)
    return {
        'ts':      ph.get('ts'),
        'summary': ph.get('summary', {}),
        'history': ph.get('history', []),
        'devices': result,
        **intel,
    }


def _capture_allowed_macs():
    """MACs on your own UniFi-managed network — the only devices captures may target."""
    try:
        ud = json.loads(DEVICES_JSON.read_text()) if DEVICES_JSON.exists() else {}
    except Exception:
        return set()
    return {(d.get('mac') or '').lower() for d in ud.get('devices', []) if d.get('mac')}


# (frame type, subtype) -> human label. Types: 0=management, 1=control, 2=data.
# These come straight from the always-cleartext 802.11 MAC header, so they're readable
# even for WPA2/3 traffic where the actual data payload is encrypted.
_FRAME_LABELS = {
    (0, 0): 'Assoc Request', (0, 1): 'Assoc Response', (0, 2): 'Reassoc Request',
    (0, 3): 'Reassoc Response', (0, 4): 'Probe Request', (0, 5): 'Probe Response',
    (0, 8): 'Beacon', (0, 9): 'ATIM', (0, 10): 'Disassociation',
    (0, 11): 'Authentication', (0, 12): 'Deauthentication', (0, 13): 'Action',
    (1, 8): 'Block Ack Request', (1, 9): 'Block Ack', (1, 10): 'PS-Poll',
    (1, 11): 'RTS', (1, 12): 'CTS', (1, 13): 'ACK', (1, 14): 'CF-End',
    (2, 0): 'Data', (2, 4): 'Null (keepalive)', (2, 8): 'QoS Data',
    (2, 12): 'QoS Null (keepalive)', (2, 14): 'QoS CF-Poll (keepalive)',
}
_TYPE_NAMES = {0: 'management', 1: 'control', 2: 'data'}
_MULTICAST_PREFIXES = ('ff:ff:ff:ff:ff:ff', '01:00:5e', '33:33')


def _summarize_capture(pcap_path, mac):
    """Turn a filtered pcap into a readable rundown using only 802.11 header metadata
    (frame type/subtype, addresses, signal, retry flag) — none of this requires
    decrypting the payload, so it works the same whether the traffic is WPA-protected
    or not.
    """
    fields = ['frame.time_relative', 'wlan.fc.type', 'wlan.fc.subtype',
              'wlan.sa', 'wlan.sa_resolved', 'wlan.da', 'wlan.da_resolved',
              'wlan.bssid', 'wlan.bssid_resolved', 'wlan.ssid', 'wlan.fc.retry',
              'radiotap.dbm_antsignal', 'radiotap.channel.freq']
    cmd = ['tshark', '-r', str(pcap_path), '-T', 'fields', '-E', 'separator=\t']
    for f in fields:
        cmd += ['-e', f]
    result = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]

    from collections import Counter
    type_counts, detail_counts, freqs, peers = Counter(), Counter(), Counter(), Counter()
    retries, signals, ssids = 0, [], set()

    for line in lines:
        p = (line.split('\t') + [''] * len(fields))[:len(fields)]
        t_rel, fc_type, fc_sub, sa, sa_r, da, da_r, bssid, bssid_r, ssid, retry, sig, freq = p
        try:
            t, s = int(fc_type), int(fc_sub)
        except ValueError:
            continue
        type_counts[_TYPE_NAMES.get(t, f'type {t}')] += 1
        detail_counts[_FRAME_LABELS.get((t, s), f'type {t}/{s}')] += 1
        if retry == 'True':
            retries += 1
        if sig:
            try:
                signals.append(max(int(x) for x in sig.split(',')))
            except ValueError:
                pass
        if freq:
            try:
                freqs[int(freq.split(',')[0])] += 1
            except ValueError:
                pass
        if ssid:
            ssids.add(ssid)
        for raw, resolved in ((sa, sa_r), (da, da_r), (bssid, bssid_r)):
            if not raw or raw.lower() == mac or raw.lower().startswith(_MULTICAST_PREFIXES):
                continue
            peers[resolved or raw] += 1

    total = len(lines)
    return {
        'frame_count':      total,
        'duration_s':       max(0.0, round(float(lines[-1].split('\t')[0]), 1)) if lines else 0,
        'type_counts':      dict(type_counts),
        'detail_counts':    dict(detail_counts.most_common(8)),
        'retry_count':      retries,
        'retry_pct':        round(retries / total * 100, 1) if total else 0,
        'signal':           {'min': min(signals), 'max': max(signals),
                              'avg': round(sum(signals) / len(signals), 1)} if signals else None,
        'channel_freq_mhz': freqs.most_common(1)[0][0] if freqs else None,
        'ssids_seen':       sorted(ssids),
        'top_peers':        [{'peer': p, 'count': c} for p, c in peers.most_common(5)],
    }


def _capture_traffic(mac, duration=20):
    """Capture `duration`s of live 802.11 traffic for one device, filtered server-side
    before it's ever written to a servable file, then summarized. Hard-blocked to devices
    on your own UniFi-managed network — see [[wireless-lab authorization]] in memory for
    why capturing other devices' packet contents (as opposed to passive RF metadata)
    isn't in scope.
    """
    mac = (mac or '').strip().lower()
    if not re.fullmatch(r'([0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
        return None, 'invalid MAC address'
    if mac not in _capture_allowed_macs():
        return None, 'device is not on your network — capture blocked'
    if not KISMET_USER or not KISMET_PASS:
        return None, 'KISMET_USER/KISMET_PASS not set in server.py environment'

    raw_fd, raw_path = tempfile.mkstemp(suffix='.pcapng')
    out_fd, out_path = tempfile.mkstemp(suffix='.pcap')
    os.close(out_fd)
    try:
        token = base64.b64encode(f'{KISMET_USER}:{KISMET_PASS}'.encode()).decode()
        req = urllib.request.Request(f'{KISMET_URL}/pcap/all_packets.pcapng',
                                      headers={'Authorization': f'Basic {token}'})
        deadline = time.time() + duration
        with urllib.request.urlopen(req, timeout=duration + 10) as resp, os.fdopen(raw_fd, 'wb') as f:
            while time.time() < deadline:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

        # Filter to just this MAC before the data is ever exposed for download. tshark
        # exits non-zero here because we deliberately cut the live stream mid-packet at
        # the deadline — it still writes everything filtered up to that point, so trust
        # the output file rather than the exit code.
        result = subprocess.run(
            ['tshark', '-r', raw_path, '-Y', f'wlan.addr=={mac}', '-w', out_path],
            capture_output=True, timeout=30,
        )
        data = Path(out_path).read_bytes()
        if len(data) < 100:
            # An empty filtered result is the normal case when the device was idle for
            # the whole window — tshark's stderr here is just the expected mid-packet
            # truncation warning from us deliberately cutting the stream, not a real
            # error, so don't surface it as if it explains the failure.
            return None, f'no packets captured for this device — it may have been idle during this {duration}s window, try again'
        summary = _summarize_capture(out_path, mac)
        return {'summary': summary, 'pcap_b64': base64.b64encode(data).decode()}, None
    except Exception as e:
        return None, str(e)
    finally:
        for p in (raw_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _unifi_device_history(hostname):
    if not hostname or not UNIFI_DB.exists():
        return {'history': [], 'events': []}
    try:
        conn = sqlite3.connect(UNIFI_DB)
        cutoff = int(time.time()) - 86400 * 7  # 7 days
        history = conn.execute(
            'SELECT ts, signal, retry_pct, essid FROM weak_client_log '
            'WHERE hostname=? AND ts>=? ORDER BY ts',
            (hostname, cutoff)
        ).fetchall()
        events = conn.execute(
            'SELECT ts, type, title, message FROM events_log '
            'WHERE message LIKE ? ORDER BY ts DESC LIMIT 50',
            (f'%{hostname}%',)
        ).fetchall()
        conn.close()
        return {
            'history': [{'ts': r[0], 'signal': r[1], 'retry_pct': r[2], 'essid': r[3]} for r in history],
            'events':  [{'ts': r[0], 'type': r[1], 'title': r[2], 'message': r[3]} for r in events],
        }
    except Exception as e:
        return {'history': [], 'events': [], 'error': str(e)}


def _unifi_radio_history(days=3):
    """Continuous per-radio utilization (for the timeline chart), hour-of-day
    averages (over a longer 14-day window — more data makes a better hourly
    signal than the timeline's shorter lookback), and the events worth
    overlaying on that timeline."""
    if not UNIFI_DB.exists():
        return {'radios': [], 'hourly': [], 'events': []}
    try:
        conn = sqlite3.connect(UNIFI_DB)
        cutoff = int(time.time()) - days * 86400

        rows = conn.execute(
            'SELECT ts, ap, radio, cu_total, num_sta FROM radio_util_log WHERE ts>=? ORDER BY ts',
            (cutoff,)
        ).fetchall()
        radios = {}
        for ts, ap, radio, cu, n in rows:
            radios.setdefault(f'{ap} · {radio}', []).append({'ts': ts, 'cu': cu, 'num_sta': n})

        hourly_cutoff = int(time.time()) - 14 * 86400
        hrows = conn.execute(
            'SELECT ts, ap, radio, cu_total FROM radio_util_log WHERE ts>=? AND cu_total IS NOT NULL',
            (hourly_cutoff,)
        ).fetchall()
        hourly_acc = {}
        for ts, ap, radio, cu in hrows:
            key = f'{ap} · {radio}'
            hour = time.localtime(ts).tm_hour
            hourly_acc.setdefault(key, {}).setdefault(hour, []).append(cu)
        hourly = [
            {'name': key, 'hours': [{'hour': h, 'avg_cu': round(sum(vs) / len(vs), 1)}
                                     for h, vs in sorted(hours.items())]}
            for key, hours in hourly_acc.items()
        ]

        events = conn.execute(
            "SELECT ts, type, title, message FROM events_log WHERE ts>=? AND type IN "
            "('device_joined','device_left','channel_changed','congestion','resolved') ORDER BY ts",
            (cutoff,)
        ).fetchall()
        conn.close()
        return {
            'radios': [{'name': k, 'points': v} for k, v in radios.items()],
            'hourly': hourly,
            'events': [{'ts': r[0], 'type': r[1], 'title': r[2], 'message': r[3]} for r in events],
        }
    except Exception as e:
        return {'radios': [], 'hourly': [], 'events': [], 'error': str(e)}


def _trigger_gateway_speedtest():
    try:
        import sys
        sys.path.insert(0, str(Path.home() / 'projects' / 'unifi_poller'))
        from unifi_client import UnifiClient
        import os
        pw = os.environ.get('UNIFI_PASSWORD', '')
        if not pw:
            return {'ok': False, 'error': 'UNIFI_PASSWORD not set'}
        client = UnifiClient('https://192.168.1.1', 'unifi', pw)
        client.trigger_speedtest()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _speedtest_history():
    if not UNIFI_DB.exists():
        return {'results': []}
    try:
        conn = sqlite3.connect(UNIFI_DB)
        rows = conn.execute(
            'SELECT ts, ping_ms, download_mbps, upload_mbps FROM speedtest_log ORDER BY ts'
        ).fetchall()
        conn.close()
        return {'results': [{'ts': r[0], 'ping_ms': r[1],
                              'download_mbps': r[2], 'upload_mbps': r[3]} for r in rows]}
    except Exception as e:
        return {'results': [], 'error': str(e)}


def _unifi_events():
    if not UNIFI_DB.exists():
        return {'events': []}
    try:
        conn = sqlite3.connect(UNIFI_DB)
        rows = conn.execute(
            'SELECT ts, type, title, message FROM events_log ORDER BY ts DESC LIMIT 300'
        ).fetchall()
        conn.close()
        return {'events': [{'ts': r[0], 'type': r[1], 'title': r[2], 'message': r[3]} for r in rows]}
    except Exception as e:
        return {'events': [], 'error': str(e)}


@dataclass(frozen=True)
class Route:
    """One dispatch-table entry. `handler(request)` writes the whole response; `auth`
    gates the route behind the cross-ref Basic-auth login; `json_ct` (POST only) rejects
    anything but an application/json body. Every route's auth policy lives right here on
    it — there is no separate list of "protected" paths to keep in sync."""
    handler: Callable
    auth: bool = False
    json_ct: bool = False


def _page(name, auth=False):
    return Route(lambda h: h._file(name, 'text/html; charset=utf-8'), auth=auth)


def _absfile(path, ct):
    return Route(lambda h: h._abs_file(path, ct))


def _jsonfn(fn, auth=False):
    return Route(lambda h: h._json(fn()), auth=auth)


def _proxy_to(url):
    return Route(lambda h: h._proxy(url))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Append the Referer so index -> subpage click-throughs are visible in the
        # journal (same-site referers look like http://<host>:8080/). headers may be
        # unset when logging a malformed request line, hence the guard.
        hdrs = getattr(self, 'headers', None)
        ref = hdrs.get('Referer', '-') if hdrs else '-'
        # flush: stdout is block-buffered under systemd, so without this the journal
        # lags the actual requests by however long the pipe buffer takes to fill.
        print('%s ref=%s' % (fmt % args, ref), flush=True)

    def _require_cross_ref_auth(self):
        """True if the request is authorized. Sends the 401/503 response itself on failure."""
        if not CROSS_REF_USER or not CROSS_REF_PASS:
            self.send_error(503, 'CROSS_REF_USER/CROSS_REF_PASS not configured on server')
            return False
        given = self.headers.get('Authorization', '')
        ok = False
        if given.startswith('Basic '):
            try:
                user, pw = base64.b64decode(given[6:]).decode().split(':', 1)
                ok = hmac.compare_digest(user, CROSS_REF_USER) and hmac.compare_digest(pw, CROSS_REF_PASS)
            except Exception:
                ok = False
        if not ok:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="cross-ref"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        return True

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/eufy/snapshot/'):
            route = ROUTES_GET.get('/eufy/snapshot/')
        else:
            route = ROUTES_GET.get(path)
        if route is None:
            self.send_error(404)
            return
        if route.auth and not self._require_cross_ref_auth():
            return
        route.handler(self)

    def do_POST(self):
        path = self.path.split('?')[0]
        route = ROUTES_POST.get(path)
        if route is None:
            self.send_error(404)
            return
        # State-changing endpoints need the login: a restart rides passwordless sudo and a
        # speedtest saturates the WAN, so a hostile LAN host must not reach them unauthenticated.
        if route.auth and not self._require_cross_ref_auth():
            return
        # Requiring application/json forces a cross-origin POST through a CORS preflight, which
        # we never answer — so a malicious page can't drive these from a plain <form> (text/plain
        # and form-urlencoded are CORS "simple" types that skip preflight and would otherwise
        # reach this handler with no browser pushback).
        if route.json_ct:
            ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if ctype != 'application/json':
                self.send_error(400, 'Content-Type must be application/json')
                return
        route.handler(self)

    # ── request-specific response builders (wired up in ROUTES_GET / ROUTES_POST) ──
    def _res_kismet(self):
        # _kismet_bytes() is already serialized JSON, so it bypasses _json().
        self._raw(_kismet_bytes(), 'application/json')

    def _res_unifi_device_history(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        self._json(_unifi_device_history(qs.get('hostname', [''])[0]))

    def _res_unifi_radio_history(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            days = max(1, min(int(qs.get('days', ['3'])[0]), 14))
        except ValueError:
            days = 3
        self._json(_unifi_radio_history(days))

    def _res_is_it_broken(self):
        status = _is_it_broken_status()
        status['client_ip'] = self.client_address[0]
        status['client_device'] = _device_from_ua(self.headers.get('User-Agent'))
        self._json(status)

    def _res_moisture(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            hours = max(1, min(int(qs.get('hours', ['24'])[0]), 24 * 90))
        except ValueError:
            hours = 24
        self._json(_moisture_data(hours))

    def _res_smokeping_series(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        target = qs.get('target', [''])[0]
        rng = qs.get('range', ['3h'])[0]
        if not _SP_TARGET_RE.match(target) or rng not in SMOKEPING_RANGES:
            self.send_error(400, 'bad target or range')
            return
        try:
            data = _smokeping_series(target, rng)
        except FileNotFoundError as e:
            self.send_error(404, str(e))
            return
        except Exception as e:
            self.send_error(502, f'smokeping rrd read failed: {e}')
            return
        self._json(data)

    def _res_unifi_png(self):
        self._abs_file(UNIFI_DATA / self.path.split('?')[0].split('/')[-1], 'image/png')

    def _res_eufy_snapshot(self):
        name = self.path.split('?')[0].split('/')[-1]
        if not _EUFY_SNAPSHOT_RE.match(name):
            self.send_error(404)
            return
        ct = 'image/png' if name.lower().endswith('.png') else 'image/jpeg'
        self._abs_file(EUFY_SNAPSHOTS / name, ct)

    def _res_steve_restart(self):
        name = self._body_json().get('service', '')
        if name == 'home-menu':
            # Restarting the service that's handling this very request kills the
            # process before a synchronous reply can be sent — the client just sees
            # a dropped connection even though the restart succeeds. Reply first,
            # then restart shortly after so the ack actually reaches the browser.
            if name not in STEVE_SERVICES:
                self._json({'ok': False, 'error': 'unknown service'})
            else:
                self._json({'ok': True})
                threading.Timer(0.5, subprocess.run,
                                 args=(['sudo', '-n', 'systemctl', 'restart', 'home-menu.service'],),
                                 kwargs={'capture_output': True, 'timeout': 15}).start()
        else:
            self._json(_restart_steve_service(name))

    def _res_capture(self):
        mac = self._body_json().get('mac', '')
        result, err = _capture_traffic(mac)
        self._json({'ok': False, 'error': err} if err else {'ok': True, **result})

    def _body_json(self):
        """Parsed JSON request body, or {} on anything malformed. Bounded so a huge
        (or garbage) Content-Length can't make us allocate an arbitrary buffer."""
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            return {}
        if not 0 < length <= 65536:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _raw(self, body, ct, extra=None):
        """Send a 200 with a pre-serialized body and Content-Type, plus any extra headers."""
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(body))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._raw(json.dumps(data).encode(), 'application/json')

    def _proxy(self, url):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'home-menu/1.0'})
            with urllib.request.urlopen(req, timeout=4) as r:
                body = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            err = json.dumps({'error': str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(err))
            self.end_headers()
            self.wfile.write(err)

    def _abs_file(self, path, ct):
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)

    def _file(self, name, ct):
        try:
            with open(PAGES / name, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)


# ── Route tables ──────────────────────────────────────────────────────────────
# The single source of truth for what each path serves and whether it needs the
# cross-ref login. cross_ref is the only browser-facing route behind auth=True; the
# four state-changing POSTs carry auth=True + json_ct=True together (see do_POST).
ROUTES_GET = {
    # HTML pages
    '/':                     _page('index.html'),
    '/index.html':           _page('index.html'),
    '/unifi':                _page('unifi.html'),
    '/unifi/map':            _page('unifi_map.html'),
    '/unifi/devices':        _page('unifi_devices.html'),
    '/unifi/events':         _page('unifi_events.html'),
    '/speedtest':            _page('speedtest.html'),
    '/cross_ref':            _page('cross_ref.html', auth=True),
    '/kismet':               _page('kismet.html'),
    '/pihole':               _page('pihole.html'),
    '/atlas':                _page('atlas.html'),
    '/uplink':               _page('uplink.html'),
    '/moisture':             _page('moisture.html'),
    '/smokeping':            _page('smokeping.html'),
    '/steve':                _page('steve.html'),
    '/wacky':                _page('wacky.html'),
    '/jeff':                 _page('jeff.html'),
    '/bazza':                _page('bazza.html'),
    '/eufy':                 _page('eufy.html'),
    '/arr':                  _page('arr.html'),
    '/is-it-broken':         _page('is-it-broken.html'),
    '/report':               _absfile(REPORT_HTML, 'text/html; charset=utf-8'),

    # Static assets
    '/vis-network.min.js':   _absfile(STATIC / 'vis-network.min.js', 'application/javascript'),
    '/static/world_land.js': _absfile(STATIC / 'world_land.js', 'application/javascript'),
    '/chart.min.js':         _absfile(STATIC / 'chart.min.js', 'application/javascript'),

    # JSON served straight from a file on disk (written by the pollers)
    '/api/unifi/graph':      _absfile(UNIFI_DATA / 'topology.json', 'application/json'),
    '/api/unifi/devices':    _absfile(DEVICES_JSON, 'application/json'),
    '/api/pihole':           _absfile(PIHOLE_JSON, 'application/json'),
    '/api/atlas':            _absfile(DATA / 'atlas.json', 'application/json'),
    '/api/uplink':           _absfile(DATA / 'uplink.json', 'application/json'),
    '/api/smokeping':        _jsonfn(_smokeping_status),
    '/api/smokeping/rrd':    _absfile(DATA / 'smokeping_rrd.json', 'application/json'),
    '/api/steve':            _absfile(STEVE_JSON, 'application/json'),
    '/api/wacky':            _absfile(WACKY_JSON, 'application/json'),
    '/api/jeff':             _absfile(JEFF_JSON, 'application/json'),
    '/api/bazza':            _absfile(BAZZA_JSON, 'application/json'),
    '/api/arr':              _absfile(ARR_JSON, 'application/json'),
    '/api/eufy':             _absfile(EUFY_JSON, 'application/json'),
    '/api/eufy/vacuum':      _absfile(EUFY_VACUUM_JSON, 'application/json'),

    # JSON computed on demand
    '/api/unifi':            _jsonfn(_unifi_status),
    '/api/is-it-broken':     Route(Handler._res_is_it_broken),
    '/api/unifi/events':     _jsonfn(_unifi_events),
    '/api/speedtest':        _jsonfn(_speedtest_history),
    '/api/steve/history':    _jsonfn(_steve_history),
    '/api/wacky/history':    _jsonfn(_wacky_history),
    '/api/jeff/history':     _jsonfn(_jeff_history),
    '/api/bazza/history':    _jsonfn(_bazza_history),
    '/api/cross_ref':        _jsonfn(_cross_ref, auth=True),

    # Request-specific (parse the query string / stream raw bytes)
    '/api/kismet':           Route(Handler._res_kismet),
    '/api/unifi/device':     Route(Handler._res_unifi_device_history),
    '/api/unifi/history':    Route(Handler._res_unifi_radio_history),
    '/api/moisture':         Route(Handler._res_moisture),
    '/api/smokeping/series': Route(Handler._res_smokeping_series),
    '/unifi/topology.png':   Route(Handler._res_unifi_png),
    '/unifi/dashboard.png':  Route(Handler._res_unifi_png),
    '/eufy/snapshot/':       Route(Handler._res_eufy_snapshot),
}
# Service API pass-throughs (all unauthenticated GETs) — see PROXIES.
for _p, _u in PROXIES.items():
    ROUTES_GET[_p] = _proxy_to(_u)

ROUTES_POST = {
    '/api/speedtest/trigger': Route(lambda h: h._json(_trigger_gateway_speedtest()),
                                    auth=True, json_ct=True),
    '/api/pihole/gravity':    Route(lambda h: h._json(_trigger_gravity()),
                                    auth=True, json_ct=True),
    '/api/steve/restart':     Route(Handler._res_steve_restart, auth=True, json_ct=True),
    '/api/capture':           Route(Handler._res_capture, auth=True, json_ct=True),
}


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Home menu running on http://0.0.0.0:{PORT}')
    srv.serve_forever()
