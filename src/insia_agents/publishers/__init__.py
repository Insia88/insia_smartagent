"""Human-confirmed API publishing to LinkedIn and Instagram (DESIGN.md; never automatic).

Only two places import this package: ``server_publish.py`` (the server's publishing handlers) and the
``cmd_publish_*`` / ``_publish_*`` functions of ``cli.py``. The pipeline, planner, actions, importer, agents,
backends and the ``run-due`` / calendar commands never do (AST guard tests). A post is sent only through
``PublishService.send(HumanConfirmation(...))`` after a person confirmed an exact preview.

Modules: ``base`` (types, errors, protocols, ``HumanConfirmation``), ``settings`` (environment), ``http``
(transport + ``FakeTransport``), ``service`` (``PublishService``); package A adds ``store``, ``redact``,
``oauth``, ``linkedin``, ``instagram`` and ``media``.
"""

from __future__ import annotations

from .base import (
    AI_LABEL_REQUIRED_MESSAGE,
    CALLBACK_REDIRECT,
    CALLBACK_RESULTS,
    CHANNEL_PLATFORM,
    LINKEDIN_CALLBACK_PATH,
    MEDIA_PATH_PATTERN,
    OAUTH_COOKIE_NAME,
    OAUTH_COOKIE_PATH,
    OAUTH_STATE_TTL_SECONDS,
    PLATFORM_LABELS,
    PUBLISH_PLATFORMS,
    AccountTypeError,
    AlreadyPublishedError,
    AttemptGuard,
    AttemptStateError,
    AttemptTakenOverError,
    Clock,
    ConfirmationMismatchError,
    ConfirmCodeError,
    ConnectError,
    ConnectStart,
    HostingError,
    HumanConfirmation,
    InstagramPreviewOptions,
    InvalidInputError,
    InvalidOptionsError,
    InvalidTokenError,
    ItemLockedError,
    LinkedInPreviewOptions,
    NotConfiguredError,
    NotConnectedError,
    NotHumanRequestError,
    NotPublishableError,
    OAuthCancelledError,
    OAuthExchangeError,
    OAuthStateError,
    OutcomeUnknownError,
    PlatformError,
    PlatformId,
    PreviewDraft,
    PreviewExpiredError,
    PreviewOptions,
    PreviewResult,
    PublishDisabledError,
    Publisher,
    PublisherBusyError,
    PublishError,
    PublishInProgressError,
    QuotaExceededError,
    RateLimitedError,
    Readiness,
    ReconnectRequiredError,
    RenderError,
    SendOutcome,
    SettingLockedError,
    SystemClock,
    UnavailableError,
    ValidationFailedError,
    ValidationIssue,
    channel_platform,
    parse_preview_options,
    platform_label,
)
from .http import FakeTransport, Response, Transport, TransportError, first_record, json_response
from .service import PublishService
from .settings import PUBLISH_ENV_VARS, PublishSettings

__all__ = [
    "AI_LABEL_REQUIRED_MESSAGE", "CALLBACK_REDIRECT", "CALLBACK_RESULTS", "CHANNEL_PLATFORM", "LINKEDIN_CALLBACK_PATH",
    "MEDIA_PATH_PATTERN", "OAUTH_COOKIE_NAME", "OAUTH_COOKIE_PATH", "OAUTH_STATE_TTL_SECONDS", "PLATFORM_LABELS",
    "PUBLISH_ENV_VARS", "PUBLISH_PLATFORMS",
    "AttemptGuard", "Clock", "ConnectStart", "FakeTransport", "HumanConfirmation", "InstagramPreviewOptions",
    "LinkedInPreviewOptions", "PlatformId", "PreviewDraft", "PreviewOptions", "PreviewResult", "PublishService",
    "PublishSettings", "Publisher", "Readiness", "Response", "SendOutcome", "SystemClock", "Transport",
    "TransportError", "ValidationIssue",
    "channel_platform", "first_record", "json_response", "parse_preview_options", "platform_label",
    "AccountTypeError", "AlreadyPublishedError", "AttemptStateError", "AttemptTakenOverError", "ConfirmCodeError",
    "ConfirmationMismatchError", "ConnectError", "HostingError", "InvalidInputError", "InvalidOptionsError",
    "InvalidTokenError", "ItemLockedError", "NotConfiguredError", "NotConnectedError", "NotHumanRequestError",
    "NotPublishableError", "OAuthCancelledError", "OAuthExchangeError", "OAuthStateError", "OutcomeUnknownError",
    "PlatformError", "PreviewExpiredError", "PublishDisabledError", "PublishError", "PublishInProgressError",
    "PublisherBusyError", "QuotaExceededError", "RateLimitedError", "ReconnectRequiredError", "RenderError",
    "SettingLockedError", "UnavailableError", "ValidationFailedError",
]
