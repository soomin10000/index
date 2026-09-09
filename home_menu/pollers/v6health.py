#!/usr/bin/env python3
"""Roll the IPv6 path-health samples from bazza + steve into data/v6health.json
for the /v6health card and page.

Each host runs pollers/v6health/v6probe.sh every 5 min (cron), appending a JSON
line to ~/v6mon/v6health.jsonl and a forensic block to ~/v6mon/v6health-detail.log
on any loss / DNS timeout / default-route flip / prefix renumber. This poller
(cron on steve, offset +2 min) reads steve's file locally and bazza's over SSH,
computes per-target 24h rollups + a 15-min bucketed series, classifies each v6
failure (IPv6-only vs uplink-wide, one host vs both), raises alerts with carried
onsets, and writes the JSON the dashboard reads.

The 12-public-upstream Pi-hole config was cut to unbound-only on 2026-09-09; this
card exists to confirm the v6 path itself is clean and to catch it if it isn't.
"""
import json
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / 'data'
OUT = DATA / 'v6health.json'
STATE = DATA / 'v6health_state.json'

LOCAL_JSONL = Path.home() / 'v6mon' / 'v6health.jsonl'
LOCAL_DETAIL = Path.home() / 'v6mon' / 'v6health-detail.log'
BAZZA_SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
             '-i', str(Path.home() / '.ssh' / 'id_rsa_bazza'), 'simon@192.168.1.246']
BAZZA_JSONL = '/home/simon/v6mon/v6health.jsonl'
BAZZA_DETAIL = '/home/simon/v6mon/v6health-detail.log'

TARGET_LABELS = {
    'gw_v6': 'Gateway v6', 'cf_v6': 'Cloudflare v6', 'goog_v6': 'Google v6',
    'quad9_v6': 'Quad9 v6', 'root_v6': 'k-root v6',
    'gw_v4': 'Gateway v4', 'cf_v4': 'Cloudflare v4', 'goog_v4': 'Google v4',
}
V6_TARGETS = [k for k in TARGET_LABELS if k.endswith('_v6')]
BIG_TARGETS = {'cf_v6', 'goog_v6', 'quad9_v6', 'root_v6'}

WINDOW_S = 24 * 3600
BUCKET_S = 900
STALE_RUN_S = 20 * 60          # a host's probe silent longer than this -> warn
DU_RATE_WARN = 600            # Icmp6InDestUnreachs per hour (bazza baseline was <10/hr post-fix)
FLAKY_FAIL_24H = 6            # failed samples for a single target over 24h -> warn


# ── ingest ──────────────────────────────────────────────────────────────────
def _read_local(path):
    try:
        return path.read_text()
    except Exception:
        return ''


def _read_remote(argv, remote_path, n=1400):
    try:
        r = subprocess.run(argv + [f'tail -n {n} {remote_path}'],
                           capture_output=True, text=True, timeout=25)
        return r.stdout if r.returncode == 0 else ''
    except Exception:
        return ''


def parse_lines(text):
    """JSONL -> list of run dicts, oldest first, junk lines dropped."""
    runs = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if isinstance(o, dict) and isinstance(o.get('ts'), (int, float)) and 'targets' in o:
            runs.append(o)
    runs.sort(key=lambda r: r['ts'])
    return runs


# ── rollup ──────────────────────────────────────────────────────────────────
def sample_fail(run, name):
    """Did target `name` fail in this run? (ICMP loss, or resolver <3/3 DNS ok)"""
    t = (run.get('targets') or {}).get(name)
    if not t:
        return False
    if (t.get('loss') or 0) > 0:
        return True
    dok = t.get('dns_ok')
    return dok is not None and dok < 3


def _bucket(points, now):
    """15-min buckets over the window, keyed (bucket, host): worst loss, mean rtt."""
    nb = WINDOW_S // BUCKET_S
    start = now - WINDOW_S
    acc = {}
    for p in points:
        i = int((p['t'] - start) // BUCKET_S)
        if i < 0 or i >= nb:
            continue
        key = (i, p['h'])
        cur = acc.get(key)
        if cur is None:
            acc[key] = {'t': start + i * BUCKET_S, 'h': p['h'], 'l': p['l'],
                        'r': ([p['r']] if p['r'] is not None else [])}
        else:
            cur['l'] = max(cur['l'], p['l'])
            if p['r'] is not None:
                cur['r'].append(p['r'])
    out = [{'t': v['t'], 'h': v['h'], 'l': v['l'],
            'r': round(sum(v['r']) / len(v['r']), 1) if v['r'] else None}
           for v in acc.values()]
    out.sort(key=lambda x: (x['t'], x['h']))
    return out


def summarize(runs, now):
    """Per-target 24h rollup + bucketed series (both hosts merged, host in each point)."""
    win = [r for r in runs if r['ts'] >= now - WINDOW_S]
    out = {}
    for name, label in TARGET_LABELS.items():
        pts, rtts = [], []
        seen = loss_n = fail_n = big_fail_n = 0
        worst_loss = worst_rtt = 0.0
        for r in win:
            t = (r.get('targets') or {}).get(name)
            if not t:
                continue
            seen += 1
            loss = t.get('loss') or 0
            rtt = t.get('rtt')
            if loss > 0:
                loss_n += 1
            worst_loss = max(worst_loss, loss)
            if isinstance(rtt, (int, float)):
                rtts.append(rtt)
                worst_rtt = max(worst_rtt, rtt)
            if sample_fail(r, name):
                fail_n += 1
            if name in BIG_TARGETS and t.get('dns_ok') not in (None, 0) and t.get('big_ms') is None:
                big_fail_n += 1
            pts.append({'t': r['ts'], 'h': (r.get('host') or '?')[:1], 'l': loss,
                        'r': round(rtt, 1) if isinstance(rtt, (int, float)) else None})
        out[name] = {
            'label': label, 'fam': 6 if name.endswith('_v6') else 4,
            'samples': seen, 'loss_events': loss_n, 'fail_events': fail_n,
            'big_fail_events': big_fail_n,
            'worst_loss': round(worst_loss, 1), 'worst_rtt': round(worst_rtt, 1),
            'avg_rtt': round(sum(rtts) / len(rtts), 1) if rtts else None,
            'series': _bucket(pts, now),
        }
    return out


def _nearest(runs, ts, tol):
    best = None
    for r in runs:
        d = abs(r['ts'] - ts)
        if d <= tol and (best is None or d < abs(best['ts'] - ts)):
            best = r
    return best


def failure_events(runs_by_host, now):
    """Recent per-run v6 failures, each tagged with scope:
    uplink-wide (v4 failed too) / both-hosts / single-host."""
    hosts = list(runs_by_host)
    ev = []
    for host, runs in runs_by_host.items():
        others = [h for h in hosts if h != host]
        for r in runs:
            if r['ts'] < now - WINDOW_S:
                continue
            v6f = [f for f in (r.get('fail') or []) if f.endswith('_v6')]
            route_ch = bool(r.get('route6_changed'))
            gua_ch = bool(r.get('gua_changed'))
            if not v6f and not route_ch and not gua_ch:
                continue
            both = any(
                f in ((_nearest(runs_by_host[oh], r['ts'], 180) or {}).get('fail') or [])
                for f in v6f for oh in others
            )
            ev.append({
                't': r['ts'], 'host': host, 'targets': v6f,
                'kind': r.get('fail_kind') or ('v6' if v6f else ''),
                'scope': ('uplink-wide' if r.get('fail_kind') == 'both'
                          else 'both-hosts' if both else 'single-host'),
                'route_changed': route_ch, 'gua_changed': gua_ch,
            })
    ev.sort(key=lambda e: e['t'], reverse=True)
    return ev[:40]


def snmp_rates(runs_by_host, now):
    """Icmp6InDestUnreachs per hour per host, first vs last run in the window."""
    rates = {}
    for host, runs in runs_by_host.items():
        win = [r for r in runs if r['ts'] >= now - WINDOW_S and r.get('snmp6')]
        if len(win) < 2:
            rates[host] = None
            continue
        a, b = win[0], win[-1]
        dv = (b['snmp6'].get('in_dstunreach', 0)) - (a['snmp6'].get('in_dstunreach', 0))
        dt = b['ts'] - a['ts']
        rates[host] = round(dv / dt * 3600, 1) if dt > 0 and dv >= 0 else None
    return rates


def build_alerts(summary, events, hoststat, snmp_rate, now, prev):
    prev_active = (prev or {}).get('active', {})
    cands = {}

    for h, st in hoststat.items():
        age = st.get('age_s')
        if age is None or age > STALE_RUN_S:
            cands[f'probe_silent_{h}'] = (
                'warn', 'v6 probe silent',
                f'no sample from {h}' + (f' for {int(age / 60)} min' if age else ''))

    flip = next((e for e in events if e['route_changed'] and e['t'] > now - 3600), None)
    if flip:
        cands['route_flip'] = (
            'critical', 'IPv6 default route changed',
            f'{flip["host"]} switched v6 gateway at '
            + time.strftime('%H:%M', time.localtime(flip['t'])))

    renum = next((e for e in events if e['gua_changed']), None)
    if renum:
        cands['prefix_change'] = (
            'warn', 'IPv6 prefix renumbered',
            f'global address changed on {renum["host"]} in the last 24h')

    for name in V6_TARGETS:
        s = summary[name]
        rec = [e for e in events if name in e['targets'] and e['t'] > now - 3600]
        shared = [e for e in rec if e['scope'] in ('both-hosts', 'uplink-wide')]
        if len(shared) >= 2:
            uplink = any(e['kind'] == 'both' for e in shared)
            cands[f'loss_{name}'] = (
                'critical' if uplink else 'warn', f'{s["label"]} unstable',
                f'{len(rec)} failed samples in the last hour · '
                + ('IPv4 affected too — uplink-wide' if uplink else 'IPv6 only'))
        elif s['fail_events'] >= FLAKY_FAIL_24H:
            cands[f'loss_{name}'] = (
                'warn', f'{s["label"]} flaky',
                f'{s["fail_events"]} failed samples in 24h (worst loss {s["worst_loss"]}%)')

    for h, rate in snmp_rate.items():
        if rate and rate > DU_RATE_WARN:
            cands[f'du_{h}'] = (
                'warn', 'Elevated ICMPv6 unreachables',
                f'{h}: ~{int(rate)}/hr Icmp6InDestUnreachs (was <10/hr after the 09-09 fix)')

    active, alerts = {}, []
    for k, (lvl, hd, tx) in cands.items():
        onset = prev_active.get(k, {}).get('ts', now)
        active[k] = {'ts': onset, 'level': lvl, 'header': hd, 'text': tx}
        alerts.append({'id': k, 'ts': onset, 'level': lvl, 'header': hd, 'text': tx})
    alerts.sort(key=lambda a: a['ts'], reverse=True)
    return alerts, active


def verdict(summary, events, alerts):
    crit = next((a for a in alerts if a['level'] == 'critical'), None)
    if crit:
        return f'{crit["header"]} — {crit["text"]}'
    v6fail = sum(summary[n]['fail_events'] for n in V6_TARGETS)
    if v6fail == 0:
        tot = sum(summary[n]['samples'] for n in V6_TARGETS)
        return f'IPv6 stable — no loss or DNS failure across {tot} v6 samples / 24h'
    last = next((e for e in events if e['targets']), None)
    if last:
        names = ', '.join(TARGET_LABELS.get(t, t) for t in last['targets'])
        when = time.strftime('%H:%M', time.localtime(last['t']))
        return (f'{names} failed on {last["host"]} at {when} '
                f'({last["scope"]}, {last["kind"] or "v6"}) · {v6fail} bad v6 samples / 24h')
    return f'{v6fail} failed v6 samples / 24h'


# ── main ────────────────────────────────────────────────────────────────────
def main():
    now = int(time.time())
    try:
        prev = json.loads(STATE.read_text())
    except Exception:
        prev = {}

    texts = {
        'steve': _read_local(LOCAL_JSONL),
        'bazza': _read_remote(BAZZA_SSH, BAZZA_JSONL),
    }
    runs_by_host = {h: parse_lines(t) for h, t in texts.items()}
    all_runs = sorted((r for rs in runs_by_host.values() for r in rs), key=lambda r: r['ts'])

    hoststat = {}
    for h, runs in runs_by_host.items():
        last = runs[-1] if runs else None
        hoststat[h] = {
            'last_run': last['ts'] if last else None,
            'age_s': (now - last['ts']) if last else None,
            'samples_24h': len([r for r in runs if r['ts'] >= now - WINDOW_S]),
            'gua': last.get('gua') if last else None,
            'valid_lft': last.get('valid_lft') if last else None,
            'route6': last.get('route6') if last else None,
            'ra_routers': last.get('ra_routers') if last else None,
        }

    summary = summarize(all_runs, now)
    events = failure_events(runs_by_host, now)
    rates = snmp_rates(runs_by_host, now)
    alerts, active = build_alerts(summary, events, hoststat, rates, now, prev)

    detail = (_read_local(LOCAL_DETAIL).splitlines()[-60:]
              + _read_remote(BAZZA_SSH, BAZZA_DETAIL, n=60).splitlines()[-60:])

    ok = bool(all_runs)
    data = {
        'ts': now, 'ok': ok,
        'window_h': WINDOW_S // 3600,
        'hosts': hoststat,
        'targets': summary,
        'snmp6_rate_per_hr': rates,
        'events': events,
        'detail_tail': '\n'.join(detail[-90:]),
        'alerts': alerts,
        'verdict': verdict(summary, events, alerts),
    }
    if not ok:
        data['error'] = 'no probe samples from either host yet'
    OUT.write_text(json.dumps(data, indent=2))
    STATE.write_text(json.dumps({'ts': now, 'active': active}))

    v6fail = sum(summary[n]['fail_events'] for n in V6_TARGETS)
    print(f"{time.strftime('%F %T')} ok={ok} "
          f"steve={hoststat['steve']['samples_24h']} bazza={hoststat['bazza']['samples_24h']} "
          f"v6_fail_24h={v6fail} alerts={len(alerts)}")


if __name__ == '__main__':
    main()
