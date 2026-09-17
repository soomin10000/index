"""Tests for pollers/adsb_routes.py — the adsbdb.com callsign->route lookup + cache."""
import adsb_routes


# ── fixtures ──────────────────────────────────────────────────────────────
def _airport(iata, icao, city, country, name):
    return {"iata_code": iata, "icao_code": icao, "municipality": city,
            "country_iso_name": country, "name": name}


def _resp(origin=None, dest=None):
    origin = origin or _airport("LHR", "EGLL", "London", "GB", "London Heathrow Airport")
    dest = dest or _airport("JFK", "KJFK", "New York", "US", "John F Kennedy Intl")
    return {"response": {"flightroute": {"origin": origin, "destination": dest}}}


def _flight(cs="BAW123", lat=51.4, lon=-0.4):
    return {"cs": cs, "lat": lat, "lon": lon}


class _Fetch:
    """Stub for adsb_routes._fetch — records callsigns, returns a canned reply.

    `reply` may be a dict (adsbdb payload), None ("unknown callsign"), an
    Exception (raised), or a {callsign: one-of-those} map for per-callsign
    behaviour."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, callsign):
        self.calls.append(callsign)
        r = self.reply[callsign] if isinstance(self.reply, dict) and "response" not in self.reply else self.reply
        if isinstance(r, Exception):
            raise r
        return r


# ── resolve: happy path + caching ─────────────────────────────────────────
def test_resolve_fetches_annotates_and_caches():
    fetch = _Fetch(_resp())
    cache = {}
    flights = [_flight("BAW123")]
    routes, dirty = adsb_routes.resolve(flights, cache, now=1000, fetch=fetch)

    assert dirty is True
    assert fetch.calls == ["BAW123"]
    assert routes["BAW123"]["origin"]["iata"] == "LHR"
    assert routes["BAW123"]["dest"]["iata"] == "JFK"
    assert routes["BAW123"]["via"] == []
    assert cache["BAW123"]["ok"] is True and cache["BAW123"]["fetched"] == 1000

    adsb_routes.annotate(flights, routes)
    assert flights[0]["route"]["dest"]["city"] == "New York"


def test_resolve_cache_hit_skips_fetch():
    fetch = _Fetch(RuntimeError("must not be called"))
    cache = {"BAW123": {"fetched": 990, "ok": True,
                        "origin": {"iata": "LHR"}, "dest": {"iata": "JFK"}, "via": []}}
    routes, dirty = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert fetch.calls == [] and dirty is False
    assert routes["BAW123"]["dest"]["iata"] == "JFK"


def test_resolve_expired_hit_refetches():
    fetch = _Fetch(_resp())
    stale = {"BAW123": {"fetched": 10, "ok": True, "origin": {}, "dest": {}, "via": []}}
    _, dirty = adsb_routes.resolve([_flight("BAW123")], stale,
                                   now=10 + adsb_routes.CACHE_TTL + 1, fetch=fetch)
    assert fetch.calls == ["BAW123"] and dirty is True


# ── resolve: unknown callsigns / negative caching ─────────────────────────
def test_resolve_unknown_is_negative_cached():
    fetch = _Fetch(None)                       # adsbdb 404 -> _fetch returns None
    cache = {}
    routes, dirty = adsb_routes.resolve([_flight("N12345")], cache, now=1000, fetch=fetch)
    assert "N12345" not in routes
    assert cache["N12345"] == {"fetched": 1000, "ok": False}
    assert dirty is True

    fetch2 = _Fetch(RuntimeError("must not be called"))
    _, dirty2 = adsb_routes.resolve([_flight("N12345")], cache,
                                    now=1000 + adsb_routes.NEGATIVE_TTL - 1, fetch=fetch2)
    assert fetch2.calls == [] and dirty2 is False


def test_resolve_negative_cache_expires_sooner_than_positive():
    fetch = _Fetch(None)
    cache = {"N12345": {"fetched": 0, "ok": False}}
    adsb_routes.resolve([_flight("N12345")], cache,
                        now=adsb_routes.NEGATIVE_TTL + 1, fetch=fetch)
    assert fetch.calls == ["N12345"]


def test_resolve_partial_flightroute_treated_as_unknown():
    fetch = _Fetch({"response": {"flightroute": {"origin": {"iata_code": "LHR"}}}})  # no destination
    cache = {}
    routes, _ = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert routes == {}
    assert cache["BAW123"]["ok"] is False


# ── resolve: robustness ──────────────────────────────────────────────────
def test_resolve_fetch_failure_leaves_it_uncached():
    fetch = _Fetch(OSError("connection refused"))
    cache = {}
    routes, dirty = adsb_routes.resolve([_flight("BAW123")], cache, now=1000, fetch=fetch)
    assert routes == {} and dirty is False and cache == {}


def test_resolve_one_bad_lookup_does_not_sink_the_rest():
    fetch = _Fetch({"BAW1": OSError("boom"), "BAW2": _resp()})
    cache = {}
    routes, dirty = adsb_routes.resolve([_flight("BAW1"), _flight("BAW2")],
                                        cache, now=1000, fetch=fetch)
    assert "BAW1" not in cache and dirty is True
    assert routes["BAW2"]["origin"]["iata"] == "LHR"


def test_resolve_dedupes_and_caps_lookups():
    fetch = _Fetch(None)
    flights = [_flight(f"ABC{i:03d}") for i in range(20)] + [_flight("ABC001")]
    adsb_routes.resolve(flights, {}, now=1000, fetch=fetch)
    assert len(fetch.calls) == adsb_routes.MAX_LOOKUP
    assert len(fetch.calls) == len(set(fetch.calls))          # deduped


def test_resolve_matches_callsign_case_insensitively():
    fetch = _Fetch(_resp())
    flights = [{"cs": "baw123", "lat": 51.4, "lon": -0.4}]
    routes, _ = adsb_routes.resolve(flights, {}, now=1000, fetch=fetch)
    adsb_routes.annotate(flights, routes)
    assert fetch.calls == ["BAW123"]
    assert flights[0]["route"]["origin"]["iata"] == "LHR"


def test_resolve_blank_callsign_ignored():
    fetch = _Fetch(_resp())
    routes, dirty = adsb_routes.resolve([{"cs": "  "}], {}, now=1000, fetch=fetch)
    assert fetch.calls == [] and routes == {} and dirty is False


# ── _fetch response parsing ──────────────────────────────────────────────
def test_route_from_response_maps_fields():
    r = adsb_routes._route_from_response(_resp())
    assert r["origin"] == {"iata": "LHR", "icao": "EGLL", "city": "London",
                           "country": "GB", "name": "London Heathrow Airport"}
    assert r["dest"]["iata"] == "JFK" and r["via"] == []


def test_route_from_response_unknown_shapes():
    for bad in (None, {}, {"response": "unknown callsign"},
                {"response": {"flightroute": None}},
                {"response": {"flightroute": {"origin": {}, "destination": None}}}):
        assert adsb_routes._route_from_response(bad) is None


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


def test_save_cache_custom_prune_after(tmp_path):
    """The aircraft cache passes its own (much longer) prune_after — an entry
    that would be pruned under the route cache's default must survive here."""
    import time
    now = int(time.time())
    path = tmp_path / "aircraft.json"
    cache = {"424C32": {"fetched": now - adsb_routes.PRUNE_AFTER - 1, "ok": True}}
    adsb_routes.save_cache(path, cache, prune_after=adsb_routes.AIRCRAFT_PRUNE_AFTER)
    assert "424C32" in adsb_routes.load_cache(path)


# ── resolve_aircraft: happy path + caching (mirrors resolve() above) ───────
def _ac_resp(reg="M-ONEY", owner="Century Aviation Ltd"):
    return {"response": {"aircraft": {
        "type": "525", "icao_type": "C525", "manufacturer": "Cessna",
        "registration": reg, "registered_owner": owner,
        "registered_owner_country_name": "Isle of Man",
    }}}


class _FetchAircraft:
    """Same shape as _Fetch but keyed by hex, for resolve_aircraft()."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, hex_code):
        self.calls.append(hex_code)
        r = self.reply[hex_code] if isinstance(self.reply, dict) and "response" not in self.reply else self.reply
        if isinstance(r, Exception):
            raise r
        return r


def test_resolve_aircraft_fetches_annotates_and_caches():
    fetch = _FetchAircraft(_ac_resp())
    cache = {}
    flights = [{"cs": "MONEY", "hex": "424c32"}]
    aircraft, dirty = adsb_routes.resolve_aircraft(flights, cache, now=1000, fetch=fetch)

    assert dirty is True
    assert fetch.calls == ["424c32"]
    assert aircraft["424c32"]["reg"] == "M-ONEY"
    assert aircraft["424c32"]["owner"] == "Century Aviation Ltd"
    assert cache["424c32"]["ok"] is True and cache["424c32"]["fetched"] == 1000

    adsb_routes.annotate_aircraft(flights, aircraft)
    assert flights[0]["aircraft"]["owner"] == "Century Aviation Ltd"


def test_resolve_aircraft_cache_hit_skips_fetch():
    fetch = _FetchAircraft(RuntimeError("must not be called"))
    cache = {"424c32": {"fetched": 990, "ok": True, "reg": "M-ONEY", "owner": "Century Aviation Ltd"}}
    aircraft, dirty = adsb_routes.resolve_aircraft(
        [{"cs": "MONEY", "hex": "424c32"}], cache, now=1000, fetch=fetch)
    assert fetch.calls == [] and dirty is False
    assert aircraft["424c32"]["reg"] == "M-ONEY"


def test_resolve_aircraft_unknown_hex_is_negative_cached():
    fetch = _FetchAircraft(None)                # adsbdb 404 -> _fetch_aircraft returns None
    cache = {}
    aircraft, dirty = adsb_routes.resolve_aircraft([{"hex": "ffffff"}], cache, now=1000, fetch=fetch)
    assert "ffffff" not in aircraft
    assert cache["ffffff"] == {"fetched": 1000, "ok": False}
    assert dirty is True

    fetch2 = _FetchAircraft(RuntimeError("must not be called"))
    _, dirty2 = adsb_routes.resolve_aircraft(
        [{"hex": "ffffff"}], cache, now=1000 + adsb_routes.AIRCRAFT_NEGATIVE_TTL - 1, fetch=fetch2)
    assert fetch2.calls == [] and dirty2 is False


def test_resolve_aircraft_negative_ttl_much_shorter_than_positive():
    assert adsb_routes.AIRCRAFT_NEGATIVE_TTL < adsb_routes.AIRCRAFT_CACHE_TTL


def test_resolve_aircraft_dedupes_by_hex_case_insensitively():
    fetch = _FetchAircraft(_ac_resp())
    flights = [{"hex": "424C32"}, {"hex": "424c32"}]
    aircraft, _ = adsb_routes.resolve_aircraft(flights, {}, now=1000, fetch=fetch)
    assert fetch.calls == ["424c32"]
    assert aircraft["424c32"]["reg"] == "M-ONEY"


def test_resolve_aircraft_blank_hex_ignored():
    fetch = _FetchAircraft(_ac_resp())
    aircraft, dirty = adsb_routes.resolve_aircraft([{"hex": ""}], {}, now=1000, fetch=fetch)
    assert fetch.calls == [] and aircraft == {} and dirty is False


def test_aircraft_from_response_maps_fields():
    ac = adsb_routes._aircraft_from_response(_ac_resp())
    assert ac == {"reg": "M-ONEY", "type": "525", "manufacturer": "Cessna",
                  "owner": "Century Aviation Ltd", "owner_country": "Isle of Man"}


def test_aircraft_from_response_falls_back_to_icao_type():
    resp = {"response": {"aircraft": {"icao_type": "C525", "registration": "M-ONEY"}}}
    assert adsb_routes._aircraft_from_response(resp)["type"] == "C525"


def test_aircraft_from_response_unknown_shapes():
    for bad in (None, {}, {"response": "unknown callsign"}, {"response": {"aircraft": None}}):
        assert adsb_routes._aircraft_from_response(bad) is None
