"""``PublishService`` — the one object the server (``server_publish.py``) and the CLI (``cmd_publish_*``) talk to.

Contract skeleton (package A0): every public method's signature and behaviour is fixed here; package A fills
in the bodies. Shapes follow DESIGN.md 5-3 (workspace methods it builds on) and 6-2 (JSON).

Rules every implementation keeps:

* Nothing here runs by itself except token refresh, file cleanup and ownerless-attempt recovery
  (``start_background``). There is no scheduling argument anywhere, and no background retry of a publish:
  one human confirmation = one post.
* ``send`` / ``resolve`` take a ``HumanConfirmation`` and raise ``TypeError`` for anything else. Without a
  valid, unexpired, unused preview whose hash matches, no network request is made.
* Errors are ``PublishError`` subclasses (Korean ``str(exc)``, ``http_status``, ``code``, ``extra()``), plus
  ``db.ItemLockedError`` (= ``PublishInProgressError``) and ``db.NotFoundError`` for unknown ids (404).
* Tokens, client secrets, OAuth codes and states never appear in return values, exceptions, logs or
  ``insia.db`` (the ``confirm_code`` of a CLI preview is the single, deliberate exception: returned once,
  to the terminal that asked for it).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .base import (
    CallbackResult,
    Clock,
    ConnectStart,
    HumanConfirmation,
    PlatformId,
    PreviewResult,
    Readiness,
    SystemClock,
)
from .settings import DEFAULT_SERVER_PORT, PublishSettings

if TYPE_CHECKING:
    from ..db import Workspace
    from ..models import ContentItem, PublishAttempt
    from .http import Transport

_TODO = "package A"


class PublishService:
    """Status, connections, previews, confirmed sends (worker thread + heartbeat), attempt records, recovery.

    One instance per process (the server builds it in ``server_publish.build_publish_service``; the CLI per
    command). It shares the process's ``Workspace``. ``transport`` defaults to the real ``UrllibTransport``
    (allow-listed hosts) or, when ``settings.fake``, the in-memory fake platform; ``clock`` to ``SystemClock``.
    Tests pass ``FakeTransport`` and a fake clock.
    """

    def __init__(self, settings: PublishSettings, workspace: Workspace, *, transport: Transport | None = None,
                 clock: Clock | None = None) -> None:
        self.settings = settings
        self.workspace = workspace
        self.transport = transport
        self.clock: Clock = clock or SystemClock()

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

    # -- status (no network unless check=True) ------------------------------------

    def configured(self) -> bool:
        """True when any platform has stored app info or a connection, or ``settings.env_configured``.
        ``False`` → the dashboard adds nothing to the library screen (``GET /api/items/<id>`` ``publish: null``)."""
        raise NotImplementedError(_TODO)

    def readiness(self, platform: PlatformId) -> Readiness:
        """Local readiness of one platform (disabled / not_configured / not_connected / connected / expiring /
        needs_reconnect / unavailable + Korean reason + blockers). No network."""
        raise NotImplementedError(_TODO)

    def status(self, *, check: bool = False) -> dict[str, Any]:
        """``GET /api/publish`` body (DESIGN.md 6-2): ``{"enabled", "configured", "fake", "media", "platforms":
        {"linkedin": {…}, "instagram": {…}}}``. ``check=True`` (``?check=1`` / ``publish status --check``) also
        validates tokens against the platforms (and refreshes the in-memory LinkedIn name). Secrets only as
        ``*_set`` booleans / ``id_hint``."""
        raise NotImplementedError(_TODO)

    def platform_status(self, platform: PlatformId, *, check: bool = False) -> dict[str, Any]:
        """One ``platforms.<platform>`` block of ``status()`` (also the answer of ``PUT …/linkedin/app``,
        ``POST …/linkedin/complete`` and ``PUT …/instagram/token``)."""
        raise NotImplementedError(_TODO)

    def health_summary(self) -> dict[str, Any]:
        """``GET /api/health`` ``publish`` block: ``{"enabled", "configured", "fake", "linkedin": <state>,
        "instagram": <state>}`` (states from ``READINESS_STATES``). Cheap, no network."""
        raise NotImplementedError(_TODO)

    def item_block(self, item: ContentItem, *, agent_job: bool = False) -> dict[str, Any] | None:
        """The ``publish`` block of ``GET /api/items/<id>`` (DESIGN.md 6-2): ``{"platform", "available", "state",
        "reason", "blocked_by", "active_attempt", "last_attempt"}``; ``platform`` is ``None`` for naver_blog /
        bizplan. Returns ``None`` when publishing is disabled or not ``configured()``. ``agent_job=True`` (the
        server saw a running agent job/run for this item) → ``blocked_by="agent_job"``."""
        raise NotImplementedError(_TODO)

    def summary_line(self) -> str:
        """One Korean line for ``serve`` output, e.g. "API 게시: LinkedIn 연결됨 · 인스타그램 공개 주소 없음(수동 게시)";
        ``""`` when not configured (then ``serve`` prints nothing)."""
        raise NotImplementedError(_TODO)

    def doctor_report(self) -> list[dict[str, str]]:
        """``insia doctor`` publishing checks (DESIGN.md 8-4) as ``[{"id", "level": "ok"|"info"|"warn"|"error",
        "message"}]`` — enabled/fake mode, LinkedIn app/connection/days left/API-version sunset/redirect URI,
        Instagram beta/connection/estimated expiry/media configuration/render, ``credentials/`` permissions.
        Never includes a value (only set / not set)."""
        raise NotImplementedError(_TODO)

    # -- connections -----------------------------------------------------------------

    def save_linkedin_app(self, *, client_id: str | None = None, client_secret: str | None = None,
                          redirect_uri: str | None = None) -> dict[str, Any]:
        """``PUT /api/publish/linkedin/app`` / ``insia publish setup linkedin``. For each field ``None`` keeps the
        stored value and ``""`` deletes it. ``SettingLockedError`` (409) when the app comes from environment
        variables; ``InvalidInputError`` (400) for a bad redirect URI (``settings.check_redirect_uri``). Returns
        ``platform_status("linkedin")`` (no secrets)."""
        raise NotImplementedError(_TODO)

    def linkedin_connect(self, *, request_origin: str = "") -> ConnectStart:
        """``POST /api/publish/linkedin/connect``: new one-time state (256-bit, 10 min, in memory), authorize URL,
        and the mode — ``redirect`` when ``request_origin`` (scheme://host[:port] the dashboard was opened on)
        equals the redirect URI's origin (then ``cookie_value`` is set for the ``insia_oauth`` cookie), else
        ``paste`` (with ``open_url`` only for a loopback / ``public_hosts`` redirect host). ``request_origin=""``
        (CLI) → ``paste``. ``NotConfiguredError`` without app info."""
        raise NotImplementedError(_TODO)

    def linkedin_callback(self, *, code: str = "", state: str = "", error: str = "",
                          cookie: str = "") -> CallbackResult:
        """``GET /oauth/linkedin/callback``: checks state (exists, unused, ≤ 10 min) and the ``insia_oauth``
        cookie HMAC, exchanges the code, reads ``/v2/userinfo``, stores the connection. Never raises for these
        outcomes: returns one of ``CALLBACK_RESULTS`` (``ok``/``cancelled``/``expired``/``exchange_failed``/
        ``invalid``) for ``303 Location: CALLBACK_REDIRECT.format(result=…)``."""
        raise NotImplementedError(_TODO)

    def linkedin_complete(self, url: str) -> dict[str, Any]:
        """``POST /api/publish/linkedin/complete`` / ``connect linkedin --paste``: the pasted address-bar URL (or
        just ``code=…&state=…``). Only states this process issued, once, within 10 minutes; no origin check.
        Errors: ``OAuthStateError`` 400, ``OAuthCancelledError`` 400, ``OAuthExchangeError`` 502,
        ``InvalidInputError`` 400 (wrong path / not http(s)). Returns ``platform_status("linkedin")``."""
        raise NotImplementedError(_TODO)

    def save_instagram_token(self, access_token: str) -> dict[str, Any]:
        """``PUT /api/publish/instagram/token`` / ``connect instagram``: ``/me`` (case-insensitive
        ``account_type``), an immediate refresh attempt (exact expiry, or pasted + 60 days marked estimated),
        then one secrets transaction. ``PublishDisabledError`` when Instagram is off; ``InvalidTokenError`` /
        ``AccountTypeError`` 400. Returns ``platform_status("instagram")`` (never the token)."""
        raise NotImplementedError(_TODO)

    def disconnect(self, platform: PlatformId, *, forget_app: bool = False) -> dict[str, Any]:
        """``DELETE /api/publish/<platform>`` / ``publish disconnect``: deletes the platform's token keys (and app
        info with ``forget_app``) and the connection row. ``PublishInProgressError`` (409) while an attempt is
        ``sending``/``unknown``. Returns ``platform_status(platform)`` plus ``"revoke_hint"`` (Korean: where to
        remove the app's permission on the platform)."""
        raise NotImplementedError(_TODO)

    # -- previews and sending ---------------------------------------------------------

    def preview(self, item_id: str, *, platform: PlatformId | None = None,
                options: Mapping[str, Any] | None = None, via: Literal["dashboard", "cli"],
                requested_by: str, issue_confirm_code: bool = False) -> PreviewResult:
        """Build and store a preview (``publish_previews`` row, 30 min) of exactly what would be sent.

        ``platform`` defaults to the item's channel platform. ``options`` go through
        ``parse_preview_options`` (Instagram requires a bool ``is_ai_generated`` → ``InvalidOptionsError`` 400).
        Checks: enabled/connected/ready, item ``approved``|``scheduled`` and ``approved_version == version``
        (``NotPublishableError``), account id; Instagram renders the JPEGs once into
        ``publish/staging/<preview_id>/`` (``RenderError`` 422, ``PublisherBusyError`` while another render runs).
        Validation errors do **not** raise: they come back in ``errors`` with ``can_publish=False`` (such a
        preview is stored but can never be sent). ``via="cli"`` + ``issue_confirm_code=True`` (only
        ``cmd_publish_send``) sets ``confirm_code`` (stored as its hash only); ``insia publish preview`` never
        issues one. A preview is only sendable from where it was made (``via``)."""
        raise NotImplementedError(_TODO)

    def preview_slide(self, preview_id: str, n: int) -> bytes:
        """JPEG bytes of staged slide ``n`` (1-based) for ``GET /api/publish/previews/<pv>/slides/<n>.jpg``.
        ``db.NotFoundError`` (404) for an unknown/expired preview or slide."""
        raise NotImplementedError(_TODO)

    def send(self, confirmation: HumanConfirmation, *, item_id: str | None = None,
             platform: PlatformId | None = None, background: bool = True,
             on_step: Callable[[str], None] | None = None) -> PublishAttempt:
        """Publish exactly the confirmed preview. ``TypeError`` unless ``confirmation`` is a ``HumanConfirmation``.

        In one workspace transaction (``begin_publish_attempt``): preview exists, unused, unexpired, made by
        ``confirmation.via``, hash matches (and, for ``via="cli"``, the normalized ``confirm_code`` hash matches
        → else ``ConfirmCodeError``), staged files unchanged, item still approved at that version, connected
        account unchanged → ``sending`` attempt with a fresh owner token (the unique index turns a second one
        into ``PublishInProgressError`` / ``AlreadyPublishedError``). ``item_id`` / ``platform`` (from the URL /
        request) must match the preview (``ConfirmationMismatchError``). One worker per platform
        (``PublisherBusyError``). Any failure before the attempt row exists means no request was made.

        ``background=True`` (server): starts the worker thread + heartbeat and returns the ``sending`` attempt
        at once (202 + poll URL). ``background=False`` (CLI): runs the worker in the calling thread, calls
        ``on_step(step)`` on progress, and returns the finished attempt (``published``/``failed``/``unknown``);
        on ``KeyboardInterrupt`` it closes the attempt (``failed`` before the write step, ``unknown`` after)
        and re-raises. For Instagram without a running media listener, the CLI path opens one on
        ``settings.media_port`` only while sending. Worker-stage errors end up in the attempt record
        (``status``/``error_code``/``error``), not as exceptions."""
        raise NotImplementedError(_TODO)

    # -- attempts ---------------------------------------------------------------------

    def get_attempt(self, attempt_id: str) -> PublishAttempt:
        """One attempt (``db.NotFoundError`` → 404)."""
        raise NotImplementedError(_TODO)

    def list_attempts(self, *, item_id: str | None = None, status: str | None = None,
                      limit: int = 50) -> list[PublishAttempt]:
        """Attempts, newest first (``GET /api/items/<id>/publish``, ``insia publish attempts``)."""
        raise NotImplementedError(_TODO)

    def attempt_json(self, attempt: PublishAttempt) -> dict[str, Any]:
        """The attempt JSON of DESIGN.md 6-2: the model's fields (without ``state``) plus ``"progress": {"done",
        "total"}`` (Instagram: slides + 3; LinkedIn: 1)."""
        raise NotImplementedError(_TODO)

    def set_permalink(self, attempt_id: str, url: str, *, by: str) -> PublishAttempt:
        """``PUT /api/publish/attempts/<pa>/permalink``: a person fills in the post URL of a ``published`` attempt
        that has none (also the item's ``published_url``). URL must be https on the platform's allow-listed hosts
        (``InvalidInputError`` 400); ``AttemptStateError`` 409 otherwise."""
        raise NotImplementedError(_TODO)

    def resolve(self, confirmation: HumanConfirmation, outcome: Literal["published", "not_published"], *,
                url: str = "") -> tuple[PublishAttempt, ContentItem | None]:
        """A person closes an ``unknown`` attempt (``confirmation.preview_id`` is the attempt id). ``published`` →
        attempt ``published`` + item ``published`` (``published_via=<platform>_api``, optional checked ``url``);
        ``not_published`` → ``abandoned`` (item untouched). ``TypeError`` without a ``HumanConfirmation``;
        ``AttemptStateError`` 409 unless the attempt is ``unknown``."""
        raise NotImplementedError(_TODO)

    def check_attempt(self, attempt_id: str) -> PublishAttempt:
        """``POST /api/publish/attempts/<pa>/check`` / ``resolve --check``: Instagram read-only reconcile of an
        ``unknown`` attempt (never calls ``media_publish``); returns the updated attempt (may stay ``unknown``).
        LinkedIn cannot be re-read → ``AttemptStateError``."""
        raise NotImplementedError(_TODO)

    # -- maintenance (no human confirmation needed, never publishes) --------------------

    def refresh_tokens(self) -> dict[str, dict[str, Any]]:
        """Refresh due Instagram tokens (``insia publish refresh``, the 6-hour background job, before previews/
        sends). Returns ``{"instagram": {"refreshed": bool, "expires_at": str, "estimated": bool, "message": str}}``
        (no token). Shares no code path with sending and never needs a ``HumanConfirmation``."""
        raise NotImplementedError(_TODO)

    def recover(self) -> list[str]:
        """Close ownerless ``sending`` attempts (``write`` step → ``unknown``, earlier → ``failed``) via
        ``workspace.recover_publish_attempts()`` and clean up their media; returns the attempt ids. Called at
        server start, every minute by the server, and at the start of every CLI ``publish`` command."""
        raise NotImplementedError(_TODO)

    def cleanup(self) -> dict[str, int]:
        """Delete expired/used staging folders and finished/expired public media folders (``.expires`` set to 0
        first). Returns counts, e.g. ``{"staging": 2, "public": 1}``."""
        raise NotImplementedError(_TODO)

    def start_background(self) -> None:
        """Start the server's maintenance thread: every 6 hours ``cleanup()`` + ``refresh_tokens()``. Never sends."""
        raise NotImplementedError(_TODO)

    def start_media_listener(self, host: str) -> tuple[str, int] | None:
        """Configuration A: open the media-only listener (``/pub/m/…`` only, no ``Workspace`` access) on
        ``host`` + ``settings.media_port``; returns the bound ``(host, port)``. ``None`` when no media port is
        set, or when the port cannot be opened (then Instagram becomes ``unavailable`` with
        ``BLOCKER_MEDIA_PORT_UNAVAILABLE`` and the server still starts). Closed by ``shutdown()``."""
        raise NotImplementedError(_TODO)

    def public_media_file(self, url_path: str) -> Path | None:
        """Configuration B (the main port serves ``/pub/m/``): the file to serve for ``url_path`` when
        ``settings.media_mode == "main"``, the path matches ``MEDIA_PATH_PATTERN`` exactly and the folder's
        ``.expires`` is in the future; else ``None`` (→ 404 without body)."""
        raise NotImplementedError(_TODO)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop background threads and the media listener; wait up to ``timeout`` seconds for a running worker."""
        raise NotImplementedError(_TODO)


__all__ = ["PublishService"]
