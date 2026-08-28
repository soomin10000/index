import time

import server


def test_roundtrip():
    tok = server._make_session("simon")
    assert server._session_user(f"session={tok}") == "simon"


def test_no_cookie():
    assert server._session_user(None) is None
    assert server._session_user("") is None
    assert server._session_user("other=1") is None


def test_garbage_cookie():
    assert server._session_user("session=not-a-real-token") is None
    assert server._session_user("session=a|b") is None


def test_tampered_signature():
    tok = server._make_session("simon")
    user, exp, _sig = tok.split("|")
    forged = f"{user}|{exp}|{'0' * 64}"
    assert server._session_user(f"session={forged}") is None


def test_privilege_swap_rejected():
    # keep a valid signature but swap the username -> must fail
    tok = server._make_session("guest")
    _user, exp, sig = tok.split("|")
    assert server._session_user(f"session=admin|{exp}|{sig}") is None


def test_expired(monkeypatch):
    tok = server._make_session("simon")
    user, exp, sig = tok.split("|")
    past = f"{user}|{int(time.time()) - 10}|{sig}"
    assert server._session_user(f"session={past}") is None


def test_make_session_shape():
    tok = server._make_session("simon")
    parts = tok.split("|")
    assert len(parts) == 3 and parts[0] == "simon" and len(parts[2]) == 64
    assert int(parts[1]) > time.time()
