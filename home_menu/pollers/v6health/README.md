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
| `../v6health.py` | steve | `2-59/5` cron | reads steve's jsonl locally + bazza's over SSH (`id_rsa_bazza`), rolls up 24h, classifies failures, folds in the RIPE Atlas external vantage, writes `data/v6health.json` (+ `data/v6health_state.json` for alert onsets) |
| `../../scripts/v6atlas_bootstrap.py` | steve (by hand) | once | creates the recurring Atlas ping/traceroute measurements, records ids in `data/v6atlas_state.json`. `--dry-run` to preview. Re-runnable. |
| `../../pages/v6health.html` | — | — | `/v6health` page: per-target 24h RTT/loss charts, stability tiles, **external-vantage (Atlas) tiers + localisation**, failure table, forensic log tail |

## External vantage — RIPE Atlas

bazza and steve share one UCG-Max, one Community Fibre line and one delegated `/64`, so
`both-hosts` still can't split *our line* from *our ISP* from *the resolver*.
`v6atlas_bootstrap.py` creates seven recurring measurements (probe 64460 earns
the credits; ids in `data/v6atlas_state.json`, never delete it):

| desc | type | target | probes | interval |
|---|---|---|---|---|
| `v6health:ping:{cf,quad9}_v6:isp` | ping | resolver v6 | 64460 + 5× AS201838 | 30 min |
| `v6health:ping:{cf,quad9}_v6:ww` | ping | resolver v6 | 6× worldwide | 30 min |
| `v6health:trace:{cf,quad9}_v6:isp` | traceroute | resolver v6 | 5× AS201838 | 2 h |
| `v6health:in:bazza` | traceroute | bazza GUA | 6× worldwide | 2 h |

`v6health.py` reads the results (free), caches them in `data/v6atlas_cache.json`
(refreshed at most every 25 min; a fetch failure serves the stale cache, never a
top-level `error`), and for any target with a house-side failure in the last hour
emits a **localisation**:

- ISP peers **and** worldwide clean → `your-line` (our LAN / CPE)
- ISP peers lossy, worldwide clean → `isp-network` (Community Fibre's v6)
- worldwide lossy too → `resolver-or-internet`

`in:bazza` reaching < 60 % of worldwide probes raises a **critical**
`atlas_inbound_bazza` (our prefix is unreachable from outside — renumber, ISP
blackhole, or CPE firewall; the last responding hop is in the alert).

~25 k credits/day (balance was 151 M, income ~59 k/day). To stop it: delete the
seven `v6health:*` measurements on atlas.ripe.net and remove
`data/v6atlas_state.json`. bazza's GUA (`2a02:6b67:d7e0:2500:8aa2:9eff:fe76:6568`,
EUI-64) is hardcoded in the bootstrap — re-run it if the ISP renumbers the /48.

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
