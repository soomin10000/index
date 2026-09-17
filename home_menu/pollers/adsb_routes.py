"""Callsign -> flight-route lookup for the /jeff ADS-B panel.

readsb's aircraft.json gives a callsign but no route, so origin / destination
airports come from adsbdb.com's free per-callsign API. Results are cached to
`data/jeff_routes.json`: a route is fixed for the life of a flight number, so a
hit is good for CACHE_TTL; callsigns the API doesn't know (general aviation,
military, positioning flights) are remembered as misses for a shorter
NEGATIVE_TTL so they're retried occasionally instead of on every poll.

Nothing here raises out of `resolve()` — a lookup that fails just leaves the
flights without a route; it never breaks the jeff poll or stales the card.

(adsb.lol's batched `routeset` endpoint was tried first but returns empty 201s
as of 2026-09-10; adsbdb is one GET per callsign, which the cache keeps cheap.)
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.adsbdb.com/v0/callsign/"
HTTP_TIMEOUT = 4
USER_AGENT = "home-menu/jeff-card (+homelab dashboard)"

CACHE_TTL = 24 * 3600        # a known route is stable for the life of the flight number
NEGATIVE_TTL = 3 * 3600      # re-ask about unknown callsigns every few hours
MAX_LOOKUP = 8              # callsigns resolved per poll (one GET each)
PRUNE_AFTER = CACHE_TTL * 7  # drop cache entries untouched for this long


def _airport(a):
    """Compact one adsbdb origin/destination object down to what the card shows."""
    return {
        "iata": a.get("iata_code") or "",
        "icao": a.get("icao_code") or "",
        "city": a.get("municipality") or "",
        "country": a.get("country_iso_name") or "",
        "name": a.get("name") or "",
    }


def _route_from_response(resp):
    """adsbdb `/v0/callsign` payload -> {origin, dest, via} or None when unknown."""
    fr = (resp or {}).get("response")
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if not isinstance(fr, dict):
        return None
    o, d = fr.get("origin"), fr.get("destination")
    if not isinstance(o, dict) or not isinstance(d, dict):
        return None
    return {"origin": _airport(o), "dest": _airport(d), "via": []}


def _fetch(callsign, *, timeout=HTTP_TIMEOUT):
    """GET one callsign from adsbdb. Returns the decoded JSON dict, or None when
    adsbdb says it doesn't know the callsign (HTTP 404). Raises on any other
    transport / decode error so the caller can leave it uncached and retry."""
    req = urllib.request.Request(
        API_URL + urllib.parse.quote(callsign, safe=""),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fresh(entry, now):
    if not isinstance(entry, dict) or "fetched" not in entry:
        return False
    age = now - entry.get("fetched", 0)
    ttl = CACHE_TTL if entry.get("ok") else NEGATIVE_TTL
    return 0 <= age < ttl


def load_cache(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache, prune_after=PRUNE_AFTER):
    """Write the cache back, pruning entries nothing has touched in `prune_after`
    seconds (default a week, sized for the route cache's 24h TTL) so the file
    doesn't grow without bound. Atomic via a temp file + rename."""
    now = int(time.time())
    pruned = {cs: e for cs, e in cache.items()
              if isinstance(e, dict) and 0 <= now - e.get("fetched", 0) < prune_after}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(pruned, fh)
    os.replace(tmp, path)


def resolve(flights, cache, now=None, fetch=None):
    """Fill `cache` with routes for the callsigns in `flights`.

    Returns (routes, dirty): `routes` maps upper-cased callsign -> {origin, dest,
    via} for every callsign we now have a known route for; `dirty` is True when
    `cache` was changed and should be written back. Only callsigns missing or
    expired from the cache are looked up, at most MAX_LOOKUP per call. A lookup
    that raises is swallowed and left uncached (retried next poll); a definitive
    "unknown callsign" is negative-cached."""
    now = int(now if now is not None else time.time())
    fetch = fetch or _fetch
    if not isinstance(cache, dict):
        cache = {}

    want, seen = [], set()
    for f in flights:
        cs = str((f or {}).get("cs") or "").strip().upper()
        if not cs or cs in seen:
            continue
        seen.add(cs)
        if _fresh(cache.get(cs), now):
            continue
        if len(want) < MAX_LOOKUP:
            want.append(cs)

    dirty = False
    for cs in want:
        try:
            resp = fetch(cs)
        except Exception:
            continue
        route = _route_from_response(resp)
        cache[cs] = {"fetched": now, "ok": bool(route), **(route or {})}
        dirty = True

    routes = {}
    for cs in seen:
        entry = cache.get(cs)
        if isinstance(entry, dict) and entry.get("ok"):
            routes[cs] = {k: entry[k] for k in ("origin", "dest", "via") if k in entry}
    return routes, dirty


def annotate(flights, routes):
    """Attach `route` to each flight dict that has a known route."""
    for f in flights:
        r = routes.get(str((f or {}).get("cs") or "").strip().upper())
        if r:
            f["route"] = r
    return flights


# ── aircraft (registration / owner) lookup, by ICAO hex ─────────────────────
# A private/GA callsign like a bizjet's often has no route (resolve() above
# comes up empty), which is exactly when who-owns-this-plane is the
# interesting question. adsbdb's per-aircraft endpoint answers that from the
# Mode S hex rather than the callsign. Registration/ownership essentially
# never changes, so this is cached far longer than a route.
AIRCRAFT_API_URL = "https://api.adsbdb.com/v0/aircraft/"
AIRCRAFT_CACHE_TTL = 30 * 24 * 3600     # registration/owner is stable for months-years
AIRCRAFT_NEGATIVE_TTL = 24 * 3600       # hex adsbdb doesn't know — retry daily, not every poll
MAX_AIRCRAFT_LOOKUP = 8
AIRCRAFT_PRUNE_AFTER = AIRCRAFT_CACHE_TTL * 7


def _aircraft_from_response(resp):
    """adsbdb `/v0/aircraft` payload -> {reg, type, manufacturer, owner,
    owner_country}, or None when adsbdb doesn't know the hex."""
    ac = (resp or {}).get("response")
    if not isinstance(ac, dict):
        return None
    ac = ac.get("aircraft")
    if not isinstance(ac, dict):
        return None
    return {
        "reg": ac.get("registration") or "",
        "type": ac.get("type") or ac.get("icao_type") or "",
        "manufacturer": ac.get("manufacturer") or "",
        "owner": ac.get("registered_owner") or "",
        "owner_country": ac.get("registered_owner_country_name") or "",
    }


def _fetch_aircraft(hex_code, *, timeout=HTTP_TIMEOUT):
    """GET one ICAO hex from adsbdb. Same 404-vs-raise contract as _fetch()."""
    req = urllib.request.Request(
        AIRCRAFT_API_URL + urllib.parse.quote(hex_code, safe=""),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fresh_aircraft(entry, now):
    if not isinstance(entry, dict) or "fetched" not in entry:
        return False
    age = now - entry.get("fetched", 0)
    ttl = AIRCRAFT_CACHE_TTL if entry.get("ok") else AIRCRAFT_NEGATIVE_TTL
    return 0 <= age < ttl


def resolve_aircraft(flights, cache, now=None, fetch=None):
    """Same shape/contract as resolve(), keyed by lower-cased ICAO hex instead
    of callsign. Returns (aircraft, dirty): `aircraft` maps hex -> {reg, type,
    manufacturer, owner, owner_country} for every hex now known."""
    now = int(now if now is not None else time.time())
    fetch = fetch or _fetch_aircraft
    if not isinstance(cache, dict):
        cache = {}

    want, seen = [], set()
    for f in flights:
        hx = str((f or {}).get("hex") or "").strip().lower()
        if not hx or hx in seen:
            continue
        seen.add(hx)
        if _fresh_aircraft(cache.get(hx), now):
            continue
        if len(want) < MAX_AIRCRAFT_LOOKUP:
            want.append(hx)

    dirty = False
    for hx in want:
        try:
            resp = fetch(hx)
        except Exception:
            continue
        ac = _aircraft_from_response(resp)
        cache[hx] = {"fetched": now, "ok": bool(ac), **(ac or {})}
        dirty = True

    aircraft = {}
    for hx in seen:
        entry = cache.get(hx)
        if isinstance(entry, dict) and entry.get("ok"):
            aircraft[hx] = {k: entry[k] for k in
                            ("reg", "type", "manufacturer", "owner", "owner_country") if k in entry}
    return aircraft, dirty


def annotate_aircraft(flights, aircraft):
    """Attach `aircraft` to each flight dict that has known registration/owner info."""
    for f in flights:
        ac = aircraft.get(str((f or {}).get("hex") or "").strip().lower())
        if ac:
            f["aircraft"] = ac
    return flights
