"""LinkedIn OAuth 2.0 (3-legged authorization code) for connecting an account (DESIGN.md 3-1, 3-2, 3-4; LI §3).

* ``OAuthStateStore`` — one-time states in memory (256-bit ``token_urlsafe(32)``, 10 minutes, only their sha256
  is kept) plus the per-process HMAC key for the ``insia_oauth`` cookie that binds a redirect-mode state to the
  browser that asked for it. A restart forgets every pending state ("만료됐어요, 다시 연결해 주세요").
* ``authorize_url`` / ``exchange_code`` / ``fetch_userinfo`` — the exact LinkedIn requests (LI §3.1, §3.3, §4),
  built on a small ``OAuthProvider`` description so a stage-2 Instagram login can reuse the structure (DESIGN.md
  3-6; not wired in v1).
* ``parse_pasted_callback`` — the dashboard/CLI "paste the address bar" path: a full callback URL or just
  ``code=…&state=…``; only ``code``/``state``/``error`` are read.
* ``OneShotCallbackListener`` — the CLI's temporary callback server: the callback path only, on ``127.0.0.1`` and
  (when possible) ``::1`` on the same port, Host must be a loopback name with that port, fixed ``text/plain``
  answers with ``Content-Security-Policy: default-src 'none'``; closes after the first valid callback.

Codes and states are never logged. They are registered with ``redact`` only once they check out (a state this
process issued, a code whose state was just used up): a string from a stranger's request never joins the register.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

from .base import (
    LINKEDIN_CALLBACK_PATH,
    OAUTH_STATE_TTL_SECONDS,
    Clock,
    InvalidInputError,
    OAuthCancelledError,
    OAuthExchangeError,
    OAuthStateError,
    SystemClock,
)
from .http import Transport, TransportError, first_record
from .redact import get_logger, register_secret
from .settings import LOOPBACK_NAMES

log = get_logger(__name__)


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    scope_separator: str = " "


LINKEDIN = OAuthProvider(
    name="linkedin",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    token_url="https://www.linkedin.com/oauth/v2/accessToken",
    scopes=("openid", "profile", "w_member_social"),
)
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
LINKEDIN_PUBLISH_SCOPE = "w_member_social"
CANCEL_ERRORS = frozenset({"user_cancelled_login", "user_cancelled_authorize", "access_denied"})

StateCheck = Literal["ok", "unknown", "expired", "used"]


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class OAuthStateStore:
    """Pending OAuth states of this process (see the module docstring)."""

    def __init__(self, clock: Clock | None = None, *, ttl_seconds: int = OAUTH_STATE_TTL_SECONDS) -> None:
        self.clock = clock or SystemClock()
        self.ttl = timedelta(seconds=ttl_seconds)
        self._key = secrets.token_bytes(32)
        self._states: dict[str, tuple[datetime, bool]] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        state = secrets.token_urlsafe(32)
        register_secret(state)
        now = self.clock.now()
        with self._lock:
            for digest in [d for d, (created, _used) in self._states.items() if now - created > self.ttl * 2]:
                del self._states[digest]
            self._states[_digest(state)] = (now, False)
        return state

    def cookie_value(self, state: str) -> str:
        return hmac.new(self._key, state.encode("utf-8"), hashlib.sha256).hexdigest()

    def cookie_matches(self, state: str, cookie: str) -> bool:
        return bool(state) and bool(cookie) and hmac.compare_digest(self.cookie_value(state), str(cookie))

    def check(self, state: str) -> StateCheck:
        """Look at a state without using it up."""
        if not state:
            return "unknown"
        with self._lock:
            entry = self._states.get(_digest(state))
        if entry is None:
            return "unknown"
        created, used = entry
        if used:
            return "used"
        if self.clock.now() - created > self.ttl:
            return "expired"
        return "ok"

    def consume(self, state: str) -> StateCheck:
        """Use a state once: ``ok`` the first time within 10 minutes, else ``unknown``/``expired``/``used``."""
        if not state:
            return "unknown"
        digest = _digest(state)
        with self._lock:
            entry = self._states.get(digest)
            if entry is None:
                return "unknown"
            register_secret(state)  # one this process issued (never a stranger's string)
            created, used = entry
            if used:
                return "used"
            self._states[digest] = (created, True)
            if self.clock.now() - created > self.ttl:
                return "expired"
            return "ok"


def authorize_url(provider: OAuthProvider, client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "scope": provider.scope_separator.join(provider.scopes),
    }, quote_via=urllib.parse.quote)
    return f"{provider.authorize_url}?{query}"


def exchange_code(transport: Transport, provider: OAuthProvider, *, code: str, client_id: str, client_secret: str,
                  redirect_uri: str) -> dict[str, Any]:
    """``POST`` the authorization code (form-encoded, LI §3.3). Returns ``{"access_token", "expires_in", "scope"}``.
    ``OAuthExchangeError`` (502) for any failure; the platform's body is never included."""
    register_secret(code)
    register_secret(client_secret, pin=True)
    try:
        response = transport.request("POST", provider.token_url, form={
            "grant_type": "authorization_code", "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        })
    except TransportError:
        raise OAuthExchangeError("LinkedIn에 연결하지 못했어요. 인터넷 연결을 확인한 뒤 다시 연결해 주세요.",
                                 platform=provider.name) from None
    body = first_record(response.json())
    if response.status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        code_name = body.get("error") if isinstance(body, dict) else ""
        log.warning("%s 코드 교환 실패: HTTP %s %s", provider.name, response.status, str(code_name or "")[:40])
        raise OAuthExchangeError(platform=provider.name)
    token = str(body["access_token"])
    register_secret(token, pin=True)
    try:
        expires_in = int(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return {"access_token": token, "expires_in": expires_in, "scope": str(body.get("scope") or "")}


def fetch_userinfo(transport: Transport, token: str) -> tuple[int, dict[str, Any]]:
    """``GET /v2/userinfo`` with the member token: ``(status, body)``. ``TransportError`` propagates."""
    response = transport.request("GET", LINKEDIN_USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
    body = response.json()
    return response.status, body if isinstance(body, dict) else {}


def parse_pasted_callback(text: str, redirect_uri: str) -> dict[str, str]:
    """Read ``code``/``state``/``error`` from a pasted address-bar URL (``http(s)``, same path as the redirect URI)
    or from a bare ``code=…&state=…`` string. ``InvalidInputError`` (400) otherwise."""
    value = (text or "").strip()
    if not value or len(value) > 4000:
        raise InvalidInputError("LinkedIn에서 돌아온 페이지의 주소창 주소 전체를 붙여 넣어 주세요.")
    if value.startswith(("http://", "https://")):
        try:
            parts = urllib.parse.urlsplit(value)
            expected = urllib.parse.urlsplit(redirect_uri) if redirect_uri else None
        except ValueError:
            raise InvalidInputError("붙여 넣은 주소 형식이 올바르지 않아요.") from None
        expected_path = (expected.path if expected else "") or LINKEDIN_CALLBACK_PATH
        if parts.path.rstrip("/") != expected_path.rstrip("/"):
            raise InvalidInputError("LinkedIn 연결 주소가 아니에요. 동의한 뒤 이동한 페이지의 주소창 주소를 그대로 붙여 넣어 주세요.")
        query = parts.query
    elif "://" in value:
        raise InvalidInputError("http:// 또는 https:// 주소만 붙여 넣을 수 있어요.")
    else:
        query = value.lstrip("?")
    params = urllib.parse.parse_qs(query, keep_blank_values=False)
    out = {key: (params.get(key) or [""])[0] for key in ("code", "state", "error")}
    # nothing is registered yet: ``check_callback`` registers the state and code once the state checks out
    if not out["state"] or (not out["code"] and not out["error"]):
        raise InvalidInputError("주소에 연결 정보(code·state)가 없어요. 동의한 뒤 이동한 페이지의 주소창 주소 전체를 붙여 넣어 주세요.")
    return out


def check_callback(states: OAuthStateStore, *, state: str, code: str, error: str, platform: str = "linkedin") -> None:
    """Validate a pasted/CLI callback (no cookie): state issued here, unused, ≤ 10 min; then the ``error`` param.
    Raises ``OAuthStateError`` / ``OAuthCancelledError`` / ``OAuthExchangeError``."""
    result = states.consume(state)  # registers the state only when this process issued it
    if result != "ok":
        raise OAuthStateError(platform=platform)
    if error:
        if error in CANCEL_ERRORS:
            raise OAuthCancelledError(platform=platform)
        raise OAuthExchangeError(platform=platform)
    if not code:
        raise OAuthStateError(platform=platform)
    register_secret(code)


# ---------------------------------------------------------------------------
# CLI one-time callback listener
# ---------------------------------------------------------------------------

LISTENER_OK_TEXT = "LinkedIn 연결 정보를 받았어요. 이 창을 닫고 터미널로 돌아가 주세요.\n"
LISTENER_BAD_TEXT = "연결 요청이 올바르지 않아요. 터미널의 안내를 따라 다시 시도해 주세요.\n"
_LISTENER_HEADERS = (("Content-Type", "text/plain; charset=utf-8"), ("Content-Security-Policy", "default-src 'none'"),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"))


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "INSIA-connect"
    sys_version = ""

    def _answer(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        for name, value in _LISTENER_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        owner: OneShotCallbackListener = self.server.owner  # type: ignore[attr-defined]
        host_header = (self.headers.get("Host") or "").strip().lower()
        if not owner.host_ok(host_header):
            self._answer(400, LISTENER_BAD_TEXT)
            return
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != owner.path:
            self._answer(404, LISTENER_BAD_TEXT)
            return
        params = urllib.parse.parse_qs(parts.query)
        got = {key: (params.get(key) or [""])[0] for key in ("code", "state", "error")}
        if not got["state"] or (owner.state_ok is not None and not owner.state_ok(got["state"])):
            self._answer(400, LISTENER_BAD_TEXT)  # a stranger's values are never registered
            return
        register_secret(got["code"])
        register_secret(got["state"])
        owner.deliver(got)
        self._answer(200, LISTENER_OK_TEXT)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - never log the query (code, state)
        return


class _ListenerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], family: int, owner: "OneShotCallbackListener") -> None:
        self.address_family = family
        self.owner = owner
        super().__init__(address, _CallbackHandler)


class OneShotCallbackListener:
    """``insia publish connect linkedin`` without a dashboard server (DESIGN.md 3-4).

    ``start()`` binds ``127.0.0.1:<port>`` (``OSError`` when the port is taken — e.g. the dashboard server runs)
    and, when possible, ``[::1]:<port>``. ``wait(timeout)`` returns ``{"code", "state", "error"}`` of the first
    callback whose state ``state_ok`` accepts (or ``None`` after ``timeout``); ``close()`` stops both sockets.
    """

    def __init__(self, port: int, *, path: str = LINKEDIN_CALLBACK_PATH,
                 state_ok: Callable[[str], bool] | None = None) -> None:
        self.port = int(port)
        self.path = path
        self.state_ok = state_ok
        self._servers: list[_ListenerServer] = []
        self._threads: list[threading.Thread] = []
        self._result: dict[str, str] | None = None
        self._event = threading.Event()

    def start(self) -> int:
        first = _ListenerServer(("127.0.0.1", self.port), socket.AF_INET, self)
        self._servers.append(first)
        self.port = int(first.server_address[1])
        if socket.has_ipv6:
            try:
                self._servers.append(_ListenerServer(("::1", self.port), socket.AF_INET6, self))
            except OSError:
                pass
        for server in self._servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                      name="insia-oauth-callback", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self.port

    @property
    def families(self) -> list[int]:
        return [server.address_family for server in self._servers]

    def host_ok(self, host_header: str) -> bool:
        if not host_header:
            return False
        if host_header.startswith("["):
            name, _, rest = host_header[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
        else:
            name, _, port = host_header.partition(":")
        return name in LOOPBACK_NAMES and port == str(self.port)

    def deliver(self, result: dict[str, str]) -> None:
        if self._result is None:
            self._result = dict(result)
        self._event.set()

    def wait(self, timeout: float) -> dict[str, str] | None:
        self._event.wait(timeout)
        return dict(self._result) if self._result is not None else None

    def close(self) -> None:
        for server in self._servers:
            try:
                server.shutdown()
            finally:
                server.server_close()
        self._servers.clear()

    def __enter__(self) -> "OneShotCallbackListener":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = [
    "CANCEL_ERRORS", "LINKEDIN", "LINKEDIN_PUBLISH_SCOPE", "LINKEDIN_USERINFO_URL", "LISTENER_OK_TEXT",
    "OAuthProvider", "OAuthStateStore", "OneShotCallbackListener", "authorize_url", "check_callback", "exchange_code",
    "fetch_userinfo", "parse_pasted_callback",
]
