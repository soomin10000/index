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


# ── external vantage (RIPE Atlas) ─────────────────────────────────────────
def _ext(isp_loss=0, ww_loss=0, mine_loss=0, inbound_pct=100, inbound_rounds=12):
    return {
        'ok': True, 'ts': int(time.time()), 'isp_asn': 201838,
        'targets': {'cf_v6': {
            'isp': {'probes': 5, 'loss_pct': isp_loss, 'worst_loss': isp_loss,
                    'rtt_p50': 4.0, 'series': [{'t': 1, 'l': isp_loss}]},
            'ww': {'probes': 6, 'loss_pct': ww_loss, 'worst_loss': ww_loss,
                   'rtt_p50': 5.0, 'series': [{'t': 1, 'l': ww_loss}]},
            'mine': {'loss_pct': mine_loss, 'rtt': 3.5},
        }},
        'inbound': {'probes': 6, 'rounds': inbound_rounds, 'reached_pct': inbound_pct,
                    'stuck_at': None, 'series': []},
        'trace_tail': {'cf_v6': 'probe 1 -> x'},
    }


def _house_fail(now, name='cf_v6'):
    return [{'t': now - 300, 'host': 'bazza', 'targets': [name], 'kind': 'v6',
             'scope': 'single-host', 'route_changed': False, 'gua_changed': False}]


def test_localise_your_line_when_isp_and_ww_clean():
    now = int(time.time())
    loc = v6health.localise(_house_fail(now), _ext(), now)
    assert loc['cf_v6']['verdict'] == 'your-line'


def test_localise_isp_network():
    now = int(time.time())
    loc = v6health.localise(_house_fail(now), _ext(isp_loss=40), now)
    assert loc['cf_v6']['verdict'] == 'isp-network'


def test_localise_resolver_or_internet_when_ww_lossy():
    now = int(time.time())
    loc = v6health.localise(_house_fail(now), _ext(isp_loss=40, ww_loss=35), now)
    assert loc['cf_v6']['verdict'] == 'resolver-or-internet'


def test_localise_needs_a_house_failure():
    now = int(time.time())
    assert v6health.localise([], _ext(ww_loss=80), now) == {}


def test_localise_unknown_without_external_target():
    now = int(time.time())
    ext = _ext()
    ext['targets'] = {}
    assert v6health.localise(_house_fail(now), ext, now)['cf_v6']['verdict'] == 'unknown'


def test_ping_tier_parse_splits_own_probe():
    now = int(time.time())
    rounds = [
        {'timestamp': now - 400, 'prb_id': 64460, 'sent': 3, 'rcvd': 3, 'avg': 3.2},
        {'timestamp': now - 400, 'prb_id': 111, 'sent': 3, 'rcvd': 1, 'avg': 9.0},
        {'timestamp': now - 300, 'prb_id': 222, 'sent': 3, 'rcvd': 3, 'avg': 4.0},
        {'timestamp': now - v6health.WINDOW_S - 50, 'prb_id': 333, 'sent': 3, 'rcvd': 0},
    ]
    t = v6health._atlas_ping_tier(rounds, now, 64460)
    assert t['mine']['loss_pct'] == 0.0
    assert t['probes'] == 2 and t['worst_loss'] > 50 and t['series']


def test_atlas_inbound_reached_and_stuck():
    now = int(time.time())
    gua = '2a02:6b67:d7e0:2500::1'
    rounds = [
        {'timestamp': now - 300, 'prb_id': 1,
         'result': [{'hop': 1, 'result': [{'from': 'fe80::a'}]},
                    {'hop': 2, 'result': [{'from': gua, 'rtt': 12}]}]},
        {'timestamp': now - 300, 'prb_id': 2,
         'result': [{'hop': 1, 'result': [{'from': '2a02:6b60::2a'}]},
                    {'hop': 2, 'result': [{'x': '*'}]}]},
    ]
    r = v6health._atlas_inbound(rounds, now, gua)
    assert r['rounds'] == 2 and r['reached_pct'] == 50 and r['stuck_at'] == '2a02:6b60::2a'


def test_alerts_inbound_unreachable_is_critical_once_seen_ok():
    now = int(time.time())
    alerts, _ = v6health.build_alerts(
        _summary_clean(now), [], {'steve': {'age_s': 60}}, {}, now,
        {'inbound_ever_ok': True}, external=_ext(inbound_pct=10))
    assert any(a['id'] == 'atlas_inbound_bazza' and a['level'] == 'critical' for a in alerts)


def test_alerts_inbound_cold_start_is_only_warn():
    now = int(time.time())
    alerts, _ = v6health.build_alerts(
        _summary_clean(now), [], {'steve': {'age_s': 60}}, {}, now, {},
        external=_ext(inbound_pct=10))
    ids = {a['id']: a['level'] for a in alerts}
    assert ids.get('atlas_inbound_cold') == 'warn'
    assert 'atlas_inbound_bazza' not in ids


def test_alerts_inbound_ok_no_alert():
    now = int(time.time())
    alerts, _ = v6health.build_alerts(
        _summary_clean(now), [], {'steve': {'age_s': 60}}, {}, now, {},
        external=_ext(inbound_pct=100))
    assert not any(a['id'] == 'atlas_inbound_bazza' for a in alerts)


def test_atlas_external_serves_fresh_cache(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(v6health, 'V6ATLAS_STATE', tmp_path / 'st.json')
    monkeypatch.setattr(v6health, 'V6ATLAS_CACHE', tmp_path / 'c.json')
    (tmp_path / 'st.json').write_text('{"measurements": {}}')
    (tmp_path / 'c.json').write_text(json.dumps({'ok': True, 'ts': now - 60, 'targets': {}}))
    got = v6health.atlas_external(now)
    assert got['ok'] and got['ts'] == now - 60


def test_atlas_external_not_bootstrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(v6health, 'V6ATLAS_STATE', tmp_path / 'nope.json')
    monkeypatch.setattr(v6health, 'V6ATLAS_CACHE', tmp_path / 'nope-c.json')
    got = v6health.atlas_external(int(time.time()))
    assert got['ok'] is False and 'reason' in got


def test_atlas_external_stale_cache_on_fetch_failure(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(v6health, 'V6ATLAS_STATE', tmp_path / 'st.json')
    monkeypatch.setattr(v6health, 'V6ATLAS_CACHE', tmp_path / 'c.json')
    (tmp_path / 'st.json').write_text(json.dumps(
        {'measurements': {'ping:cf_v6:isp': 1}, 'targets': {'cf_v6': '2606:4700:4700::1111'}}))
    (tmp_path / 'c.json').write_text(json.dumps({'ok': True, 'ts': now - 9999, 'targets': {}}))

    class Boom:
        def results_window(self, *a, **k):
            raise RuntimeError('network down')

    got = v6health.atlas_external(now, client=Boom())
    assert got['ok'] and got['stale'] and 'refresh failed' in got['reason']


def test_verdict_appends_atlas_localisation():
    now = int(time.time())
    events = [{'t': now - 200, 'host': 'bazza', 'targets': ['cf_v6'], 'kind': 'v6',
               'scope': 'single-host', 'route_changed': False, 'gua_changed': False}]
    summ = _summary_clean(now)
    summ['cf_v6']['fail_events'] = 2
    loc = {'cf_v6': {'verdict': 'isp-network', 'why': 'ISP peers lossy'}}
    assert 'isp-network' in v6health.verdict(summ, events, [], loc)
