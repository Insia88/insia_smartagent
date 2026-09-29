"""``PublishService`` — the one object the server (``server_publish.py``) and the CLI (``cmd_publish_*``) talk to.

Shapes follow DESIGN.md 5-3 (the workspace methods it builds on) and 6-2 (JSON).

Rules every part keeps:

* Nothing here runs by itself except token refresh, file cleanup and ownerless-attempt recovery
  (``start_background``, ``recover``). There is no scheduling argument anywhere, and no background retry of a
  publish: one human confirmation = one post.
* ``send`` / ``resolve`` take a ``HumanConfirmation`` and raise ``TypeError`` for anything else. Without a
  valid, unexpired, unused preview whose hash matches, no network request is made.
* The worker writes to the platform only while it holds the attempt's owner token: every progress record is an
  owner-token-conditional UPDATE, a heartbeat thread keeps the attempt alive, and the irreversible call happens
  only after ``claim_write`` succeeded (DESIGN.md 1-7, 5-3). A worker whose attempt was taken over stops silently.
* Errors are ``PublishError`` subclasses (Korean ``str(exc)``, ``http_status``, ``code``, ``extra()``), plus
  ``db.ItemLockedError`` (= ``PublishInProgressError``) and ``db.NotFoundError`` for unknown ids (404).
* Tokens, client secrets, OAuth codes and states never appear in return values, exceptions, logs or
  ``insia.db`` (the ``confirm_code`` of a CLI preview is the single, deliberate exception: returned once,
  to the terminal that asked for it).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib.util
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from ..db import (
    PUBLISH_HEARTBEAT_SECONDS,
    AttemptTakenOverError,
    ItemLockedError,
    NotFoundError,
    PublishStateError,
    WorkspaceError,
    profile_is_empty,
)
from ..models import PublishConnection
from .base import (
    BLOCKER_MEDIA_PORT_UNAVAILABLE,
    BLOCKER_PUBLIC_URL_MISSING,
    BLOCKER_RENDER_UNAVAILABLE,
    CHANNEL_PLATFORM,
    FAKE_VIA,
    PLATFORM_LABELS,
    PREVIEW_HASH_SCHEMA,
    PREVIEW_TTL_SECONDS,
    PUBLISH_PLATFORMS,
    PUBLISHED_VIA_API,
    AttemptStateError,
    CallbackResult,
    Clock,
    ConfirmationMismatchError,
    ConfirmCodeError,
    ConnectError,
    ConnectStart,
    HumanConfirmation,
    InvalidInputError,
    InvalidTokenError,
    AccountTypeError,
    NotConfiguredError,
    NotConnectedError,
    NotPublishableError,
    OAuthExchangeError,
    PlatformError,
    PlatformId,
    PreviewDraft,
    PreviewExpiredError,
    PreviewResult,
    PublishDisabledError,
    PublisherBusyError,
    PublishError,
    AlreadyPublishedError,
    Readiness,
    ReconnectRequiredError,
    RenderError,
    SendOutcome,
    SettingLockedError,
    SystemClock,
    UnavailableError,
    ValidationFailedError,
    ValidationIssue,
    confirm_code_hash,
    new_confirm_code,
    parse_preview_options,
    payload_hash,
    platform_label,
)
from .http import INSTAGRAM_HOSTS, LINKEDIN_HOSTS, FakePlatformTransport, TransportError, UrllibTransport
from .instagram import (
    EXPIRING_DAYS as IG_EXPIRING_DAYS,
    INSTAGRAM_POST_HOSTS,
    JPEG_QUALITY,
    TOKEN_LIFETIME,
    InstagramPublisher,
    account_kind,
    expiry_after,
    refresh_due,
)
from .linkedin import ACCOUNT_KIND as LINKEDIN_ACCOUNT_KIND
from .linkedin import LINKEDIN_POST_HOSTS, LinkedInPublisher, sunset_warning, version_sunset
from .media import PublicMediaHost, resolve_public_file, start_media_listener
from .oauth import (
    LINKEDIN,
    LINKEDIN_PUBLISH_SCOPE,
    CANCEL_ERRORS,
    OAuthStateStore,
    authorize_url,
    check_callback,
    exchange_code,
    fetch_userinfo,
    parse_pasted_callback,
)
from .redact import get_logger, redact_exception, register_secret
from .settings import (
    DEFAULT_SERVER_PORT,
    FAKE_BLOCKED_MESSAGE,
    INSTAGRAM_BETA,
    INSTAGRAM_BETA_OFF_MESSAGE,
    LOOPBACK_NAMES,
    PublishSettings,
    check_redirect_uri,
)
from .store import CredentialStore, CredentialStoreError

if TYPE_CHECKING:
    from ..db import Workspace
    from ..models import ContentItem, ContentItemDetail, PublishAttempt
    from .http import Transport
    from .media import MediaListener

log = get_logger(__name__)

LINKEDIN_EXPIRING_DAYS = 10            # "N일 뒤 연결이 끝나요" from day 50 of 60 (DESIGN.md 2-4)
LINKEDIN_TOKEN_LIFETIME = timedelta(days=60)
MAINTENANCE_SECONDS = 6 * 3600         # cleanup + Instagram token refresh (never a post)
WORKER_JOIN_SECONDS = 5.0

LINKEDIN_TOKEN_KEYS = ("access_token", "scope", "sub", "expires_at", "issued_at")
LINKEDIN_APP_KEYS = ("client_id", "client_secret", "redirect_uri")
INSTAGRAM_TOKEN_KEYS = ("access_token", "user_id", "username", "account_type", "expires_at", "issued_at",
                        "refreshed_at", "expires_estimated", "refresh_failed_at")
PROFESSIONAL_ACCOUNT_TYPES = frozenset({"business", "media_creator"})
NON_PROFESSIONAL_ACCOUNT_TYPES = frozenset({"personal"})
_PRINTABLE = re.compile(r"^[\x21-\x7e]+$")

REVOKE_HINTS = {
    "linkedin": ("INSIA에서 토큰을 지웠어요. LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스에서 앱 권한도 "
                 "지울 수 있어요."),
    "instagram": ("INSIA에서 토큰을 지웠어요. 인스타그램 설정 → 앱 및 웹사이트에서 앱을 지우거나, Meta 개발자 앱에서 "
                  "토큰을 무효화할 수 있어요."),
}
BLOCKED_MESSAGES = {
    "not_approved": "승인한 콘텐츠만 API로 게시할 수 있어요. 먼저 승인해 주세요.",
    "published": "이미 게시 완료로 표시한 콘텐츠예요.",
    "archived": "보관한 콘텐츠는 API로 게시하지 않아요. 먼저 복원해 주세요.",
    "agent_job": "에이전트가 이 콘텐츠를 수정하는 중이에요.",
    "published_attempt": "게시는 됐지만 보관함 상태를 바꾸지 못했어요. ‘게시 완료 표시’를 눌러 주세요.",
}
STATE_SUMMARY = {"connected": "연결됨", "expiring": "곧 만료", "not_connected": "연결 안 됨",
                 "needs_reconnect": "다시 연결 필요", "unavailable": "지금 쓸 수 없음"}
BLOCKER_SUMMARY = {BLOCKER_PUBLIC_URL_MISSING: "공개 주소 없음(수동 게시)",
                   BLOCKER_MEDIA_PORT_UNAVAILABLE: "미디어 포트를 열 수 없음(수동 게시)",
                   BLOCKER_RENDER_UNAVAILABLE: "카드 이미지 렌더링 불가(수동 게시)"}
PUBLIC_URL_REASON = ("인스타그램 API는 이미지를 공개 HTTPS 주소에서 가져가요. 지금 INSIA는 이 컴퓨터에서만 열려 있어서 "
                     "API로 올릴 수 없어요.")
RENDER_REASON = "카드 이미지를 그릴 브라우저(Playwright·Chromium)나 한글 글꼴이 없어요."
INTERRUPTED_BEFORE_WRITE = "중단해서 아무것도 올리지 않았어요."
INTERRUPTED_AFTER_WRITE = {
    "linkedin": "게시 요청을 보낸 뒤 중단했어요. LinkedIn에 올라갔을 수도 있어요. LinkedIn 내 활동에서 확인한 뒤 알려 주세요.",
    "instagram": "게시 요청을 보낸 뒤 중단했어요. 인스타그램에 올라갔을 수도 있어요. ‘인스타그램에서 다시 확인’을 눌러 주세요.",
}
INTERNAL_ERROR = {
    "failed": "INSIA 내부 오류로 게시를 마치지 못했어요. 아무것도 올리지 않았어요.",
    "unknown": "INSIA 내부 오류로 게시 결과를 확인하지 못했어요. 올라갔을 수도 있어요. 플랫폼에서 확인한 뒤 알려 주세요.",
}
# attempt.state keys the attempt JSON shows (never an id the dashboard does not need, never a secret)
ATTEMPT_JSON_STATE = ("item_update_error", "permalink_missing", "is_ai_generated", "visibility", "candidates",
                      "note", "duplicate")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_left(expires: datetime | None, now: datetime) -> int | None:
    if expires is None:
        return None
    return int((expires - now).total_seconds() // 86400)


def _id_hint(value: str) -> str:
    return ("…" + value[-3:]) if value else ""


def _origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an ``http(s)`` URL with the default port filled in; ``None`` if unusable."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


class _WorkerGuard:
    """``AttemptGuard`` for one worker: every write is conditional on the owner token (DESIGN.md 5-3)."""

    def __init__(self, service: "PublishService", attempt_id: str, owner_token: str,
                 on_step: Callable[[str], None] | None = None) -> None:
        self.service = service
        self.attempt_id = attempt_id
        self._token = owner_token
        self.on_step = on_step
        self.write_claimed = False
        self.lost = threading.Event()
        self.media_token = ""

    def __repr__(self) -> str:  # the owner token stays out of every repr / log line
        return f"_WorkerGuard({self.attempt_id!r})"

    def _check(self) -> None:
        if self.lost.is_set():
            raise AttemptTakenOverError(attempt_id=self.attempt_id)

    def _notify(self, step: str) -> None:
        if self.on_step is None:
            return
        try:
            self.on_step(step)
        except Exception:  # noqa: BLE001 - a progress printer must never change the outcome
            log.debug("on_step failed", exc_info=True)

    def step(self, step: str, state_patch: dict[str, Any] | None = None) -> None:
        self._check()
        try:
            self.service.workspace.update_publish_attempt(self.attempt_id, self._token, step=step,
                                                          state_patch=state_patch, now=self.service.clock.now())
        except AttemptTakenOverError:
            self.lost.set()
            raise
        self._notify(step)

    def claim_write(self, step: str = "write") -> None:
        self._check()
        try:
            self.service.workspace.claim_publish_write(self.attempt_id, self._token, step=step,
                                                       now=self.service.clock.now())
        except AttemptTakenOverError:
            self.lost.set()
            raise
        self.write_claimed = True
        self._notify(step)

    def set_media_token(self, media_token: str) -> None:
        """Record the public media folder name first (cleanup and recovery can always find it)."""
        self._check()
        try:
            self.service.workspace.update_publish_attempt(self.attempt_id, self._token, media_token=media_token,
                                                          now=self.service.clock.now())
        except AttemptTakenOverError:
            self.lost.set()
            raise
        self.media_token = media_token

    def heartbeat(self) -> bool:
        try:
            alive = self.service.workspace.heartbeat_publish_attempt(self.attempt_id, self._token,
                                                                     now=self.service.clock.now())
        except WorkspaceError:  # the workspace was closed
            return False
        if not alive:
            self.lost.set()
        return alive

    # used only by the service to close its own attempt
    @property
    def owner_token(self) -> str:
        return self._token


class _Heartbeat:
    """Refreshes ``heartbeat_at`` every ``interval`` seconds from its own thread (also while the worker waits for
    Instagram); a failed beat means the attempt was taken over, so the worker stops at its next step."""

    def __init__(self, service: "PublishService", guard: _WorkerGuard) -> None:
        self.service = service
        self.guard = guard
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"insia-publish-heartbeat-{guard.attempt_id}",
                                       daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while True:
            if self.service.clock.wait(self.stop_event, self.service.heartbeat_seconds) or self.stop_event.is_set():
                return
            try:
                if not self.guard.heartbeat():
                    return
            except Exception:  # noqa: BLE001 - e.g. a locked database: try again next beat
                log.debug("heartbeat failed", exc_info=True)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)


class PublishService:
    """Status, connections, previews, confirmed sends (worker thread + heartbeat), attempt records, recovery.

    One instance per process (the server builds it in ``server_publish.build_publish_service``; the CLI per
    command). It shares the process's ``Workspace``. ``transport`` defaults to the real ``UrllibTransport``
    (allow-listed hosts) or, when ``settings.fake``, the in-memory fake platform; ``clock`` to ``SystemClock``.
    ``renderer`` (tests) replaces the JPEG rendering of Instagram slides. Tests pass ``FakeTransport`` and a fake
    clock.
    """

    def __init__(self, settings: PublishSettings, workspace: Workspace, *, transport: Transport | None = None,
                 clock: Clock | None = None, renderer: Callable[[str, int], list[bytes]] | None = None) -> None:
        self.settings = settings
        self.workspace = workspace
        self.clock: Clock = clock or SystemClock()
        if transport is None:
            if settings.fake:
                transport = FakePlatformTransport()
            else:
                hosts = [*LINKEDIN_HOSTS, *INSTAGRAM_HOSTS]
                if settings.media_host:
                    hosts.append(settings.media_host)  # the public media self-check fetches our own URLs
                transport = UrllibTransport(hosts)
        self.transport: Transport = transport
        self.store = CredentialStore(settings.credentials_dir)
        self.media = PublicMediaHost(settings.publish_dir, now=lambda: self.clock.now().timestamp())
        self.oauth_states = OAuthStateStore(self.clock)
        self.linkedin = LinkedInPublisher(self)
        self.instagram = InstagramPublisher(self)
        self.heartbeat_seconds = PUBLISH_HEARTBEAT_SECONDS
        self._renderer = renderer
        self._lock = threading.RLock()
        self._busy: dict[str, str] = {}                 # platform → attempt id ("" while starting)
        self._threads: list[threading.Thread] = []
        self._render_lock = threading.Lock()            # one Instagram preview render at a time (DESIGN.md 6-4)
        self._refresh_lock = threading.Lock()
        self._linkedin_name: tuple[str, str] = ("", "")  # (sub, name) — memory only (LinkedIn API terms 4.1)
        self._media_listener: MediaListener | None = None
        self._media_error = ""
        self._bg_thread: threading.Thread | None = None
        self._bg_stop = threading.Event()
        self._render_checked: bool | None = None
        for secret in (settings.linkedin_client_secret, settings.ig_app_secret):
            register_secret(secret)

    @classmethod
    def from_env(cls, workspace: Workspace, *, env: Mapping[str, str] | None = None,
                 public_hosts: tuple[str, ...] | list[str] = (), server_port: int = DEFAULT_SERVER_PORT,
                 media_port: int | None = None, media_base_url: str | None = None, trust_proxy: bool = False,
                 transport: Transport | None = None, clock: Clock | None = None) -> "PublishService":
        """``PublishSettings.from_env(env, workspace.home, …)`` + a service for ``workspace`` (convenience for B)."""
        settings = PublishSettings.from_env(env, workspace.home, public_hosts=public_hosts, server_port=server_port,
                                            media_port=media_port, media_base_url=media_base_url,
                                            trust_proxy=trust_proxy)
        return cls(settings, workspace, transport=transport, clock=clock)

    # ------------------------------------------------------------------------------
    # small helpers the platform modules use
    # ------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise PublishDisabledError(self.settings.disabled_reason or None)

    def _require_platform_enabled(self, platform: str) -> None:
        self._require_enabled()
        if platform not in PUBLISH_PLATFORMS:
            raise InvalidInputError(f"API로 게시할 수 없는 플랫폼이에요: {platform!r} (linkedin, instagram만 돼요)")
        if not self.settings.platform_enabled(platform):
            raise PublishDisabledError(INSTAGRAM_BETA_OFF_MESSAGE, platform=platform)

    def _publisher(self, platform: str) -> LinkedInPublisher | InstagramPublisher:
        return self.linkedin if platform == "linkedin" else self.instagram

    def _secrets(self, platform: str) -> dict[str, str]:
        try:
            return self.store.get(platform)
        except CredentialStoreError as exc:
            log.warning("%s", exc)
            return {}

    def linkedin_token(self) -> tuple[str, str]:
        """``(access token, member sub)`` for sending. ``ReconnectRequiredError`` when there is none, it expired or
        the connection needs a reconnect."""
        values = self._secrets("linkedin")
        token, sub = values.get("access_token", ""), values.get("sub", "")
        if not token or not sub:
            raise ReconnectRequiredError(platform="linkedin")
        expires = _parse(values.get("expires_at"))
        if expires is not None and expires <= self._now():
            self.mark_reconnect("linkedin", "LinkedIn 연결이 끝났어요 (60일).")
            raise ReconnectRequiredError(platform="linkedin")
        connection = self.workspace.get_publish_connection("linkedin")
        if connection is not None and connection.status == "needs_reconnect":
            raise ReconnectRequiredError(platform="linkedin")
        return token, sub

    def instagram_token(self) -> tuple[str, str]:
        """``(access token, IG user id)``; ``ReconnectRequiredError`` like ``linkedin_token``."""
        values = self._secrets("instagram")
        token, user_id = values.get("access_token", ""), values.get("user_id", "")
        if not token or not user_id:
            raise ReconnectRequiredError(platform="instagram")
        expires = _parse(values.get("expires_at"))
        if expires is not None and expires <= self._now():
            self.mark_reconnect("instagram", "인스타그램 토큰이 만료됐어요.")
            raise ReconnectRequiredError(platform="instagram")
        connection = self.workspace.get_publish_connection("instagram")
        if connection is not None and connection.status == "needs_reconnect":
            raise ReconnectRequiredError(platform="instagram")
        return token, user_id

    def mark_reconnect(self, platform: str, reason: str = "") -> None:
        try:
            self.workspace.set_publish_connection_status(platform, "needs_reconnect", reason)
        except WorkspaceError:
            log.warning("연결 상태를 바꾸지 못했어요 (%s)", platform)

    def remember_linkedin_name(self, name: str) -> None:
        values = self._secrets("linkedin")
        self._linkedin_name = (values.get("sub", ""), (name or "").strip()[:200])

    def _linkedin_display_name(self, sub: str) -> str:
        cached_sub, name = self._linkedin_name
        return name if sub and cached_sub == sub else ""

    def checked_permalink(self, platform: str, url: str) -> str:
        """``url`` when it is an ``https`` post address on the platform's hosts (the fake mode's
        ``example.invalid`` too), else ``""`` (a warning is logged; nothing unexpected is stored)."""
        value = (url or "").strip()
        if not value:
            return ""
        if self.permalink_ok(platform, value):
            return value
        log.warning("%s가 준 게시물 주소가 예상한 도메인이 아니라 저장하지 않았어요", platform_label(platform))
        return ""

    def permalink_ok(self, platform: str, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme != "https" or not host or parts.username or parts.password or any(c.isspace() for c in url):
            return False
        if len(url) > 2000:
            return False
        allowed = LINKEDIN_POST_HOSTS if platform == "linkedin" else INSTAGRAM_POST_HOSTS
        if self.settings.fake and host == "example.invalid":
            return True
        return host in allowed

    def render_slides(self, page_html: str, count: int) -> list[bytes]:
        """JPEG 1080×1350 (quality 90) of every slide, rendered once per preview (``RenderUnavailable`` otherwise)."""
        if self._renderer is not None:
            return self._renderer(page_html, count)
        from ..exporters.instagram import render_images

        return render_images(page_html, count, image_type="jpeg", quality=JPEG_QUALITY)

    def _render_available(self) -> bool:
        if self._renderer is not None:
            return True
        if (os.environ.get("INSIA_RENDER") or "").strip().lower() in {"0", "false", "no", "off"}:
            return False
        executable = (os.environ.get("INSIA_CHROMIUM") or "").strip()
        if executable and not Path(executable).exists():
            return False
        if self._render_checked is None:
            try:
                self._render_checked = importlib.util.find_spec("playwright") is not None
            except (ImportError, ValueError):
                self._render_checked = False
        return self._render_checked

    # ------------------------------------------------------------------------------
    # status (no network unless check=True)
    # ------------------------------------------------------------------------------

    def configured(self) -> bool:
        """True when any platform has stored app info or a connection, or ``settings.env_configured``.
        ``False`` → the dashboard adds nothing to the library screen (``GET /api/items/<id>`` ``publish: null``)."""
        if not self.settings.enabled:
            return False
        if self.settings.env_configured:
            return True
        try:
            if self.store.has_any():
                return True
        except CredentialStoreError:
            return True  # something is there, even if unreadable
        return bool(self.workspace.list_publish_connections())

    def _linkedin_app(self) -> dict[str, str]:
        stored = self._secrets("linkedin")
        client_id = self.settings.linkedin_client_id or stored.get("client_id", "")
        secret = self.settings.linkedin_client_secret or stored.get("client_secret", "")
        if self.settings.linkedin_client_id:
            source = "env"
        elif stored.get("client_id") or stored.get("client_secret"):
            source = "workspace"
        else:
            source = ""
        return {"client_id": client_id, "client_secret": secret, "source": source,
                "redirect_uri": self.settings.linkedin_redirect_uri_for(stored.get("redirect_uri", ""))}

    def _instagram_blockers(self) -> list[str]:
        blockers: list[str] = []
        if not self.settings.fake:
            if not self.settings.media_valid:
                blockers.append(BLOCKER_PUBLIC_URL_MISSING)
            elif self._media_error:
                blockers.append(BLOCKER_MEDIA_PORT_UNAVAILABLE)
        if not self._render_available():
            blockers.append(BLOCKER_RENDER_UNAVAILABLE)
        return blockers

    def _blocker_reason(self, blocker: str) -> str:
        if blocker == BLOCKER_PUBLIC_URL_MISSING:
            return PUBLIC_URL_REASON
        if blocker == BLOCKER_MEDIA_PORT_UNAVAILABLE:
            return self._media_error or "미디어 전용 포트를 열 수 없어요."
        return RENDER_REASON

    def readiness(self, platform: PlatformId) -> Readiness:
        """Local readiness of one platform (disabled / not_configured / not_connected / connected / expiring /
        needs_reconnect / unavailable + Korean reason + blockers). No network."""
        if platform not in PUBLISH_PLATFORMS:
            raise InvalidInputError(f"API로 게시할 수 없는 플랫폼이에요: {platform!r}")
        if not self.settings.enabled:
            return Readiness(platform, "disabled", False, self.settings.disabled_reason or "API 게시가 꺼져 있어요.")
        now = self._now()
        connection = self.workspace.get_publish_connection(platform)
        if platform == "linkedin":
            values = self._secrets("linkedin")
            if not values.get("access_token") or not values.get("sub"):
                app = self._linkedin_app()
                if not app["client_id"] or not app["client_secret"]:
                    return Readiness(platform, "not_configured", False, NotConfiguredError(platform="linkedin").args[0])
                return Readiness(platform, "not_connected", False, "LinkedIn 계정을 연결해야 해요.")
            expires = _parse(values.get("expires_at"))
            if connection is not None and connection.status == "needs_reconnect":
                return Readiness(platform, "needs_reconnect", False, "LinkedIn 연결이 끝났거나 해제됐어요. 다시 연결해 주세요.")
            if expires is not None and expires <= now:
                return Readiness(platform, "needs_reconnect", False, "LinkedIn 연결이 끝났어요(60일). 다시 연결해 주세요.")
            days = _days_left(expires, now)
            if days is not None and days <= LINKEDIN_EXPIRING_DAYS:
                return Readiness(platform, "expiring", True,
                                 f"LinkedIn 연결이 {max(days, 0)}일 뒤 끝나요. 다시 연결하면 60일 연장돼요.")
            name = self._linkedin_display_name(values.get("sub", ""))
            who = f"{name}({LINKEDIN_ACCOUNT_KIND})" if name else LINKEDIN_ACCOUNT_KIND
            return Readiness(platform, "connected", True, f"{who}에 올려요.")
        # instagram
        if not self.settings.instagram_enabled:
            return Readiness(platform, "disabled", False, INSTAGRAM_BETA_OFF_MESSAGE)
        blockers = tuple(self._instagram_blockers())
        values = self._secrets("instagram")
        if not values.get("access_token") or not values.get("user_id"):
            return Readiness(platform, "not_connected", False,
                             "인스타그램 계정이 연결되지 않았어요. 브랜드·자료 → API 게시 연결에서 토큰을 붙여 넣어 주세요.", blockers)
        expires = _parse(values.get("expires_at"))
        if connection is not None and connection.status == "needs_reconnect":
            return Readiness(platform, "needs_reconnect", False, "인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요.",
                             blockers)
        if expires is not None and expires <= now:
            return Readiness(platform, "needs_reconnect", False, "인스타그램 토큰이 만료됐어요. 새 토큰을 붙여 넣어 주세요.",
                             blockers)
        if blockers:
            return Readiness(platform, "unavailable", False, self._blocker_reason(blockers[0]), blockers)
        days = _days_left(expires, now)
        handle = values.get("username", "")
        if values.get("refresh_failed_at") and days is not None and days <= 15:
            return Readiness(platform, "expiring", True,
                             f"인스타그램 토큰을 자동으로 갱신하지 못했어요. {max(days, 0)}일 뒤 끝나요. 새 토큰을 붙여 넣어 주세요.")
        if days is not None and days <= IG_EXPIRING_DAYS:
            return Readiness(platform, "expiring", True,
                             f"인스타그램 연결이 {max(days, 0)}일 뒤 끝나요. 새 토큰을 붙여 넣어 주세요.")
        who = f"@{handle}" if handle else "연결된 인스타그램 계정"
        return Readiness(platform, "connected", True, f"{who}에 올려요.")

    def _media_json(self) -> dict[str, Any]:
        media = self.settings.media_json()
        if self.settings.media_mode == "listener":
            media["listening"] = self._media_listener is not None
            if self._media_error:
                media["valid"] = False
                media["reason"] = self._media_error
        return media

    def status(self, *, check: bool = False) -> dict[str, Any]:
        """``GET /api/publish`` body (DESIGN.md 6-2): ``{"enabled", "configured", "fake", "media", "platforms":
        {"linkedin": {…}, "instagram": {…}}}``. ``check=True`` also validates tokens against the platforms."""
        return {
            "enabled": self.settings.enabled,
            "configured": self.configured(),
            "fake": self.settings.fake,
            "media": self._media_json(),
            "platforms": {platform: self.platform_status(platform, check=check) for platform in PUBLISH_PLATFORMS},
        }

    def platform_status(self, platform: PlatformId, *, check: bool = False) -> dict[str, Any]:
        """One ``platforms.<platform>`` block of ``status()`` (also the answer of ``PUT …/linkedin/app``,
        ``POST …/linkedin/complete`` and ``PUT …/instagram/token``)."""
        if platform not in PUBLISH_PLATFORMS:
            raise InvalidInputError(f"API로 게시할 수 없는 플랫폼이에요: {platform!r}")
        if check and self.settings.platform_enabled(platform):
            self._check_connection(platform)
        ready = self.readiness(platform)
        now = self._now()
        block: dict[str, Any] = {"label": PLATFORM_LABELS[platform],
                                 "channels": [ch for ch, p in CHANNEL_PLATFORM.items() if p == platform],
                                 "state": ready.state, "ready": ready.ready, "reason": ready.reason,
                                 "blockers": list(ready.blockers)}
        if platform == "linkedin":
            app = self._linkedin_app()
            values = self._secrets("linkedin")
            block["app"] = {"client_id_set": bool(app["client_id"]), "client_secret_set": bool(app["client_secret"]),
                            "source": app["source"], "redirect_uri": app["redirect_uri"],
                            "redirect_uri_source": ("env" if self.settings.linkedin_redirect_uri else
                                                    "workspace" if values.get("redirect_uri") else "default")}
            sub = values.get("sub", "")
            if values.get("access_token") and sub:
                expires = _parse(values.get("expires_at"))
                block["account"] = {"id_hint": _id_hint(sub), "name": self._linkedin_display_name(sub),
                                    "kind": LINKEDIN_ACCOUNT_KIND}
                block["token"] = {"expires_at": values.get("expires_at", ""), "days_left": _days_left(expires, now),
                                  "estimated": False, "scopes": [s for s in values.get("scope", "").split() if s]}
            else:
                block["account"] = None
                block["token"] = None
            version = self.settings.linkedin_version
            block["api_version"] = version
            block["api_version_sunset"] = version_sunset(version)
            block["api_version_warning"] = sunset_warning(version, now.date())
            return block
        values = self._secrets("instagram")
        block["beta"] = INSTAGRAM_BETA
        block["enabled_by"] = self.settings.instagram_enabled_by
        user_id = values.get("user_id", "")
        if values.get("access_token") and user_id:
            expires = _parse(values.get("expires_at"))
            handle = values.get("username", "")
            block["account"] = {"id_hint": _id_hint(user_id), "username": f"@{handle}" if handle else "",
                                "account_type": values.get("account_type", ""),
                                "kind": account_kind(values.get("account_type", ""))}
            block["token"] = {"expires_at": values.get("expires_at", ""), "days_left": _days_left(expires, now),
                              "estimated": values.get("expires_estimated") == "1",
                              "refreshed_at": values.get("refreshed_at", ""), "auto_refresh": True,
                              "refresh_failed": bool(values.get("refresh_failed_at"))}
        else:
            block["account"] = None
            block["token"] = None
        block["api_version"] = self.settings.ig_api_version
        block["requirements"] = {"public_https": self.settings.fake or (self.settings.media_valid and not self._media_error),
                                 "render": self._render_available()}
        return block

    def _check_connection(self, platform: str) -> None:
        """``?check=1``: validate the stored token against the platform (never raises)."""
        try:
            if platform == "linkedin":
                values = self._secrets("linkedin")
                if values.get("access_token"):
                    self.linkedin.account()
            else:
                values = self._secrets("instagram")
                if values.get("access_token"):
                    self._refresh_instagram()
                    token, user_id = self.instagram_token()
                    me = self.instagram.lookup(token)
                    if me["user_id"] != user_id:
                        self.mark_reconnect("instagram", "연결된 계정이 바뀌었어요.")
        except ReconnectRequiredError:
            self.mark_reconnect(platform, "토큰이 만료됐거나 해제됐어요.")
        except (PublishError, TransportError, WorkspaceError) as exc:
            log.info("%s 연결을 확인하지 못했어요: %s", platform_label(platform), redact_exception(exc))

    def health_summary(self) -> dict[str, Any]:
        """``GET /api/health`` ``publish`` block (cheap, no network)."""
        summary: dict[str, Any] = {"enabled": self.settings.enabled, "configured": self.configured(),
                                   "fake": self.settings.fake}
        for platform in PUBLISH_PLATFORMS:
            summary[platform] = self.readiness(platform).state
        return summary

    @staticmethod
    def _blocked_by(item: ContentItem) -> str:
        if item.status == "archived":
            return "archived"
        if item.status == "published":
            return "published"
        if item.status not in ("approved", "scheduled"):
            return "not_approved"
        if int(item.approved_version or 0) != max(1, int(item.version or 0)):
            return "version_changed"
        return ""

    @staticmethod
    def _blocked_message(blocked_by: str, item: ContentItem) -> str:
        if blocked_by == "version_changed":
            return (f"승인한 뒤에 내용이 바뀌었어요 (지금 v{item.version}, 승인한 버전 v{item.approved_version}). "
                    "다시 승인하면 API로 게시할 수 있어요.")
        return BLOCKED_MESSAGES.get(blocked_by, "승인한 최신 버전만 API로 게시할 수 있어요.")

    def _published_attempt(self, item: ContentItem, platform: str) -> PublishAttempt | None:
        for attempt in self.workspace.list_publish_attempts(item_id=item.id, status="published", limit=20):
            if attempt.platform == platform and attempt.version == item.version:
                return attempt
        return None

    def item_block(self, item: ContentItem, *, agent_job: bool = False) -> dict[str, Any] | None:
        """The ``publish`` block of ``GET /api/items/<id>`` (DESIGN.md 6-2); ``None`` when publishing is disabled
        or not ``configured()``."""
        if not self.settings.enabled or not self.configured():
            return None
        platform = CHANNEL_PLATFORM.get(item.channel)
        block: dict[str, Any] = {"platform": platform, "available": False, "state": "", "reason": "",
                                 "blocked_by": "", "active_attempt": None, "last_attempt": None}
        if platform is None:
            return block
        ready = self.readiness(platform)
        block["state"] = ready.state
        block["blockers"] = list(ready.blockers)
        active = self.workspace.active_publish_attempt(item.id)
        attempts = [a for a in self.workspace.list_publish_attempts(item_id=item.id, limit=20) if a.platform == platform]
        if active is not None:
            block["active_attempt"] = self.attempt_json(active)
        if attempts:
            block["last_attempt"] = self.attempt_json(attempts[0])
        blocked = ""
        if agent_job:
            blocked = "agent_job"
        elif item.status != "published" and self._published_attempt(item, platform) is not None:
            blocked = "published_attempt"
        else:
            blocked = self._blocked_by(item)
        block["blocked_by"] = blocked
        if blocked:
            block["reason"] = self._blocked_message(blocked, item)
        elif active is not None:
            block["reason"] = ItemLockedError(status=active.status).args[0]
        else:
            block["reason"] = ready.reason
        block["available"] = bool(ready.ready and not blocked and active is None)
        return block

    def summary_line(self) -> str:
        """One Korean line for ``serve`` output; ``""`` when not configured (then ``serve`` prints nothing)."""
        if not self.settings.enabled or not self.configured():
            return ""
        parts: list[str] = []
        for platform in PUBLISH_PLATFORMS:
            ready = self.readiness(platform)
            if ready.state in ("disabled", "not_configured"):
                continue
            if ready.state == "unavailable" and ready.blockers:
                text = BLOCKER_SUMMARY.get(ready.blockers[0], STATE_SUMMARY["unavailable"])
            else:
                text = STATE_SUMMARY.get(ready.state, ready.state)
            parts.append(f"{PLATFORM_LABELS[platform]} {text}")
        if not parts:
            return ""
        prefix = "API 게시(가짜 게시 모드, 실제로 올라가지 않아요)" if self.settings.fake else "API 게시"
        return f"{prefix}: {' · '.join(parts)}"

    def doctor_report(self) -> list[dict[str, str]]:
        """``insia doctor`` publishing checks (DESIGN.md 8-4). Never includes a value (only set / not set)."""
        out: list[dict[str, str]] = []

        def add(check_id: str, level: str, message: str) -> None:
            out.append({"id": check_id, "level": level, "message": message})

        settings = self.settings
        if settings.fake_requested and not settings.fake:
            add("publish.enabled", "warn", FAKE_BLOCKED_MESSAGE + " 그래서 API 게시 기능 전체가 꺼져 있어요.")
        elif not settings.enabled:
            add("publish.enabled", "info", settings.disabled_reason or "API 게시가 꺼져 있어요.")
        else:
            add("publish.enabled", "ok", "API 게시 기능이 켜져 있어요 (사람이 확인하고 누를 때만 한 건씩 올려요).")
        if settings.fake:
            add("publish.fake", "warn", "가짜 게시 모드(INSIA_PUBLISH_FAKE)가 켜져 있어요. 실제로 올라가지 않아요 (테스트용).")
        for warning in settings.warnings:
            add("publish.settings", "warn", warning)
        if not settings.enabled:
            return out
        if not self.configured():
            add("publish.configured", "info", "API 게시를 설정하지 않았어요. 지금처럼 파일을 받아 직접 올리면 돼요.")
            return out
        now = self._now()
        # LinkedIn
        app = self._linkedin_app()
        if app["client_id"] and app["client_secret"]:
            where = "환경 변수" if app["source"] == "env" else "워크스페이스"
            add("linkedin.app", "ok", f"LinkedIn 앱 정보: 설정됨 ({where})")
        else:
            missing = [name for name, key in (("Client ID", "client_id"), ("Client Secret", "client_secret"))
                       if not app[key]]
            add("linkedin.app", "info", f"LinkedIn 앱 정보가 없어요: {', '.join(missing)}")
        why = check_redirect_uri(app["redirect_uri"])
        add("linkedin.redirect_uri", "warn" if why else "ok",
            f"LinkedIn Redirect URI: {why}" if why else "LinkedIn Redirect URI 형식이 맞아요.")
        ready = self.readiness("linkedin")
        level = {"connected": "ok", "expiring": "warn", "needs_reconnect": "warn"}.get(ready.state, "info")
        add("linkedin.connection", level, f"LinkedIn: {STATE_SUMMARY.get(ready.state, ready.state)} — {ready.reason}")
        warning = sunset_warning(settings.linkedin_version, now.date())
        add("linkedin.api_version", "warn" if warning else "ok",
            warning or f"LinkedIn API 버전 {settings.linkedin_version} (지원 종료 {version_sunset(settings.linkedin_version)})")
        # Instagram
        if not settings.instagram_enabled:
            add("instagram.beta", "info", INSTAGRAM_BETA_OFF_MESSAGE)
        else:
            add("instagram.beta", "ok", "인스타그램 API 게시(시험 중)가 켜져 있어요.")
            ready = self.readiness("instagram")
            level = {"connected": "ok", "expiring": "warn", "needs_reconnect": "warn", "unavailable": "warn"}.get(
                ready.state, "info")
            add("instagram.connection", level, f"인스타그램: {STATE_SUMMARY.get(ready.state, ready.state)} — {ready.reason}")
            values = self._secrets("instagram")
            if values.get("access_token") and values.get("expires_estimated") == "1":
                add("instagram.expiry", "info", "인스타그램 토큰 만료일은 추정값이에요. 발급 24시간 뒤 자동으로 갱신해서 정확히 맞춰요.")
            media = self._media_json()
            if settings.fake:
                add("instagram.media", "info", "가짜 게시 모드라 공개 이미지 주소를 쓰지 않아요.")
            elif media["valid"]:
                mode = "미디어 전용 포트(구성 A)" if media["mode"] == "listener" else "대시보드 포트(구성 B)"
                add("instagram.media", "ok", f"이미지 공개 주소: {media['url']} · {mode}")
            else:
                add("instagram.media", "warn", f"이미지 공개 주소를 쓸 수 없어요: {media['reason'] or PUBLIC_URL_REASON}")
            add("instagram.render", "ok" if self._render_available() else "warn",
                "카드 이미지 렌더링(Playwright·Chromium)을 쓸 수 있어요." if self._render_available() else RENDER_REASON)
        problems = self.store.permission_problems()
        if problems:
            add("credentials.permissions", "warn", " ".join(problems) + " (chmod 700 credentials, chmod 600 secrets.sqlite)")
        elif self.store.exists() and os.name == "posix":
            add("credentials.permissions", "ok", "credentials 폴더·파일 권한이 맞아요 (0700/0600).")
        return out

    # ------------------------------------------------------------------------------
    # connections
    # ------------------------------------------------------------------------------

    def save_linkedin_app(self, *, client_id: str | None = None, client_secret: str | None = None,
                          redirect_uri: str | None = None) -> dict[str, Any]:
        """``PUT /api/publish/linkedin/app`` / ``insia publish setup linkedin``. ``None`` keeps a stored value and
        ``""`` deletes it. Fields set by environment variables are locked (``SettingLockedError``)."""
        self._require_enabled()
        locked = [label for label, value, env in (("Client ID", client_id, self.settings.linkedin_client_id),
                                                  ("Client Secret", client_secret, self.settings.linkedin_client_secret),
                                                  ("Redirect URI", redirect_uri, self.settings.linkedin_redirect_uri))
                  if value is not None and env]
        if locked:
            raise SettingLockedError(f"환경 변수에서 설정한 값이라 여기서 바꿀 수 없어요: {', '.join(locked)}",
                                     platform="linkedin")
        values: dict[str, str | None] = {}
        if client_id is not None:
            value = client_id.strip()
            if value and (len(value) > 200 or not _PRINTABLE.match(value)):
                raise InvalidInputError("Client ID는 띄어쓰기 없는 영문·숫자여야 해요.", platform="linkedin")
            values["client_id"] = value or None
        if client_secret is not None:
            value = client_secret.strip()
            if value and (len(value) > 500 or not _PRINTABLE.match(value)):
                raise InvalidInputError("Client Secret 형식이 올바르지 않아요 (띄어쓰기 없는 영문·숫자·기호).", platform="linkedin")
            if value:
                register_secret(value)
            values["client_secret"] = value or None
        if redirect_uri is not None:
            value = redirect_uri.strip()
            if value:
                why = check_redirect_uri(value)
                if why:
                    raise InvalidInputError(why, platform="linkedin")
            values["redirect_uri"] = value or None
        if values:
            self.store.set_many("linkedin", values)
        return self.platform_status("linkedin")

    def linkedin_connect(self, *, request_origin: str = "") -> ConnectStart:
        """``POST /api/publish/linkedin/connect``: a one-time state (10 min), the authorize URL and the mode —
        ``redirect`` when the dashboard's origin equals the redirect URI's (then ``cookie_value``), else ``paste``."""
        self._require_platform_enabled("linkedin")
        app = self._linkedin_app()
        if not app["client_id"] or not app["client_secret"]:
            raise NotConfiguredError(platform="linkedin")
        redirect = app["redirect_uri"]
        why = check_redirect_uri(redirect)
        if why:
            raise InvalidInputError(f"LinkedIn Redirect URI를 확인해 주세요: {why}", platform="linkedin")
        state = self.oauth_states.issue()
        url = authorize_url(LINKEDIN, app["client_id"], redirect, state)
        target = _origin(redirect)
        if request_origin and target is not None and _origin(request_origin) == target:
            return ConnectStart(mode="redirect", authorize_url=url, redirect_uri=redirect,
                                cookie_value=self.oauth_states.cookie_value(state))
        open_url = ""
        if target is not None:
            scheme, host, port = target
            if host.strip("[]") in LOOPBACK_NAMES or host in self.settings.public_hosts:
                default = 443 if scheme == "https" else 80
                shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
                open_url = f"{scheme}://{shown}{'' if port == default else f':{port}'}/#/brand/connections"
        return ConnectStart(mode="paste", authorize_url=url, redirect_uri=redirect, open_url=open_url)

    def linkedin_callback(self, *, code: str = "", state: str = "", error: str = "", cookie: str = "") -> CallbackResult:
        """``GET /oauth/linkedin/callback``: one of ``CALLBACK_RESULTS`` (never raises for these outcomes)."""
        register_secret(code)
        register_secret(state)
        if not self.settings.enabled or not state:
            return "invalid"
        check = self.oauth_states.check(state)
        if check in ("unknown", "used"):
            return "invalid"
        if not self.oauth_states.cookie_matches(state, cookie):
            return "invalid"  # not the browser that asked (login CSRF); the state stays usable for it
        used = self.oauth_states.consume(state)
        if used == "expired":
            return "expired"
        if used != "ok":
            return "invalid"
        if error:
            return "cancelled" if error in CANCEL_ERRORS else "exchange_failed"
        if not code:
            return "invalid"
        try:
            self._finish_linkedin_connect(code)
        except (OAuthExchangeError, ConnectError, PlatformError, NotConfiguredError, TransportError) as exc:
            log.warning("LinkedIn 연결을 마치지 못했어요: %s", redact_exception(exc))
            return "exchange_failed"
        return "ok"

    def linkedin_complete(self, url: str) -> dict[str, Any]:
        """``POST /api/publish/linkedin/complete`` / ``connect linkedin --paste``: the pasted address-bar URL."""
        self._require_platform_enabled("linkedin")
        app = self._linkedin_app()
        if not app["client_id"] or not app["client_secret"]:
            raise NotConfiguredError(platform="linkedin")
        parts = parse_pasted_callback(url, app["redirect_uri"])
        check_callback(self.oauth_states, state=parts["state"], code=parts["code"], error=parts["error"])
        self._finish_linkedin_connect(parts["code"])
        return self.platform_status("linkedin")

    def _finish_linkedin_connect(self, code: str) -> None:
        app = self._linkedin_app()
        if not app["client_id"] or not app["client_secret"]:
            raise NotConfiguredError(platform="linkedin")
        token_data = exchange_code(self.transport, LINKEDIN, code=code, client_id=app["client_id"],
                                   client_secret=app["client_secret"], redirect_uri=app["redirect_uri"])
        token = token_data["access_token"]
        scopes = [s for s in token_data["scope"].replace(",", " ").split() if s]
        if scopes and LINKEDIN_PUBLISH_SCOPE not in scopes:
            raise ConnectError("LinkedIn이 게시 권한(w_member_social)을 주지 않았어요. 개발자 앱 Products 탭에 "
                               "‘Share on LinkedIn’을 추가한 뒤 다시 연결해 주세요.", platform="linkedin")
        try:
            status, body = fetch_userinfo(self.transport, token)
        except TransportError:
            raise OAuthExchangeError("LinkedIn 계정 정보를 받지 못했어요. 인터넷 연결을 확인한 뒤 다시 연결해 주세요.",
                                     platform="linkedin") from None
        sub = str(body.get("sub") or "") if status == 200 else ""
        if not sub:
            raise OAuthExchangeError("LinkedIn 계정 정보(sub)를 받지 못했어요. 개발자 앱에 ‘Sign In with LinkedIn using "
                                     "OpenID Connect’가 있는지 확인한 뒤 다시 연결해 주세요.", platform="linkedin")
        now = self._now()
        expires = now + (timedelta(seconds=token_data["expires_in"]) if token_data["expires_in"] > 0
                         else LINKEDIN_TOKEN_LIFETIME)
        self.store.set_many("linkedin", {"access_token": token, "scope": " ".join(scopes), "sub": sub,
                                         "expires_at": _iso(expires), "issued_at": _iso(now)})
        self.workspace.save_publish_connection(PublishConnection(
            platform="linkedin", account_id=sub, account_name="", scopes=scopes, status="connected",
            token_expires_at=_iso(expires), token_issued_at=_iso(now), api_version=self.settings.linkedin_version))
        self._linkedin_name = (sub, str(body.get("name") or "").strip()[:200])

    def save_instagram_token(self, access_token: str) -> dict[str, Any]:
        """``PUT /api/publish/instagram/token`` / ``connect instagram``: ``/me`` (case-insensitive ``account_type``),
        an immediate refresh attempt (exact expiry, or pasted + 60 days marked estimated), one secrets
        transaction. Returns ``platform_status("instagram")`` (never the token)."""
        self._require_platform_enabled("instagram")
        token = (access_token or "").strip()
        if not token or len(token) > 4096 or not _PRINTABLE.match(token):
            raise InvalidTokenError("토큰 형식이 올바르지 않아요. Meta 개발자 앱에서 복사한 토큰 전체를 붙여 넣어 주세요.",
                                    platform="instagram")
        register_secret(token)
        try:
            me = self.instagram.lookup(token)
        except ReconnectRequiredError:
            raise InvalidTokenError(platform="instagram") from None
        account_type = me["account_type"].strip().lower()
        if account_type in NON_PROFESSIONAL_ACCOUNT_TYPES:
            raise AccountTypeError(platform="instagram")
        if account_type and account_type not in PROFESSIONAL_ACCOUNT_TYPES:
            log.warning("인스타그램 계정 유형을 알 수 없어요 (%s). 일단 연결해요.", account_type[:30])
        now = self._now()
        refreshed = None
        try:
            refreshed = self.instagram.try_refresh(token)
        except (ReconnectRequiredError, TransportError):
            refreshed = None  # a token younger than 24 hours cannot be refreshed yet (/me just accepted it)
        if refreshed is not None:
            token, expires_in = refreshed
            expires, estimated, refreshed_at = expiry_after(now, expires_in), False, _iso(now)
        else:
            expires, estimated, refreshed_at = now + TOKEN_LIFETIME, True, None
        values: dict[str, str | None] = {
            "access_token": token, "user_id": me["user_id"], "username": me["username"], "account_type": me["account_type"],
            "expires_at": _iso(expires), "issued_at": _iso(now), "refreshed_at": refreshed_at,
            "expires_estimated": "1" if estimated else "0", "refresh_failed_at": None,
        }
        self.store.set_many("instagram", values)
        handle = me["username"]
        self.workspace.save_publish_connection(PublishConnection(
            platform="instagram", account_id=me["user_id"], account_name=f"@{handle}" if handle else "",
            status="connected", token_expires_at=_iso(expires), expires_estimated=estimated, token_issued_at=_iso(now),
            token_refreshed_at=refreshed_at or "", api_version=self.settings.ig_api_version))
        return self.platform_status("instagram")

    def disconnect(self, platform: PlatformId, *, forget_app: bool = False) -> dict[str, Any]:
        """``DELETE /api/publish/<platform>``: deletes the token keys (and app info with ``forget_app``) and the
        connection row. ``PublishInProgressError`` (409) while an attempt is ``sending``/``unknown``."""
        if platform not in PUBLISH_PLATFORMS:
            raise InvalidInputError(f"API로 게시할 수 없는 플랫폼이에요: {platform!r}")
        active = self.workspace.active_publish_attempts(platform)
        if active:
            raise ItemLockedError("게시 중이거나 확인이 필요한 기록이 있어요. 먼저 정리한 뒤 연결을 해제해 주세요.",
                                  attempt_id=active[0].id, platform=platform, status=active[0].status)
        keys = list(LINKEDIN_TOKEN_KEYS if platform == "linkedin" else INSTAGRAM_TOKEN_KEYS)
        if forget_app and platform == "linkedin":
            keys += list(LINKEDIN_APP_KEYS)
        self.store.delete(platform, keys)
        self.workspace.delete_publish_connection(platform)
        if platform == "linkedin":
            self._linkedin_name = ("", "")
        result = self.platform_status(platform)
        result["revoke_hint"] = REVOKE_HINTS[platform]
        return result

    # ------------------------------------------------------------------------------
    # previews and sending
    # ------------------------------------------------------------------------------

    def _item_platform(self, item: ContentItem, platform: str | None) -> PlatformId:
        expected = CHANNEL_PLATFORM.get(item.channel)
        if expected is None:
            raise NotPublishableError("네이버 블로그·사업계획서는 API로 게시하지 않아요. 파일을 받아 직접 올린 뒤 "
                                      "‘게시 완료 표시’를 눌러 주세요.")
        if platform is not None and platform != expected:
            raise NotPublishableError(f"이 콘텐츠는 {platform_label(expected)}용이라 {platform_label(platform)}에 올릴 수 "
                                      "없어요.", platform=platform)
        return expected

    def _require_ready(self, platform: str) -> Readiness:
        self._require_platform_enabled(platform)
        ready = self.readiness(platform)  # type: ignore[arg-type]
        if ready.ready:
            return ready
        if ready.state == "not_configured":
            raise NotConfiguredError(platform=platform)
        if ready.state == "not_connected":
            raise NotConnectedError(ready.reason, platform=platform)
        if ready.state == "needs_reconnect":
            raise ReconnectRequiredError(platform=platform)
        if ready.state == "unavailable":
            raise UnavailableError(ready.reason, platform=platform, blockers=ready.blockers)
        raise PublishDisabledError(ready.reason or None, platform=platform)

    def _require_publishable(self, item: ContentItem, platform: str) -> None:
        active = self.workspace.active_publish_attempt(item.id)
        if active is not None:
            raise ItemLockedError(attempt_id=active.id, platform=active.platform, status=active.status)
        blocked = self._blocked_by(item)
        if blocked:
            raise NotPublishableError(self._blocked_message(blocked, item), platform=platform, blocked_by=blocked)
        done = self._published_attempt(item, platform)
        if done is not None:
            raise AlreadyPublishedError(platform=platform, permalink=done.permalink, attempt_id=done.id)

    def preview(self, item_id: str, *, platform: PlatformId | None = None,
                options: Mapping[str, Any] | None = None, via: Literal["dashboard", "cli"],
                requested_by: str, issue_confirm_code: bool = False) -> PreviewResult:
        """Build and store a preview (``publish_previews`` row, 30 min) of exactly what would be sent. Validation
        errors come back in ``errors`` (``can_publish=False``); such a preview is stored but never sendable."""
        self._require_enabled()
        if via not in ("dashboard", "cli"):
            raise InvalidInputError("미리보기를 만든 곳(via)은 dashboard 또는 cli여야 해요.")
        if issue_confirm_code and via != "cli":
            raise InvalidInputError("확인 코드는 터미널의 insia publish send에서만 만들어요.")
        detail = self.workspace.get_item(item_id)
        if detail is None:
            raise NotFoundError(f"콘텐츠 {item_id}를 찾을 수 없어요")
        item = detail.item
        chosen = self._item_platform(item, platform)
        opts = parse_preview_options(chosen, options)
        self._require_ready(chosen)
        self._require_publishable(item, chosen)
        profile = self.workspace.get_profile()
        if profile_is_empty(profile):
            profile = None
        if chosen == "instagram":
            self._refresh_instagram()
            if not self._render_lock.acquire(blocking=False):
                raise PublisherBusyError("다른 미리보기의 카드 이미지를 그리는 중이에요. 끝난 뒤 다시 눌러 주세요.",
                                         platform="instagram")
            try:
                draft = self.instagram.build_preview(detail, profile, opts)
            finally:
                self._render_lock.release()
        else:
            draft = self.linkedin.build_preview(detail, profile, opts)
        return self._store_preview(detail, draft, via=via, requested_by=requested_by,
                                   issue_confirm_code=issue_confirm_code)

    def _store_preview(self, detail: ContentItemDetail, draft: PreviewDraft, *, via: str, requested_by: str,
                       issue_confirm_code: bool) -> PreviewResult:
        item = detail.item
        wrapped: dict[str, Any] = {
            "schema": PREVIEW_HASH_SCHEMA, "platform": draft.platform, "account_id": draft.account_id,
            "item_id": item.id, "version": item.version, "api_version": draft.api_version, "options": draft.options,
            draft.platform: draft.payload,
        }
        if draft.errors:  # part of the hash: a preview with errors can never be sent
            wrapped["errors"] = sorted({issue.code for issue in draft.errors})
        digest = payload_hash(wrapped)
        code = new_confirm_code() if issue_confirm_code else None
        row = self.workspace.create_publish_preview(
            item.id, item.version, draft.platform, draft.account_id, wrapped, digest, created_via=via,
            confirm_code_hash=confirm_code_hash(code) if code else "", requested_by=requested_by,
            ttl_seconds=PREVIEW_TTL_SECONDS, now=self._now())
        slides: list[dict[str, Any]] = []
        if draft.slide_bytes:
            try:
                written = self.media.write_staging(row.id, draft.slide_bytes)
            except (OSError, ValueError) as exc:
                self.workspace.mark_publish_preview_used(row.id, now=self._now())
                raise RenderError(f"카드 이미지를 저장하지 못했어요: {exc}", platform=draft.platform) from None
            expected = [str(s.get("sha256") or "") for s in draft.slides]
            if [w["sha256"] for w in written] != expected:
                self.workspace.mark_publish_preview_used(row.id, now=self._now())
                self.media.remove_staging(row.id)
                raise RenderError("카드 이미지를 저장하는 중에 내용이 달라졌어요. 다시 시도해 주세요.", platform=draft.platform)
            slides = [{**s, "url": f"/api/publish/previews/{row.id}/slides/{s['n']}.jpg"} for s in draft.slides]
        else:
            slides = [{**s, "url": ""} for s in draft.slides]
        return PreviewResult(
            preview_id=row.id, preview_hash=digest, expires_at=row.expires_at, platform=draft.platform,
            item={"id": item.id, "version": item.version, "title": item.title, "channel": item.channel,
                  "approved_version": item.approved_version, "approval_forced": item.approval_forced,
                  "approved_score": item.approved_score},
            account=dict(draft.account), content=dict(draft.content), slides=slides, errors=list(draft.errors),
            warnings=list(draft.warnings), notices=list(draft.notices), quota=draft.quota,
            first_comment_link=draft.first_comment_link, request_preview=list(draft.request_preview),
            confirm_code=code,
        )

    def preview_slide(self, preview_id: str, n: int) -> bytes:
        """JPEG bytes of staged slide ``n`` (1-based). ``db.NotFoundError`` for an unknown/expired preview or slide."""
        preview = self.workspace.get_publish_preview(preview_id)
        if preview is None or preview.platform != "instagram":
            raise NotFoundError("미리보기 이미지를 찾을 수 없어요.")
        expires = _parse(preview.expires_at)
        if expires is None or expires <= self._now():
            raise NotFoundError("미리보기가 만료됐어요. 다시 만들어 주세요.")
        data = self.media.staged_image(preview.id, int(n))
        if data is None:
            raise NotFoundError("미리보기 이미지를 찾을 수 없어요.")
        return data

    def _map_state_error(self, exc: PublishStateError, platform: str) -> Exception:
        reason, info = exc.reason, exc.info
        if reason in ("preview_missing", "preview_used"):
            return PreviewExpiredError(str(exc), platform=platform)
        if reason == "preview_expired":
            return PreviewExpiredError(platform=platform)
        if reason in ("preview_via", "hash_mismatch", "account_changed"):
            return ConfirmationMismatchError(str(exc), platform=platform)
        if reason == "not_publishable":
            return NotPublishableError(str(exc), platform=platform, blocked_by=str(info.get("blocked_by") or ""))
        if reason == "already_published":
            return AlreadyPublishedError(platform=platform, permalink=str(info.get("permalink") or ""),
                                         attempt_id=str(info.get("attempt_id") or ""))
        if reason == "not_connected":
            return ReconnectRequiredError(platform=platform)
        if reason == "attempt_state":
            return AttemptStateError(str(exc), platform=platform)
        return PublishError(str(exc), platform=platform)

    def _check_staged(self, preview_id: str, payload: Mapping[str, Any]) -> None:
        block = payload.get("instagram") if isinstance(payload.get("instagram"), Mapping) else {}
        slides = list(block.get("slides") or []) if isinstance(block, Mapping) else []
        if not slides:
            raise ConfirmationMismatchError("미리보기에 카드 이미지가 없어요. 다시 확인해 주세요.", platform="instagram")
        for slide in slides:
            data = self.media.staged_image(preview_id, int(slide.get("n") or 0))
            if data is None or hashlib.sha256(data).hexdigest() != str(slide.get("sha256") or ""):
                raise ConfirmationMismatchError("확인한 카드 이미지가 없어졌거나 바뀌었어요. 다시 확인해 주세요.",
                                                platform="instagram")

    def send(self, confirmation: HumanConfirmation, *, item_id: str | None = None,
             platform: PlatformId | None = None, background: bool = True,
             on_step: Callable[[str], None] | None = None) -> PublishAttempt:
        """Publish exactly the confirmed preview (see the module docstring). ``TypeError`` unless ``confirmation``
        is a ``HumanConfirmation``. Any failure before the attempt row exists means no request was made."""
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("PublishService.send needs a HumanConfirmation: a person must confirm the exact preview")
        self._require_enabled()
        preview = self.workspace.get_publish_preview(confirmation.preview_id)
        if preview is None:
            raise PreviewExpiredError("미리보기를 찾을 수 없어요. 다시 확인해 주세요.")
        target = preview.platform
        if item_id is not None and item_id != preview.item_id:
            raise ConfirmationMismatchError("다른 콘텐츠의 미리보기예요. 다시 확인해 주세요.", platform=target)
        if platform is not None and platform != target:
            raise ConfirmationMismatchError("다른 플랫폼의 미리보기예요. 다시 확인해 주세요.", platform=target)
        if preview.created_via != confirmation.via:
            raise ConfirmationMismatchError("다른 곳에서 만든 미리보기라 여기서 보낼 수 없어요. 다시 확인해 주세요.",
                                            platform=target)
        if preview.used_at:
            raise PreviewExpiredError("이미 사용한 미리보기예요. 다시 확인해 주세요.", platform=target)
        expires = _parse(preview.expires_at)
        if expires is None or expires <= self._now():
            raise PreviewExpiredError(platform=target)
        if not hmac.compare_digest(preview.payload_hash, confirmation.preview_hash) or \
                payload_hash(preview.payload) != preview.payload_hash:
            raise ConfirmationMismatchError(platform=target)
        if preview.payload.get("errors"):
            codes = ", ".join(str(c) for c in preview.payload.get("errors") or [])
            raise ValidationFailedError([ValidationIssue("error", "preview_errors",
                                                         f"미리보기에 고칠 부분이 있어서 게시할 수 없어요 ({codes}).")],
                                        platform=target)
        if confirmation.via == "cli" and not self.workspace.check_confirm_code(preview.id, confirmation.confirm_code):
            self.workspace.mark_publish_preview_used(preview.id, now=self._now())  # one try per code
            raise ConfirmCodeError(platform=target)
        self._require_ready(target)
        if target == "instagram":
            self._check_staged(preview.id, preview.payload)
        with self._lock:
            if target in self._busy:
                raise PublisherBusyError(platform=target)
            self._busy[target] = ""
        try:
            if target == "instagram":
                self._refresh_instagram()
            options = preview.payload.get("options") if isinstance(preview.payload.get("options"), Mapping) else {}
            if target == "instagram":
                block = preview.payload.get("instagram") or {}
                state = {"is_ai_generated": options.get("is_ai_generated") is True,
                         "slides": len(block.get("slides") or [])}
            else:
                state = {"visibility": str(options.get("visibility") or "PUBLIC")}
            try:
                attempt, owner_token = self.workspace.begin_publish_attempt(
                    preview.id, confirmation.preview_hash, via=confirmation.via,
                    requested_by=confirmation.requested_by, state=state, now=self._now())
            except PublishStateError as exc:
                raise self._map_state_error(exc, target) from None
            with self._lock:
                self._busy[target] = attempt.id
        except BaseException:
            with self._lock:
                if self._busy.get(target) == "":
                    self._busy.pop(target, None)
            raise
        guard = _WorkerGuard(self, attempt.id, owner_token, on_step=None if background else on_step)
        payload = dict(preview.payload)
        if not background:
            return self._work(attempt, payload, guard, cli=True)
        thread = threading.Thread(target=self._work_quietly, args=(attempt, payload, guard),
                                  name=f"insia-publish-{target}", daemon=True)
        try:
            thread.start()
        except BaseException:  # the worker never ran: close the attempt as not sent
            self._abort_start(attempt, guard)
            raise
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        return attempt

    def _abort_start(self, attempt: PublishAttempt, guard: _WorkerGuard) -> None:
        try:
            self.workspace.finish_publish_failure(attempt.id, guard.owner_token, status="failed", error_code="internal",
                                                  error=INTERNAL_ERROR["failed"], now=self._now())
        except (AttemptTakenOverError, WorkspaceError):
            pass
        self.workspace.release_publish_worker(attempt.id)
        with self._lock:
            if self._busy.get(attempt.platform) == attempt.id:
                self._busy.pop(attempt.platform, None)

    @contextlib.contextmanager
    def _cli_media_listener(self, platform: str) -> Iterator[None]:
        """CLI Instagram send without a server: open the media-only listener on the media port while sending
        (a running server's listener already serves the same folder when the port is taken)."""
        opened = None
        if (platform == "instagram" and not self.settings.fake and self.settings.media_mode == "listener"
                and self.settings.media_port is not None and self._media_listener is None):
            try:
                opened = start_media_listener("127.0.0.1", int(self.settings.media_port), self.media.public_root)
            except OSError:
                opened = None  # someone (the dashboard server) already serves it
        try:
            yield
        finally:
            if opened is not None:
                opened.close()

    def _work_quietly(self, attempt: PublishAttempt, payload: dict[str, Any], guard: _WorkerGuard) -> None:
        try:
            self._work(attempt, payload, guard)
        except BaseException as exc:  # noqa: BLE001 - a background worker never raises into nothing
            log.error("게시 워커가 멈췄어요 (%s): %s", attempt.id, redact_exception(exc))

    def _work(self, attempt: PublishAttempt, payload: dict[str, Any], guard: _WorkerGuard, *,
              cli: bool = False) -> PublishAttempt:
        platform = attempt.platform
        heartbeat = _Heartbeat(self, guard)
        listener: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
        try:
            heartbeat.start()
            if cli:
                listener = self._cli_media_listener(platform)
            try:
                with listener:
                    outcome: SendOutcome | None = self._publisher(platform).send(attempt, payload, guard)
            except AttemptTakenOverError:
                log.warning("%s (%s)", AttemptTakenOverError.DEFAULT_MESSAGE, attempt.id)
                outcome = None
            except KeyboardInterrupt:
                self._close_interrupted(attempt, guard)
                raise
            except Exception as exc:  # noqa: BLE001 - a bug must still close the attempt the right way
                log.error("게시 중 예상하지 못한 오류 (%s): %s", attempt.id, redact_exception(exc))
                status: Literal["failed", "unknown"] = "unknown" if guard.write_claimed else "failed"
                outcome = SendOutcome(status, error_code="internal", error=INTERNAL_ERROR[status])
            if outcome is not None:
                self._record_outcome(attempt, guard, outcome)
        finally:
            heartbeat.stop()
            self.workspace.release_publish_worker(attempt.id)
            try:
                self._finish_files(attempt.id)
            except Exception:  # noqa: BLE001 - cleanup is best effort (the 6-hour job retries)
                log.warning("게시 시도 %s의 파일을 정리하지 못했어요", attempt.id, exc_info=True)
            with self._lock:
                if self._busy.get(platform) == attempt.id:
                    self._busy.pop(platform, None)
        final = self.workspace.get_publish_attempt(attempt.id)
        return final if final is not None else attempt

    def _close_interrupted(self, attempt: PublishAttempt, guard: _WorkerGuard) -> None:
        """Ctrl+C in the CLI: ``failed`` before the write step, ``unknown`` after (DESIGN.md 1-7)."""
        if guard.write_claimed:
            status, message = "unknown", INTERRUPTED_AFTER_WRITE.get(attempt.platform, INTERNAL_ERROR["unknown"])
        else:
            status, message = "failed", INTERRUPTED_BEFORE_WRITE
        try:
            self.workspace.finish_publish_failure(attempt.id, guard.owner_token, status=status, error_code="interrupted",
                                                  error=message, now=self._now())
        except (AttemptTakenOverError, WorkspaceError):
            pass

    def _record_outcome(self, attempt: PublishAttempt, guard: _WorkerGuard, outcome: SendOutcome) -> None:
        platform = attempt.platform
        state = dict(outcome.state)
        if outcome.status == "published":
            via = FAKE_VIA if self.settings.fake else PUBLISHED_VIA_API[platform]
            permalink = self.checked_permalink(platform, outcome.permalink)
            if outcome.permalink and not permalink:
                state["permalink_rejected"] = True
            if not permalink:
                state.setdefault("permalink_missing", True)
            try:
                self.workspace.finish_publish_success(attempt.id, guard.owner_token, external_id=outcome.external_id,
                                                      permalink=permalink, via=via, state_patch=state, now=self._now())
            except AttemptTakenOverError:
                # the post exists but the attempt was closed meanwhile (recovery after the write step → 'unknown',
                # then maybe a person's resolve or an Instagram re-check): a confirmed success is never dropped
                self._record_late_success(attempt, outcome.external_id, permalink, via, state)
            return
        try:
            self.workspace.finish_publish_failure(attempt.id, guard.owner_token, status=outcome.status,
                                                  error_code=outcome.error_code, error=outcome.error, state_patch=state,
                                                  now=self._now())
        except AttemptTakenOverError:
            log.warning("%s (%s)", AttemptTakenOverError.DEFAULT_MESSAGE, attempt.id)

    def _record_late_success(self, attempt: PublishAttempt, external_id: str, permalink: str, via: str,
                             state: dict[str, Any]) -> None:
        """A success the worker could no longer record as the owner (``Workspace.record_late_publish_success``);
        every case where a person's earlier decision is overridden or cannot be, is logged."""
        try:
            final, _ = self.workspace.record_late_publish_success(attempt.id, external_id=external_id,
                                                                  permalink=permalink, via=via, state_patch=state,
                                                                  now=self._now())
        except Exception as exc:  # noqa: BLE001 - the worker must still finish; the log keeps the post id
            log.error("게시는 됐지만 기록하지 못했어요 (%s, 게시물 %s): %s", attempt.id, external_id or "-",
                      redact_exception(exc))
            return
        before = str((final.state.get("late_success") or {}).get("status_before") or "")
        if final.status != "published":
            log.warning("게시 시도 %s는 이미 '%s'로 정리됐는데 플랫폼이 게시 성공(게시물 %s)을 알려 왔어요. 같은 버전의 다른 "
                        "게시 기록이 있어 상태를 바꾸지 않았어요. 중복 게시인지 확인해 주세요.", attempt.id, before,
                        external_id or "-")
        elif before in ("failed", "abandoned"):
            log.warning("게시 시도 %s는 '%s'로 정리됐지만 플랫폼이 게시 성공(게시물 %s)을 알려 와서 게시 완료로 바꿨어요.",
                        attempt.id, before, external_id or "-")
        elif before == "published":
            log.info("게시 시도 %s에 늦게 온 게시물 정보(%s)를 채웠어요.", attempt.id, external_id or "-")
        else:
            log.info("게시 시도 %s: 복구 뒤에 온 게시 성공 응답을 기록했어요 (게시물 %s).", attempt.id, external_id or "-")

    def _finish_files(self, attempt_id: str) -> None:
        """After an attempt: staged preview images go at once; public images go at once unless the attempt is
        ``unknown`` (kept 24 hours, like the container)."""
        attempt = self.workspace.get_publish_attempt(attempt_id)
        if attempt is None:
            return
        media_token = self.workspace.publish_attempt_media_token(attempt_id)
        if media_token and attempt.status in ("published", "failed", "abandoned"):
            self.media.expire(media_token)
        if attempt.status != "sending" and attempt.preview_id:
            self.media.remove_staging(attempt.preview_id)

    # ------------------------------------------------------------------------------
    # attempts
    # ------------------------------------------------------------------------------

    def get_attempt(self, attempt_id: str) -> PublishAttempt:
        attempt = self.workspace.get_publish_attempt(attempt_id)
        if attempt is None:
            raise NotFoundError(f"게시 시도 {attempt_id}를 찾을 수 없어요")
        return attempt

    def list_attempts(self, *, item_id: str | None = None, status: str | None = None,
                      limit: int = 50) -> list[PublishAttempt]:
        return self.workspace.list_publish_attempts(item_id=item_id, status=status, limit=limit)

    def attempt_json(self, attempt: PublishAttempt) -> dict[str, Any]:
        """The attempt JSON of DESIGN.md 6-2 (the model's fields without ``state``) plus ``"progress": {"done",
        "total"}`` and a few safe state notes (``item_update_error``, ``permalink_missing`` …)."""
        data = attempt.model_dump(mode="json", exclude={"state"})
        state = attempt.state or {}
        if attempt.platform == "instagram":
            slides = int(state.get("slides") or 0)
            total = slides + 3
            step = attempt.step or ""
            if attempt.status == "published":
                done = total
            elif step.startswith("children "):
                try:
                    done = int(step.split()[1].split("/")[0])
                except (IndexError, ValueError):
                    done = 0
            else:
                done = {"polling": slides, "carousel": slides + 1, "write": slides + 2, "permalink": slides + 2}.get(step, 0)
        else:
            total, done = 1, 1 if attempt.status == "published" else 0
        data["progress"] = {"done": min(done, total), "total": total}
        for key in ATTEMPT_JSON_STATE:
            if key in state:
                data[key] = state[key]
        data.setdefault("item_update_error", "")
        return data

    def set_permalink(self, attempt_id: str, url: str, *, by: str) -> PublishAttempt:
        """A person fills in the post URL of a ``published`` attempt that has none (also the item's
        ``published_url``). ``InvalidInputError`` for a URL off the platform's hosts, ``AttemptStateError`` otherwise."""
        attempt = self.get_attempt(attempt_id)
        value = (url or "").strip()
        if not self.permalink_ok(attempt.platform, value):
            example = ("LinkedIn 게시물 주소(https://www.linkedin.com/…)" if attempt.platform == "linkedin"
                       else "인스타그램 게시물 주소(https://www.instagram.com/p/…)")
            raise InvalidInputError(f"{example}를 넣어 주세요.", platform=attempt.platform)
        try:
            return self.workspace.set_publish_permalink(attempt_id, value, by=by, now=self._now())
        except PublishStateError as exc:
            raise AttemptStateError(str(exc), platform=attempt.platform) from None

    def resolve(self, confirmation: HumanConfirmation, outcome: Literal["published", "not_published"], *,
                url: str = "") -> tuple[PublishAttempt, ContentItem | None]:
        """A person closes an ``unknown`` attempt (``confirmation.preview_id`` is the attempt id)."""
        if not isinstance(confirmation, HumanConfirmation):
            raise TypeError("PublishService.resolve needs a HumanConfirmation: a person must say what happened")
        if outcome not in ("published", "not_published"):
            raise InvalidInputError("결과는 published(올라갔어요) 또는 not_published(안 올라갔어요)여야 해요.")
        attempt = self.get_attempt(confirmation.preview_id)
        value = (url or "").strip()
        if outcome == "not_published" and value:
            raise InvalidInputError("안 올라갔다면 게시물 주소는 넣지 않아요.", platform=attempt.platform)
        if value and not self.permalink_ok(attempt.platform, value):
            example = ("LinkedIn 게시물 주소(https://www.linkedin.com/…)" if attempt.platform == "linkedin"
                       else "인스타그램 게시물 주소(https://www.instagram.com/p/…)")
            raise InvalidInputError(f"{example}를 넣어 주세요.", platform=attempt.platform)
        if attempt.status != "unknown":
            raise AttemptStateError("결과 확인이 필요한 기록만 정리할 수 있어요.", platform=attempt.platform)
        via = FAKE_VIA if self.settings.fake else PUBLISHED_VIA_API[attempt.platform]
        how = "올라갔어요" if outcome == "published" else "안 올라갔어요"
        try:
            result = self.workspace.resolve_publish_attempt(
                attempt.id, outcome, url=value, resolved_by=f"{confirmation.requested_by} · {how}"[:200], via=via,
                now=self._now())
        except PublishStateError as exc:
            raise AttemptStateError(str(exc), platform=attempt.platform) from None
        self._finish_files(attempt.id)
        return result

    def check_attempt(self, attempt_id: str) -> PublishAttempt:
        """Instagram read-only reconcile of an ``unknown`` attempt (never calls ``media_publish``)."""
        attempt = self.get_attempt(attempt_id)
        if attempt.status != "unknown":
            raise AttemptStateError("결과 확인이 필요한 기록만 다시 확인할 수 있어요.", platform=attempt.platform)
        if attempt.platform != "instagram":
            raise AttemptStateError("LinkedIn 게시물은 INSIA가 다시 읽을 수 없어요. LinkedIn 내 활동에서 확인한 뒤 "
                                    "‘올라갔어요’ 또는 ‘안 올라갔어요’를 눌러 주세요.", platform=attempt.platform)
        self._require_platform_enabled("instagram")
        outcome = self.instagram.reconcile(attempt, None)
        via = FAKE_VIA if self.settings.fake else PUBLISHED_VIA_API["instagram"]
        try:
            if outcome is None:
                result, _ = self.workspace.reconcile_publish_attempt(
                    attempt.id, status="unknown", error="인스타그램에서 확인하지 못했어요. 잠시 뒤 다시 확인하거나 앱에서 확인한 뒤 알려 주세요.",
                    now=self._now())
                return result
            state = dict(outcome.state)
            if outcome.status == "published":
                permalink = self.checked_permalink("instagram", outcome.permalink)
                if not permalink:
                    state.setdefault("permalink_missing", True)
                result, _ = self.workspace.reconcile_publish_attempt(
                    attempt.id, status="published", external_id=outcome.external_id, permalink=permalink, via=via,
                    resolved_by="instagram_check", state_patch=state, now=self._now())
            else:
                result, _ = self.workspace.reconcile_publish_attempt(
                    attempt.id, status="failed", error_code=outcome.error_code, error=outcome.error,
                    resolved_by="instagram_check", state_patch=state, now=self._now())
        except PublishStateError as exc:
            raise AttemptStateError(str(exc), platform="instagram") from None
        self._finish_files(attempt.id)
        return result

    # ------------------------------------------------------------------------------
    # maintenance (no human confirmation needed, never publishes)
    # ------------------------------------------------------------------------------

    def _record_refresh_failure(self) -> None:
        try:
            self.store.set_many("instagram", {"refresh_failed_at": _iso(self._now())})
        except CredentialStoreError:
            pass

    def _refresh_instagram(self) -> dict[str, Any]:
        result: dict[str, Any] = {"refreshed": False, "expires_at": "", "estimated": False, "message": ""}
        if not self.settings.instagram_enabled:
            result["message"] = "인스타그램 API 게시가 꺼져 있어요."
            return result
        with self._refresh_lock:
            values = self._secrets("instagram")
            token = values.get("access_token", "")
            if not token:
                result["message"] = "연결된 인스타그램 계정이 없어요."
                return result
            result["expires_at"] = values.get("expires_at", "")
            result["estimated"] = values.get("expires_estimated") == "1"
            now = self._now()
            connection = self.workspace.get_publish_connection("instagram")
            if connection is not None and connection.status == "needs_reconnect":
                result["message"] = "인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요."
                return result
            if not refresh_due(values, now):
                days = _days_left(_parse(values.get("expires_at")), now)
                result["message"] = "아직 갱신할 때가 아니에요." + (f" ({days}일 남음)" if days is not None else "")
                return result
            try:
                got = self.instagram.try_refresh(token)
            except TransportError:
                self._record_refresh_failure()
                result["message"] = "인스타그램에 연결하지 못해 토큰을 갱신하지 못했어요. 나중에 다시 시도해요."
                return result
            except ReconnectRequiredError:
                try:
                    self.instagram.lookup(token)
                except ReconnectRequiredError:
                    self.mark_reconnect("instagram", "토큰이 만료됐거나 해제됐어요.")
                    result["message"] = "인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요."
                    return result
                except (PlatformError, TransportError):
                    pass
                self._record_refresh_failure()
                result["message"] = "인스타그램 토큰을 갱신하지 못했어요. 나중에 다시 시도해요."
                return result
            if got is None:
                self._record_refresh_failure()
                result["message"] = "인스타그램이 아직 토큰 갱신을 받지 않았어요 (발급 뒤 24시간이 지나야 해요)."
                return result
            new_token, expires_in = got
            expires = expiry_after(now, expires_in)
            self.store.set_many("instagram", {"access_token": new_token, "expires_at": _iso(expires),
                                              "refreshed_at": _iso(now), "expires_estimated": "0",
                                              "refresh_failed_at": None})
            if connection is not None:
                self.workspace.save_publish_connection(connection.model_copy(update={
                    "token_expires_at": _iso(expires), "expires_estimated": False, "token_refreshed_at": _iso(now),
                    "status": "connected", "status_reason": ""}))
            days = _days_left(expires, now)
            result.update({"refreshed": True, "expires_at": _iso(expires), "estimated": False,
                           "message": f"인스타그램 토큰을 갱신했어요 ({days}일 남음)."})
            return result

    def refresh_tokens(self) -> dict[str, dict[str, Any]]:
        """Refresh due Instagram tokens (``insia publish refresh``, the 6-hour job, before previews/sends).
        Never needs a ``HumanConfirmation`` and shares no code path with sending."""
        if not self.settings.enabled:
            return {"instagram": {"refreshed": False, "expires_at": "", "estimated": False,
                                  "message": self.settings.disabled_reason or "API 게시가 꺼져 있어요."}}
        return {"instagram": self._refresh_instagram()}

    def _after_recovery(self, attempt_ids: list[str]) -> None:
        for attempt_id in attempt_ids:
            try:
                self._finish_files(attempt_id)
            except Exception:  # noqa: BLE001 - best effort
                log.warning("정리한 게시 시도 %s의 파일을 지우지 못했어요", attempt_id, exc_info=True)

    def recover(self) -> list[str]:
        """Close ownerless ``sending`` attempts (``write`` step → ``unknown``, earlier → ``failed``) and clean up
        their files; returns the attempt ids. Server start, every minute (workspace watcher), every CLI command."""
        closed = self.workspace.recover_publish_attempts(now=self._now())
        self._after_recovery(closed)
        return closed

    def cleanup(self) -> dict[str, int]:
        """Delete expired/used staging folders and finished/expired public media folders (``.expires`` set to 0
        first). Returns counts, e.g. ``{"staging": 2, "public": 1}``."""
        now = self._now()
        dead = self.workspace.purge_publish_previews(now)
        keep = [a.preview_id for a in self.workspace.active_publish_attempts() if a.status == "sending"]
        finished = self.workspace.finished_media_tokens(older_than=now - timedelta(hours=24))
        return self.media.cleanup(finished_tokens=finished, dead_previews=dead, keep_previews=keep)

    def _maintenance_once(self) -> None:
        try:
            if not self.settings.enabled or not self.configured():
                return
        except WorkspaceError:
            return  # closed
        except Exception:  # noqa: BLE001 - e.g. a locked database: try again next round
            log.warning("API 게시 정리 작업을 시작하지 못했어요", exc_info=True)
            return
        for label, job in (("파일 정리", self.cleanup), ("인스타그램 토큰 갱신", self.refresh_tokens)):
            try:
                job()
            except WorkspaceError:
                return  # closed
            except Exception:  # noqa: BLE001 - try again next round
                log.warning("API 게시 %s에 실패했어요", label, exc_info=True)

    def start_background(self) -> None:
        """The server's maintenance thread: at start and every 6 hours ``cleanup()`` + ``refresh_tokens()``. Never
        sends. Also lets the workspace's recovery loop clean up the files of attempts it closes."""
        if not self.settings.enabled:
            return
        with self._lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return
            self.workspace.publish_recovery_hook = self._after_recovery
            stop = self._bg_stop = threading.Event()

            def loop() -> None:
                while not stop.is_set():
                    self._maintenance_once()
                    if self.clock.wait(stop, MAINTENANCE_SECONDS):
                        return

            self._bg_thread = threading.Thread(target=loop, name="insia-publish-maintenance", daemon=True)
            self._bg_thread.start()

    def start_media_listener(self, host: str) -> tuple[str, int] | None:
        """Configuration A: open the media-only listener on ``host`` + ``settings.media_port``; ``None`` when not
        configured or when the port cannot be opened (Instagram then shows ``media_port_unavailable``)."""
        if not self.settings.enabled or self.settings.media_port is None or self.settings.media_mode != "listener":
            return None
        with self._lock:
            if self._media_listener is not None:
                return self._media_listener.address
            port = int(self.settings.media_port)
            try:
                self._media_listener = start_media_listener(host, port, self.media.public_root)
            except OSError as exc:
                self._media_error = f"미디어 포트 {port}을(를) 열 수 없어요. 다른 프로그램이 쓰고 있는지 확인해 주세요."
                log.warning("%s (%s)", self._media_error, exc.__class__.__name__)
                return None
            self._media_error = ""
            return self._media_listener.address

    def public_media_file(self, url_path: str) -> Path | None:
        """Configuration B: the file to serve for ``url_path`` (exact ``MEDIA_PATH_PATTERN``, unexpired), else ``None``."""
        if not self.settings.enabled or self.settings.media_mode != "main":
            return None
        return resolve_public_file(self.media.public_root, url_path, now=self.clock.now().timestamp())

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop background threads and the media listener; wait up to ``timeout`` seconds for a running worker."""
        self._bg_stop.set()
        if getattr(self.workspace, "publish_recovery_hook", None) == self._after_recovery:
            self.workspace.publish_recovery_hook = None
        with self._lock:
            listener, self._media_listener = self._media_listener, None
            threads = [t for t in self._threads if t.is_alive()]
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        deadline = max(0.0, float(timeout))
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=deadline)
        bg = self._bg_thread
        if bg is not None and bg.is_alive() and bg is not threading.current_thread():
            bg.join(timeout=min(1.0, deadline))


__all__ = ["PublishService"]
