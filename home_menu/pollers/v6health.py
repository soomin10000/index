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


# ── external vantage (RIPE Atlas) ───────────────────────────────────────────
# bazza + steve share one line / one /64, so "both-hosts" still can't split our
# line from our ISP from the resolver. These recurring Atlas measurements ping
# the same v6 resolvers from three tiers — our own probe (64460, on the LAN),
# other probes on our ISP's AS (Community Fibre, 201838), and worldwide — plus an inbound traceroute
# to bazza's GUA. Set up once with scripts/v6atlas_bootstrap.py; we only read.
V6ATLAS_STATE = DATA / 'v6atlas_state.json'
V6ATLAS_CACHE = DATA / 'v6atlas_cache.json'
ATLAS_REFRESH_S = 25 * 60          # don't hit the API more often than this
ATLAS_LOSS_BAD = 20               # tier worst-loss % that counts as "this tier sees it too"
ATLAS_INBOUND_WARN = 60          # min % of worldwide probes that must still reach bazza


def _atlas_client():
    try:
        import sys
        sys.path.insert(0, str(BASE / 'pollers'))
        from atlas_client import AtlasClient
        return AtlasClient()
    except (Exception, SystemExit):
        return None


def _atlas_bucket(now, ts):
    return now - ((now - ts) // BUCKET_S) * BUCKET_S


def _atlas_ping_tier(rounds, now, own_id):
    """Atlas ping rounds -> {probes, loss_pct, worst_loss, rtt_p50, series,
    mine:{loss_pct,rtt}|None}. `own_id` (when given) is pulled out as `mine`."""
    by_bucket, all_loss, rtts, seen = {}, [], [], set()
    mine = None
    for r in rounds:
        ts = r.get('timestamp')
        if not ts or ts < now - WINDOW_S:
            continue
        sent = r.get('sent') or r.get('packets') or 0
        if not sent:
            continue
        loss = (sent - r.get('rcvd', 0)) / sent * 100.0
        avg = r.get('avg')
        rtt = round(avg, 1) if isinstance(avg, (int, float)) and avg > 0 else None
        pid = r.get('prb_id')
        seen.add(pid)
        if own_id and pid == own_id:
            mine = {'loss_pct': round(loss, 1), 'rtt': rtt}
            continue
        all_loss.append(loss)
        if rtt is not None:
            rtts.append(rtt)
        by_bucket.setdefault(_atlas_bucket(now, ts), []).append(loss)
    rtts.sort()
    return {
        'probes': len(seen - ({own_id} if own_id else set())),
        'loss_pct': round(sum(all_loss) / len(all_loss), 1) if all_loss else 0.0,
        'worst_loss': round(max(all_loss), 1) if all_loss else 0.0,
        'rtt_p50': rtts[len(rtts) // 2] if rtts else None,
        'series': [{'t': b, 'l': round(sum(v) / len(v), 1)} for b, v in sorted(by_bucket.items())],
        'mine': mine,
    }


def _atlas_inbound(rounds, now, gua):
    """Inbound traceroute rounds -> did worldwide probes reach bazza's GUA?"""
    by_bucket, stuck = {}, {}
    reached = total = 0
    for r in rounds:
        ts = r.get('timestamp')
        if not ts or ts < now - WINDOW_S:
            continue
        got, last_from = False, None
        for hop in r.get('result') or []:
            for p in hop.get('result') or []:
                frm = p.get('from')
                if frm:
                    last_from = frm
                    if gua and frm == gua:
                        got = True
        total += 1
        reached += 1 if got else 0
        if last_from and not got:
            stuck[last_from] = stuck.get(last_from, 0) + 1
        d = by_bucket.setdefault(_atlas_bucket(now, ts), [0, 0])
        d[0] += 1
        d[1] += 1 if got else 0
    return {
        'probes': len({r.get('prb_id') for r in rounds}),
        'rounds': total,
        'reached_pct': round(reached / total * 100) if total else None,
        'stuck_at': max(stuck, key=stuck.get) if stuck else None,
        'series': [{'t': b, 'p': round(v[1] / v[0] * 100)} for b, v in sorted(by_bucket.items()) if v[0]],
    }


def _atlas_trace_text(latest):
    """Longest traceroute in the latest round, rendered for the forensic tail."""
    if not latest:
        return ''
    r = max(latest, key=lambda x: len(x.get('result') or []))
    out = [f"probe {r.get('prb_id')} -> {r.get('dst_addr')}  "
           f"{time.strftime('%H:%M', time.localtime(r.get('timestamp', 0)))}"]
    for hop in r.get('result') or []:
        parts = []
        for p in hop.get('result') or []:
            parts.append(f"{p['from']} {p.get('rtt', '?')}ms" if p.get('from') else '*')
        out.append(f"  {str(hop.get('hop', '?')):>2}  " + '  '.join(parts or ['*']))
    return '\n'.join(out[:36])


def _atlas_fetch(now, client):
    st = json.loads(V6ATLAS_STATE.read_text())
    msms = st.get('measurements', {})
    targets = st.get('targets', {})
    own = st.get('own_probe')
    start = now - WINDOW_S
    out = {'ok': True, 'ts': now, 'isp_asn': st.get('isp_asn'),
           'targets': {}, 'inbound': None, 'trace_tail': {}}

    for name in targets:
        tinfo = {}
        for tier in ('isp', 'ww'):
            mid = msms.get(f'ping:{name}:{tier}')
            if mid:
                tinfo[tier] = _atlas_ping_tier(
                    client.results_window(mid, start), now, own if tier == 'isp' else None)
        mine = (tinfo.get('isp') or {}).pop('mine', None)
        (tinfo.get('ww') or {}).pop('mine', None)
        if mine:
            tinfo['mine'] = mine
        out['targets'][name] = tinfo
        tmid = msms.get(f'trace:{name}:isp')
        if tmid:
            try:
                out['trace_tail'][name] = _atlas_trace_text(client.latest(tmid))
            except Exception:
                pass

    inmid = msms.get('in:bazza')
    if inmid:
        out['inbound'] = _atlas_inbound(
            client.results_window(inmid, start), now, st.get('bazza_gua'))
    return out


def atlas_external(now, client=None):
    """External-vantage rollup, cached to disk so the API is hit at most every
    ~25 min. Never raises: on any failure returns a stale cache (stale:true) or
    an {'ok': False, 'reason': ...} stub — the caller must not promote that to a
    top-level `error` key (would mark the card permanently stale)."""
    if not V6ATLAS_STATE.exists():
        return {'ok': False, 'reason': 'not set up — run scripts/v6atlas_bootstrap.py'}
    cached = None
    try:
        cached = json.loads(V6ATLAS_CACHE.read_text())
        if now - cached.get('ts', 0) < ATLAS_REFRESH_S:
            return cached
    except Exception:
        pass
    client = client or _atlas_client()
    if client is None:
        if cached:
            cached['stale'] = True
            return cached
        return {'ok': False, 'reason': 'RIPE Atlas client unavailable (key / requests)'}
    try:
        data = _atlas_fetch(now, client)
        V6ATLAS_CACHE.write_text(json.dumps(data))
        return data
    except Exception as e:
        if cached:
            cached['stale'] = True
            cached['reason'] = f'refresh failed: {e}'
            return cached
        return {'ok': False, 'reason': f'fetch failed: {e}'}


def localise(events, external, now):
    """For each v6 target with a house-side failure in the last hour, use the
    Atlas tiers to place it: your-line / isp-network / resolver-or-internet."""
    if not external or not external.get('ok'):
        return {}
    tiers = external.get('targets', {})
    recent = [t for e in events if e['t'] > now - 3600 for t in e.get('targets', [])]
    out = {}
    for name in set(recent):
        ext = tiers.get(name)
        if not ext or not (ext.get('isp') or ext.get('ww')):
            out[name] = {'verdict': 'unknown', 'why': 'no external measurement for this target'}
            continue
        isp_loss = (ext.get('isp') or {}).get('worst_loss', 0)
        ww_loss = (ext.get('ww') or {}).get('worst_loss', 0)
        mine_loss = (ext.get('mine') or {}).get('loss_pct') or 0
        if ww_loss >= ATLAS_LOSS_BAD:
            v, why = 'resolver-or-internet', \
                f'worldwide Atlas probes lose {ww_loss}% too — the resolver or the wider v6 internet, not us'
        elif isp_loss >= ATLAS_LOSS_BAD:
            v, why = 'isp-network', \
                f'Community Fibre peers (AS{external.get("isp_asn")}) lose {isp_loss}% while worldwide is clean — our ISP\'s v6 network'
        elif mine_loss >= ATLAS_LOSS_BAD:
            v, why = 'your-line', \
                'our own probe sees it but ISP peers are clean — our line / CPE'
        else:
            v, why = 'your-line', \
                'ISP peers and worldwide are both clean — contained to our LAN / CPE / the probe hosts'
        out[name] = {'verdict': v, 'why': why, 'isp_loss': isp_loss, 'ww_loss': ww_loss}
    return out


def build_alerts(summary, events, hoststat, snmp_rate, now, prev,
                 external=None, localisation=None):
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

    loc = localisation or {}
    for name in V6_TARGETS:
        s = summary[name]
        rec = [e for e in events if name in e['targets'] and e['t'] > now - 3600]
        shared = [e for e in rec if e['scope'] in ('both-hosts', 'uplink-wide')]
        atlas = f' · Atlas: {loc[name]["why"]}' if name in loc else ''
        if len(shared) >= 2:
            uplink = any(e['kind'] == 'both' for e in shared)
            cands[f'loss_{name}'] = (
                'critical' if uplink else 'warn', f'{s["label"]} unstable',
                f'{len(rec)} failed samples in the last hour · '
                + ('IPv4 affected too — uplink-wide' if uplink else 'IPv6 only') + atlas)
        elif s['fail_events'] >= FLAKY_FAIL_24H:
            cands[f'loss_{name}'] = (
                'warn', f'{s["label"]} flaky',
                f'{s["fail_events"]} failed samples in 24h (worst loss {s["worst_loss"]}%)' + atlas)

    inb = (external or {}).get('inbound') or {}
    if inb.get('reached_pct') is not None and inb.get('rounds', 0) >= 3 \
            and inb['reached_pct'] < ATLAS_INBOUND_WARN:
        stall = f'; stalling at {inb["stuck_at"]}' if inb.get('stuck_at') else ''
        if (prev or {}).get('inbound_ever_ok'):
            cands['atlas_inbound_bazza'] = (
                'critical', 'Home IPv6 prefix unreachable from outside',
                f"only {inb['reached_pct']}% of worldwide Atlas probes reached "
                f"bazza's GUA (was reachable before){stall}")
        else:
            cands['atlas_inbound_cold'] = (
                'warn', 'Inbound IPv6 to bazza not answering',
                f"{inb['reached_pct']}% of worldwide Atlas probes reach bazza's GUA — "
                f"likely the UCG-Max blocks inbound ICMPv6 to it{stall}")

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


def verdict(summary, events, alerts, localisation=None):
    crit = next((a for a in alerts if a['level'] == 'critical'), None)
    if crit:
        return f'{crit["header"]} — {crit["text"]}'
    v6fail = sum(summary[n]['fail_events'] for n in V6_TARGETS)
    if v6fail == 0:
        tot = sum(summary[n]['samples'] for n in V6_TARGETS)
        return f'IPv6 stable — no loss or DNS failure across {tot} v6 samples / 24h'
    picks = sorted({l['verdict'] for l in (localisation or {}).values()
                    if l.get('verdict') and l['verdict'] != 'unknown'})
    tail = f' · RIPE Atlas points at: {", ".join(picks)}' if picks else ''
    last = next((e for e in events if e['targets']), None)
    if last:
        names = ', '.join(TARGET_LABELS.get(t, t) for t in last['targets'])
        when = time.strftime('%H:%M', time.localtime(last['t']))
        return (f'{names} failed on {last["host"]} at {when} '
                f'({last["scope"]}, {last["kind"] or "v6"}) · {v6fail} bad v6 samples / 24h{tail}')
    return f'{v6fail} failed v6 samples / 24h{tail}'


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
    external = atlas_external(now)
    localisation = localise(events, external, now)
    alerts, active = build_alerts(summary, events, hoststat, rates, now, prev,
                                  external=external, localisation=localisation)

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
        'external': external,
        'localisation': localisation,
        'detail_tail': '\n'.join(detail[-90:]),
        'alerts': alerts,
        'verdict': verdict(summary, events, alerts, localisation),
    }
    if not ok:
        data['error'] = 'no probe samples from either host yet'
    OUT.write_text(json.dumps(data, indent=2))
    reached = ((external or {}).get('inbound') or {}).get('reached_pct')
    ever_ok = bool(prev.get('inbound_ever_ok') or (reached is not None and reached >= 80))
    STATE.write_text(json.dumps({'ts': now, 'active': active, 'inbound_ever_ok': ever_ok}))

    v6fail = sum(summary[n]['fail_events'] for n in V6_TARGETS)
    ax = 'ok' if external.get('ok') else external.get('reason', 'off')
    if external.get('stale'):
        ax += '/stale'
    loc_s = ','.join('{}:{}'.format(k, v['verdict']) for k, v in localisation.items()) or '-'
    print(f"{time.strftime('%F %T')} ok={ok} "
          f"steve={hoststat['steve']['samples_24h']} bazza={hoststat['bazza']['samples_24h']} "
          f"v6_fail_24h={v6fail} alerts={len(alerts)} atlas={ax} localise={loc_s}")


if __name__ == '__main__':
    main()
