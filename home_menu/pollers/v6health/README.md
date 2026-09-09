# v6health — IPv6 path monitor

Watches the IPv6 path from **bazza** and **steve** to the gateway, the three
public resolvers (Cloudflare / Google / Quad9) and a root server, with IPv4
controls alongside. Built 2026-09-09 after a spell of slow DNS was traced to
Pi-hole racing 12 public upstreams (v6 anycast legs timing out); the upstream
list was cut to unbound-only and this card exists to confirm the v6 path itself
stays clean — and to localise it (ISP / upstream / local) if it doesn't.

## Pieces

| file | runs on | cadence | does |
|---|---|---|---|
| `v6probe.sh` | bazza + steve (`~/v6mon/v6probe.sh`) | `*/5` cron | one sample → `~/v6mon/v6health.jsonl`; forensic block → `~/v6mon/v6health-detail.log` on any failure |
| `../v6health.py` | steve | `2-59/5` cron | reads steve's jsonl locally + bazza's over SSH (`id_rsa_bazza`), rolls up 24h, classifies failures, writes `data/v6health.json` (+ `data/v6health_state.json` for alert onsets) |
| `../../pages/v6health.html` | — | — | `/v6health` page: per-target 24h RTT/loss charts, stability tiles, failure table, forensic log tail |

Card `c-v6health` sits in the **Network** band on the index; `/api/v6health`
serves the JSON (`server.py`, stale after 25 min).

## Per-sample JSON (`v6health.jsonl`)

```
{ ts, host, iface, gw, gua, valid_lft, pref_lft,
  route6, route6_changed, gua_changed, ra_routers,
  snmp6:{in_dstunreach,out_timeexcd,in_err,reasm_fail,in_ra,out_ra},
  targets:{ <name>:{ ip, fam, loss, rtt, rtt_max, mdev,
                     dns_ok(0-3|null), dns_ms, big_ms, big_size } },
  fail:[names], fail_kind:""|"v6"|"v4"|"both" }
```

Targets: `gw_v6 cf_v6 goog_v6 quad9_v6 root_v6 gw_v4 cf_v4 goog_v4`
(`gw_*` are ICMP-only first-hop checks; the rest also get a `. NS` DNS probe and
the v6/v4 public resolvers a `. DNSKEY +dnssec` fragmentation canary).

## Reading it

- **v6 target lossy, v4 sibling clean at the same time** → IPv6-specific (path or
  config). **Both break together** → uplink / gateway.
- **`both-hosts` scope** → shared cause (ISP, gateway, or that resolver's anycast
  node). **`single-host`** → local to that box.
- **`route6_changed`** → the host switched v6 default gateway: rogue RA or the
  real gateway's RA lapsed. Check the `rdisc6` dump in the detail log.
- **`gua_changed`** → ISP rotated the delegated /64 (brief blackhole for live
  flows). Correlate with `valid_lft` hitting zero.
- **`in_dstunreach` climbing fast on bazza** → recursion (or a stray forwarder)
  hitting dead v6 destinations; baseline was <10/hr after the 09-09 fix.

## Deps

`iputils-ping`, `bind9-dnsutils` (`dig`) everywhere; `iputils-tracepath` +
`ndisc6` (`rdisc6`) on bazza for full forensics (steve already has `tracepath`).
No secrets — safe to run unprivileged.

## Cron

```
# bazza + steve  (probe appends its own JSONL; keep only stderr in cron)
*/5 * * * * /home/simon/v6mon/v6probe.sh >/dev/null 2>>/home/simon/v6mon/v6probe.err
# steve only
2-59/5 * * * * /home/simon/bin/with-secrets /usr/bin/python3 /home/simon/projects/home_menu/pollers/v6health.py >> /home/simon/projects/home_menu/logs/v6health.log 2>&1
```

`~/v6mon/` is outside the repo; deploy `v6probe.sh` there with
`scripts/deploy_v6probe.sh` (or scp by hand). The jsonl/detail files self-trim.
