#!/usr/bin/env python3
"""Home services menu — serves index.html and proxies service APIs on port 8080."""
import json
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(__file__)
PORT = 8080

PROXIES = {
    '/api/harold':  'http://localhost:5000/api/status',
    '/api/train':   'http://localhost:8192/api/departures',
    '/api/parents': 'http://localhost:8092/api/status',
    '/api/darren':  'http://localhost:8193/api/status',
    '/api/weather': 'http://localhost:8186/api/weather',
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._file('index.html', 'text/html; charset=utf-8')
        elif self.path in PROXIES:
            self._proxy(PROXIES[self.path])
        else:
            self.send_error(404)

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
