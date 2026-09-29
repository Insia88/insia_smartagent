"""LinkedIn OAuth (package A): one-time states, the cookie binding, code exchange, pasted URLs, connect modes, the
CLI's one-shot callback listener and secret masking in logs (DESIGN.md 3-1, 3-2, 3-4; LI §3)."""

from __future__ import annotations

import http.client
import logging
import socket
import urllib.parse

import pytest

from insia_agents.publishers import (
    ConnectError,
    FakeTransport,
    InvalidInputError,
    NotConfiguredError,
    OAuthCancelledError,
    OAuthExchangeError,
    OAuthStateError,
    json_response,
)
from insia_agents.publishers.oauth import OAuthStateStore, OneShotCallbackListener, parse_pasted_callback
from insia_agents.publishers.redact import get_logger

pytestmark = pytest.mark.usefixtures("no_network")

REDIRECT = "http://localhost:8765/oauth/linkedin/callback"
TOKEN = "AQUv-exchanged-token-abcdef0123"


def _state(start) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(start.authorize_url).query)["state"][0]


def _service(kit, **kwargs):
    fake = FakeTransport(redact=False)
    service = kit.service(transport=fake, **kwargs)
    service.save_linkedin_app(client_id="86clientid", client_secret="li-app-secret-xyz")
    return service, fake


def _script_exchange(fake, scope="openid profile w_member_social"):
    fake.add("POST", r"/oauth/v2/accessToken$", json_response(200, {"access_token": TOKEN, "expires_in": 5184000,
                                                                    "scope": scope}))
    fake.add("GET", r"/v2/userinfo$", json_response(200, {"sub": "782bbtaQ", "name": "홍길동"}))


def test_state_store_is_one_time_and_expires(fake_clock):
    states = OAuthStateStore(fake_clock)
    first, second = states.issue(), states.issue()
    assert len(first) >= 43 and first != second
    assert states.check(first) == "ok" and states.consume(first) == "ok" and states.consume(first) == "used"
    fake_clock.advance(601)
    assert states.consume(second) == "expired" and states.consume("never-issued") == "unknown"
    cookie = states.cookie_value(second)
    assert states.cookie_matches(second, cookie) and not states.cookie_matches(second, cookie[:-1] + ("1" if cookie[-1] != "1" else "2"))
    assert OAuthStateStore(fake_clock).cookie_value(second) != cookie  # a per-process key


def test_authorize_url_and_connect_modes(publish_kit):
    service, _ = _service(publish_kit, public_hosts=("insia.example.com",))
    start = service.linkedin_connect(request_origin="http://localhost:8765")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.authorize_url).query)
    assert start.authorize_url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "scope=openid%20profile%20w_member_social" in start.authorize_url
    assert query["response_type"] == ["code"] and query["client_id"] == ["86clientid"] and query["redirect_uri"] == [REDIRECT]
    assert start.mode == "redirect" and start.cookie_value and "cookie_value" not in start.to_json()
    paste = service.linkedin_connect(request_origin="http://192.168.0.10:8765")
    assert paste.mode == "paste" and paste.cookie_value == ""
    assert paste.to_json()["open_url"] == "http://localhost:8765/#/brand/connections"
    assert service.linkedin_connect().mode == "paste"  # the CLI
    service.save_linkedin_app(redirect_uri="https://elsewhere.example.org/oauth/linkedin/callback")
    assert "open_url" not in service.linkedin_connect(request_origin="http://localhost:8765").to_json()
    service.save_linkedin_app(redirect_uri="https://insia.example.com/oauth/linkedin/callback")
    assert service.linkedin_connect(request_origin="https://insia.example.com").mode == "redirect"
    with pytest.raises(InvalidInputError):
        service.save_linkedin_app(redirect_uri="http://192.168.0.10:8765/oauth/linkedin/callback")
    service.disconnect("linkedin", forget_app=True)
    with pytest.raises(NotConfiguredError):
        service.linkedin_connect()


def test_callback_checks_state_and_cookie_then_saves(publish_kit):
    service, fake = _service(publish_kit)
    start = service.linkedin_connect(request_origin="http://localhost:8765")
    state = _state(start)
    assert service.linkedin_callback(code="the-code-123", state=state, cookie="forged") == "invalid"
    assert service.linkedin_callback(code="the-code-123", state="unknown-state", cookie=start.cookie_value) == "invalid"
    assert fake.requests == []  # nothing exchanged for a request from another browser
    _script_exchange(fake)
    assert service.linkedin_callback(code="the-code-123", state=state, cookie=start.cookie_value) == "ok"
    exchange = fake.calls("POST")[0]
    assert exchange.form == {"grant_type": "authorization_code", "code": "the-code-123", "client_id": "86clientid",
                             "client_secret": "li-app-secret-xyz", "redirect_uri": REDIRECT}
    assert fake.calls("GET")[0].headers["authorization"] == f"Bearer {TOKEN}"
    stored = service.store.get("linkedin")
    assert stored["access_token"] == TOKEN and stored["sub"] == "782bbtaQ" and "name" not in stored
    block = service.platform_status("linkedin")
    assert block["state"] == "connected" and block["account"]["name"] == "홍길동"
    assert block["token"]["scopes"] == ["openid", "profile", "w_member_social"]
    assert service.linkedin_callback(code="the-code-123", state=state, cookie=start.cookie_value) == "invalid"  # reused
    raw = publish_kit.workspace.db_path.read_bytes() + (publish_kit.home / "insia.db-wal").read_bytes()
    assert TOKEN.encode() not in raw and b"li-app-secret-xyz" not in raw and b"the-code-123" not in raw


def test_callback_outcomes(publish_kit):
    service, fake = _service(publish_kit)
    start = service.linkedin_connect(request_origin="http://localhost:8765")
    assert service.linkedin_callback(state=_state(start), error="user_cancelled_authorize",
                                     cookie=start.cookie_value) == "cancelled"
    start = service.linkedin_connect(request_origin="http://localhost:8765")
    fake.add("POST", r"/oauth/v2/accessToken$", json_response(400, {"error": "invalid_redirect_uri",
                                                                    "error_description": "<script>x</script>"}))
    assert service.linkedin_callback(code="c-123456", state=_state(start), cookie=start.cookie_value) == "exchange_failed"
    start = service.linkedin_connect(request_origin="http://localhost:8765")
    publish_kit.clock.advance(601)
    assert service.linkedin_callback(code="c-123456", state=_state(start), cookie=start.cookie_value) == "expired"


def test_pasted_url_parser():
    assert parse_pasted_callback(f"{REDIRECT}?code=abc123&state=st456", REDIRECT) == \
        {"code": "abc123", "state": "st456", "error": ""}
    assert parse_pasted_callback("code=abc123&state=st456", REDIRECT)["code"] == "abc123"
    assert parse_pasted_callback("?state=st456&error=user_cancelled_login", REDIRECT)["error"] == "user_cancelled_login"
    for bad in ("https://evil.example/other/path?code=a&state=b", "ftp://x/oauth/linkedin/callback?code=a&state=b",
                "code=abc123", "", "state=only"):
        with pytest.raises(InvalidInputError):
            parse_pasted_callback(bad, REDIRECT)


def test_complete_accepts_only_this_servers_states_once(publish_kit):
    service, fake = _service(publish_kit)
    with pytest.raises(OAuthStateError):
        service.linkedin_complete(f"{REDIRECT}?code=c1&state=made-up-state")
    start = service.linkedin_connect(request_origin="http://192.168.0.10:8765")  # paste mode: no origin check
    _script_exchange(fake)
    block = service.linkedin_complete(f"http://192.168.0.10:8765/oauth/linkedin/callback?code=c1&state={_state(start)}")
    assert block["state"] == "connected" and TOKEN not in str(block)
    with pytest.raises(OAuthStateError):
        service.linkedin_complete(f"{REDIRECT}?code=c1&state={_state(start)}")
    start = service.linkedin_connect()
    with pytest.raises(OAuthCancelledError):
        service.linkedin_complete(f"state={_state(start)}&error=user_cancelled_login")
    start = service.linkedin_connect()
    fake.add("POST", r"/oauth/v2/accessToken$", json_response(401, {"error": "invalid_request"}))
    with pytest.raises(OAuthExchangeError) as caught:
        service.linkedin_complete(f"code=c2&state={_state(start)}")
    assert caught.value.http_status == 502
    start = service.linkedin_connect()
    _script_exchange(fake, scope="openid profile")
    with pytest.raises(ConnectError, match="w_member_social"):
        service.linkedin_complete(f"code=c3&state={_state(start)}")


def _raw_get(port: int, path: str, host: str, address: str = "127.0.0.1") -> tuple[int, bytes, dict]:
    connection = http.client.HTTPConnection(address, port, timeout=5)
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", host)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    connection.close()
    return response.status, body, headers


def test_one_shot_listener():
    seen: list[str] = []
    listener = OneShotCallbackListener(0, state_ok=lambda s: seen.append(s) or s == "good-state")
    port = listener.start()
    try:
        if socket.has_ipv6:
            assert socket.AF_INET in listener.families
        status, body, headers = _raw_get(port, "/oauth/linkedin/callback?code=c&state=good-state", "evil.example")
        assert status == 400 and headers["Content-Security-Policy"] == "default-src 'none'"
        status, _, _ = _raw_get(port, "/other?code=c&state=good-state", f"localhost:{port}")
        assert status == 404
        status, _, _ = _raw_get(port, "/oauth/linkedin/callback?code=c&state=bad", f"127.0.0.1:{port}")
        assert status == 400
        status, body, headers = _raw_get(port, "/oauth/linkedin/callback?code=the-code&state=good-state", f"localhost:{port}")
        assert status == 200 and headers["Content-Type"].startswith("text/plain") and "터미널" in body.decode()
        assert listener.wait(1) == {"code": "the-code", "state": "good-state", "error": ""}
        if socket.AF_INET6 in listener.families:
            status, _, _ = _raw_get(port, "/oauth/linkedin/callback?code=c&state=good-state", f"[::1]:{port}", "::1")
            assert status == 200
    finally:
        listener.close()


def test_secret_filter_masks_codes_states_and_tokens(publish_kit, caplog):
    service, fake = _service(publish_kit)
    start = service.linkedin_connect()
    state = _state(start)
    _script_exchange(fake)
    service.linkedin_complete(f"code=code-value-777&state={state}")
    logger = get_logger("insia_agents.publishers.test")
    with caplog.at_level(logging.INFO, logger="insia_agents"):
        logger.info("callback code=%s state=%s token %s", "code-value-777", state, TOKEN)
        try:
            raise RuntimeError(f"boom with {TOKEN} and {state}")
        except RuntimeError:
            logger.exception("failed")
    text = caplog.text
    for secret in ("code-value-777", state, TOKEN):
        assert secret not in text
    assert "***" in text
