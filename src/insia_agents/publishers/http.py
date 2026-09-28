"""HTTP transport for the publishing package (DESIGN.md 1-4).

* ``Transport`` — the one protocol every platform module calls. 4xx/5xx come back as a ``Response`` (never an
  exception); interpreting them is the platform module's job. Failures raise ``TransportError`` with
  ``sent="no"`` (nothing left this machine: DNS, refused connection, a host outside the allow-list) or
  ``sent="maybe"`` (the request may have reached the platform: timeout or dropped connection after sending).
* ``FakeTransport`` — the scripted fake used by tests (working). Unscripted requests raise ``AssertionError``,
  which is also how a test proves "no network was used".
* ``UrllibTransport`` — the real transport (standard library only, no redirects, host allow-list). Package A
  implements it; the signature below is the contract.
* ``first_record()`` — Instagram answers some calls wrapped in ``{"data": [{…}]}`` and some flat (IG §0-3/§0-4);
  every response parse goes through it.
"""

from __future__ import annotations

import json
import re
import threading
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
# Real transport (package A implements the body)
# ---------------------------------------------------------------------------


class UrllibTransport:
    """``urllib.request`` transport: follows ``HTTPS_PROXY``/``SSL_CERT_FILE``, sends ``User-Agent: USER_AGENT``,
    **never follows redirects** (3xx come back as a ``Response``; the default handler would copy
    ``Authorization`` to another host), and only calls ``https`` URLs whose host is in ``allowed_hosts``
    (anything else → ``TransportError(sent="no")`` before sending). Connect timeout ``connect_timeout``,
    read timeout per call (``timeout``). Response bodies and tokens are never logged.
    """

    def __init__(self, allowed_hosts: Iterable[str], *, user_agent: str = USER_AGENT,
                 connect_timeout: float = CONNECT_TIMEOUT) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.user_agent = user_agent
        self.connect_timeout = connect_timeout

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, str] | None = None, form: Mapping[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None, timeout: float = READ_TIMEOUT) -> Response:
        raise NotImplementedError("UrllibTransport.request: package A")


__all__ = [
    "CONNECT_TIMEOUT", "IG_CONTAINER_TIMEOUT", "INSTAGRAM_HOSTS", "LINKEDIN_HOSTS", "MASK", "READ_TIMEOUT",
    "SECRET_FIELDS", "USER_AGENT",
    "FakeAnswer", "FakeTransport", "RecordedRequest", "Response", "Transport", "TransportError", "UrllibTransport",
    "first_record", "json_response",
]
