"""Shared types of the API publishing package (LinkedIn / Instagram): the contract every part builds on.

What lives here (DESIGN.md 1-2, 1-3):

* platform ids and labels, readiness, validation issues, preview / send results;
* ``HumanConfirmation`` — the proof that a person just confirmed one preview (see its docstring for where it
  may be constructed);
* the ``Publisher``, ``AttemptGuard`` and ``Clock`` protocols;
* preview options (``is_ai_generated`` is **required** for Instagram, DESIGN.md 14.2) and their validation;
* the error hierarchy. ``str(exc)`` is always a Korean sentence the dashboard / CLI can show as is; the server
  maps ``PublishError`` to ``RequestError(exc.http_status, str(exc), extra={"code": exc.code, **exc.extra()})``
  *before* its other branches, then ``ItemLockedError`` (409 ``item_locked``).

Nothing here talks to the network, reads credentials or imports ``server``/``cli``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, Union, runtime_checkable

from ..db import AttemptTakenOverError, ItemLockedError

if TYPE_CHECKING:
    from ..models import ContentItemDetail, Profile, PublishAttempt

# ---------------------------------------------------------------------------
# Platforms and fixed vocabularies
# ---------------------------------------------------------------------------

PlatformId = Literal["linkedin", "instagram"]
PUBLISH_PLATFORMS: tuple[PlatformId, ...] = ("linkedin", "instagram")
# INSIA channel -> platform. naver_blog and bizplan have no API publishing (manual only).
CHANNEL_PLATFORM: dict[str, PlatformId] = {"linkedin": "linkedin", "instagram": "instagram"}
# Plain names only: no platform logos or icons anywhere (LinkedIn API terms 6.1, Meta brand rules).
PLATFORM_LABELS: dict[str, str] = {"linkedin": "LinkedIn", "instagram": "인스타그램"}
# ContentItem.published_via written by a confirmed API publish (the fake mode writes FAKE_VIA instead).
PUBLISHED_VIA_API: dict[str, str] = {"linkedin": "linkedin_api", "instagram": "instagram_api"}
FAKE_VIA = "fake"

ReadinessState = Literal["disabled", "not_configured", "not_connected", "connected", "expiring",
                         "needs_reconnect", "unavailable"]
READINESS_STATES: tuple[str, ...] = ("disabled", "not_configured", "not_connected", "connected", "expiring",
                                     "needs_reconnect", "unavailable")
# Readiness.blockers codes (machine-readable; the Korean reason is Readiness.reason).
BLOCKER_PUBLIC_URL_MISSING = "public_url_missing"      # Instagram: no valid public HTTPS media URL
BLOCKER_MEDIA_PORT_UNAVAILABLE = "media_port_unavailable"  # the media listener port could not be opened
BLOCKER_RENDER_UNAVAILABLE = "render_unavailable"      # Instagram: no Playwright/Chromium or Hangul font

# GET /api/items/<id> ``publish.blocked_by`` values (DESIGN.md 6-2).
BLOCKED_BY: tuple[str, ...] = ("", "not_approved", "version_changed", "published", "published_attempt",
                               "already_published", "archived", "agent_job")

# Worker progress steps (``PublishAttempt.step``) in the words people see — one table for the dashboard (the
# attempt JSON's ``step_label``) and ``insia publish send`` (its progress lines). ``children 3/8`` is built below.
STEP_LABELS: dict[str, str] = {
    "check": "연결 확인", "media": "이미지 올릴 준비", "self_check": "공개 주소 확인",
    "polling": "인스타그램이 이미지를 처리하는 중", "carousel": "캐러셀 만드는 중", "write": "게시 요청 보내는 중",
    "permalink": "게시물 주소를 받는 중",
}
_CHILDREN_STEP = re.compile(r"^children (\d+)/(\d+)$")
NOTHING_POSTED = "아무것도 올라가지 않았어요."
# publisher failure messages mostly say it already ("글은 올라가지 않았어요", "아무것도 게시되지 않았어요" …);
# web/js/publish.js (failText) uses the same pattern
_NOTHING_POSTED_SAID = re.compile(r"(올라가|올리|게시되|게시하)지 않았")


def step_label(step: str | None) -> str:
    """The Korean words for a worker step; ``""`` for a step without words (a machine name is never shown)."""
    text = str(step or "").strip()
    match = _CHILDREN_STEP.match(text)
    if match:
        return f"이미지 등록 {match.group(1)}/{match.group(2)}"
    return STEP_LABELS.get(text, "")


def with_nothing_posted(message: str | None) -> str:
    """A failed attempt's message that says "nothing was posted" exactly once."""
    text = str(message or "").strip() or "이유를 받지 못했어요."
    return text if _NOTHING_POSTED_SAID.search(text) else f"{text} {NOTHING_POSTED}"

# LinkedIn OAuth (DESIGN.md 3-1). The callback answers only ``303 Location: CALLBACK_REDIRECT.format(result=…)``
# where ``result`` is one of CALLBACK_RESULTS; the dashboard draws the Korean sentence itself.
LINKEDIN_CALLBACK_PATH = "/oauth/linkedin/callback"
CALLBACK_REDIRECT = "/#/brand/connections/linkedin/{result}"
CallbackResult = Literal["ok", "cancelled", "expired", "exchange_failed", "invalid"]
CALLBACK_RESULTS: tuple[str, ...] = ("ok", "cancelled", "expired", "exchange_failed", "invalid")
OAUTH_COOKIE_NAME = "insia_oauth"          # SameSite=Lax, HttpOnly, Path=OAUTH_COOKIE_PATH, Max-Age=OAUTH_STATE_TTL_SECONDS
OAUTH_COOKIE_PATH = "/oauth/"
OAUTH_STATE_TTL_SECONDS = 600

# Public media (Instagram fetches the JPEGs from here). The media listener and, in configuration B, the main
# port serve exactly this path shape and nothing else (DESIGN.md 3-3, 4-2-3).
MEDIA_PATH_PATTERN = r"^/pub/m/([0-9a-f]{32})/(\d{2})\.jpg$"

PREVIEW_TTL_SECONDS = 1800                 # a preview can be sent for 30 minutes
PREVIEW_HASH_SCHEMA = 1                    # "schema" key of the hashed payload (DESIGN.md 4-0)

# CLI confirm code: 6 random characters, without the look-alikes 0 O 1 I. Only its sha256 is stored.
CONFIRM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CONFIRM_CODE_LENGTH = 6

LINKEDIN_VISIBILITIES: tuple[str, ...] = ("PUBLIC", "CONNECTIONS")
VISIBILITY_LABELS: dict[str, str] = {"PUBLIC": "전체 공개", "CONNECTIONS": "1촌 공개"}
AI_LABEL_REQUIRED_MESSAGE = "AI 정보 라벨을 붙일지 골라 주세요 (options.is_ai_generated: true 또는 false)"


def platform_label(platform: str) -> str:
    """Korean display name of a platform ("LinkedIn", "인스타그램"); unknown ids read "플랫폼"."""
    return PLATFORM_LABELS.get(platform, "플랫폼")


def channel_platform(channel: str) -> PlatformId | None:
    """The API platform for an INSIA channel, or ``None`` (네이버 블로그·사업계획서: 수동 게시만)."""
    return CHANNEL_PLATFORM.get(channel)


# ---------------------------------------------------------------------------
# Hashing and confirm codes (working helpers shared by service and tests)
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """The canonical JSON that preview hashes are computed over (sorted keys, UTF-8 text, no spaces)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload_hash(payload: Any) -> str:
    """``"sha256:<64 hex>"`` of ``canonical_json(payload)``. The payload never contains a token."""
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def new_confirm_code() -> str:
    """A fresh random CLI confirm code (``secrets.choice``); a new one for every CLI send preview."""
    return "".join(secrets.choice(CONFIRM_CODE_ALPHABET) for _ in range(CONFIRM_CODE_LENGTH))


def normalize_confirm_code(text: str) -> str:
    """What the person typed, normalized before hashing/comparing (trimmed, upper case)."""
    return (text or "").strip().upper()


def confirm_code_hash(code: str) -> str:
    """Hex sha256 of the normalized code — the only form stored (``publish_previews.confirm_code_hash``)."""
    return hashlib.sha256(normalize_confirm_code(code).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    platform: PlatformId
    state: ReadinessState
    ready: bool                     # state in (connected, expiring) and every requirement is met
    reason: str                     # one Korean sentence (shown next to the button / as a tooltip)
    blockers: tuple[str, ...] = ()  # e.g. BLOCKER_PUBLIC_URL_MISSING, BLOCKER_RENDER_UNAVAILABLE

    def to_json(self) -> dict[str, Any]:
        return {"platform": self.platform, "state": self.state, "ready": self.ready, "reason": self.reason,
                "blockers": list(self.blockers)}


@dataclass(frozen=True)
class ValidationIssue:
    level: Literal["error", "warning"]
    code: str                       # "too_long", "hashtags", "placeholder", "forced_approval", "overflow" …
    message: str                    # Korean

    def to_json(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class LinkedInPreviewOptions:
    """LinkedIn preview options: who can see the post. Part of the preview hash."""

    visibility: Literal["PUBLIC", "CONNECTIONS"] = "PUBLIC"

    def to_json(self) -> dict[str, Any]:
        return {"visibility": self.visibility}


@dataclass(frozen=True)
class InstagramPreviewOptions:
    """Instagram preview options. ``is_ai_generated`` has **no default**: a person picks it for every post
    (DESIGN.md 14.2). It is part of the preview hash, is sent only on the carousel parent and only when true,
    and is recorded on the attempt (``attempt.state["is_ai_generated"]``)."""

    is_ai_generated: bool

    def to_json(self) -> dict[str, Any]:
        return {"is_ai_generated": self.is_ai_generated}


PreviewOptions = Union[LinkedInPreviewOptions, InstagramPreviewOptions]


def parse_preview_options(platform: str, raw: Mapping[str, Any] | None) -> PreviewOptions:
    """Validate the ``options`` object of a preview request (API body or CLI flags) — working, not a stub.

    * LinkedIn: ``{"visibility": "PUBLIC" | "CONNECTIONS"}``; missing → ``"PUBLIC"``.
    * Instagram: ``{"is_ai_generated": true | false}`` — **required, a real JSON boolean** (not 0/1/"true").
      Missing or not a bool → ``InvalidOptionsError(AI_LABEL_REQUIRED_MESSAGE)`` (HTTP 400, CLI exit 2).
    * Unknown keys, a non-object ``options`` or an unknown platform → ``InvalidOptionsError``: the options are
      hashed into the preview, so nothing unexpected may slip through silently.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise InvalidOptionsError("게시 옵션(options)은 JSON 객체여야 해요.")
    if platform == "linkedin":
        unknown = sorted(str(key) for key in raw if key != "visibility")
        if unknown:
            raise InvalidOptionsError(f"LinkedIn 게시에 없는 옵션이에요: {', '.join(unknown)}")
        visibility = raw.get("visibility", "PUBLIC")
        if visibility is None:
            visibility = "PUBLIC"
        if not isinstance(visibility, str) or visibility not in LINKEDIN_VISIBILITIES:
            raise InvalidOptionsError("공개 범위(options.visibility)는 PUBLIC(전체 공개) 또는 CONNECTIONS(1촌 공개)여야 해요.")
        return LinkedInPreviewOptions(visibility=visibility)  # type: ignore[arg-type]
    if platform == "instagram":
        unknown = sorted(str(key) for key in raw if key != "is_ai_generated")
        if unknown:
            raise InvalidOptionsError(f"인스타그램 게시에 없는 옵션이에요: {', '.join(unknown)}")
        value = raw.get("is_ai_generated")
        if type(value) is not bool:
            raise InvalidOptionsError(AI_LABEL_REQUIRED_MESSAGE)
        return InstagramPreviewOptions(is_ai_generated=value)
    raise InvalidOptionsError(f"API로 게시할 수 없는 플랫폼이에요: {platform!r} (linkedin, instagram만 돼요)")


@dataclass
class PreviewDraft:
    """What a ``Publisher.build_preview`` returns, before the service stores it as a preview.

    ``payload`` is the platform block of the hashed payload (DESIGN.md 4-0), e.g. LinkedIn
    ``{"commentary": …, "text": …}`` or Instagram ``{"caption": …, "slides": [{"n", "sha256", "bytes", "alt"}]}``;
    the service wraps it as ``{"schema", "platform", "account_id", "item_id", "version", "api_version",
    "options", <platform>: payload}`` and hashes that. ``slide_bytes`` are the rendered Instagram JPEGs (in
    order) that the service writes to ``publish/staging/<preview_id>/NN.jpg``; they are never re-rendered.
    """

    platform: PlatformId
    account_id: str
    account: dict[str, Any]                 # name, kind, id_hint (+ username for Instagram)
    api_version: str
    options: dict[str, Any]                 # PreviewOptions.to_json()
    payload: dict[str, Any]
    content: dict[str, Any]                 # text, chars, limit, hashtags, options
    slides: list[dict[str, Any]] = field(default_factory=list)   # n, alt, bytes, width, height, sha256
    slide_bytes: list[bytes] = field(default_factory=list, repr=False)
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    notices: list[dict[str, str]] = field(default_factory=list)  # code, message
    quota: dict[str, int] | None = None     # Instagram: used, total
    first_comment_link: str = ""
    request_preview: list[dict[str, Any]] = field(default_factory=list)  # tokens masked


@dataclass
class PreviewResult:
    """A stored preview as the dashboard / CLI shows it (JSON shape: DESIGN.md 6-2 ``…/publish/preview``)."""

    preview_id: str
    preview_hash: str               # "sha256:<64 hex>"
    expires_at: str
    platform: PlatformId
    item: dict[str, Any]            # id, version, title, channel, approved_version, approval_forced, approved_score
    account: dict[str, Any]         # name, kind, id_hint
    content: dict[str, Any]         # text, chars, limit, hashtags, options
    slides: list[dict[str, Any]]    # Instagram only: n, url, alt, bytes, width, height, sha256
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    notices: list[dict[str, str]]   # code, message (the dialog's "알아 두세요")
    quota: dict[str, int] | None    # Instagram: used, total
    first_comment_link: str         # LinkedIn change_log "첫 댓글 링크: <URL>"
    request_preview: list[dict[str, Any]]  # summary of the HTTP requests to be sent (tokens masked)
    # Only set when the CLI ``send`` asked for it (``issue_confirm_code=True``): 6 random characters shown once on
    # that terminal. The DB keeps only its sha256; it never appears in to_json(), --json output or logs.
    confirm_code: str | None = field(default=None, repr=False)

    @property
    def can_publish(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        """The API/--json shape. Never includes ``confirm_code``."""
        return {
            "preview_id": self.preview_id, "preview_hash": self.preview_hash, "expires_at": self.expires_at,
            "platform": self.platform, "item": dict(self.item), "account": dict(self.account),
            "content": dict(self.content), "slides": [dict(s) for s in self.slides],
            "errors": [i.to_json() for i in self.errors], "warnings": [i.to_json() for i in self.warnings],
            "notices": [dict(n) for n in self.notices], "quota": dict(self.quota) if self.quota is not None else None,
            "first_comment_link": self.first_comment_link,
            "request_preview": [dict(r) for r in self.request_preview], "can_publish": self.can_publish,
        }


@dataclass(frozen=True)
class SendOutcome:
    """What a publisher's worker step ended with. ``published`` only after the platform confirmed success."""

    status: Literal["published", "failed", "unknown"]
    external_id: str = ""           # LinkedIn post URN / Instagram media id
    permalink: str = ""
    error_code: str = ""
    error: str = ""                 # Korean
    state: dict[str, Any] = field(default_factory=dict)  # container ids etc. (merged into attempt.state)


@dataclass(frozen=True)
class ConnectStart:
    """Answer of ``PublishService.linkedin_connect`` (``POST /api/publish/linkedin/connect``, DESIGN.md 3-1/6-2).

    ``cookie_value`` (redirect mode only) is the value for the ``OAUTH_COOKIE_NAME`` cookie the server sets
    (HMAC of the state with a per-process key); it is not part of the JSON body.
    """

    mode: Literal["redirect", "paste"]
    authorize_url: str
    redirect_uri: str
    expires_in: int = OAUTH_STATE_TTL_SECONDS
    open_url: str = ""              # paste mode only, and only for a loopback / public_hosts redirect_uri host
    cookie_value: str = field(default="", repr=False)

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"mode": self.mode, "authorize_url": self.authorize_url,
                                "expires_in": self.expires_in, "redirect_uri": self.redirect_uri}
        if self.open_url:
            body["open_url"] = self.open_url
        return body


# ---------------------------------------------------------------------------
# Human confirmation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanConfirmation:
    """Proof that a person just confirmed one preview (or one ``unknown`` attempt's outcome).

    Constructed **only** in two places (enforced by the AST test ``tests/test_no_autopublish_surface.py``):

    * ``server_publish.py`` — the publish handler (``POST /api/items/<id>/publish``) and the resolve handler
      (``POST /api/publish/attempts/<pa>/resolve``), after ``_require_human()`` passed:
      ``HumanConfirmation(via="dashboard", requested_by=f"dashboard@{client_key}", preview_id=…, preview_hash=…)``.
    * ``cli.py`` — ``cmd_publish_send`` (TTY + the random confirm code the person typed) and
      ``cmd_publish_resolve`` (TTY + y/N): ``HumanConfirmation(via="cli", requested_by=f"cli:{user}@{host}", …,
      confirm_code=typed)``.

    Nothing else — no scheduler, planner, pipeline, agent, backend or service code — may create one, and
    ``PublishService.send``/``resolve`` refuse anything that is not an instance (runtime ``TypeError``).

    Field use: for a send, ``preview_id``/``preview_hash`` are the confirmed preview's; for a resolve,
    ``preview_id`` is the attempt id (``pa_…``) and ``preview_hash`` is ``""``. ``confirm_code`` is CLI-only
    (what the person typed; the service normalizes and compares its sha256) and must be empty for the dashboard.
    """

    via: Literal["dashboard", "cli"]
    requested_by: str               # "dashboard@<client key>" | "cli:<user>@<host>"
    preview_id: str                 # resolve: the attempt id
    preview_hash: str               # resolve: ""
    confirm_code: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for name in ("via", "requested_by", "preview_id", "preview_hash", "confirm_code"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"HumanConfirmation.{name} must be a str")
        if self.via not in ("dashboard", "cli"):
            raise ValueError("HumanConfirmation.via must be 'dashboard' or 'cli'")
        if not self.requested_by.strip() or len(self.requested_by) > 200:
            raise ValueError("HumanConfirmation.requested_by must be a non-empty string (max 200 chars)")
        if not self.preview_id.strip() or len(self.preview_id) > 100:
            raise ValueError("HumanConfirmation.preview_id must be a non-empty id (max 100 chars)")
        if len(self.preview_hash) > 100:
            raise ValueError("HumanConfirmation.preview_hash is too long")
        if self.via == "dashboard" and self.confirm_code:
            raise ValueError("the dashboard confirms with the checkbox + preview_hash, never a confirm code")


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Time source injected into the service, the heartbeat thread and the platform modules (DESIGN.md 1-5).

    Publishing code never calls ``time.sleep`` / ``Event.wait(timeout)`` directly, so tests can pass a fake
    clock whose ``sleep``/``wait`` only move time forward.
    """

    def now(self) -> datetime: ...                                      # timezone-aware UTC
    def sleep(self, seconds: float) -> None: ...
    def wait(self, event: threading.Event, seconds: float) -> bool: ...  # True when the event was set


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))

    def wait(self, event: threading.Event, seconds: float) -> bool:
        return event.wait(max(0.0, float(seconds)))


@runtime_checkable
class AttemptGuard(Protocol):
    """Held by the worker for one attempt: checks it still owns the attempt and secures the write step.

    Implemented by the service on top of the owner-token-conditional workspace writes
    (``update_publish_attempt`` / ``claim_publish_write``, DESIGN.md 5-3).
    """

    attempt_id: str

    def step(self, step: str, state_patch: dict[str, Any] | None = None) -> None:
        """Record progress (``"check"``, ``"children 3/8"``, ``"polling"``, ``"carousel"``, ``"permalink"``) and merge
        ``state_patch`` into ``attempt.state``. Raises ``AttemptTakenOverError`` when ownership was lost."""
        ...

    def claim_write(self, step: str = "write") -> None:
        """Conditional UPDATE right before an irreversible call (LinkedIn ``POST /rest/posts``, Instagram
        ``POST /media_publish``). ``rowcount != 1`` → ``AttemptTakenOverError``: the caller must not send."""
        ...


@runtime_checkable
class Publisher(Protocol):
    """One platform (``linkedin.LinkedInPublisher``, ``instagram.InstagramPublisher``)."""

    platform: PlatformId

    def readiness(self) -> Readiness:
        """Local state only (no network): configured / connected / expiring / requirements."""
        ...

    def build_preview(self, detail: ContentItemDetail, profile: Profile | None,
                      options: PreviewOptions) -> PreviewDraft:
        """Transform + validate the item's current version (and, for Instagram, render the JPEGs)."""
        ...

    def send(self, attempt: PublishAttempt, payload: dict[str, Any], guard: AttemptGuard) -> SendOutcome:
        """Send exactly the stored preview ``payload``. Calls ``guard.claim_write()`` right before the irreversible
        request and sends nothing if it raises. Never retries in the background."""
        ...

    def reconcile(self, attempt: PublishAttempt, guard: AttemptGuard) -> SendOutcome | None:
        """Read-only re-check of an ``unknown`` attempt (Instagram only; LinkedIn returns ``None``). Never publishes."""
        ...


# ---------------------------------------------------------------------------
# Errors (DESIGN.md 1-3)
# ---------------------------------------------------------------------------

Outcome = Literal["not_sent", "unknown"]


class PublishError(Exception):
    """Base of every publishing error. Deliberately **not** a ``ValueError``/``WorkspaceError`` (the server's
    existing mapping would swallow it as 400).

    * ``str(exc)`` — Korean sentence for the dashboard / CLI;
    * ``http_status`` / ``code`` — the server's answer (``{"error": str(exc), "code": code, **extra()}``);
    * ``outcome`` — ``"not_sent"`` (nothing was published) or ``"unknown"`` (it may have been published);
    * ``exit_code`` — the CLI exit code (1 = the job failed, 2 = wrong use);
    * ``extra()`` — additional JSON fields, never a secret.

    ``platform`` (``"linkedin"``/``"instagram"``/``""``) fills ``{label}`` in the default message.
    """

    http_status: ClassVar[int] = 409
    code: ClassVar[str] = "publish_error"
    outcome: ClassVar[Outcome] = "not_sent"
    exit_code: ClassVar[int] = 1
    default_message: ClassVar[str] = "API로 게시할 수 없어요."

    def __init__(self, message: str | None = None, *, platform: str = "") -> None:
        self.platform = platform
        super().__init__(message or self._default(platform))

    @classmethod
    def _default(cls, platform: str) -> str:
        return cls.default_message.format(label=platform_label(platform))

    def extra(self) -> dict[str, Any]:
        return {}


class PublishDisabledError(PublishError):
    """``INSIA_PUBLISH=0``, the fake mode outside a temporary workspace, or Instagram's beta flag is off."""

    code = "disabled"
    default_message = "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0)."


class NotConfiguredError(PublishError):
    code = "not_configured"
    default_message = "{label} 앱 정보(Client ID·Secret)가 없어요. 연결 설정에서 넣어 주세요."


class NotConnectedError(PublishError):
    code = "not_connected"
    default_message = "{label} 계정이 연결되지 않았어요."


class ReconnectRequiredError(PublishError):
    code = "reconnect"
    MESSAGES: ClassVar[dict[str, str]] = {
        "linkedin": "LinkedIn 연결이 끝났거나 해제됐어요. 다시 연결해 주세요. (글은 올라가지 않았어요)",
        "instagram": "인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요. (글은 올라가지 않았어요)",
    }
    default_message = "{label} 연결을 다시 해 주세요. (글은 올라가지 않았어요)"

    @classmethod
    def _default(cls, platform: str) -> str:
        return cls.MESSAGES.get(platform) or super()._default(platform)

    def extra(self) -> dict[str, Any]:
        return {"reconnect": True}


class UnavailableError(PublishError):
    """A requirement is missing (e.g. Instagram without a public HTTPS media URL). ``blockers``: Readiness codes."""

    code = "unavailable"
    default_message = "{label} API 게시를 지금 쓸 수 없어요."

    def __init__(self, message: str | None = None, *, platform: str = "", blockers: Sequence[str] = ()) -> None:
        super().__init__(message, platform=platform)
        self.blockers = tuple(blockers)

    def extra(self) -> dict[str, Any]:
        return {"blockers": list(self.blockers)}


class NotPublishableError(PublishError):
    """The item cannot be published through the API (not approved, new version after approval, archived,
    already published, a channel without API publishing). ``blocked_by`` uses the ``BLOCKED_BY`` vocabulary."""

    code = "not_publishable"
    default_message = "승인한 최신 버전만 API로 게시할 수 있어요."

    def __init__(self, message: str | None = None, *, platform: str = "", blocked_by: str = "") -> None:
        super().__init__(message, platform=platform)
        self.blocked_by = blocked_by

    def extra(self) -> dict[str, Any]:
        return {"blocked_by": self.blocked_by}


class InvalidInputError(PublishError):
    """Wrong request input (a malformed URL, redirect URI, empty client id …). HTTP 400, CLI exit 2."""

    http_status = 400
    code = "invalid_input"
    exit_code = 2
    default_message = "입력한 값이 올바르지 않아요."


class InvalidOptionsError(InvalidInputError):
    """Invalid preview ``options``; notably Instagram without a bool ``is_ai_generated`` (DESIGN.md 14.2)."""

    code = "invalid_options"
    default_message = AI_LABEL_REQUIRED_MESSAGE


class ValidationFailedError(PublishError):
    """The content has blocking validation errors (a preview with errors can never be sent). HTTP 422."""

    http_status = 422
    code = "validation"
    default_message = "게시하기 전에 고칠 부분이 있어요."

    def __init__(self, issues: Sequence[ValidationIssue], message: str | None = None, *, platform: str = "") -> None:
        self.issues = [issue for issue in issues if issue.level == "error"] or list(issues)
        if message is None and self.issues:
            shown = "; ".join(issue.message for issue in self.issues[:3])
            more = f" 외 {len(self.issues) - 3}건" if len(self.issues) > 3 else ""
            message = f"게시하기 전에 고칠 부분이 있어요: {shown}{more}"
        super().__init__(message, platform=platform)

    def extra(self) -> dict[str, Any]:
        return {"errors": [issue.to_json() for issue in self.issues]}


class PreviewExpiredError(PublishError):
    """The preview is older than 30 minutes or was already used."""

    code = "preview_expired"
    default_message = "미리보기가 30분이 지나 만료됐어요. 다시 확인해 주세요."


class ConfirmationMismatchError(PublishError):
    """Hash mismatch, content/version/account changed since the preview, a staged image changed, or a preview
    made elsewhere (a CLI preview sent from the dashboard or the other way round)."""

    code = "changed"
    default_message = "확인한 뒤에 내용이나 계정이 바뀌었어요. 다시 확인해 주세요."


# The same class as ``db.ItemLockedError`` (409 ``item_locked``, ``attempt_id``/``platform``/``status``).
PublishInProgressError = ItemLockedError


class AlreadyPublishedError(PublishError):
    code = "already_published"
    default_message = "이미 {label}에 게시한 버전이에요."

    def __init__(self, message: str | None = None, *, platform: str = "", permalink: str = "",
                 attempt_id: str = "") -> None:
        super().__init__(message, platform=platform)
        self.permalink = permalink
        self.attempt_id = attempt_id

    def extra(self) -> dict[str, Any]:
        return {"permalink": self.permalink, "attempt_id": self.attempt_id}


class PublisherBusyError(PublishError):
    """One worker per platform (and one Instagram preview render at a time): no queue, answer 409 (DESIGN.md 6-4)."""

    code = "busy"
    default_message = "다른 게시가 진행 중이에요. 끝난 뒤 다시 눌러 주세요."


class NotHumanRequestError(PublishError):
    """The publish/resolve request did not come from a person (Bearer token, cross-site, no TTY …). HTTP 403."""

    http_status = 403
    code = "not_human"
    exit_code = 2
    default_message = ("API 게시는 대시보드에서 사람이 직접 눌러야 해요. "
                       "스크립트(Bearer 토큰)나 다른 사이트에서는 게시할 수 없어요.")


class ConfirmCodeError(PublishError):
    """The CLI confirm code did not match (CLI exit code 2; nothing was sent)."""

    http_status = 400
    code = "confirm_code"
    exit_code = 2
    default_message = "확인 코드가 맞지 않아요. 아무것도 올리지 않았어요."


class AttemptStateError(PublishError):
    """The attempt is not in a state that allows this (e.g. resolving an attempt that is not ``unknown``,
    adding a permalink to an attempt that is not ``published`` or already has one)."""

    code = "attempt_state"
    default_message = "이 게시 기록은 이미 정리됐어요."


class SettingLockedError(PublishError):
    """The value was set by an environment variable and cannot be changed from the dashboard / CLI."""

    code = "env_locked"
    default_message = "환경 변수에서 설정한 값이라 여기서 바꿀 수 없어요."


class QuotaExceededError(PublishError):
    """Instagram's 24-hour publishing quota is used up (numbers come from the API response, never hard-coded)."""

    code = "quota"
    default_message = "오늘 인스타그램 API 게시 한도를 다 썼어요. 내일 다시 시도하거나 앱에서 직접 올려 주세요."

    def __init__(self, message: str | None = None, *, platform: str = "instagram", used: int | None = None,
                 total: int | None = None) -> None:
        if message is None and total is not None:
            message = f"오늘 인스타그램 API 게시 한도({total}개)를 다 썼어요. 내일 다시 시도하거나 앱에서 직접 올려 주세요."
        super().__init__(message, platform=platform)
        self.used = used
        self.total = total

    def extra(self) -> dict[str, Any]:
        return {"used": self.used, "total": self.total}


class RateLimitedError(PublishError):
    """The platform's rate limit (LinkedIn 429, Instagram 80002). ``retry_after`` in seconds when known."""

    http_status = 429
    code = "rate_limited"
    MESSAGES: ClassVar[dict[str, str]] = {
        "linkedin": "LinkedIn 하루 한도에 걸렸어요. 한국 시간 오전 9시(UTC 자정) 이후 다시 시도해 주세요.",
        "instagram": "인스타그램 API 호출 한도에 걸렸어요. 잠시 뒤 다시 시도해 주세요.",
    }
    default_message = "{label} 호출 한도에 걸렸어요. 잠시 뒤 다시 시도해 주세요."

    def __init__(self, message: str | None = None, *, platform: str = "", retry_after: int | None = None) -> None:
        super().__init__(message, platform=platform)
        self.retry_after = retry_after

    @classmethod
    def _default(cls, platform: str) -> str:
        return cls.MESSAGES.get(platform) or super()._default(platform)

    def extra(self) -> dict[str, Any]:
        return {"retry_after": self.retry_after}


class HostingError(PublishError):
    """The public media URL is not reachable from outside (self-check failed: not 200, not image/jpeg, wrong
    bytes, or a redirect). Nothing was created on Instagram."""

    code = "hosting"
    default_message = ("인스타그램이 이미지를 가져갈 공개 주소에 바깥에서 접속되지 않아요. 터널·리버스 프록시가 켜져 있는지, "
                       "미디어 포트를 가리키는지, 비밀번호·봇 차단이 /pub/m/을 막지 않는지 확인해 주세요.")


class RenderError(PublishError):
    """The Instagram card images could not be rendered (no Playwright/Chromium, no Hangul font, timeout). HTTP 422."""

    http_status = 422
    code = "render"
    default_message = "카드 이미지를 그리지 못했어요: 카드 이미지를 그릴 브라우저(Playwright·Chromium)나 한글 글꼴이 없어요."


class PlatformError(PublishError):
    """A definite error answer from LinkedIn / Instagram (nothing was published).

    The platform's own HTTP status and codes are ``platform_status`` / ``platform_code`` / ``platform_subcode``
    (named so they never clobber ``http_status``/``code``, which describe INSIA's own answer: 502
    ``platform_error``). Only codes and the trace id are kept — never the platform's raw body.
    """

    http_status = 502
    code = "platform_error"
    default_message = "{label}에서 오류가 났어요. 글은 올라가지 않았어요."

    def __init__(self, message: str | None = None, *, platform: str = "", platform_status: int = 0,
                 platform_code: str | int = "", platform_subcode: str | int = "", trace_id: str = "") -> None:
        super().__init__(message, platform=platform)
        self.platform_status = int(platform_status or 0)
        self.platform_code = str(platform_code or "")
        self.platform_subcode = str(platform_subcode or "")
        self.trace_id = str(trace_id or "")

    @property
    def error_code(self) -> str:
        """Compact code for ``publish_attempts.error_code``: ``"<code>/<subcode>"``, ``"<code>"`` or the HTTP status."""
        parts = [p for p in (self.platform_code, self.platform_subcode) if p]
        return "/".join(parts) if parts else (str(self.platform_status) if self.platform_status else "")

    def extra(self) -> dict[str, Any]:
        return {"platform": self.platform, "platform_status": self.platform_status,
                "platform_code": self.platform_code, "platform_subcode": self.platform_subcode,
                "trace_id": self.trace_id}


class OutcomeUnknownError(PublishError):
    """The irreversible call was sent but no answer came back (timeout, 5xx, dropped connection): it may have
    been published. The attempt becomes ``unknown`` and the item stays locked until a person resolves it."""

    http_status = 502
    code = "unknown"
    outcome = "unknown"
    MESSAGES: ClassVar[dict[str, str]] = {
        "linkedin": ("LinkedIn의 응답을 받지 못했어요. 글이 올라갔을 수도 있어요. "
                     "LinkedIn 내 활동에서 확인한 뒤 알려 주세요."),
        "instagram": ("인스타그램의 응답을 받지 못했어요. 게시됐을 수도 있어요. "
                      "‘인스타그램에서 다시 확인’을 눌러 확인해 주세요."),
    }
    default_message = "{label}의 응답을 받지 못했어요. 게시됐을 수도 있어요. 확인한 뒤 알려 주세요."

    @classmethod
    def _default(cls, platform: str) -> str:
        return cls.MESSAGES.get(platform) or super()._default(platform)


class ConnectError(PublishError):
    """Connecting an account failed because of the request (HTTP 400). Subclasses name the reason."""

    http_status = 400
    code = "connect_failed"
    default_message = "{label} 계정을 연결하지 못했어요. 다시 시도해 주세요."


class OAuthStateError(ConnectError):
    """Unknown, expired (10 min), reused or cookie-mismatched OAuth state (a pasted URL included)."""

    code = "oauth_state"
    default_message = "연결 요청이 만료됐거나 올바르지 않아요. 다시 연결해 주세요."


class OAuthCancelledError(ConnectError):
    code = "oauth_cancelled"
    default_message = "{label} 연결을 취소했어요."


class InvalidTokenError(ConnectError):
    """A pasted Instagram token was rejected (authentication error from ``/me``)."""

    code = "invalid_token"
    default_message = "토큰이 맞지 않거나 만료됐어요. Meta 개발자 앱의 ‘Generate token’으로 새로 만들어 주세요."


class AccountTypeError(ConnectError):
    """The Instagram account is not a professional (business / creator) account."""

    code = "account_type"
    default_message = "인스타그램 프로페셔널 계정(비즈니스·크리에이터)만 연결할 수 있어요."


class OAuthExchangeError(PublishError):
    """The authorization code could not be exchanged (e.g. ``invalid_redirect_uri``). HTTP 502."""

    http_status = 502
    code = "exchange_failed"
    default_message = ("{label}에서 연결을 마치지 못했어요. 개발자 앱의 Redirect URL이 "
                       "연결 설정에 보이는 주소와 똑같은지 확인해 주세요.")


__all__ = [
    "AI_LABEL_REQUIRED_MESSAGE", "BLOCKED_BY", "BLOCKER_MEDIA_PORT_UNAVAILABLE", "BLOCKER_PUBLIC_URL_MISSING",
    "BLOCKER_RENDER_UNAVAILABLE", "CALLBACK_REDIRECT", "CALLBACK_RESULTS", "CHANNEL_PLATFORM",
    "CONFIRM_CODE_ALPHABET", "CONFIRM_CODE_LENGTH", "FAKE_VIA", "LINKEDIN_CALLBACK_PATH", "LINKEDIN_VISIBILITIES",
    "MEDIA_PATH_PATTERN", "OAUTH_COOKIE_NAME", "OAUTH_COOKIE_PATH", "OAUTH_STATE_TTL_SECONDS", "PLATFORM_LABELS",
    "NOTHING_POSTED", "PREVIEW_HASH_SCHEMA", "PREVIEW_TTL_SECONDS", "PUBLISHED_VIA_API", "PUBLISH_PLATFORMS",
    "READINESS_STATES", "STEP_LABELS", "VISIBILITY_LABELS",
    "AttemptGuard", "CallbackResult", "Clock", "ConnectStart", "HumanConfirmation", "InstagramPreviewOptions",
    "LinkedInPreviewOptions", "Outcome", "PlatformId", "PreviewDraft", "PreviewOptions", "PreviewResult",
    "Publisher", "Readiness", "ReadinessState", "SendOutcome", "SystemClock", "ValidationIssue",
    "canonical_json", "channel_platform", "confirm_code_hash", "new_confirm_code", "normalize_confirm_code",
    "parse_preview_options", "payload_hash", "platform_label", "step_label", "with_nothing_posted",
    "AccountTypeError", "AlreadyPublishedError", "AttemptStateError", "AttemptTakenOverError",
    "ConfirmCodeError", "ConfirmationMismatchError", "ConnectError", "HostingError", "InvalidInputError",
    "InvalidOptionsError", "InvalidTokenError", "ItemLockedError", "NotConfiguredError", "NotConnectedError",
    "NotHumanRequestError", "NotPublishableError", "OAuthCancelledError", "OAuthExchangeError", "OAuthStateError",
    "OutcomeUnknownError", "PlatformError", "PreviewExpiredError", "PublishDisabledError", "PublishError",
    "PublishInProgressError", "PublisherBusyError", "QuotaExceededError", "RateLimitedError", "ReconnectRequiredError",
    "RenderError", "SettingLockedError", "UnavailableError", "ValidationFailedError",
]
