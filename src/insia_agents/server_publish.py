"""The server's API-publishing surface: every publishing route handler, and nothing else (DESIGN.md 6).

This is the only server-side module that imports ``insia_agents.publishers`` (AST guard:
``tests/test_no_autopublish_surface.py``). ``server.py`` wires it in with ``PUBLISH_ROUTES`` (added to its route
table), ``PublishHandlerMixin`` (inherited by ``InsiaHandler``) and ``build_publish_service`` (called by
``make_server``); the handlers here call the one ``PublishService`` of the process (``server.publish``).

Routes (JSON; errors ``{"error": "<Korean>", "status": N, "code": "<machine code>", …}``)::

    GET    /api/publish[?check=1]                         feature + per-platform readiness (no network unless check)
    PUT    /api/publish/linkedin/app                      Client ID / Secret / Redirect URI (409 when set by env)
    POST   /api/publish/linkedin/connect                  authorize URL; mode redirect (+ insia_oauth cookie) | paste
    POST   /api/publish/linkedin/complete {url}           finish with the pasted address-bar URL (this server's state)
    GET    /oauth/linkedin/callback                       no token: state + Lax cookie; always 303 to a fixed hash URL
    PUT    /api/publish/instagram/token {access_token}    check, refresh at once, store (Instagram beta only)
    DELETE /api/publish/<linkedin|instagram>[?forget_app=1]
    POST   /api/items/<id>/publish/preview {platform?, options}
    GET    /api/publish/previews/<pv>/slides/<n>.jpg      the staged JPEG the dashboard shows
    POST   /api/items/<id>/publish {preview_id, preview_hash, confirm: true}   human request only → 202
    GET    /api/items/<id>/publish                        the item's attempts
    GET    /api/publish/attempts/<pa>                     one attempt (polling)
    PUT    /api/publish/attempts/<pa>/permalink {permalink}
    POST   /api/publish/attempts/<pa>/resolve {outcome, url?}                  human request only
    POST   /api/publish/attempts/<pa>/check               Instagram read-only re-check
    GET    /pub/m/<32 hex>/<NN>.jpg                       main port, configuration B only (else 404, no body)

Rules kept here:

* **Human requests only** for the two routes that make a post exist (publish, resolve): ``_require_human`` —
  ``Sec-Fetch-Site: same-origin`` when the header is present, otherwise an ``Origin`` equal to this request's
  scheme://host[:port]; in token mode the ``insia_token`` cookie, never ``Authorization: Bearer`` (DESIGN.md 6-3).
  Only after it passed is a ``HumanConfirmation(via="dashboard")`` built — in ``_h_publish_item`` and
  ``_h_publish_resolve`` and nowhere else.
* The OAuth callback never renders HTML and never echoes a query value: ``303 See Other`` to
  ``/#/brand/connections/linkedin/<ok|cancelled|expired|exchange_failed|invalid>`` with
  ``Content-Security-Policy: default-src 'none'`` and friends; bad states are rate limited separately from logins.
* Access-log lines never hold an OAuth code/state (``/oauth/`` queries are cut) or a whole public media token;
  the server's own logger (500 tracebacks included) runs through the publishing ``SecretFilter``.
* ``INSIA_PUBLISH=0`` → ``GET /api/publish`` answers ``200 {"enabled": false, …, "platforms": {}}`` (so the
  dashboard draws no connection card) and every route that could reach a platform answers 409
  ``{"code": "disabled"}`` (the callback 303s to ``invalid``, ``/pub/m/`` is 404). The record-only routes keep
  working — ``GET /api/items/<id>/publish``, ``GET /api/publish/attempts/<pa>``, ``PUT …/permalink`` and
  ``POST …/resolve`` (still human requests only) — so an attempt left ``unknown`` before the switch was flipped
  can be answered and its item unlocked (``_publish(allow_disabled=True)``).
"""

from __future__ import annotations

import logging
import math
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

from .config import Settings
from .db import NotFoundError, Workspace
from .publishers import (
    CALLBACK_REDIRECT,
    CALLBACK_RESULTS,
    LINKEDIN_CALLBACK_PATH,
    MEDIA_PATH_PATTERN,
    OAUTH_COOKIE_NAME,
    OAUTH_COOKIE_PATH,
    OAUTH_STATE_TTL_SECONDS,
    PUBLISH_PLATFORMS,
    HumanConfirmation,
    InvalidInputError,
    NotHumanRequestError,
    NotPublishableError,
    OAuthStateError,
    PublishDisabledError,
    PublishError,
    PublishService,
    RateLimitedError,
    channel_platform,
    parse_preview_options,
)
from .publishers.redact import install_secret_filter, redact
from .publishers.settings import DEFAULT_SERVER_PORT

if TYPE_CHECKING:
    from .server import InsiaServer, RequestError

log = logging.getLogger(__name__)
# Registered secret values and secret-looking parameters never reach the server's log records (a 500 traceback
# included): logger filters only see records made on that logger, so both server loggers get one (DESIGN.md 1-4).
for _logger in (log, logging.getLogger("insia_agents.server")):
    install_secret_filter(_logger)

# Body limits (DESIGN.md 6-1).
PUBLISH_BODY = 4 * 1024
TOKEN_BODY = 8 * 1024

# Per client (IPv4 / IPv6 /64) and minute (DESIGN.md 6-4, 3-1). Each is its own LoginLimiter instance, so none of
# them can lock anyone out of the dashboard login.
PREVIEW_PER_MINUTE = 10
PUBLISH_PER_MINUTE = 5
OAUTH_BAD_STATE_PER_MINUTE = 10
MEDIA_404_PER_MINUTE = 30

OAUTH_PREFIX = "/oauth/"
MEDIA_PREFIX = "/pub/"
_MEDIA_PATH = re.compile(MEDIA_PATH_PATTERN)
_ID = r"([A-Za-z0-9][A-Za-z0-9._-]{0,120})"   # the same id segment as server.ID_SEGMENT
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_LOG_MEDIA_TOKEN = re.compile(r"(/pub/m/)([0-9a-fA-F]{6})[^/\s\"]*")

CALLBACK_BODY = "INSIA 대시보드로 돌아가요.\n".encode("utf-8")
CALLBACK_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
MEDIA_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
}

# (method, pattern, handler, auth). ``<id>`` becomes server.ID_SEGMENT; ``server._route`` anchors the pattern.
PUBLISH_ROUTES: tuple[tuple[str, str, str, bool], ...] = (
    ("GET", r"/api/publish", "publish_status", True),
    ("PUT", r"/api/publish/linkedin/app", "publish_linkedin_app", True),
    ("POST", r"/api/publish/linkedin/connect", "publish_linkedin_connect", True),
    ("POST", r"/api/publish/linkedin/complete", "publish_linkedin_complete", True),
    ("PUT", r"/api/publish/instagram/token", "publish_instagram_token", True),
    ("DELETE", r"/api/publish/(linkedin|instagram)", "publish_disconnect", True),
    ("GET", r"/api/publish/previews/<id>/slides/(\d{1,2})\.jpg", "publish_preview_slide", True),
    ("GET", r"/api/publish/attempts/<id>", "publish_attempt", True),
    ("PUT", r"/api/publish/attempts/<id>/permalink", "publish_permalink", True),
    ("POST", r"/api/publish/attempts/<id>/resolve", "publish_resolve", True),
    ("POST", r"/api/publish/attempts/<id>/check", "publish_check", True),
    ("POST", r"/api/items/<id>/publish/preview", "publish_preview", True),
    ("POST", r"/api/items/<id>/publish", "publish_item", True),
    ("GET", r"/api/items/<id>/publish", "publish_item_attempts", True),
)


def build_publish_service(settings: Settings, workspace: Workspace, *, public_hosts: tuple[str, ...] | list[str] = (),
                          server_port: int = DEFAULT_SERVER_PORT, media_port: int | None = None,
                          media_base_url: str | None = None, trust_proxy: bool = False) -> PublishService:
    """The server's ``PublishService``: environment (``INSIA_PUBLISH*``, ``INSIA_LINKEDIN_*``, ``INSIA_MEDIA_*`` …)
    + the server's own values, sharing the ``RunManager``'s workspace. Never raises for bad environment values
    (they become ``service.settings.warnings``). ``settings`` is the INSIA ``Settings`` (its ``home`` is the
    workspace's)."""
    del settings  # the workspace already carries the home; kept for the documented call shape
    return PublishService.from_env(workspace, public_hosts=tuple(public_hosts), server_port=int(server_port or 0),
                                   media_port=media_port, media_base_url=media_base_url, trust_proxy=bool(trust_proxy))


def _request_error(status: int, message: str, *, extra: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> "RequestError":
    # server.py imports this module, so its RequestError is looked up at call time (no import cycle).
    from .server import RequestError

    return RequestError(status, message, extra=extra, headers=headers)


def safe_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Error ``extra()`` fields for the JSON answer. ``status``/``error`` belong to the error envelope itself, so an
    attempt status travels as ``attempt_status``."""
    out: dict[str, Any] = {}
    for key, value in extra.items():
        if key == "status":
            out["attempt_status"] = value
        elif key != "error":
            out[key] = value
    return out


def redact_log_line(text: str) -> str:
    """An access-log line without OAuth codes/states, tokens or whole public media names (DESIGN.md 3-1, 4-2-3):
    registered secret values and secret-looking parameters are masked (``publishers.redact``), a public media
    name keeps its first 6 characters."""
    return redact(_LOG_MEDIA_TOKEN.sub(r"\1\2…", text))


def log_requestline(requestline: str) -> str:
    """``METHOD target VERSION`` for the access log: an ``/oauth/`` target loses its whole query (the OAuth code
    and state), whatever form the target has (``/oauth/…``, ``http://host/oauth/…``, ``//oauth/…``); then
    ``redact_log_line``."""
    method, sep, rest = requestline.partition(" ")
    if sep:
        target, sep2, version = rest.rpartition(" ") if " " in rest else (rest, "", "")
        before_query = target.split("?", 1)[0]
        try:
            path = urllib.parse.urlsplit(before_query).path
        except ValueError:
            path = before_query
        if "?" in target and (OAUTH_PREFIX in before_query or path.startswith(OAUTH_PREFIX)):
            requestline = f"{method} {before_query}{sep2}{version}"
    return redact_log_line(requestline)


def _bool_query(query: dict[str, list[str]], key: str) -> bool:
    values = query.get(key) or []
    return bool(values) and values[-1].strip().lower() in ("1", "true", "yes", "on")


def _str_field(body: dict[str, Any], key: str, *, label: str, limit: int, required: bool = False) -> str | None:
    """A string field of a request body. Messages name the field, never echo the value (it may be a secret)."""
    value = body.get(key)
    if value is None:
        if required:
            raise InvalidInputError(f"{label}({key})을(를) 보내 주세요.")
        return None
    if not isinstance(value, str):
        raise InvalidInputError(f"{label}({key})은(는) 문자열이어야 해요.")
    if len(value) > limit:
        raise InvalidInputError(f"{label}({key})이(가) 너무 길어요 (최대 {limit:,}자).")
    if required and not value.strip():
        raise InvalidInputError(f"{label}({key})을(를) 보내 주세요.")
    return value


def _only_keys(body: dict[str, Any], allowed: set[str]) -> None:
    """400 for unexpected fields. Only plain field names are repeated back (a pasted secret is not a field name)."""
    unknown = [str(key) for key in body if key not in allowed]
    if unknown:
        shown = sorted({key if _FIELD_NAME.fullmatch(key) else "…" for key in unknown})
        raise InvalidInputError(f"알 수 없는 항목이에요: {', '.join(shown)} (가능: {', '.join(sorted(allowed))})")


class PublishHandlerMixin:
    """Publishing handlers for ``server.InsiaHandler`` (which provides ``_read_json``, ``_send_json``,
    ``_check_host``, ``_cookie``, ``_forwarded_https``, ``_limiter_key``, ``headers`` …)."""

    server: "InsiaServer"

    # -- plumbing ---------------------------------------------------------------------
    def _publish(self, *, allow_disabled: bool = False) -> PublishService:
        """The process's service, or 409 ``disabled`` (``INSIA_PUBLISH=0`` / the fake mode was refused).
        ``allow_disabled`` — for the record-only routes (attempt history, polling, permalink, resolve), which never
        call a platform: an attempt left ``unknown`` must stay answerable after publishing was switched off."""
        service = getattr(self.server, "publish", None)
        if service is None:
            raise PublishDisabledError()
        if not service.settings.enabled and not allow_disabled:
            raise PublishDisabledError(service.settings.disabled_reason or None)
        return service

    @staticmethod
    def _map_publish_error(exc: BaseException) -> "RequestError | None":
        """``PublishError`` → its own status/``code``/``extra()`` (checked before every other mapping)."""
        if not isinstance(exc, PublishError):
            return None
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitedError) and exc.retry_after:
            headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return _request_error(exc.http_status, str(exc), extra={"code": exc.code, **safe_extra(exc.extra())},
                              headers=headers or None)

    def _dashboard_requester(self) -> str:
        return f"dashboard@{self._limiter_key()}"  # type: ignore[attr-defined]

    def _publish_rate(self, limiter: Any, what: str) -> None:
        wait = limiter.attempt(self._limiter_key())  # type: ignore[attr-defined]
        if wait:
            seconds = max(1, math.ceil(wait))
            raise _request_error(429, f"{what} 요청이 너무 많아요. {seconds}초 뒤에 다시 시도해 주세요.",
                                 extra={"code": "too_many"}, headers={"Retry-After": str(seconds)})

    def _request_origin(self) -> str:
        """scheme://host[:port] the dashboard was opened on (the ``Host`` of this same-origin request)."""
        host = (self.headers.get("Host") or "").strip().lower()  # type: ignore[attr-defined]
        if not host:
            return ""
        scheme = "https" if self._forwarded_https() else "http"  # type: ignore[attr-defined]
        return f"{scheme}://{host}"

    def _oauth_cookie(self, value: str, max_age: int) -> str:
        parts = [f"{OAUTH_COOKIE_NAME}={value}", f"Path={OAUTH_COOKIE_PATH}", f"Max-Age={max_age}", "HttpOnly",
                 "SameSite=Lax"]
        if self._forwarded_https():  # type: ignore[attr-defined]
            parts.append("Secure")
        return "; ".join(parts)

    def _require_human(self) -> None:
        """403 unless this request came from a person using the dashboard in a browser (DESIGN.md 6-3).

        1. ``Sec-Fetch-Site`` present → it must be ``same-origin``. Absent (browsers only send Fetch Metadata to
           secure origins; ``http://192.168.…`` LAN use has none) → ``Origin`` must be present and name exactly
           this request's scheme, host and port.
        2. Token mode: authenticated by the ``insia_token`` cookie; an ``Authorization: Bearer`` request (a
           script) is refused even with the right token.

        Headers can be forged by a program running as the same user (DESIGN.md 0-5): the goal is that nothing
        publishes by accident or through a routine script, not a proof of humanity.
        """
        from .server import COOKIE_NAME, split_host

        headers = self.headers  # type: ignore[attr-defined]
        fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site:
            if fetch_site != "same-origin":
                raise NotHumanRequestError()
        else:
            origin = (headers.get("Origin") or "").strip()
            scheme = "https" if self._forwarded_https() else "http"  # type: ignore[attr-defined]
            prefix = f"{scheme}://"
            default_port = 443 if scheme == "https" else 80
            if not origin.lower().startswith(prefix):
                raise NotHumanRequestError()
            given = split_host(origin[len(prefix):], default_port)
            requested = split_host(headers.get("Host") or "", default_port)
            if given is None or requested is None or given != requested:
                raise NotHumanRequestError()
        if self.server.token_required:
            scheme_name = (headers.get("Authorization") or "").strip().partition(" ")[0].lower()
            if scheme_name == "bearer":
                raise NotHumanRequestError()
            cookie = self._cookie(COOKIE_NAME)  # type: ignore[attr-defined]
            if not cookie or not self.server.check_cookie(cookie):
                raise NotHumanRequestError()

    def _item_detail(self, item_id: str) -> Any:
        detail = self.server.manager.workspace.get_item(item_id)
        if detail is None:
            raise NotFoundError(f"콘텐츠 {item_id}를 찾을 수 없어요")
        return detail

    # -- values other handlers add (never fail the whole request) -----------------------
    def _publish_health(self) -> dict[str, Any] | None:
        """``GET /api/health`` ``publish`` summary (``None`` if the service cannot answer)."""
        service = getattr(self.server, "publish", None)
        if service is None:
            return None
        try:
            return service.health_summary()
        except Exception:  # noqa: BLE001 - health must answer even if publishing is broken
            log.warning("API 게시 상태를 확인하지 못했어요", exc_info=True)
            return None

    def _publish_item_block(self, item: Any) -> dict[str, Any] | None:
        """``GET /api/items/<id>`` ``publish`` block; ``None`` = draw nothing (disabled, not configured, error)."""
        service = getattr(self.server, "publish", None)
        if service is None:
            return None
        try:
            agent = self.server.manager.agent_on_item(item.id, item.run_id)
            return service.item_block(item, agent_job=agent is not None)
        except Exception:  # noqa: BLE001 - the item itself must still load
            log.warning("콘텐츠 %s의 API 게시 상태를 확인하지 못했어요", item.id, exc_info=True)
            return None

    def _publish_log_requestline(self, requestline: str) -> str:
        """The request line for the access log: ``/oauth/`` without its query (also in the absolute form
        ``GET http://host/oauth/…``), media tokens cut to 6 characters, secrets masked."""
        return log_requestline(requestline)

    # -- public paths outside /api (no access token) ----------------------------------------
    def _dispatch_publish_public(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        """``/oauth/…`` (only the LinkedIn callback exists) and ``/pub/…`` (configuration B media) — never static
        files. Both check ``Host`` first, like every main-port ``/api`` route (DNS rebinding)."""
        self._check_host()  # type: ignore[attr-defined]
        if path.startswith(MEDIA_PREFIX):
            self._publish_media(method, path)
            return
        if path.rstrip("/") != LINKEDIN_CALLBACK_PATH:
            raise _request_error(404, "없는 주소예요.")
        if method != "GET":
            raise _request_error(405, "허용되지 않는 요청이에요", headers={"Allow": "GET"})
        self._publish_callback(query)

    def _publish_callback(self, query: dict[str, list[str]]) -> None:
        """``GET /oauth/linkedin/callback``: always a fixed 303, whatever happened (DESIGN.md 3-1)."""
        limiter = self.server.oauth_limiter
        key = self._limiter_key()  # type: ignore[attr-defined]
        wait = limiter.attempt(key)
        if wait:
            seconds = max(1, math.ceil(wait))
            self._send_fixed(429, "text/plain; charset=utf-8",
                             f"연결 시도가 너무 많아요. {seconds}초 뒤에 다시 시도해 주세요.\n".encode("utf-8"),
                             {**CALLBACK_HEADERS, "Retry-After": str(seconds)})
            return
        result = "invalid"
        service = getattr(self.server, "publish", None)
        if service is not None and service.settings.enabled:
            def first(name: str) -> str:
                values = query.get(name) or [""]
                value = values[0]
                return value if len(value) <= 4096 else ""

            try:
                result = service.linkedin_callback(code=first("code"), state=first("state"), error=first("error"),
                                                   cookie=self._cookie(OAUTH_COOKIE_NAME) or "")  # type: ignore[attr-defined]
            except PublishError:
                result = "invalid"
            except Exception as exc:  # noqa: BLE001 - the callback answers 303 no matter what; no values in the log
                log.error("LinkedIn 연결 콜백을 처리하지 못했어요 (%s)", type(exc).__name__)
                result = "exchange_failed"
        if result not in CALLBACK_RESULTS:
            result = "invalid"
        if result not in ("invalid", "expired"):
            limiter.release(key)  # only bad / expired states count toward the limit
        headers = {**CALLBACK_HEADERS, "Location": CALLBACK_REDIRECT.format(result=result),
                   "Set-Cookie": self._oauth_cookie("", 0)}
        self._send_fixed(303, "text/plain; charset=utf-8", CALLBACK_BODY, headers)

    def _publish_media(self, method: str, path: str) -> None:
        """Main-port ``/pub/m/<32 hex>/<NN>.jpg``: configuration B only (the media host is a ``--public-host`` and
        there is no media port). Anything else is a 404 without a body; 404s are rate limited per client."""
        if method not in ("GET", "HEAD"):
            self._send_fixed(405, "", b"", {"Allow": "GET, HEAD", **MEDIA_HEADERS})
            return
        service = getattr(self.server, "publish", None)
        target = None
        if (service is not None and service.settings.enabled and service.settings.media_mode == "main"
                and _MEDIA_PATH.fullmatch(path)):
            try:
                target = service.public_media_file(path)
            except Exception:  # noqa: BLE001 - an unreadable folder is simply not served
                log.warning("공개 이미지를 확인하지 못했어요", exc_info=True)
                target = None
        if target is None:
            wait = self.server.media_limiter.attempt(self._limiter_key())  # type: ignore[attr-defined]
            if wait:
                self._send_fixed(429, "", b"", {"Retry-After": "60", **MEDIA_HEADERS})
            else:
                self._send_fixed(404, "", b"", MEDIA_HEADERS)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._send_fixed(404, "", b"", MEDIA_HEADERS)
            return
        self._send_fixed(200, "image/jpeg", data, MEDIA_HEADERS)

    def _send_fixed(self, status: int, content_type: str, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)  # type: ignore[attr-defined]
        if content_type:
            self.send_header("Content-Type", content_type)  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))  # type: ignore[attr-defined]
        for name, value in headers.items():
            self.send_header(name, value)  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        if body and self.command != "HEAD":  # type: ignore[attr-defined]
            self.wfile.write(body)  # type: ignore[attr-defined]

    # -- status and connections ----------------------------------------------------------------
    def _h_publish_status(self, *, query: dict[str, list[str]]) -> None:
        """``GET /api/publish``. The one publishing route that still answers when the feature is off: ``enabled:
        false`` tells the dashboard to draw no connection card at all (nothing else runs, no platform block)."""
        service = getattr(self.server, "publish", None)
        settings = getattr(service, "settings", None)
        if service is None or not getattr(settings, "enabled", False):
            reason = getattr(settings, "disabled_reason", "") or PublishDisabledError().args[0]
            media = settings.media_json() if hasattr(settings, "media_json") else {
                "url": "", "mode": "none", "port": None, "valid": False, "reason": ""}
            self._send_json(200, {"enabled": False, "configured": False, "fake": False,  # type: ignore[attr-defined]
                                  "reason": reason, "media": media, "platforms": {}})
            return
        self._send_json(200, service.status(check=_bool_query(query, "check")))  # type: ignore[attr-defined]

    def _h_publish_linkedin_app(self, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        body = self._read_json(PUBLISH_BODY, required=True, empty_message="LinkedIn 앱 정보를 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"client_id", "client_secret", "redirect_uri"})
        payload = service.save_linkedin_app(
            client_id=_str_field(body, "client_id", label="Client ID", limit=200),
            client_secret=_str_field(body, "client_secret", label="Client Secret", limit=500),
            redirect_uri=_str_field(body, "redirect_uri", label="Redirect URI", limit=2000))
        self._send_json(200, payload)  # type: ignore[attr-defined]

    def _h_publish_linkedin_connect(self, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        self._read_json(PUBLISH_BODY)  # type: ignore[attr-defined]
        start = service.linkedin_connect(request_origin=self._request_origin())
        headers: dict[str, str] = {}
        if start.mode == "redirect" and start.cookie_value:
            if _COOKIE_VALUE.fullmatch(start.cookie_value):
                headers["Set-Cookie"] = self._oauth_cookie(start.cookie_value, OAUTH_STATE_TTL_SECONDS)
            else:  # pragma: no cover - the service makes hex HMACs
                log.error("OAuth 쿠키 값의 형식이 올바르지 않아 쿠키를 보내지 않았어요")
        self._send_json(200, start.to_json(), headers)  # type: ignore[attr-defined]

    def _h_publish_linkedin_complete(self, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        body = self._read_json(PUBLISH_BODY, required=True, empty_message="LinkedIn에서 돌아온 주소를 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"url"})
        url = _str_field(body, "url", label="붙여 넣은 주소", limit=4000, required=True) or ""
        limiter = self.server.oauth_limiter
        key = self._limiter_key()  # type: ignore[attr-defined]
        wait = limiter.attempt(key)
        if wait:
            seconds = max(1, math.ceil(wait))
            raise _request_error(429, f"연결 시도가 너무 많아요. {seconds}초 뒤에 다시 시도해 주세요.",
                                 extra={"code": "too_many"}, headers={"Retry-After": str(seconds)})
        try:
            payload = service.linkedin_complete(url.strip())
        except OAuthStateError:
            raise  # a state this server did not issue (or used/expired) stays counted
        except BaseException:
            limiter.release(key)
            raise
        limiter.release(key)
        self._send_json(200, payload)  # type: ignore[attr-defined]

    def _h_publish_instagram_token(self, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        body = self._read_json(TOKEN_BODY, required=True, empty_message="인스타그램 토큰을 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"access_token"})
        token = _str_field(body, "access_token", label="인스타그램 토큰", limit=4096, required=True) or ""
        self._send_json(200, service.save_instagram_token(token.strip()))  # type: ignore[attr-defined]

    def _h_publish_disconnect(self, platform: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        payload = service.disconnect(platform, forget_app=_bool_query(query, "forget_app"))  # type: ignore[arg-type]
        self._send_json(200, payload)  # type: ignore[attr-defined]

    # -- previews and publishing -----------------------------------------------------------------
    def _publish_platform(self, raw: Any, detail: Any) -> str:
        if raw is not None:
            if not isinstance(raw, str) or raw not in PUBLISH_PLATFORMS:
                raise InvalidInputError("platform은 linkedin 또는 instagram이어야 해요.")
            return raw
        platform = channel_platform(detail.item.channel)
        if platform is None:
            raise NotPublishableError("네이버 블로그·사업계획서는 API로 게시하지 않아요. 파일을 받아 직접 올린 뒤 "
                                      "‘게시 완료 표시’를 눌러 주세요.", blocked_by="")
        return platform

    def _refuse_during_agent_work(self, detail: Any) -> None:
        agent = self.server.manager.agent_on_item(detail.item.id, detail.item.run_id)
        if agent is not None:
            raise _request_error(409, f"에이전트가 이 콘텐츠를 작업하는 중이에요 (실행 {agent.run_id}). 끝난 뒤 새 버전을 "
                                      "확인하고 다시 승인해 주세요.",
                                 extra={"code": "agent_job", "run_id": agent.run_id, "item_id": detail.item.id})

    def _h_publish_preview(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        self._publish_rate(self.server.preview_limiter, "미리보기")
        body = self._read_json(PUBLISH_BODY)  # type: ignore[attr-defined]
        _only_keys(body, {"platform", "options"})
        detail = self._item_detail(item_id)
        platform = self._publish_platform(body.get("platform"), detail)
        # 400 before anything is rendered: Instagram needs an explicit AI-label choice (DESIGN.md 14.2)
        options = parse_preview_options(platform, body.get("options"))
        self._refuse_during_agent_work(detail)
        result = service.preview(item_id, platform=platform, options=options.to_json(),  # type: ignore[arg-type]
                                 via="dashboard", requested_by=self._dashboard_requester())
        self._send_json(200, result.to_json())  # type: ignore[attr-defined]

    def _h_publish_preview_slide(self, preview_id: str, number: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        n = int(number)
        if not 1 <= n <= 10:
            raise NotFoundError("없는 슬라이드예요.")
        data = service.preview_slide(preview_id, n)
        self._send_fixed(200, "image/jpeg", data, {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                                    "Content-Security-Policy": "default-src 'none'"})

    def _h_publish_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        """``POST /api/items/<id>/publish``: the one route that makes a post exist (with ``_h_publish_resolve``)."""
        service = self._publish()
        self._require_human()
        self._publish_rate(self.server.publish_limiter, "게시")
        body = self._read_json(PUBLISH_BODY, required=True, empty_message="확인한 미리보기(preview_id, preview_hash)를 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"platform", "preview_id", "preview_hash", "confirm"})
        if body.get("confirm") is not True:
            raise InvalidInputError("게시될 내용과 계정을 확인했다는 표시(confirm: true)가 있어야 게시할 수 있어요.")
        preview_id = _str_field(body, "preview_id", label="미리보기 id", limit=100, required=True) or ""
        preview_hash = _str_field(body, "preview_hash", label="미리보기 해시", limit=100, required=True) or ""
        detail = self._item_detail(item_id)
        platform = body.get("platform")
        if platform is not None and (not isinstance(platform, str) or platform not in PUBLISH_PLATFORMS):
            raise InvalidInputError("platform은 linkedin 또는 instagram이어야 해요.")
        confirmation = HumanConfirmation(via="dashboard", requested_by=self._dashboard_requester(),
                                         preview_id=preview_id.strip(), preview_hash=preview_hash.strip())
        # 409 while an agent job / the item's own run works on it; jobs and edits wait for this start in turn
        with self.server.manager.publishing(item_id, detail.item.run_id):
            attempt = service.send(confirmation, item_id=item_id, platform=platform, background=True)
        self._send_json(202, {"attempt": service.attempt_json(attempt),  # type: ignore[attr-defined]
                              "poll_url": f"/api/publish/attempts/{attempt.id}"})

    def _h_publish_item_attempts(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish(allow_disabled=True)
        self._item_detail(item_id)
        attempts = service.list_attempts(item_id=item_id, limit=50)
        self._send_json(200, {"item_id": item_id,  # type: ignore[attr-defined]
                              "attempts": [service.attempt_json(attempt) for attempt in attempts]})

    # -- attempts ----------------------------------------------------------------------------------
    def _h_publish_attempt(self, attempt_id: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish(allow_disabled=True)
        self._send_json(200, {"attempt": service.attempt_json(service.get_attempt(attempt_id))})  # type: ignore[attr-defined]

    def _h_publish_permalink(self, attempt_id: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish(allow_disabled=True)
        body = self._read_json(PUBLISH_BODY, required=True, empty_message="게시물 주소(permalink)를 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"permalink"})
        url = _str_field(body, "permalink", label="게시물 주소", limit=2000, required=True) or ""
        attempt = service.set_permalink(attempt_id, url.strip(), by=self._dashboard_requester())
        self._send_json(200, {"attempt": service.attempt_json(attempt)})  # type: ignore[attr-defined]

    def _h_publish_resolve(self, attempt_id: str, *, query: dict[str, list[str]]) -> None:
        """``POST /api/publish/attempts/<pa>/resolve``: a person says whether an ``unknown`` attempt went out (also
        while publishing is off: nothing is sent, only the record and the item change)."""
        service = self._publish(allow_disabled=True)
        self._require_human()
        body = self._read_json(PUBLISH_BODY, required=True, empty_message="결과(outcome)를 보내 주세요")  # type: ignore[attr-defined]
        _only_keys(body, {"outcome", "url"})
        outcome = body.get("outcome")
        if outcome not in ("published", "not_published"):
            raise InvalidInputError("outcome은 published(올라갔어요) 또는 not_published(안 올라갔어요)여야 해요.")
        url = _str_field(body, "url", label="게시물 주소", limit=2000) or ""
        if outcome == "not_published" and url.strip():
            raise InvalidInputError("안 올라갔다면 게시물 주소(url)는 보내지 않아요.")
        confirmation = HumanConfirmation(via="dashboard", requested_by=self._dashboard_requester(),
                                         preview_id=attempt_id, preview_hash="")
        attempt, item = service.resolve(confirmation, outcome, url=url.strip())
        self._send_json(200, {"attempt": service.attempt_json(attempt),  # type: ignore[attr-defined]
                              "item": item.model_dump(mode="json") if item is not None else None})

    def _h_publish_check(self, attempt_id: str, *, query: dict[str, list[str]]) -> None:
        service = self._publish()
        self._read_json(PUBLISH_BODY)  # type: ignore[attr-defined]
        attempt = service.check_attempt(attempt_id)
        self._send_json(200, {"attempt": service.attempt_json(attempt)})  # type: ignore[attr-defined]


__all__ = [
    "MEDIA_PREFIX", "OAUTH_PREFIX", "PUBLISH_ROUTES", "PublishHandlerMixin", "build_publish_service",
    "log_requestline", "redact_log_line", "safe_extra",
]
