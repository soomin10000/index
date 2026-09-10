"""Callsign -> flight-route lookup for the /jeff ADS-B panel.

readsb's aircraft.json gives a callsign but no route, so origin / destination
airports come from adsb.lol's free `routeset` API — the same service tar1090
uses for its route overlay. Results are cached to `data/jeff_routes.json`: a
route is fixed for the life of a flight number, so a hit is good for CACHE_TTL;
callsigns the API doesn't know (general aviation, military, positioning flights)
are remembered as misses for a shorter NEGATIVE_TTL so they're retried
occasionally instead of on every poll.

Nothing here raises out of `resolve()` — a lookup that fails just leaves the
flights without a route; it never breaks the jeff poll or stales the card.
"""

import json
import os
import time
import urllib.request

API_URL = "https://api.adsb.lol/api/0/routeset"
HTTP_TIMEOUT = 6
USER_AGENT = "home-menu/jeff-card (+homelab dashboard)"

CACHE_TTL = 24 * 3600        # a known route is stable for the life of the flight number
NEGATIVE_TTL = 3 * 3600      # re-ask about unknown callsigns every few hours
MAX_LOOKUP = 12             # callsigns resolved per poll (one batched request)
PRUNE_AFTER = CACHE_TTL * 7  # drop cache entries untouched for this long


def _airport(a):
    """Compact one adsb.lol `_airports` entry down to what the card shows."""
    return {
        "iata": a.get("iata") or "",
        "icao": a.get("icao") or "",
        "city": a.get("location") or "",
        "country": a.get("countryiso2") or a.get("country") or "",
        "name": a.get("name") or "",
    }


def _route_from_plane(p):
    """adsb.lol plane record -> {origin, dest, via} or None when unknown.

    `_airports` is the ordered leg list: [0] is the origin, [-1] the
    destination, anything between is a stopover. One or zero airports means the
    route couldn't be pinned down — treat it as a miss."""
    aps = [_airport(a) for a in (p.get("_airports") or []) if isinstance(a, dict)]
    if len(aps) < 2:
        return None
    via = [a["iata"] or a["icao"] for a in aps[1:-1] if a["iata"] or a["icao"]]
    return {"origin": aps[0], "dest": aps[-1], "via": via}


def _fetch(planes, *, url=API_URL, timeout=HTTP_TIMEOUT):
    """POST the batch to adsb.lol. Raises on any transport / decode error."""
    body = json.dumps({"planes": planes}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


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


def save_cache(path, cache):
    """Write the cache back, pruning entries nothing has touched in a week so
    the file doesn't grow without bound. Atomic via a temp file + rename."""
    now = int(time.time())
    pruned = {cs: e for cs, e in cache.items()
              if isinstance(e, dict) and 0 <= now - e.get("fetched", 0) < PRUNE_AFTER}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(pruned, fh)
    os.replace(tmp, path)


def resolve(flights, cache, now=None, fetch=None):
    """Fill `cache` with routes for the callsigns in `flights`.

    Returns (routes, dirty): `routes` maps upper-cased callsign -> {origin, dest,
    via} for every callsign we now have a known route for; `dirty` is True when
    `cache` was changed and should be written back. Only callsigns missing or
    expired from the cache are looked up, at most MAX_LOOKUP per call, in one
    batched request. A failed request is swallowed — the flights just go without
    a route this poll."""
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
        lat, lon = (f or {}).get("lat"), (f or {}).get("lon")
        if lat is None or lon is None:
            continue
        if len(want) < MAX_LOOKUP:
            want.append({"callsign": cs, "lat": lat, "lng": lon})

    dirty = False
    if want:
        try:
            resp = fetch(want)
        except Exception:
            resp = None
        if resp is not None:
            planes = resp.get("planes", []) if isinstance(resp, dict) else resp
            got = {}
            for p in planes or []:
                if not isinstance(p, dict):
                    continue
                cs = str(p.get("callsign") or "").strip().upper()
                if cs:
                    got[cs] = _route_from_plane(p)
            for w in want:
                route = got.get(w["callsign"])
                cache[w["callsign"]] = {"fetched": now, "ok": bool(route), **(route or {})}
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
