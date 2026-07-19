#!/usr/bin/env python3
"""Scrape the SmokePing target tree on the Plex box into data/smokeping.json.

SmokePing 2.9's CGI exposes no JSON API — the sidebar menu HTML is the only
machine-readable list of targets, so this parses it. Graph PNGs are proxied
live by server.py; this poller only maintains the tree + reachability, so the
/smokeping page and index card still render (with a down flag and the last
known tree) when the Plex box is off.
"""
import json
import re
import time
import urllib.request
from pathlib import Path

BASE_URL = 'http://192.168.1.212:8888/smokeping/smokeping.cgi'
OUT = Path(__file__).parent.parent / 'data' / 'smokeping.json'

MENU_RE = re.compile(r'href="\?target=([A-Za-z0-9._-]+)"[^>]*>([^<]+)</a>', re.I)


def fetch(target=''):
    url = BASE_URL + (f'?target={target}' if target else '')
    req = urllib.request.Request(url, headers={'User-Agent': 'home-menu/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode('utf-8', 'replace')


def menu_items(html):
    """(id, label) pairs from the sidebar menu, deduped in order.

    Skips pseudo-targets like _charts — they are CGI views, not probe targets.
    """
    seen, items = set(), []
    for tid, label in MENU_RE.findall(html):
        if tid.startswith('_') or tid in seen:
            continue
        seen.add(tid)
        items.append((tid, label.strip()))
    return items


def main():
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        prev = {}
    try:
        top = [(t, n) for t, n in menu_items(fetch()) if '.' not in t]
        sections = []
        for sid, sname in top:
            # A section's own page lists the top-level menu again plus its
            # children — the prefix filter keeps only the children.
            kids = [{'id': t, 'name': n} for t, n in menu_items(fetch(sid))
                    if t.startswith(sid + '.')]
            sections.append({'id': sid, 'name': sname, 'targets': kids})
        data = {
            'ts': time.time(), 'ok': True, 'error': '',
            'base': BASE_URL,
            'sections': sections,
            'target_count': sum(len(s['targets']) for s in sections),
        }
    except Exception as e:
        data = {
            'ts': time.time(), 'ok': False, 'error': str(e),
            'base': BASE_URL,
            'sections': prev.get('sections', []),
            'target_count': prev.get('target_count', 0),
        }
    OUT.write_text(json.dumps(data, indent=2))
    print(f"{time.strftime('%F %T')} ok={data['ok']} "
          f"targets={data['target_count']} {data['error']}")


if __name__ == '__main__':
    main()
