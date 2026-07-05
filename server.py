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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.path.dirname(__file__)
PORT = 8080

UNIFI_DB   = Path.home() / 'unifi_poller.db'
UNIFI_DIR  = Path.home() / 'projects' / 'unifi_poller'

PROXIES = {
    '/api/harold':  'http://localhost:5000/api/status',
    '/api/train':   'http://localhost:8192/api/departures',
    '/api/parents': 'http://localhost:8092/api/status',
    '/api/darren':  'http://localhost:8193/api/status',
    '/api/weather': 'http://localhost:8186/api/weather',
}


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


PIHOLE_JSON  = Path(__file__).parent / 'pihole.json'
KISMET_JSON  = Path(__file__).parent / 'kismet.json'
STEVE_JSON   = Path(__file__).parent / 'steve.json'
STEVE_DB     = Path(__file__).parent / 'steve_history.db'
DEVICES_JSON = UNIFI_DIR / 'devices.json'

KISMET_URL  = os.environ.get('KISMET_URL', 'http://localhost:2501')
KISMET_USER = os.environ.get('KISMET_USER')
KISMET_PASS = os.environ.get('KISMET_PASS')

# Gates /cross_ref, /api/cross_ref and /api/capture — this page can trigger live packet
# captures, so it needs its own login rather than riding on the rest of the (unauthenticated)
# home dashboard suite. Fails closed: unset creds means the routes refuse all requests.
CROSS_REF_USER = os.environ.get('CROSS_REF_USER')
CROSS_REF_PASS = os.environ.get('CROSS_REF_PASS')

from steve_poller import SERVICES as STEVE_SERVICES


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
        return json.dumps({'error': 'no kismet.json — run kismet_poller.py'}).encode()
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
        # IPv6 privacy address UniFi has seen it use — merge all of them together.
        matched = [c for c in (by_ip.get(a) for a in (ip, *ipv6s)) if c]
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

        mac_key = (dev.get('mac') or '').lower()
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
    unmatched = [c for c in ph_clients
                 if c['ip'] not in unifi_ips
                 and not any(_hostnames_match(c['name'], n) for n in unifi_names)]

    # Cluster unmatched clients by (fuzzy) name so the same device doesn't fragment
    # into several rows just because its name differs slightly across IPs.
    groups = []  # [(representative_name_or_ip, [clients])]
    for c in unmatched:
        name = (c['name'] or '').lower()
        if not name:
            groups.append((c['ip'], [c]))  # unnamed clients stay separate, by IP
            continue
        for key, members in groups:
            if key and _hostnames_match(name, key):
                members.append(c)
                break
        else:
            groups.append((name, [c]))

    for _, group in groups:
        ipv4 = [c for c in group if ':' not in c['ip']]
        primary = max(ipv4 or group, key=lambda c: c['total'])  # prefer a stable IPv4 over a rotating IPv6
        total   = sum(c['total'] for c in group)
        blocked = sum(c['blocked'] for c in group)
        other_ips = sorted({c['ip'] for c in group} - {primary['ip']})
        result.append({
            'mac': None, 'hostname': primary['name'], 'display_name': primary['name'] or primary['ip'],
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


def _capture_traffic(mac, duration=10):
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
            capture_output=True, timeout=20,
        )
        data = Path(out_path).read_bytes()
        if len(data) < 100:
            # An empty filtered result is the normal case when the device was idle for
            # the whole window — tshark's stderr here is just the expected mid-packet
            # truncation warning from us deliberately cutting the stream, not a real
            # error, so don't surface it as if it explains the failure.
            return None, 'no packets captured for this device — it may have been idle during this 10s window, try again'
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

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
        if path in ('/cross_ref', '/api/cross_ref') and not self._require_cross_ref_auth():
            return
        if path in ('/', '/index.html'):
            self._file('index.html', 'text/html; charset=utf-8')
        elif path in PROXIES:
            self._proxy(PROXIES[path])
        elif path == '/api/unifi':
            body = json.dumps(_unifi_status()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/unifi':
            self._file('unifi.html', 'text/html; charset=utf-8')
        elif path == '/unifi/map':
            self._file('unifi_map.html', 'text/html; charset=utf-8')
        elif path == '/vis-network.min.js':
            self._file('vis-network.min.js', 'application/javascript')
        elif path == '/api/unifi/graph':
            self._abs_file(UNIFI_DIR / 'topology.json', 'application/json')
        elif path == '/api/unifi/devices':
            self._abs_file(UNIFI_DIR / 'devices.json', 'application/json')
        elif path == '/unifi/devices':
            self._file('unifi_devices.html', 'text/html; charset=utf-8')
        elif path.startswith('/api/unifi/device'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            hostname = qs.get('hostname', [''])[0]
            self._json(_unifi_device_history(hostname))
        elif path == '/speedtest':
            self._file('speedtest.html', 'text/html; charset=utf-8')
        elif path == '/api/speedtest':
            self._json(_speedtest_history())
        elif path == '/cross_ref':
            self._file('cross_ref.html', 'text/html; charset=utf-8')
        elif path == '/api/cross_ref':
            self._json(_cross_ref())
        elif path == '/kismet':
            self._file('kismet.html', 'text/html; charset=utf-8')
        elif path == '/api/kismet':
            body = _kismet_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/pihole':
            self._file('pihole.html', 'text/html; charset=utf-8')
        elif path == '/api/pihole':
            self._abs_file(Path(__file__).parent / 'pihole.json', 'application/json')
        elif path == '/steve':
            self._file('steve.html', 'text/html; charset=utf-8')
        elif path == '/api/steve':
            self._abs_file(STEVE_JSON, 'application/json')
        elif path == '/api/steve/history':
            self._json(_steve_history())
        elif path == '/chart.min.js':
            self._file('chart.min.js', 'application/javascript')
        elif path == '/api/unifi/events':
            body = json.dumps(_unifi_events()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/unifi/events':
            self._file('unifi_events.html', 'text/html; charset=utf-8')
        elif path in ('/unifi/topology.png', '/unifi/dashboard.png'):
            self._abs_file(UNIFI_DIR / path.split('/')[-1], 'image/png')
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/capture' and not self._require_cross_ref_auth():
            return
        # These endpoints change state (trigger a speedtest, restart a service). Requiring
        # an application/json Content-Type means a cross-origin request needs a CORS
        # preflight, which we don't answer — so a malicious page can't trigger them via a
        # plain <form> (text/plain and form-urlencoded are CORS "simple" types that skip
        # preflight and would otherwise reach this handler with no browser pushback).
        if path in ('/api/speedtest/trigger', '/api/steve/restart', '/api/capture'):
            ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if ctype != 'application/json':
                self.send_error(400, 'Content-Type must be application/json')
                return
        if path == '/api/speedtest/trigger':
            self._json(_trigger_gateway_speedtest())
        elif path == '/api/steve/restart':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b'{}'
            try:
                name = json.loads(body).get('service', '')
            except Exception:
                name = ''
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
        elif path == '/api/capture':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b'{}'
            try:
                mac = json.loads(body).get('mac', '')
            except Exception:
                mac = ''
            result, err = _capture_traffic(mac)
            self._json({'ok': False, 'error': err} if err else {'ok': True, **result})
        else:
            self.send_error(404)

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

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
            with open(os.path.join(BASE, name), 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Home menu running on http://0.0.0.0:{PORT}')
    srv.serve_forever()
