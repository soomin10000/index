#!/usr/bin/env python3
"""Home services menu — serves index.html and proxies service APIs on port 8080."""
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
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


PIHOLE_JSON = Path(__file__).parent / 'pihole.json'
DEVICES_JSON = UNIFI_DIR / 'devices.json'


def _cross_ref():
    """Join Pi-hole clients_detail with UniFi devices.json."""
    try:
        ph = json.loads(PIHOLE_JSON.read_text()) if PIHOLE_JSON.exists() else {}
        ud = json.loads(DEVICES_JSON.read_text()) if DEVICES_JSON.exists() else {}
    except Exception as e:
        return {'error': str(e), 'devices': []}

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
        hostname = (dev.get('hostname') or dev.get('display_name') or '').lower()

        ph_c = by_ip.get(ip)
        if ph_c is None and hostname:
            matches = by_name.get(hostname, [])
            # Only trust hostname match when it's unique (not "iphone" matching 6 entries)
            if len(matches) == 1:
                ph_c = matches[0]

        result.append({
            **dev,
            'dns_total':   ph_c['total']       if ph_c else None,
            'dns_blocked': ph_c['blocked']      if ph_c else None,
            'dns_pct':     ph_c['blocked_pct']  if ph_c else None,
            'dns_ip':      ph_c['ip']            if ph_c else None,
        })

    # Also include Pi-hole-only clients (not in UniFi — wired/unknown)
    unifi_ips   = {d.get('ip') for d in unifi_devices}
    unifi_names = {(d.get('hostname') or '').lower() for d in unifi_devices}
    for c in ph_clients:
        if c['ip'] not in unifi_ips and (c['name'] or '').lower() not in unifi_names:
            result.append({
                'mac': None, 'hostname': c['name'], 'display_name': c['name'] or c['ip'],
                'ip': c['ip'], 'vendor': '', 'ssid': '', 'ap': '', 'signal': None,
                'retry_pct': None, 'is_wired': True, 'flagged': False,
                'first_seen': None, 'last_seen': None, 'guessed': False,
                'dns_total': c['total'], 'dns_blocked': c['blocked'],
                'dns_pct': c['blocked_pct'], 'dns_ip': c['ip'],
            })

    result.sort(key=lambda d: -(d['dns_total'] or 0))
    return {
        'ts':      ph.get('ts'),
        'summary': ph.get('summary', {}),
        'history': ph.get('history', []),
        'devices': result,
    }


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

    def do_GET(self):
        path = self.path.split('?')[0]
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
        elif path == '/pihole':
            self._file('pihole.html', 'text/html; charset=utf-8')
        elif path == '/api/pihole':
            self._abs_file(Path(__file__).parent / 'pihole.json', 'application/json')
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
        if path == '/api/speedtest/trigger':
            self._json(_trigger_gateway_speedtest())
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
    srv = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Home menu running on http://0.0.0.0:{PORT}')
    srv.serve_forever()
