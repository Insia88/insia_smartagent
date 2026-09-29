"""HTTP transport for the publishing package (DESIGN.md 1-4).

* ``Transport`` — the one protocol every platform module calls. 4xx/5xx come back as a ``Response`` (never an
  exception); interpreting them is the platform module's job. Failures raise ``TransportError`` with
  ``sent="no"`` (nothing left this machine: DNS, refused connection, a host outside the allow-list) or
  ``sent="maybe"`` (the request may have reached the platform: timeout or dropped connection after sending).
* ``FakeTransport`` — the scripted fake used by tests (working). Unscripted requests raise ``AssertionError``,
  which is also how a test proves "no network was used".
* ``UrllibTransport`` — the real transport (standard library only, no redirects, host allow-list).
* ``FakePlatformTransport`` — the in-memory LinkedIn/Instagram of the fake mode (``INSIA_PUBLISH_FAKE=1``).
* ``first_record()`` — Instagram answers some calls wrapped in ``{"data": [{…}]}`` and some flat (IG §0-3/§0-4);
  every response parse goes through it.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Union

from .. import __version__

USER_AGENT = f"INSIA-SmartAgent/{__version__}"

# Hosts the real transport may call (scheme https only). The media self-check adds the one public media host.
LINKEDIN_HOSTS: tuple[str, ...] = ("www.linkedin.com", "api.linkedin.com")
INSTAGRAM_HOSTS: tuple[str, ...] = ("graph.instagram.com", "api.instagram.com")

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
IG_CONTAINER_TIMEOUT = 60.0     # Instagram container creation waits while Meta fetches the image

# Recorded requests mask these (header ``Authorization`` keeps only its scheme: "Bearer ***").
SECRET_FIELDS: frozenset[str] = frozenset({"access_token", "client_secret", "code"})
MASK = "***"
_URL_SECRET = re.compile(r"(?i)([?&](?:access_token|client_secret|code)=)[^&#]*")


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)   # lower-case keys
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", {str(k).lower(): str(v) for k, v in dict(self.headers).items()})
        if isinstance(self.body, str):
            object.__setattr__(self, "body", self.body.encode("utf-8"))

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower(), default)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The decoded JSON body, or ``None`` when the body is empty or not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


def json_response(status: int, body: Any = None, headers: Mapping[str, str] | None = None) -> Response:
    """A JSON ``Response`` (handy for scripting ``FakeTransport``)."""
    merged = {"content-type": "application/json"}
    merged.update({str(k).lower(): str(v) for k, v in (headers or {}).items()})
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    return Response(status=status, headers=merged, body=payload)


class TransportError(Exception):
    """The request did not produce a response.

    ``sent="no"``: it never left this machine (DNS failure, refused connection, host not allowed, bad scheme) —
    safe to report "nothing was published". ``sent="maybe"``: it may have reached the platform (timeout or a
    dropped connection after sending) — after ``claim_write`` this means the outcome is unknown.
    """

    def __init__(self, message: str, *, sent: Literal["no", "maybe"]) -> None:
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None, timeout: float = READ_TIMEOUT) -> Response:
        """Send one request. ``params`` go in the query string, ``form`` as ``application/x-www-form-urlencoded``,
        ``json_body`` as ``application/json``, ``data`` as raw bytes (at most one body kind). 4xx/5xx are
        returned, not raised. Raises ``TransportError`` only when no response was received."""
        ...


def first_record(body: Any) -> Any:
    """``{"data": [{…}, …]}`` → the first record; anything else is returned unchanged (IG §0-3/§0-4)."""
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    return body


# ---------------------------------------------------------------------------
# Fake transport (tests, and the base of the fake platform mode)
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """One request seen by ``FakeTransport`` (secrets masked unless the fake was made with ``redact=False``)."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)   # lower-case keys
    params: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    data: bytes | None = None
    timeout: float = READ_TIMEOUT


# A scripted answer: a Response, an exception instance to raise (e.g. TransportError(..., sent="maybe")),
# or a callable that receives the RecordedRequest and returns either of those.
FakeAnswer = Union[Response, BaseException, Callable[[RecordedRequest], Union[Response, BaseException]]]


def _mask_mapping(values: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(k): (MASK if str(k).lower() in SECRET_FIELDS else v) for k, v in (values or {}).items()}


def _mask_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key).lower()
        if name == "authorization":
            scheme = str(value).split(" ", 1)[0] if " " in str(value) else ""
            out[name] = f"{scheme} {MASK}".strip()
        else:
            out[name] = str(value)
    return out


def _mask_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if str(k).lower() in SECRET_FIELDS else _mask_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    return value


class _Rule:
    def __init__(self, method: str, pattern: str, answers: Iterable[FakeAnswer], repeat: bool) -> None:
        self.method = method.upper()
        self.pattern = pattern
        self.regex = re.compile(pattern)
        self.answers: deque[FakeAnswer] = deque(answers)
        self.repeat = repeat

    def matches(self, method: str, url: str) -> bool:
        return bool(self.answers) and self.method == method and self.regex.search(url) is not None

    def take(self) -> FakeAnswer:
        if self.repeat and len(self.answers) == 1:
            return self.answers[0]
        return self.answers.popleft()


class FakeTransport:
    """Scripted fake transport: ``(method, URL regex) → answers in order``.

    Example::

        fake = FakeTransport()
        fake.add("POST", r"/rest/posts$", Response(201, {"x-restli-id": "urn:li:share:1"}))
        fake.add("GET", r"/v25\\.0/\\d+$", json_response(200, {"status_code": "IN_PROGRESS"}),
                 json_response(200, {"status_code": "FINISHED"}), repeat=True)
        fake.add("POST", r"/media_publish$", TransportError("timed out", sent="maybe"))

    * Rules are tried in the order they were added; the pattern is matched with ``re.search`` against the ``url``
      argument (the query from ``params`` is not part of it). A rule is used up when its answers run out, unless
      ``repeat=True`` (the last answer then keeps answering — handy for polling).
    * Every request is appended to ``requests`` first (thread-safe). With ``redact=True`` (default) the header
      ``Authorization`` becomes ``"Bearer ***"`` and ``access_token`` / ``client_secret`` / ``code`` in params,
      form, JSON bodies and the URL query become ``"***"``; pass ``redact=False`` to assert on real values.
    * A request no rule answers raises ``AssertionError`` (so ``FakeTransport()`` with no rules proves that no
      request was made at all).
    """

    def __init__(self, *, redact: bool = True) -> None:
        self.redact = redact
        self.requests: list[RecordedRequest] = []
        self._rules: list[_Rule] = []
        self._lock = threading.Lock()

    def add(self, method: str, url_pattern: str, *answers: FakeAnswer, repeat: bool = False) -> "FakeTransport":
        """Script answers for ``method`` + ``url_pattern``; returns ``self`` for chaining."""
        if not answers:
            raise ValueError("FakeTransport.add needs at least one answer")
        with self._lock:
            self._rules.append(_Rule(method, url_pattern, answers, repeat))
        return self

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None, timeout: float = READ_TIMEOUT) -> Response:
        method = method.upper()
        if self.redact:
            record = RecordedRequest(method=method, url=_URL_SECRET.sub(lambda m: m.group(1) + MASK, url),
                                     headers=_mask_headers(headers), params=_mask_mapping(params),
                                     form=_mask_mapping(form), json_body=_mask_json(json_body), data=data,
                                     timeout=timeout)
        else:
            record = RecordedRequest(method=method, url=url, headers={str(k).lower(): str(v) for k, v in (headers or {}).items()},
                                     params=dict(params or {}), form=dict(form or {}), json_body=json_body, data=data,
                                     timeout=timeout)
        with self._lock:
            self.requests.append(record)
            rule = next((r for r in self._rules if r.matches(method, url)), None)
            if rule is None:
                raise AssertionError(f"FakeTransport: unscripted request {method} {record.url}")
            answer = rule.take()
        if callable(answer) and not isinstance(answer, (Response, BaseException)):
            answer = answer(record)
        if isinstance(answer, BaseException):
            raise answer
        if not isinstance(answer, Response):
            raise AssertionError(f"FakeTransport: a scripted answer must be a Response or an exception, got {answer!r}")
        return answer

    def calls(self, method: str | None = None, url_pattern: str | None = None) -> list[RecordedRequest]:
        """Recorded requests, optionally filtered by method and URL regex (``re.search``)."""
        regex = re.compile(url_pattern) if url_pattern else None
        with self._lock:
            return [r for r in self.requests
                    if (method is None or r.method == method.upper()) and (regex is None or regex.search(r.url))]

    def pending(self) -> list[tuple[str, str, int]]:
        """Rules with answers left: ``(method, pattern, remaining)``; ``repeat`` rules count while > 1 answer left."""
        with self._lock:
            return [(r.method, r.pattern, len(r.answers)) for r in self._rules
                    if r.answers and not (r.repeat and len(r.answers) == 1)]

    def assert_done(self) -> None:
        """Fail when a scripted answer was never used (the code under test skipped a request)."""
        left = self.pending()
        if left:
            raise AssertionError(f"FakeTransport: unused scripted answers: {left}")


# ---------------------------------------------------------------------------
# Real transport
# ---------------------------------------------------------------------------

MAX_RESPONSE_BYTES = 10_000_000     # API answers are small; the media self-check reads one JPEG (≤ 8 MB)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx comes back as a Response (urllib would copy Authorization to the new host)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib signature
        return None


class _Probe:
    """Tracks whether the connection was established (after that, a failure means the request may have been sent)."""

    def __init__(self) -> None:
        self.connected = False


def _connection_class(base: type[http.client.HTTPConnection], probe: _Probe, connect_timeout: float):
    class _Connection(base):  # type: ignore[misc, valid-type]
        def connect(self) -> None:
            read_timeout = self.timeout
            self.timeout = connect_timeout
            try:
                super().connect()
            finally:
                self.timeout = read_timeout
            if self.sock is not None:
                self.sock.settimeout(read_timeout)
            probe.connected = True

    return _Connection


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, probe: _Probe, connect_timeout: float) -> None:
        super().__init__(context=ssl.create_default_context())
        self._conn_class = _connection_class(http.client.HTTPSConnection, probe, connect_timeout)

    def https_open(self, req):  # noqa: ANN001
        return self.do_open(self._conn_class, req, context=self._context)


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, probe: _Probe, connect_timeout: float) -> None:
        super().__init__()
        self._conn_class = _connection_class(http.client.HTTPConnection, probe, connect_timeout)

    def http_open(self, req):  # noqa: ANN001
        return self.do_open(self._conn_class, req)


def _read_limited(stream: Any) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1) if stream is not None else b""
    if len(data) > MAX_RESPONSE_BYTES:
        raise TransportError("응답이 너무 커요.", sent="maybe")
    return data


class UrllibTransport:
    """``urllib.request`` transport: follows ``HTTPS_PROXY``/``SSL_CERT_FILE``, sends ``User-Agent: USER_AGENT``,
    **never follows redirects** (3xx come back as a ``Response``; the default handler would copy
    ``Authorization`` to another host), and only calls ``https`` URLs whose host is in ``allowed_hosts``
    (anything else → ``TransportError(sent="no")`` before sending). Connect timeout ``connect_timeout``,
    read timeout per call (``timeout``). Response bodies and tokens are never logged.

    ``insecure_loopback_for_tests`` additionally allows ``http://127.0.0.1:<port>`` (the redirect test's local
    server); production code never sets it.
    """

    def __init__(self, allowed_hosts: Iterable[str], *, user_agent: str = USER_AGENT,
                 connect_timeout: float = CONNECT_TIMEOUT, insecure_loopback_for_tests: bool = False) -> None:
        self.allowed_hosts = frozenset(h.lower().strip("[]") for h in allowed_hosts if h)
        self.user_agent = user_agent
        self.connect_timeout = connect_timeout
        self._insecure_loopback = bool(insecure_loopback_for_tests)

    def allow_host(self, host: str) -> None:
        """Add one host (the public media host for the self-check)."""
        if host:
            self.allowed_hosts = self.allowed_hosts | {host.lower().strip("[]")}

    def _check_url(self, url: str) -> None:
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower()
            parts.port  # noqa: B018 - raises ValueError for a bad port
        except ValueError:
            raise TransportError("주소 형식이 올바르지 않아요.", sent="no") from None
        if parts.username or parts.password:
            raise TransportError("주소에 사용자 정보를 넣을 수 없어요.", sent="no")
        if parts.scheme == "http" and self._insecure_loopback and host in ("127.0.0.1", "localhost"):
            return
        if parts.scheme != "https":
            raise TransportError("https 주소만 부를 수 있어요.", sent="no")
        if host not in self.allowed_hosts:
            raise TransportError(f"허용하지 않은 호스트예요: {host}", sent="no")

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None, timeout: float = READ_TIMEOUT) -> Response:
        self._check_url(url)
        if sum(x is not None for x in (form, json_body, data)) > 1:
            raise ValueError("form, json_body and data are mutually exclusive")
        full = url
        if params:
            query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
            full = f"{url}{'&' if '?' in url else '?'}{query}"
        body: bytes | None = None
        send_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if form is not None:
            body = urllib.parse.urlencode({k: str(v) for k, v in form.items()}).encode("ascii")
            send_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data
        for key, value in (headers or {}).items():
            send_headers[str(key)] = str(value)
        probe = _Probe()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _NoRedirect(),
                                             _HTTPSHandler(probe, self.connect_timeout),
                                             _HTTPHandler(probe, self.connect_timeout))
        req = urllib.request.Request(full, data=body, headers=send_headers, method=method.upper())
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = _read_limited(resp)
                return Response(status=int(resp.status), headers=dict(resp.headers.items()), body=payload)
        except urllib.error.HTTPError as exc:  # 4xx/5xx and (no redirects) 3xx
            try:
                payload = _read_limited(exc)
            except (OSError, http.client.HTTPException):
                payload = b""
            finally:
                exc.close()
            return Response(status=int(exc.code), headers=dict(exc.headers.items()) if exc.headers else {}, body=payload)
        except TransportError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            sent: Literal["no", "maybe"] = "maybe" if probe.connected else "no"
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"요청을 마치지 못했어요 ({reason.__class__.__name__})", sent=sent) from None


# ---------------------------------------------------------------------------
# Fake platform (INSIA_PUBLISH_FAKE=1: tests and demos in a temporary workspace)
# ---------------------------------------------------------------------------

FAKE_PERMALINK_BASE = "https://example.invalid"


class FakePlatformTransport:
    """An in-memory LinkedIn + Instagram that always succeeds (``INSIA_PUBLISH_FAKE=1`` only).

    It answers exactly the requests the real publishers make, so the whole flow (connect, preview, confirmed
    send, permalink) runs through the same code without the network. Nothing leaves the machine; permalinks point
    to ``https://example.invalid/…`` and the service records ``published_via='fake'``. Any other request (including
    the public-media self-check, which the fake mode skips) raises ``TransportError(sent="no")``.
    """

    LINKEDIN_SUB = "fakeMember01"
    LINKEDIN_NAME = "가짜 게시 계정"
    IG_USER_ID = "17841400000000000"
    IG_USERNAME = "insia.fake"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 7000000000000000000
        self._containers: dict[str, str] = {}
        self._media: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []

    def _next(self) -> str:
        with self._lock:
            self._counter += 1
            return str(self._counter)

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None, timeout: float = READ_TIMEOUT) -> Response:
        method = method.upper()
        parts = urllib.parse.urlsplit(url)
        host, path = (parts.hostname or "").lower(), parts.path
        with self._lock:
            self.requests.append((method, f"{host}{path}"))
        if host == "www.linkedin.com" and path == "/oauth/v2/accessToken" and method == "POST":
            return json_response(200, {"access_token": f"fake-li-{self._next()}", "expires_in": 5184000,
                                       "scope": "openid profile w_member_social"})
        if host == "api.linkedin.com" and path == "/v2/userinfo" and method == "GET":
            return json_response(200, {"sub": self.LINKEDIN_SUB, "name": self.LINKEDIN_NAME})
        if host == "api.linkedin.com" and path == "/rest/posts" and method == "POST":
            return Response(201, {"x-restli-id": f"urn:li:share:{self._next()}"}, b"")
        if host == "graph.instagram.com":
            return self._instagram(method, path, params or {}, form or {})
        raise TransportError("가짜 게시 모드에서는 이 주소를 부르지 않아요.", sent="no")

    def _instagram(self, method: str, path: str, params: Mapping[str, str], form: Mapping[str, str]) -> Response:
        segments = [s for s in path.split("/") if s]
        if path == "/refresh_access_token":
            return json_response(200, {"access_token": f"fake-ig-{self._next()}", "token_type": "bearer",
                                       "expires_in": 5184000})
        if len(segments) < 2:
            return json_response(404, {"error": {"message": "unknown", "code": 100}})
        tail = segments[1:]
        if tail == ["me"] and method == "GET":
            return json_response(200, {"data": [{"user_id": self.IG_USER_ID, "username": self.IG_USERNAME,
                                                 "account_type": "BUSINESS"}]})
        if len(tail) == 2 and tail[1] == "content_publishing_limit":
            return json_response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50, "quota_duration": 86400}}]})
        if len(tail) == 2 and tail[1] == "media" and method == "POST":
            container = self._next()
            with self._lock:
                self._containers[container] = "FINISHED"
            return json_response(200, {"id": container})
        if len(tail) == 2 and tail[1] == "media_publish" and method == "POST":
            media_id = self._next()
            with self._lock:
                self._containers[str(form.get("creation_id", ""))] = "PUBLISHED"
                self._media[media_id] = f"{FAKE_PERMALINK_BASE}/instagram/p/{media_id}/"
            return json_response(200, {"id": media_id})
        if len(tail) == 2 and tail[1] == "media" and method == "GET":
            with self._lock:
                items = [{"id": k, "permalink": v, "timestamp": "2100-01-01T00:00:00+0000"} for k, v in self._media.items()]
            return json_response(200, {"data": items[-5:]})
        if len(tail) == 1 and method == "GET":
            object_id = tail[0]
            with self._lock:
                if object_id in self._media:
                    return json_response(200, {"id": object_id, "permalink": self._media[object_id],
                                               "media_type": "CAROUSEL_ALBUM", "timestamp": "2100-01-01T00:00:00+0000"})
                status = self._containers.get(object_id)
            if status:
                return json_response(200, {"id": object_id, "status_code": status})
        return json_response(404, {"error": {"message": "unknown object", "code": 100}})


__all__ = [
    "CONNECT_TIMEOUT", "IG_CONTAINER_TIMEOUT", "INSTAGRAM_HOSTS", "LINKEDIN_HOSTS", "MASK", "READ_TIMEOUT",
    "SECRET_FIELDS", "USER_AGENT",
    "FAKE_PERMALINK_BASE", "MAX_RESPONSE_BYTES",
    "FakeAnswer", "FakePlatformTransport", "FakeTransport", "RecordedRequest", "Response", "Transport", "TransportError",
    "UrllibTransport",
    "first_record", "json_response",
]
