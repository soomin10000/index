"""Unit tests for pollers/v6health.py — the bazza+steve IPv6 path rollup."""
import json
import time

import v6health


def _run(ts, host, targets, **kw):
    base = {'ts': ts, 'host': host, 'iface': 'eth0', 'gw': 'fe80::1',
            'gua': '2a02:6b67:d7e0:2500::abcd', 'valid_lft': 86000, 'pref_lft': 86000,
            'route6': 'fe80::1', 'route6_changed': False, 'gua_changed': False,
            'ra_routers': 4,
            'snmp6': {'in_dstunreach': 100, 'out_timeexcd': 0, 'in_err': 0,
                      'reasm_fail': 0, 'in_ra': 0, 'out_ra': 0},
            'targets': targets, 'fail': [], 'fail_kind': ''}
    base.update(kw)
    return base


def _tgt(loss=0, rtt=3.0, dns_ok=3, big_ms=5):
    return {'ip': '2606:4700:4700::1111', 'fam': 6, 'loss': loss, 'rtt': rtt,
            'rtt_max': rtt + 1, 'mdev': 0.2, 'dns_ok': dns_ok, 'dns_ms': 4,
            'big_ms': big_ms, 'big_size': 1139}


# ── parse_lines ─────────────────────────────────────────────────────────────
def test_parse_lines_orders_and_drops_junk():
    text = '\n'.join([
        json.dumps(_run(200, 'steve', {'cf_v6': _tgt()})),
        'not json',
        '{}',
        json.dumps(_run(100, 'steve', {'cf_v6': _tgt()})),
        '',
    ])
    runs = v6health.parse_lines(text)
    assert [r['ts'] for r in runs] == [100, 200]


# ── sample_fail ────────────────────────────────────────────────────────────
def test_sample_fail_on_loss_and_dns():
    assert v6health.sample_fail(_run(1, 's', {'cf_v6': _tgt(loss=10)}), 'cf_v6')
    assert v6health.sample_fail(_run(1, 's', {'cf_v6': _tgt(dns_ok=1)}), 'cf_v6')
    assert not v6health.sample_fail(_run(1, 's', {'cf_v6': _tgt()}), 'cf_v6')
    assert not v6health.sample_fail(_run(1, 's', {'cf_v6': _tgt(dns_ok=None)}), 'cf_v6')


# ── summarize ──────────────────────────────────────────────────────────────
def test_summarize_counts_and_buckets():
    now = int(time.time())
    runs = [
        _run(now - 1800, 'steve', {'cf_v6': _tgt(rtt=3)}),
        _run(now - 1200, 'steve', {'cf_v6': _tgt(loss=20, rtt=9)}),
        _run(now - 600, 'bazza', {'cf_v6': _tgt(rtt=4)}),
    ]
    s = v6health.summarize(runs, now)['cf_v6']
    assert s['samples'] == 3
    assert s['loss_events'] == 1 and s['fail_events'] == 1
    assert s['worst_loss'] == 20 and s['worst_rtt'] == 9
    assert s['series'] and all('h' in p and 't' in p for p in s['series'])
    # a stale sample (older than the window) is excluded
    old = v6health.summarize([_run(now - v6health.WINDOW_S - 10, 'steve', {'cf_v6': _tgt()})], now)
    assert old['cf_v6']['samples'] == 0


def test_summarize_big_fail_counted():
    now = int(time.time())
    runs = [_run(now - 300, 'steve', {'cf_v6': _tgt(big_ms=None)})]
    assert v6health.summarize(runs, now)['cf_v6']['big_fail_events'] == 1


# ── failure_events / classification ────────────────────────────────────────
def test_failure_events_scope():
    now = int(time.time())
    rbh = {
        'bazza': [_run(now - 300, 'bazza', {'goog_v6': _tgt(loss=5)},
                       fail=['goog_v6'], fail_kind='v6')],
        'steve': [_run(now - 360, 'steve', {'goog_v6': _tgt(loss=5)},
                       fail=['goog_v6'], fail_kind='v6')],
    }
    ev = v6health.failure_events(rbh, now)
    assert ev and all(e['scope'] == 'both-hosts' for e in ev)

    rbh2 = {
        'bazza': [_run(now - 300, 'bazza', {'goog_v6': _tgt(loss=5)},
                       fail=['goog_v6'], fail_kind='both')],
        'steve': [_run(now - 300, 'steve', {'goog_v6': _tgt()})],
    }
    assert v6health.failure_events(rbh2, now)[0]['scope'] == 'uplink-wide'

    rbh3 = {
        'bazza': [_run(now - 300, 'bazza', {'goog_v6': _tgt(loss=5)},
                       fail=['goog_v6'], fail_kind='v6')],
        'steve': [_run(now - 300, 'steve', {'goog_v6': _tgt()})],
    }
    assert v6health.failure_events(rbh3, now)[0]['scope'] == 'single-host'


def test_failure_events_route_flip_recorded_without_target_fail():
    now = int(time.time())
    rbh = {'steve': [_run(now - 120, 'steve', {'cf_v6': _tgt()}, route6_changed=True)]}
    ev = v6health.failure_events(rbh, now)
    assert ev and ev[0]['route_changed'] and ev[0]['targets'] == []


# ── snmp_rates ────────────────────────────────────────────────────────────
def test_snmp_rates_per_hour():
    now = int(time.time())
    runs = [
        _run(now - 3600, 'bazza', {'cf_v6': _tgt()},
             snmp6={'in_dstunreach': 1000, 'out_timeexcd': 0, 'in_err': 0,
                    'reasm_fail': 0, 'in_ra': 0, 'out_ra': 0}),
        _run(now, 'bazza', {'cf_v6': _tgt()},
             snmp6={'in_dstunreach': 1700, 'out_timeexcd': 0, 'in_err': 0,
                    'reasm_fail': 0, 'in_ra': 0, 'out_ra': 0}),
    ]
    assert v6health.snmp_rates({'bazza': runs}, now)['bazza'] == 700.0
    # counter reset -> None, not a negative rate
    reset = [
        _run(now - 3600, 'bazza', {'cf_v6': _tgt()},
             snmp6={'in_dstunreach': 5000, 'out_timeexcd': 0, 'in_err': 0,
                    'reasm_fail': 0, 'in_ra': 0, 'out_ra': 0}),
        _run(now, 'bazza', {'cf_v6': _tgt()},
             snmp6={'in_dstunreach': 12, 'out_timeexcd': 0, 'in_err': 0,
                    'reasm_fail': 0, 'in_ra': 0, 'out_ra': 0}),
    ]
    assert v6health.snmp_rates({'bazza': reset}, now)['bazza'] is None


# ── build_alerts ──────────────────────────────────────────────────────────
def _summary_clean(now):
    return v6health.summarize([_run(now - 300, 'steve', {k: _tgt() for k in v6health.TARGET_LABELS})], now)


def test_alerts_probe_silent_and_onset_carry():
    now = int(time.time())
    summ = _summary_clean(now)
    hoststat = {'steve': {'age_s': 60}, 'bazza': {'age_s': 4000}}
    a1, active1 = v6health.build_alerts(summ, [], hoststat, {}, now, {})
    keys = {a['id'] for a in a1}
    assert 'probe_silent_bazza' in keys and 'probe_silent_steve' not in keys
    # onset is carried from prior state on the next run
    later = now + 300
    a2, _ = v6health.build_alerts(summ, [], hoststat, {}, later, {'active': active1})
    assert next(a for a in a2 if a['id'] == 'probe_silent_bazza')['ts'] == \
           next(a for a in a1 if a['id'] == 'probe_silent_bazza')['ts']


def test_alerts_route_flip_is_critical():
    now = int(time.time())
    events = [{'t': now - 120, 'host': 'steve', 'targets': [], 'kind': '',
              'scope': 'single-host', 'route_changed': True, 'gua_changed': False}]
    alerts, _ = v6health.build_alerts(_summary_clean(now), events, {'steve': {'age_s': 60}}, {}, now, {})
    assert any(a['id'] == 'route_flip' and a['level'] == 'critical' for a in alerts)


def test_alerts_uplink_wide_loss_critical():
    now = int(time.time())
    summ = _summary_clean(now)
    events = [
        {'t': now - 200, 'host': 'bazza', 'targets': ['cf_v6'], 'kind': 'both',
         'scope': 'uplink-wide', 'route_changed': False, 'gua_changed': False},
        {'t': now - 400, 'host': 'steve', 'targets': ['cf_v6'], 'kind': 'both',
         'scope': 'uplink-wide', 'route_changed': False, 'gua_changed': False},
    ]
    alerts, _ = v6health.build_alerts(summ, events, {'steve': {'age_s': 60}}, {}, now, {})
    assert any(a['id'] == 'loss_cf_v6' and a['level'] == 'critical' for a in alerts)


def test_verdict_clean():
    now = int(time.time())
    assert 'stable' in v6health.verdict(_summary_clean(now), [], [])
