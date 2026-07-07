"""RIPE Atlas v2 API client for the DNS cross-check card.

Key from RIPE_ATLAS_KEY (crontab env — never a tracked file, repo is public).
Probe id from RIPE_ATLAS_PROBE_ID, or auto-discovered from the account.
"""

import base64
import os
import struct
import requests

BASE = "https://atlas.ripe.net/api/v2"
DESC_PREFIX = "home-dns-xcheck:"

# Curated canaries — fixed so the recurring measurements persist. Two groups:
# integrity domains should agree everywhere; block domains verified nulled by
# Pi-hole (0.0.0.0) on 2026-07-07. tiktok.com deliberately absent: its apex
# resolves normally (the regex blocks bytedance CNAMEs, not this name).
ATLAS_DOMAINS = {
    "www.ripe.net":        "integrity",
    "dns.google":          "integrity",
    "bbc.co.uk":           "integrity",
    "www.google.com":      "integrity",
    "doubleclick.net":     "block",
    "graph.instagram.com": "block",
    "dit.whatsapp.net":    "block",
}

# Quad9 unfiltered (no ad/malware blocking, so block canaries resolve here).
# Not 8.8.8.8/8.8.4.4/1.1.1.1: Atlas caps concurrent measurements per target
# platform-wide at 25 and those are saturated (hit 2026-07-07).
RESOLVER = "9.9.9.10"
EXTERNAL_PROBES = 3
INTERVAL = 3600


class AtlasClient:
    def __init__(self,
                 key=os.environ.get("RIPE_ATLAS_KEY", ""),
                 probe_id=os.environ.get("RIPE_ATLAS_PROBE_ID", "")):
        if not key:
            raise SystemExit("Set RIPE_ATLAS_KEY (crontab env) before running")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Key {key}"
        self.probe_id = int(probe_id) if probe_id else self._discover_probe()

    def _get(self, path, **params):
        r = self.s.get(f"{BASE}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _discover_probe(self):
        probes = self._get("probes/my/").get("results", [])
        if not probes:
            raise SystemExit("No probes on this account — set RIPE_ATLAS_PROBE_ID")
        for p in probes:
            if (p.get("status") or {}).get("name") == "Connected":
                return p["id"]
        return probes[0]["id"]

    def probe_status(self):
        p = self._get(f"probes/{self.probe_id}/")
        return {"id": self.probe_id,
                "connected": (p.get("status") or {}).get("name") == "Connected"}

    def credits(self):
        # Needs its own key grant; a schedule-only key gets 403 here.
        try:
            c = self._get("credits/")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                return {"balance": None, "daily_income": None}
            raise
        return {"balance": c.get("current_balance"),
                "daily_income": c.get("estimated_daily_income")}

    def list_my_measurements(self):
        """domain -> measurement id for our ongoing cross-check measurements.

        403 (key lacks the list grant) -> {}: the bootstrap then relies on
        data/atlas_state.json alone for idempotency, so don't delete it.
        """
        try:
            r = self._get("measurements/my/", search=DESC_PREFIX.rstrip(":"),
                          status__in="0,1,2", page_size=100)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                return {}
            raise
        out = {}
        for m in r.get("results", []):
            desc = m.get("description") or ""
            if desc.startswith(DESC_PREFIX):
                out[desc[len(DESC_PREFIX):]] = m["id"]
        return out

    def create_dns_measurement(self, domain, resolver=RESOLVER,
                               external_probes=EXTERNAL_PROBES, interval=INTERVAL):
        """One recurring A-record measurement: our probe + N worldwide, via a
        public resolver so every probe asks the same question."""
        payload = {
            "definitions": [{
                "type": "dns",
                "af": 4,
                "query_class": "IN",
                "query_type": "A",
                "query_argument": domain,
                "target": resolver,
                "use_probe_resolver": False,
                "description": DESC_PREFIX + domain,
                "interval": interval,
            }],
            "probes": [
                {"type": "probes", "value": str(self.probe_id), "requested": 1},
                {"type": "area", "value": "WW", "requested": external_probes},
            ],
            "is_oneoff": False,
        }
        r = self.s.post(f"{BASE}/measurements/", json=payload, timeout=30)
        if not r.ok:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:500]
            raise SystemExit(f"measurement create failed ({r.status_code}) "
                             f"for {domain}: {detail}")
        return r.json()["measurements"][0]

    def latest_results(self, msm_id):
        """[{prb_id, ips}] from the latest round; A records only."""
        out = []
        for res in self._get(f"measurements/{msm_id}/latest/"):
            result = res.get("result") or {}
            ips = []
            for a in result.get("answers") or []:
                if a.get("TYPE") == "A":
                    rd = a.get("RDATA")
                    ips += rd if isinstance(rd, list) else [rd]
            if not ips and result.get("abuf"):
                ips = _parse_abuf_a(result["abuf"])
            out.append({"prb_id": res.get("prb_id"), "ips": sorted(set(ips))})
        return out


def _parse_abuf_a(abuf_b64):
    """A records from a raw DNS response. Minimal: walks label/pointer names,
    reads answer RRs, keeps TYPE=A. Anything unparseable returns []."""
    try:
        buf = base64.b64decode(abuf_b64)
        qd, an = struct.unpack(">HH", buf[4:8])
        pos = 12

        def skip_name(p):
            while buf[p] != 0:
                if buf[p] & 0xC0:       # compression pointer: 2 bytes, ends name
                    return p + 2
                p += buf[p] + 1
            return p + 1

        for _ in range(qd):
            pos = skip_name(pos) + 4    # qtype + qclass
        ips = []
        for _ in range(an):
            pos = skip_name(pos)
            rtype, _, _, rdlen = struct.unpack(">HHIH", buf[pos:pos + 10])
            pos += 10
            if rtype == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in buf[pos:pos + 4]))
            pos += rdlen
        return ips
    except Exception:
        return []
