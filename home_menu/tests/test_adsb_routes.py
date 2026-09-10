"""Tests for pollers/adsb_routes.py — the adsb.lol callsign->route lookup + cache."""
import adsb_routes


# ── fixtures ──────────────────────────────────────────────────────────────
_LHR = {"iata": "LHR", "icao": "EGLL", "location": "London",
        "countryiso2": "GB", "name": "London Heathrow Airport"}
_JFK = {"iata": "JFK", "icao": "KJFK", "location": "New York",
        "countryiso2": "US", "name": "John F Kennedy Intl"}
_AMS = {"iata": "AMS", "icao": "EHAM", "location": "Amsterdam",
        "countryiso2": "NL", "name": "Schiphol"}


def _plane(cs, airports):
    return {"callsign": cs, "airport_codes": "x", "_airports": airports}


def _flight(cs="BAW123", lat=51.4, lon=-0.4):
    return {"cs": cs, "lat": lat, "lon": lon}


class _Fetch:
    """Stub for adsb_routes._fetch — records calls, returns a canned response."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, planes):
        self.calls.append(planes)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# ── resolve: happy path + caching ─────────────────────────────────────────
def test_resolve_fetches_annotates_and_caches():
    fetch = _Fetch({"planes": [_plane("BAW123", [_LHR, _JFK])]})
    cache = {}
    flights = [_flight("BAW123")]
    routes, dirty = adsb_routes.resolve(flights, cache, now=1000, fetch=fetch)

    assert dirty is True
    assert len(fetch.calls) == 1
    assert routes["BAW123"]["origin"]["iata"] == "LHR"
    assert routes["BAW123"]["dest"]["iata"] == "JFK"
    assert routes["BAW123"]["via"] == []
    assert cache["BAW123"]["ok"] is True and cache["BAW123"]["fetched"] == 1000

    adsb_routes.annotate(flights, routes)
    assert flights[0]["route"]["dest"]["city"] == "New York"


def test_resolve_cache_hit_skips_fetch():
    fetch = _Fetch(RuntimeError("must not be called"))
    cache = {"BAW123": {"fetched": 990, "ok": True,
                        "origin": dict(iata="LHR"), "dest": dict(iata="JFK"), "via": []}}
    routes, dirty = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert fetch.calls == [] and dirty is False
    assert routes["BAW123"]["dest"]["iata"] == "JFK"


def test_resolve_expired_hit_refetches():
    fetch = _Fetch({"planes": [_plane("BAW123", [_LHR, _JFK])]})
    stale = {"BAW123": {"fetched": 10, "ok": True, "origin": {}, "dest": {}, "via": []}}
    _, dirty = adsb_routes.resolve([_flight("BAW123")], stale,
                                   now=10 + adsb_routes.CACHE_TTL + 1, fetch=fetch)
    assert len(fetch.calls) == 1 and dirty is True


# ── resolve: unknown callsigns / negative caching ─────────────────────────
def test_resolve_unknown_is_negative_cached():
    fetch = _Fetch({"planes": [_plane("N12345", [])]})
    cache = {}
    routes, dirty = adsb_routes.resolve([_flight("N12345")], cache, now=1000, fetch=fetch)
    assert "N12345" not in routes
    assert cache["N12345"] == {"fetched": 1000, "ok": False}
    assert dirty is True

    # a second sighting inside NEGATIVE_TTL must not re-hit the API
    fetch2 = _Fetch(RuntimeError("must not be called"))
    _, dirty2 = adsb_routes.resolve([_flight("N12345")], cache,
                                    now=1000 + adsb_routes.NEGATIVE_TTL - 1, fetch=fetch2)
    assert fetch2.calls == [] and dirty2 is False


def test_resolve_negative_cache_expires_sooner_than_positive():
    fetch = _Fetch({"planes": [_plane("N12345", [])]})
    cache = {"N12345": {"fetched": 0, "ok": False}}
    adsb_routes.resolve([_flight("N12345")], cache,
                        now=adsb_routes.NEGATIVE_TTL + 1, fetch=fetch)
    assert len(fetch.calls) == 1


def test_resolve_single_airport_treated_as_unknown():
    fetch = _Fetch({"planes": [_plane("BAW123", [_LHR])]})
    cache = {}
    routes, _ = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert routes == {}
    assert cache["BAW123"]["ok"] is False


# ── resolve: multi-leg, response shapes, robustness ───────────────────────
def test_resolve_multileg_route_has_via():
    fetch = _Fetch({"planes": [_plane("KLM641", [_AMS, _LHR, _JFK])]})
    routes, _ = adsb_routes.resolve([_flight("KLM641")], {}, now=1000, fetch=fetch)
    r = routes["KLM641"]
    assert r["origin"]["iata"] == "AMS" and r["dest"]["iata"] == "JFK"
    assert r["via"] == ["LHR"]


def test_resolve_accepts_bare_list_response():
    fetch = _Fetch([_plane("BAW123", [_LHR, _JFK])])
    routes, _ = adsb_routes.resolve([_flight("BAW123")], {}, now=1000, fetch=fetch)
    assert routes["BAW123"]["origin"]["iata"] == "LHR"


def test_resolve_fetch_failure_never_raises():
    fetch = _Fetch(OSError("connection refused"))
    cache = {}
    routes, dirty = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert routes == {} and dirty is False and cache == {}


def test_resolve_dedupes_and_caps_lookups():
    fetch = _Fetch({"planes": []})
    flights = [_flight(f"ABC{i:03d}") for i in range(20)] + [_flight("ABC001")]
    adsb_routes.resolve(flights, {}, now=1000, fetch=fetch)
    sent = fetch.calls[0]
    callsigns = [p["callsign"] for p in sent]
    assert len(sent) == adsb_routes.MAX_LOOKUP
    assert len(callsigns) == len(set(callsigns))          # deduped


def test_resolve_skips_flights_without_position():
    fetch = _Fetch({"planes": []})
    routes, dirty = adsb_routes.resolve(
        [{"cs": "BAW123", "lat": None, "lon": None}], {}, now=1000, fetch=fetch)
    assert fetch.calls == [] and dirty is False


def test_resolve_matches_callsign_case_insensitively():
    fetch = _Fetch({"planes": [_plane("BAW123", [_LHR, _JFK])]})
    flights = [{"cs": "baw123", "lat": 51.4, "lon": -0.4}]
    routes, _ = adsb_routes.resolve(flights, {}, now=1000, fetch=fetch)
    adsb_routes.annotate(flights, routes)
    assert flights[0]["route"]["origin"]["iata"] == "LHR"


# ── cache load / save ────────────────────────────────────────────────────
def test_load_cache_missing_or_malformed(tmp_path):
    assert adsb_routes.load_cache(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert adsb_routes.load_cache(bad) == {}
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]")
    assert adsb_routes.load_cache(lst) == {}


def test_save_cache_roundtrips_and_prunes(tmp_path):
    import time
    now = int(time.time())
    path = tmp_path / "routes.json"
    cache = {
        "FRESH": {"fetched": now, "ok": True, "origin": {}, "dest": {}, "via": []},
        "ANCIENT": {"fetched": now - adsb_routes.PRUNE_AFTER - 1, "ok": True},
    }
    adsb_routes.save_cache(path, cache)
    back = adsb_routes.load_cache(path)
    assert "FRESH" in back and "ANCIENT" not in back
