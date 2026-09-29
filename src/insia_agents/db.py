"""Workspace: the local SQLite store that makes INSIA restart-safe.

Layout of ``Settings.home`` (env ``INSIA_HOME``, default ``./workspace``)::

    insia.db   SQLite (WAL): profile, documents, runs + events, content items + versions, usage, calendar
    exports/   files written by the exporters
    uploads/   original files the user added (the DB keeps the extracted text)
    logs/      log files

Concurrency: one connection per ``Workspace`` opened with
``check_same_thread=False``. Every statement runs under a module-level
``RLock``, so threads in one process never interleave inside a transaction
(the pipeline's channel threads, the HTTP server's request threads). Other
processes on the same workspace (CLI next to the server) are serialized by
SQLite itself (WAL + busy timeout).

Run ownership: a run row records which process works on it (pid, host, boot
id, a random owner token) and a heartbeat that the owner refreshes every
``HEARTBEAT_SECONDS`` while the run is going (``acquire_run`` → ``RunLease``).
``recover_stale`` (``mark_interrupted`` at server start, ``insia run-due`` /
``insia resume`` in the CLI) only takes over a ``running`` run whose owner is
gone: a dead pid on this machine, a machine that restarted, or no heartbeat for
``STALE_AFTER_SECONDS``; the server keeps checking in the background
(``watch_stale_runs``). A taken-over owner notices (token mismatch) at its next
checkpoint and stops; its later writes to the run, its items and versions are
refused (``RunTakenOverError``), and it only hands a calendar slot back while
the slot is still its own claim (``release_slot`` / ``link_slot``).

Schema changes are forward-only: append a new SQL script to ``MIGRATIONS``;
never edit one that has shipped.

All methods raise ``WorkspaceError`` (a ``ValueError``) with a Korean message
fit for the dashboard when the input is invalid; ``NotFoundError`` for unknown
ids, ``InvalidTransitionError`` / ``ApprovalBlockedError`` for status changes.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, NamedTuple, Sequence

from pydantic import BaseModel, ValidationError

from .config import KST
from .events import TERMINAL_TYPES, to_jsonable
from .models import (
    ALL_CHANNELS,
    Brief,
    CalendarSlot,
    ChannelResult,
    ContentItem,
    ContentItemDetail,
    Draft,
    DraftVersion,
    Plan,
    PlannedSlot,
    Profile,
    PublishAttempt,
    PublishConnection,
    PublishPreview,
    ResearchPack,
    Review,
    UsageRecord,
    UserDocument,
)

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

DB_NAME = "insia.db"
SUBDIRS = ("exports", "uploads", "logs")

# One lock for every Workspace in the process: several Workspace objects may
# point at the same file (server + job threads), and SQLite allows only one
# writer at a time anyway.
_LOCK = threading.RLock()

RUN_STATUSES = ("running", "completed", "failed", "cancelled", "interrupted")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "interrupted")
CONTENT_STATUSES = ("draft", "needs_changes", "approved", "scheduled", "published", "archived")
SLOT_STATUSES = ("planned", "generating", "drafted", "skipped")
DOCUMENT_KINDS = ("text", "markdown", "pdf", "docx")
VERSION_SOURCES = ("agent", "human")

STATUS_LABELS = {
    "draft": "초안", "needs_changes": "수정 필요", "approved": "승인", "scheduled": "게시 예정",
    "published": "게시 완료", "archived": "보관",
}

# Allowed manual status changes (``set_item_status``). A same-status call is
# always allowed and only updates the given fields (e.g. a new published URL).
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"needs_changes", "approved", "archived"}),
    "needs_changes": frozenset({"draft", "approved", "archived"}),
    "approved": frozenset({"draft", "needs_changes", "scheduled", "published", "archived"}),
    "scheduled": frozenset({"approved", "published", "archived"}),
    "published": frozenset({"archived"}),
    "archived": frozenset({"draft"}),
}

MAX_DOCUMENT_CHARS = 2_000_000
MAX_NOTE_CHARS = 2_000
INTERRUPTED_MESSAGE = "프로그램이 다시 시작되면서 실행이 중단됐어요. '이어서 실행'으로 남은 작업을 마칠 수 있어요."
# Run kinds ``pipeline.resume_run`` can continue; every other kind (review/revise/edit/import) must be started again.
RESUMABLE_RUN_KINDS = ("pipeline", "slot")
JOB_KIND_LABELS = {"review": "재검수", "revise": "수정 요청", "edit": "직접 수정", "import": "가져오기"}

# Run ownership (see the module docstring). A live owner refreshes ``runs.heartbeat_at`` every
# HEARTBEAT_SECONDS from a background thread (also during long API calls); a run whose heartbeat is
# older than STALE_AFTER_SECONDS belongs to a process that is gone or asleep and may be taken over.
HEARTBEAT_SECONDS = 30.0
STALE_AFTER_SECONDS = 600.0
# A slot left 'generating' by a run that already ended (or was never created) is released after this.
ORPHAN_SLOT_SECONDS = 60.0
# How often a long-running process (the server) checks again for runs whose owner went away (watch_stale_runs).
RECOVERY_WATCH_SECONDS = 60.0

# API publish attempts (``publish_attempts``): the worker refreshes ``heartbeat_at`` every PUBLISH_HEARTBEAT_SECONDS from
# its own thread (also while it polls Instagram for minutes); an attempt without a sign of life for
# PUBLISH_STALE_AFTER_SECONDS (6 missed beats) belongs to a worker that is gone or asleep. Shorter than runs on purpose:
# a worker that wakes up later finds its owner token cleared and sends nothing.
PUBLISH_HEARTBEAT_SECONDS = 30.0
PUBLISH_STALE_AFTER_SECONDS = 180.0
PUBLISH_PLATFORMS = ("linkedin", "instagram")
PUBLISH_ATTEMPT_STATUSES = ("sending", "published", "failed", "unknown", "abandoned")
ACTIVE_PUBLISH_STATUSES = ("sending", "unknown")  # the item's versions and status are frozen while one exists
# Steps recorded at or after the irreversible call: an interrupted attempt there may have been published.
PUBLISH_AFTER_WRITE_STEPS = ("write", "permalink")
PUBLISHED_VIA_VALUES = ("", "linkedin_api", "instagram_api", "fake")
PUBLISHED_VIA_BY_PLATFORM = {"linkedin": "linkedin_api", "instagram": "instagram_api"}
PUBLISH_CONNECTION_STATUSES = ("connected", "needs_reconnect")
PUBLISH_PREVIEW_VIA = ("dashboard", "cli")
# attempt.state never holds a credential (the service only records ids, steps and choices)
_STATE_FORBIDDEN_KEYS = frozenset({"access_token", "client_secret", "refresh_token", "token", "code"})
PUBLISH_ITEM_UPDATE_ERROR = "게시는 됐지만 보관함 상태를 바꾸지 못했어요. ‘게시 완료 표시’를 눌러 주세요."
PUBLISH_LATE_SUCCESS_BLOCKED = ("이 기록을 정리한 뒤에 플랫폼이 게시 성공을 알려 왔어요. 같은 버전의 다른 게시 기록이 있어 "
                                "상태를 바꾸지 않았어요. 같은 글이 두 번 올라갔는지 확인해 주세요.")
PUBLISH_LATE_ANSWER = "system:late_answer"
# An item's publish fields describe one post. Details that arrive later for a published attempt (the address a
# person fills in, a late answer) reach the item only while it still describes that attempt's post: published
# through the API and no newer version posted since (params: the attempt's version) …
_ITEM_STILL_SHOWS_POST = ("status = 'published' AND published_via <> '' AND NOT EXISTS (SELECT 1 FROM publish_attempts "
                          "newer WHERE newer.item_id = items.id AND newer.status = 'published' AND newer.version > ?)")
# … and its address is replaced only when it has none: empty, or an older post's address (another attempt's
# permalink, which items published before the final review F1 fixes could keep) (params: the attempt's id)
_ITEM_URL_UNSET = ("(published_url = '' OR published_url IN (SELECT other.permalink FROM publish_attempts other "
                   "WHERE other.item_id = items.id AND other.id <> ? AND other.permalink <> ''))")
PUBLISH_INTERRUPTED_FAILED = "게시 도중 프로그램이 멈춰서 아무것도 올리지 않았어요. 다시 확인하고 게시해 주세요."
PUBLISH_INTERRUPTED_UNKNOWN = {
    "linkedin": "게시 도중 프로그램이 멈춰서 LinkedIn에 올라갔는지 확인하지 못했어요. LinkedIn 내 활동에서 확인한 뒤 알려 주세요.",
    "instagram": "게시 도중 프로그램이 멈춰서 인스타그램에 올라갔는지 확인하지 못했어요. ‘인스타그램에서 다시 확인’을 눌러 주세요.",
}


def interrupted_message(kind: str) -> str:
    """What an interrupted run says: resumable kinds point to '이어서 실행', jobs to starting them again."""
    if kind in RESUMABLE_RUN_KINDS:
        return INTERRUPTED_MESSAGE
    label = JOB_KIND_LABELS.get(kind)
    what = f"{label} 작업" if label else "작업"
    if kind == "import":
        again = "'insia import-run'으로 같은 폴더를 다시 가져와 주세요 (중복 없이 갱신돼요)."
    elif label:
        again = f"보관함에서 같은 작업({label})을 다시 시작해 주세요."
    else:
        again = "같은 작업을 다시 시작해 주세요."
    return f"프로그램이 다시 시작되면서 {what}이 중단됐어요. 이 작업은 이어서 할 수 없어서 {again}"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_ITEM_ID = re.compile(r"^it_[A-Za-z0-9._-]{1,120}$")
_KIND = re.compile(r"^[a-z][a-z_]{0,31}$")
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkspaceError(ValueError):
    """Invalid input or state. ``str(exc)`` is a Korean message for the user."""


class NotFoundError(WorkspaceError):
    pass


class InvalidTransitionError(WorkspaceError):
    pass


class RunTakenOverError(WorkspaceError):
    """Another process took this run over (its owner token changed): this process must stop writing to it."""


class ApprovalBlockedError(WorkspaceError):
    """Approval refused because the latest version has not passed review.

    The dashboard offers "그래도 승인" (``force=True``) when it sees this.
    """

    def __init__(self, message: str, *, item_id: str, version: int, score: int | None) -> None:
        super().__init__(message)
        self.item_id = item_id
        self.version = version
        self.score = score


class ItemLockedError(InvalidTransitionError):
    """The item has a live API publish attempt (``sending`` or ``unknown``): its versions and status are frozen.

    Raised by the workspace's internal write paths (new versions, reviews, status changes) so that every
    caller — the pipeline, jobs, ``import-run``, the dashboard — is stopped in one place. It lives here, not in
    ``insia_agents.publishers``, so those callers can catch it without importing the publishing package.
    ``insia_agents.publishers.PublishInProgressError`` is this same class. The server answers 409
    ``{"code": "item_locked", "attempt_id": …}`` (``code``/``extra()`` mirror ``PublishError``).
    """

    http_status = 409
    code = "item_locked"
    outcome = "not_sent"
    SENDING_MESSAGE = "이 콘텐츠를 지금 게시하는 중이에요."
    UNKNOWN_MESSAGE = "게시됐는지 확인이 필요한 기록이 있어요. 먼저 정리해 주세요."

    def __init__(self, message: str | None = None, *, attempt_id: str = "", platform: str = "",
                 status: str = "sending") -> None:
        if not message:
            message = self.UNKNOWN_MESSAGE if status == "unknown" else self.SENDING_MESSAGE
        super().__init__(message)
        self.attempt_id = attempt_id
        self.platform = platform
        self.status = status

    def extra(self) -> dict[str, str]:
        return {"attempt_id": self.attempt_id, "platform": self.platform, "status": self.status}


class AttemptTakenOverError(WorkspaceError):
    """This worker no longer owns the publish attempt (recovery cleared its ``owner_token``): stop, send nothing.

    Raised by the owner-token-conditional publish-attempt writes (``update_publish_attempt``,
    ``claim_publish_write``, ``finish_publish_*``) when the conditional UPDATE matches no row. It lives here for
    the same reason as ``ItemLockedError`` (the workspace raises it and must not import the publishing package);
    ``insia_agents.publishers.AttemptTakenOverError`` is this same class. It is only used inside the worker and
    never becomes an HTTP answer (the attempt record already holds the recovery's outcome).
    """

    http_status = 409
    code = "taken_over"
    outcome = "not_sent"
    DEFAULT_MESSAGE = "다른 곳에서 이 게시 시도를 정리했어요. 이 프로세스는 보내지 않고 멈춰요."

    def __init__(self, message: str | None = None, *, attempt_id: str = "") -> None:
        super().__init__(message or self.DEFAULT_MESSAGE)
        self.attempt_id = attempt_id

    def extra(self) -> dict[str, str]:
        return {"attempt_id": self.attempt_id}


class PublishStateError(WorkspaceError):
    """A publish preview / attempt precondition failed inside the workspace transaction.

    ``reason`` is machine-readable and ``info`` carries the details (``blocked_by``, ``attempt_id``, ``permalink``).
    The publishing service turns it into its own error types (the workspace never imports the publishing package):

    ``preview_missing`` · ``preview_used`` · ``preview_expired`` · ``preview_via`` (made on the other surface) ·
    ``hash_mismatch`` · ``not_publishable`` (``blocked_by``) · ``already_published`` (``attempt_id``, ``permalink``) ·
    ``not_connected`` · ``account_changed`` · ``attempt_state`` (the attempt is not in a state that allows this).
    """

    def __init__(self, message: str, *, reason: str, **info: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.info = info


# Why a run's new version was kept in the history instead of becoming the item's current one (``RunVersion.reason``).
HELD_HUMAN_EDIT = "human_edit"  # a person saved an edit after the run's last stored round
HELD_STATUS = "status"  # a person approved, scheduled or published the item
HELD_NEWER_VERSION = "newer_version"  # another job (e.g. 수정 요청) saved a newer version

# Item statuses a person set on purpose: a run's later rounds never replace the version they refer to.
HELD_STATUSES = ("approved", "scheduled", "published")


class RunVersion(NamedTuple):
    """What ``Workspace.add_run_version`` stored.

    ``restored`` is set when the run's version was only kept in the history:
    the version that must stay current (a person's edit, the approved text, …)
    was put back on top as this copy, which is the item's current version.
    """

    version: DraftVersion
    restored: DraftVersion | None = None
    by_human: bool = False  # a person saved a version after the run's last stored round
    reason: str = ""  # HELD_HUMAN_EDIT / HELD_STATUS / HELD_NEWER_VERSION when ``restored`` is set


class _RunHead(NamedTuple):
    keep: sqlite3.Row | None  # the version that must stay current, or None when the run may add its version on top
    own: int  # the newest version the run itself stored (0 = none)
    by_human: bool
    reason: str


# ---------------------------------------------------------------------------
# Migrations (forward-only; append, never edit)
# ---------------------------------------------------------------------------

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'text',
        filename TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        chars INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'pipeline',
        status TEXT NOT NULL DEFAULT 'running',
        brief TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '{}',
        mode TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        profile TEXT,
        parent_item_id TEXT NOT NULL DEFAULT '',
        plan TEXT,
        research TEXT,
        progress TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        cost_usd REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at);
    CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
    CREATE INDEX IF NOT EXISTS runs_parent ON runs (parent_item_id);
    CREATE TABLE IF NOT EXISTS events (
        run_id TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        agent TEXT NOT NULL,
        t REAL NOT NULL DEFAULT 0,
        ts TEXT NOT NULL DEFAULT '',
        event TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        version INTEGER NOT NULL DEFAULT 0,
        approved_version INTEGER NOT NULL DEFAULT 0,
        score INTEGER,
        passed INTEGER,
        scheduled_at TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT '',
        published_url TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        brief TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS items_status ON items (status, updated_at);
    CREATE INDEX IF NOT EXISTS items_channel ON items (channel, updated_at);
    CREATE INDEX IF NOT EXISTS items_run ON items (run_id);
    CREATE TABLE IF NOT EXISTS versions (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        source TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'round',
        round INTEGER NOT NULL DEFAULT 0,
        draft TEXT NOT NULL,
        review TEXT,
        instructions TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE (item_id, version)
    );
    CREATE INDEX IF NOT EXISTS versions_run ON versions (item_id, run_id, role, round);
    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '',
        task TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        web_search_requests INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS usage_run ON usage (run_id);
    CREATE INDEX IF NOT EXISTS usage_created ON usage (created_at);
    CREATE TABLE IF NOT EXISTS slots (
        id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        channel TEXT NOT NULL,
        topic TEXT NOT NULL,
        angle TEXT NOT NULL DEFAULT '',
        keywords TEXT NOT NULL DEFAULT '[]',
        goal TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'planned',
        item_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS slots_date ON slots (date, status);
    """,
    # 2 — run ownership (owner process + heartbeat, so a restart only takes over runs whose owner is gone)
    #     and the approval audit (forced approvals are recorded with the version and score they approved)
    """
    ALTER TABLE runs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN owner_host TEXT NOT NULL DEFAULT '';
    ALTER TABLE runs ADD COLUMN owner_boot TEXT NOT NULL DEFAULT '';
    ALTER TABLE runs ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';
    ALTER TABLE runs ADD COLUMN heartbeat_at TEXT NOT NULL DEFAULT '';
    ALTER TABLE items ADD COLUMN approval_forced INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE items ADD COLUMN approved_score INTEGER;
    ALTER TABLE items ADD COLUMN approved_at TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS slots_run ON slots (run_id);
    """,
    # 3 — API publishing (LinkedIn/Instagram): connection metadata (no secrets), confirmed previews,
    #     publish attempts (one live attempt per item version and platform, owned by one worker at a time);
    #     items remember how they were published
    """
    CREATE TABLE IF NOT EXISTS publish_connections (
        platform TEXT PRIMARY KEY,
        account_id TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '',
        scopes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'connected',
        status_reason TEXT NOT NULL DEFAULT '',
        token_expires_at TEXT NOT NULL DEFAULT '',
        expires_estimated INTEGER NOT NULL DEFAULT 0,
        token_issued_at TEXT NOT NULL DEFAULT '',
        token_refreshed_at TEXT NOT NULL DEFAULT '',
        api_version TEXT NOT NULL DEFAULT '',
        connected_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS publish_previews (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        platform TEXT NOT NULL,
        account_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        created_via TEXT NOT NULL DEFAULT 'dashboard',
        confirm_code_hash TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS publish_previews_item ON publish_previews (item_id, created_at);
    CREATE TABLE IF NOT EXISTS publish_attempts (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        platform TEXT NOT NULL,
        preview_id TEXT NOT NULL DEFAULT '',
        payload_hash TEXT NOT NULL,
        account_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        step TEXT NOT NULL DEFAULT '',
        external_id TEXT NOT NULL DEFAULT '',
        permalink TEXT NOT NULL DEFAULT '',
        media_token TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL DEFAULT '',
        resolved_by TEXT NOT NULL DEFAULT '',
        owner_pid INTEGER NOT NULL DEFAULT 0,
        owner_host TEXT NOT NULL DEFAULT '',
        owner_boot TEXT NOT NULL DEFAULT '',
        owner_token TEXT NOT NULL DEFAULT '',
        heartbeat_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS publish_attempts_item ON publish_attempts (item_id, created_at);
    CREATE INDEX IF NOT EXISTS publish_attempts_status ON publish_attempts (status, updated_at);
    CREATE INDEX IF NOT EXISTS publish_attempts_media ON publish_attempts (media_token) WHERE media_token <> '';
    CREATE UNIQUE INDEX IF NOT EXISTS publish_attempts_live
        ON publish_attempts (item_id, version, platform) WHERE status IN ('sending', 'published', 'unknown');
    ALTER TABLE items ADD COLUMN published_via TEXT NOT NULL DEFAULT '';
    ALTER TABLE items ADD COLUMN published_external_id TEXT NOT NULL DEFAULT '';
    """,
]


def _statements(script: str) -> list[str]:
    """Split a migration script into complete SQL statements."""
    out: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                out.append(buffer.strip())
            buffer = ""
    rest = "\n".join(line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--"))
    if rest.strip():
        raise ValueError(f"incomplete SQL statement in migration: {rest[:80]!r}")
    return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _fmt(moment: datetime) -> str:
    """The one stored timestamp format: UTC, milliseconds, ``Z`` (sorts correctly as text)."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (UTC)."""
    return _fmt(datetime.now(timezone.utc))


def _parse_ts(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_ts(value: str | None) -> str:
    """Any ISO timestamp → the stored UTC format; empty/invalid → now."""
    parsed = _parse_ts(value or "")
    return utc_now() if parsed is None else _fmt(parsed)


def kst_date(ts: str) -> str:
    """The Korean calendar date (YYYY-MM-DD) of a stored UTC timestamp."""
    parsed = _parse_ts(ts)
    if parsed is None:
        return ""
    return parsed.astimezone(KST).date().isoformat()


def _check_date(value: str, what: str = "날짜") -> str:
    text = (value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise WorkspaceError(f"{what}는 YYYY-MM-DD 형식이어야 해요 (받은 값: {value!r})") from None


def _check_when(value: str) -> str:
    """``YYYY-MM-DD`` or an ISO date-time (kept as given, trimmed)."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        return _check_date(text, "게시 예정일")
    if _parse_ts(text) is None:
        raise WorkspaceError(f"게시 예정일은 YYYY-MM-DD 또는 ISO 날짜·시각이어야 해요 (받은 값: {value!r})")
    return text


def _time_bound(value: str, *, end: bool) -> tuple[str, str]:
    """Filter bound for ``created_at``. A plain date is a Korean calendar day
    (``until`` includes the whole day); an ISO date-time is used as is."""
    text = (value or "").strip()
    if len(text) == 10:
        day = date.fromisoformat(_check_date(text, "기간"))
        if end:
            day = day + timedelta(days=1)
        return ("<" if end else ">="), _fmt(datetime(day.year, day.month, day.day, tzinfo=KST))
    parsed = _parse_ts(text)
    if parsed is None:
        raise WorkspaceError(f"기간은 YYYY-MM-DD 또는 ISO 날짜·시각이어야 해요 (받은 값: {value!r})")
    return ("<=" if end else ">="), _fmt(parsed)


def _dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False)


def _loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _as_model(model: type[BaseModel], value: Any, what: str) -> Any:
    if value is None or isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise WorkspaceError(f"{what} 형식이 올바르지 않아요: {exc.errors()[0].get('msg', '')}") from None


def _hex_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def pipeline_item_id(run_id: str, channel: str) -> str:
    """Content item id for a pipeline run's channel output."""
    return f"it_{run_id}_{channel}"


def _finite(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def profile_is_empty(profile: Profile | None) -> bool:
    """True when no field (other than ``updated_at``) was filled in."""
    if profile is None:
        return True
    return profile.model_dump(exclude={"updated_at"}) == Profile().model_dump(exclude={"updated_at"})


def normalize_hashtags(tags: Sequence[str] | str | None) -> list[str]:
    """``["AI 마케팅", "#창업", "창업"]`` → ``["#AI마케팅", "#창업"]`` (order kept, duplicates dropped)."""
    if tags is None:
        return []
    raw = re.split(r"[\s,]+(?=#)|,", tags) if isinstance(tags, str) else list(tags)
    out: list[str] = []
    for tag in raw:
        text = re.sub(r"\s+", "", str(tag or "")).lstrip("#")
        if not text:
            continue
        text = "#" + text
        if text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Run ownership: process identity, liveness, leases
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def this_host() -> str:
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


@functools.lru_cache(maxsize=1)
def boot_marker() -> str:
    """Identifies the current OS boot ("" when unknown): a pid recorded before a reboot says nothing now.

    Linux (containers included) has a boot id; Windows and macOS use the boot time (``t:<epoch>``).
    """
    try:
        text = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if text:
            return text
    except (OSError, ValueError):
        pass
    try:
        import ctypes

        if os.name == "nt":
            tick_count = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
            tick_count.restype = ctypes.c_ulonglong
            return f"t:{int(time.time() - tick_count() / 1000.0)}"
        if sys.platform == "darwin":
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"))

            class _TimeVal(ctypes.Structure):
                _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

            value = _TimeVal()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            if libc.sysctlbyname(b"kern.boottime", ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)) == 0 \
                    and value.tv_sec > 0:
                return f"t:{int(value.tv_sec)}"
    except Exception:  # noqa: BLE001 - unknown just means the boot check is skipped
        pass
    return ""


def _same_boot(recorded: str, current: str) -> bool | None:
    """True/False when both markers are known; ``None`` when one is missing."""
    if not recorded or not current:
        return None
    if recorded.startswith("t:") and current.startswith("t:"):  # boot times drift by a few seconds
        try:
            return abs(int(recorded[2:]) - int(current[2:])) <= 120
        except ValueError:
            return None
    return recorded == current


def pid_alive(pid: int) -> bool:
    """Whether process ``pid`` exists on this machine (unsure → True, so only the heartbeat decides)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: someone else's process
        return True
    try:  # Linux: a killed process its parent has not reaped yet is a zombie, not a worker
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return stat.rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


_LEASES_LOCK = threading.Lock()
_LEASES: dict[tuple[str, str], "RunLease"] = {}

# Publish attempts this process's workers hold right now: (database key, attempt id) -> owner token. A registered
# worker is alive by definition (rule 1 of ``_attempt_owner_alive``); ``begin_publish_attempt`` registers it in the
# same step that creates the row, the finish methods and ``release_publish_worker`` forget it.
_PUBLISH_WORKERS_LOCK = threading.Lock()
_PUBLISH_WORKERS: dict[tuple[str, str], str] = {}


class RunLease:
    """This process's claim on a running run: its owner token plus a heartbeat thread.

    Created by ``Workspace.acquire_run`` (acquiring a run this process already
    holds returns the same lease). The owner calls ``release()`` when the run
    ends. ``lost`` becomes true once another process took the run over (the
    stored token no longer matches): the owner then stops at its next
    checkpoint, and its event/status writes raise ``RunTakenOverError``.
    """

    def __init__(self, workspace: "Workspace", run_id: str, token: str, interval: float) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.token = token
        self.interval = max(0.01, float(interval))
        self.key = (workspace._key, run_id)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"insia-heartbeat-{run_id}", daemon=True)

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def released(self) -> bool:
        return self._stop.is_set()

    def mark_lost(self) -> None:
        self._lost.set()

    def verify(self) -> bool:
        """Whether this process still owns the run, read from the database now (the heartbeat only notices every
        ``HEARTBEAT_SECONDS``). A run taken over marks the lease lost. For checkpoints before paid calls."""
        if self._lost.is_set():
            return False
        try:
            mine = self.workspace._owns(self.run_id, self.token)
        except Exception:  # noqa: BLE001 - cannot tell right now (closed, locked): the write guards still decide
            return True
        if not mine:
            self._lost.set()
        return mine

    def start(self) -> None:
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                mine = self.workspace._heartbeat(self.run_id, self.token)
            except WorkspaceError:  # the workspace was closed
                return
            except Exception:  # noqa: BLE001 - e.g. the database stayed locked: try again next beat
                log.warning("실행 %s의 heartbeat를 기록하지 못했어요", self.run_id, exc_info=True)
                continue
            if not mine:
                log.warning("다른 곳에서 실행 %s를 넘겨받았어요. 이 프로세스는 여기서 멈춰요.", self.run_id)
                self._lost.set()
                return

    def release(self) -> None:
        """Stop the heartbeat and forget the lease (safe to call more than once)."""
        self._stop.set()
        with _LEASES_LOCK:
            if _LEASES.get(self.key) is self:
                del _LEASES[self.key]


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class Workspace:
    """The local store. Safe to share between threads."""

    def __init__(self, home: Path | str) -> None:
        self.home = Path(home).expanduser()
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            for sub in SUBDIRS:
                (self.home / sub).mkdir(exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"워크스페이스 폴더를 만들 수 없어요: {self.home} ({exc.strerror or exc})") from None
        self.db_path = self.home / DB_NAME
        try:
            self._key = str(self.db_path.resolve())  # run leases are per database file and run id
        except OSError:
            self._key = str(self.db_path.absolute())
        try:
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise WorkspaceError(f"워크스페이스 DB를 열 수 없어요: {self.db_path} ({exc})") from None
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        self._watcher: threading.Thread | None = None  # background recovery (watch_stale_runs)
        self._watch_stop = threading.Event()
        # Called with the ids of publish attempts the background recovery closed (the publishing service sets it to
        # clean up their public media); never needed for correctness.
        self.publish_recovery_hook: Any = None
        try:
            with _LOCK:
                self._conn.execute("PRAGMA busy_timeout = 30000")
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._migrate()
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            raise WorkspaceError(f"워크스페이스 DB를 읽을 수 없어요: {self.db_path} ({exc})") from None
        except BaseException:
            self._conn.close()
            raise

    @classmethod
    def from_settings(cls, settings: "Settings") -> "Workspace":
        return cls(settings.home)

    # -- lifecycle -------------------------------------------------------------
    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    @property
    def uploads_dir(self) -> Path:
        return self.home / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def schema_version(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._watch_stop.set()
        with _LOCK:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Workspace({str(self.home)!r})"

    # -- transactions ------------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise WorkspaceError("워크스페이스가 이미 닫혔어요")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (``BEGIN IMMEDIATE``); nested calls join the outer one."""
        with _LOCK:
            self._check_open()
            conn = self._conn
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def transaction(self):
        """Public write transaction for callers outside this module (all nested workspace writes join it)."""
        return self._tx()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            self._check_open()
            yield self._conn

    def _migrate(self) -> None:
        conn = self._conn
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (0)")
                current = 0
            else:
                current = int(row[0])
            if current > len(MIGRATIONS):
                raise WorkspaceError(
                    f"이 워크스페이스는 더 새 버전의 INSIA로 만들어졌어요 (DB 버전 {current}, 지원 {len(MIGRATIONS)}). "
                    "INSIA를 업데이트한 뒤 다시 열어 주세요.")
            for index in range(current, len(MIGRATIONS)):
                try:
                    for statement in _statements(MIGRATIONS[index]):
                        conn.execute(statement)
                except sqlite3.Error as exc:
                    raise WorkspaceError(f"워크스페이스 DB를 버전 {index + 1}로 올리지 못했어요 (변경은 모두 되돌렸어요): {exc}") from exc
                conn.execute("UPDATE schema_version SET version = ?", (index + 1,))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # -- profile -----------------------------------------------------------------
    def get_profile(self) -> Profile:
        with self._read() as conn:
            row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        if row is None:
            return Profile()
        try:
            return Profile.model_validate_json(row["data"])
        except ValidationError as exc:
            raise WorkspaceError(f"저장된 프로필을 읽지 못했어요: {exc.errors()[0].get('msg', '')}") from None

    def save_profile(self, profile: Profile | Mapping[str, Any]) -> Profile:
        value = _as_model(Profile, profile, "프로필")
        saved = value.model_copy(update={"updated_at": utc_now()})
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (saved.model_dump_json(), saved.updated_at),
            )
        return saved

    # -- user documents ----------------------------------------------------------
    def _next_counter(self, conn: sqlite3.Connection, key: str, floor: int = 0) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        value = max(int(row["value"]) if row else 0, floor) + 1
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))
        return value

    @staticmethod
    def _document(row: sqlite3.Row) -> UserDocument:
        return UserDocument(id=row["id"], title=row["title"], kind=row["kind"], filename=row["filename"], text=row["text"],
                            chars=row["chars"], created_at=row["created_at"])

    def add_document(self, title: str, text: str, *, kind: str = "text", filename: str = "") -> UserDocument:
        text = (text or "").replace("\r\n", "\n")
        if not text.strip():
            raise WorkspaceError("자료 내용이 비어 있어요. 텍스트를 붙여 넣거나 파일을 다시 확인해 주세요.")
        if len(text) > MAX_DOCUMENT_CHARS:
            raise WorkspaceError(f"자료가 너무 길어요 ({len(text):,}자). {MAX_DOCUMENT_CHARS:,}자 이하로 나눠서 올려 주세요.")
        if kind not in DOCUMENT_KINDS:
            raise WorkspaceError(f"자료 종류는 {', '.join(DOCUMENT_KINDS)} 중 하나여야 해요 (받은 값: {kind!r})")
        filename = Path(str(filename or "").replace("\\", "/")).name
        title = re.sub(r"\s+", " ", title or "").strip() or Path(filename).stem or "제목 없는 자료"
        title = title[:200]
        now = utc_now()
        with self._tx() as conn:
            floor = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM documents").fetchone()[0]
            seq = self._next_counter(conn, "document_seq", floor=int(floor))
            doc_id = f"u{seq}"
            conn.execute("INSERT INTO documents (id, seq, title, kind, filename, text, chars, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (doc_id, seq, title, kind, filename, text, len(text), now))
        return UserDocument(id=doc_id, title=title, kind=kind, filename=filename, text=text, chars=len(text), created_at=now)  # type: ignore[arg-type]

    def list_documents(self) -> list[UserDocument]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM documents ORDER BY seq").fetchall()
        return [self._document(row) for row in rows]

    def get_document(self, doc_id: str) -> UserDocument | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (str(doc_id or "").strip(),)).fetchone()
        return self._document(row) if row else None

    def delete_document(self, doc_id: str) -> bool:
        """Delete the document and its original file copies (``uploads/<id>_*``); False when it didn't exist."""
        doc_id = str(doc_id or "").strip()
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        if cursor.rowcount <= 0:
            return False
        if self.uploads_dir.is_dir():
            for original in self.uploads_dir.iterdir():
                if original.name.startswith(f"{doc_id}_") and original.is_file():
                    try:
                        original.unlink()
                    except OSError:  # the text is already gone from the DB; a stuck copy is harmless
                        pass
        return True

    # -- runs --------------------------------------------------------------------
    @staticmethod
    def _owner_stamp(token: str = "") -> tuple[int, str, str, str, str]:
        """(owner_pid, owner_host, owner_boot, owner_token, heartbeat_at) for this process, now."""
        return os.getpid(), this_host(), boot_marker(), token, utc_now()

    def create_run(self, run_id: str, brief: Brief, *, kind: str = "pipeline", options: dict | None = None,
                   mode: str = "", model: str = "", profile: Profile | None = None, parent_item_id: str = "") -> None:
        """Insert a ``running`` run owned by this process (pid/host recorded; ``acquire_run`` starts the heartbeat)."""
        if not _RUN_ID.match(run_id or "") or ".." in run_id:
            raise WorkspaceError(f"실행 id 형식이 올바르지 않아요: {run_id!r}")
        if not _KIND.match(kind or ""):
            raise WorkspaceError(f"실행 종류 형식이 올바르지 않아요: {kind!r}")
        brief = _as_model(Brief, brief, "브리프")
        profile = _as_model(Profile, profile, "프로필")
        now = utc_now()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
                raise WorkspaceError(f"이미 있는 실행 id예요: {run_id}")
            conn.execute(
                "INSERT INTO runs (id, kind, status, brief, options, mode, model, profile, parent_item_id, created_at, updated_at, "
                "owner_pid, owner_host, owner_boot, owner_token, heartbeat_at) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, kind, brief.model_dump_json(), _dumps(options or {}), mode or "", model or "",
                 profile.model_dump_json() if profile is not None else None, parent_item_id or "", now, now,
                 *self._owner_stamp()),
            )

    _RUN_FIELDS = frozenset({"status", "error", "finished_at", "plan", "research", "cost_usd", "mode", "model", "options",
                             "profile", "progress", "parent_item_id"})

    def _lease_for(self, run_id: str) -> RunLease | None:
        lease = _LEASES.get((self._key, run_id))
        return lease if lease is not None and not lease.released else None

    @staticmethod
    def _check_owner(conn: sqlite3.Connection, run_id: str, lease: RunLease | None) -> None:
        """Refuse a write from this process when it holds ``run_id``'s lease but another process took the run over."""
        if lease is None:
            return
        row = conn.execute("SELECT owner_token FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None and row["owner_token"] != lease.token:
            lease.mark_lost()
            raise RunTakenOverError(f"다른 곳에서 실행 {run_id}를 넘겨받았어요. 이 프로세스는 더 기록하지 않고 멈춰요.")

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Update run columns: status, error, finished_at, plan (Plan), research (ResearchPack), cost_usd,
        mode, model, options (dict), profile (Profile), progress (dict), parent_item_id.

        A terminal status sets ``finished_at`` (unless given); going back to
        ``running`` (resume) clears ``error`` and ``finished_at``. While this
        process holds the run's lease, the update is refused with
        ``RunTakenOverError`` once another process took the run over.
        """
        unknown = set(fields) - self._RUN_FIELDS
        if unknown:
            raise TypeError(f"update_run: unknown field(s) {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "status":
                if value not in RUN_STATUSES:
                    raise WorkspaceError(f"실행 상태는 {', '.join(RUN_STATUSES)} 중 하나여야 해요 (받은 값: {value!r})")
                values["status"] = value
            elif key == "plan":
                values["plan"] = None if value is None else _as_model(Plan, value, "계획").model_dump_json()
            elif key == "research":
                values["research"] = None if value is None else _as_model(ResearchPack, value, "리서치 팩").model_dump_json()
            elif key == "profile":
                values["profile"] = None if value is None else _as_model(Profile, value, "프로필").model_dump_json()
            elif key in ("options", "progress"):
                values[key] = _dumps(value or {})
            elif key == "cost_usd":
                values["cost_usd"] = _finite(value)
            else:
                values[key] = "" if value is None else str(value)
        status = values.get("status")
        if status in TERMINAL_RUN_STATUSES and "finished_at" not in values:
            values["finished_at"] = utc_now()
        if status == "running":
            values.setdefault("finished_at", "")
            values.setdefault("error", "")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        lease = self._lease_for(run_id)
        with self._tx() as conn:
            self._check_owner(conn, run_id, lease)
            cursor = conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*values.values(), run_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요")

    def claim_run(self, run_id: str, expected_status: str) -> bool:
        """Atomically set a run back to ``running`` (owned by this process) if its status is still ``expected_status``.

        Two resumes of the same run (a double click) cannot both win. Claiming a
        run another process still holds (``expected_status='running'``, a forced
        resume) takes it over: that process stops at its next checkpoint and
        its later writes to the run, its items and its slot are refused. The
        caller then takes the run with ``acquire_run``. A run this process is
        running right now (``holds_run``) is never claimed: one owner per run
        in a process.
        """
        if self.holds_run(run_id):
            return False
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'running', error = '', finished_at = '', updated_at = ?, "
                "owner_pid = ?, owner_host = ?, owner_boot = ?, owner_token = ?, heartbeat_at = ? WHERE id = ? AND status = ?",
                (utc_now(), *self._owner_stamp(), run_id, expected_status))
        return cursor.rowcount == 1

    def holds_run(self, run_id: str) -> bool:
        """Whether this process is running ``run_id`` right now (it holds a lease that was not taken over)."""
        lease = self._lease_for(run_id)
        return lease is not None and not lease.lost

    def acquire_run(self, run_id: str) -> RunLease:
        """Mark this process as the run's owner and keep its heartbeat going until ``lease.release()``.

        Acquiring a run this process already holds returns the same lease;
        when another process took the run over meanwhile, that lease is marked
        lost and ``RunTakenOverError`` is raised (the run is not taken back).
        """
        token = secrets.token_hex(12)
        with self._tx() as conn:
            existing = self._lease_for(run_id)
            if existing is not None and not existing.lost:
                self._check_owner(conn, run_id, existing)
                return existing
            cursor = conn.execute("UPDATE runs SET owner_pid = ?, owner_host = ?, owner_boot = ?, owner_token = ?, heartbeat_at = ? "
                                  "WHERE id = ?", (*self._owner_stamp(token), run_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요")
        lease = RunLease(self, run_id, token, HEARTBEAT_SECONDS)
        with _LEASES_LOCK:
            previous = _LEASES.get(lease.key)
            _LEASES[lease.key] = lease
        if previous is not None and previous is not lease:
            previous.mark_lost()
            previous._stop.set()
        lease.start()
        return lease

    def _heartbeat(self, run_id: str, token: str) -> bool:
        """Refresh the run's heartbeat; False when the run is no longer ours (taken over or deleted)."""
        with self._tx() as conn:
            cursor = conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ? AND owner_token = ?", (utc_now(), run_id, token))
        return cursor.rowcount == 1

    def _owns(self, run_id: str, token: str) -> bool:
        """Whether ``token`` is still the run's stored owner token (False when taken over or deleted)."""
        with self._read() as conn:
            row = conn.execute("SELECT owner_token FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row is not None and bool(token) and row["owner_token"] == token

    def _guard_run(self, conn: sqlite3.Connection, run_id: str) -> None:
        """Inside a write transaction: refuse a write made on behalf of ``run_id`` when this process held the run
        but another process took it over (``RunTakenOverError``). Writes from processes without the lease pass."""
        if run_id:
            self._check_owner(conn, run_id, self._lease_for(run_id))

    @staticmethod
    def _last_sign(row: sqlite3.Row) -> datetime | None:
        """The owner's last sign of life: its heartbeat or its last write to the run row, whichever is newer."""
        moments = [m for m in (_parse_ts(row["heartbeat_at"]), _parse_ts(row["updated_at"])) if m is not None]
        return max(moments) if moments else None

    def _owner_alive(self, row: sqlite3.Row, now: datetime, stale_after: float, trust_own_pid: bool) -> bool:
        """Whether the process recorded as a ``running`` run's owner may still be working on it.

        - a lease this process holds → alive;
        - no heartbeat (nor any write to the run) for ``stale_after`` seconds → gone (dead, or a machine that slept);
        - no owner recorded (older INSIA) or another machine/container → only the heartbeat decides;
        - this machine restarted since → gone;
        - our own pid without our lease → an earlier process that had the same pid (e.g. a restarted
          container) → gone, unless ``trust_own_pid`` (a long-running process that may be between
          ``create_run`` and ``acquire_run``);
        - otherwise: whether that pid is still running.
        """
        lease = self._lease_for(row["id"])
        if lease is not None and not lease.lost and row["owner_token"] and row["owner_token"] == lease.token:
            return True
        beat = self._last_sign(row)
        if beat is None or (now - beat).total_seconds() > stale_after:
            return False
        pid = int(row["owner_pid"] or 0)
        host = row["owner_host"] or ""
        if pid <= 0 or not host or host != this_host():
            return True
        if _same_boot(row["owner_boot"] or "", boot_marker()) is False:
            return False
        if pid == os.getpid():
            return trust_own_pid
        return pid_alive(pid)

    def run_owner(self, run_id: str) -> dict | None:
        """Who is (or was last) running the run: ``{pid, host, heartbeat_at, this_host, live}``.

        ``live`` is only meaningful while the status is ``running`` (for other statuses it is False).
        """
        with self._read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        live = row["status"] == "running" and self._owner_alive(row, datetime.now(timezone.utc), STALE_AFTER_SECONDS, True)
        beat = self._last_sign(row)
        return {"pid": int(row["owner_pid"] or 0) or None, "host": row["owner_host"] or None,
                "heartbeat_at": _fmt(beat) if beat is not None else None,
                "this_host": bool(row["owner_host"]) and row["owner_host"] == this_host(), "live": live}

    def _close_interrupted(self, conn: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
        """Mark a ``running`` run ``interrupted`` and close its event stream (inside a transaction)."""
        run_id = row["id"]
        message = interrupted_message(row["kind"])
        last = conn.execute("SELECT seq, type, t FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        if last is None or last["type"] not in TERMINAL_TYPES:
            seq = (last["seq"] if last else 0) + 1
            t = float(last["t"]) if last else 0.0
            event = {"seq": seq, "t": t, "ts": now, "run_id": run_id, "type": "run.failed", "agent": "system",
                     "data": {"error": message, "interrupted": True, "resumable": row["kind"] in RESUMABLE_RUN_KINDS}}
            conn.execute("INSERT INTO events (run_id, seq, type, agent, t, ts, event) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (run_id, seq, "run.failed", "system", t, now, _dumps(event)))
        conn.execute("UPDATE runs SET status = 'interrupted', error = ?, finished_at = ?, updated_at = ?, owner_token = '' "
                     "WHERE id = ? AND status = 'running'", (message, now, now, run_id))

    def _release_slot(self, conn: sqlite3.Connection, slot: sqlite3.Row, now: str) -> None:
        """A slot whose generating run is gone: link the run's item when it finished, keep an earlier draft,
        or put the slot back to ``planned``."""
        run_id = slot["run_id"] or ""
        run = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone() if run_id else None
        item_id = pipeline_item_id(run_id, slot["channel"]) if run_id else ""
        if run is not None and run["status"] == "completed" and item_id and \
                conn.execute("SELECT 1 FROM versions WHERE item_id = ?", (item_id,)).fetchone():
            conn.execute("UPDATE items SET scheduled_at = ?, updated_at = ? WHERE id = ? AND scheduled_at = ''",
                         (slot["date"], now, item_id))
            conn.execute("UPDATE slots SET status = 'drafted', item_id = ?, updated_at = ? WHERE id = ?", (item_id, now, slot["id"]))
        elif slot["item_id"] and conn.execute("SELECT 1 FROM items WHERE id = ?", (slot["item_id"],)).fetchone():
            conn.execute("UPDATE slots SET status = 'drafted', updated_at = ? WHERE id = ?", (now, slot["id"]))
        else:
            conn.execute("UPDATE slots SET status = 'planned', updated_at = ? WHERE id = ?", (now, slot["id"]))

    def _running_rows(self, conn: sqlite3.Connection, run_ids: Sequence[str] | None) -> list[sqlite3.Row]:
        if run_ids is None:
            return conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
        wanted = [str(r) for r in run_ids if r]
        if not wanted:
            return []
        return conn.execute(f"SELECT * FROM runs WHERE status = 'running' AND id IN ({', '.join('?' for _ in wanted)})",
                            wanted).fetchall()

    def recover_stale(self, *, run_ids: Sequence[str] | None = None, stale_after: float = STALE_AFTER_SECONDS,
                      trust_own_pid: bool = False, sweep_orphans: bool | None = None) -> list[str]:
        """Take over ``running`` runs whose owner process is gone; returns their ids.

        Each is marked ``interrupted`` (pipeline/slot runs are resumable; the
        message tells jobs to start again), its event stream is closed with a
        ``run.failed`` event (``data.interrupted = true``), and only the
        calendar slots those runs were generating are released (the finished
        item is linked, an earlier draft kept, otherwise ``planned``). A slot
        left ``generating`` by a run that already ended, or never started, is
        released too once it is ``ORPHAN_SLOT_SECONDS`` old (``sweep_orphans``;
        by default only when ``run_ids`` is not given).

        Runs whose owner may still be working (see ``_owner_alive``) are left
        alone, so this is safe while other processes (the server, a cron
        ``insia run-due``) are running. A taken-over owner that is in fact
        still running stops at its next checkpoint and cannot write to the
        run, its items or its slot any more. ``run_ids`` limits the check to
        those runs; ``trust_own_pid=True`` is for a long-running process that
        may itself be starting runs right now.
        """
        now_dt = datetime.now(timezone.utc)
        now = _fmt(now_dt)
        taken: list[str] = []
        with self._tx() as conn:
            for row in self._running_rows(conn, run_ids):
                if self._owner_alive(row, now_dt, stale_after, trust_own_pid):
                    continue
                self._close_interrupted(conn, row, now)
                taken.append(row["id"])
            if taken:
                marks = ", ".join("?" for _ in taken)
                for slot in conn.execute(f"SELECT * FROM slots WHERE status = 'generating' AND run_id IN ({marks})", taken).fetchall():
                    self._release_slot(conn, slot, now)
            if sweep_orphans if sweep_orphans is not None else run_ids is None:
                orphan_before = _fmt(now_dt - timedelta(seconds=ORPHAN_SLOT_SECONDS))
                orphans = conn.execute(
                    "SELECT s.* FROM slots s LEFT JOIN runs r ON r.id = s.run_id WHERE s.status = 'generating' AND "
                    "(s.run_id = '' OR ((r.id IS NULL OR r.status != 'running') AND s.updated_at < ?))", (orphan_before,)).fetchall()
                for slot in orphans:
                    self._release_slot(conn, slot, now)
        return taken

    def stale_runs(self, *, stale_after: float = STALE_AFTER_SECONDS, trust_own_pid: bool = True) -> dict[str, str]:
        """``running`` runs whose owner looks gone right now → their owner's last sign of life (read-only)."""
        now_dt = datetime.now(timezone.utc)
        with self._read() as conn:
            rows = self._running_rows(conn, None)
        out: dict[str, str] = {}
        for row in rows:
            if not self._owner_alive(row, now_dt, stale_after, trust_own_pid):
                beat = self._last_sign(row)
                out[row["id"]] = _fmt(beat) if beat is not None else ""
        return out

    def _recovery_tick(self, suspects: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """One round of the background recovery (``watch_stale_runs``).

        A run is taken over only when its owner already looked gone at the
        previous round with the same last sign of life, so an owner that just
        woke up (a laptop back from sleep) gets a full round to send its next
        heartbeat. Orphan slots are released too. Returns ``(taken, suspects
        for the next round)``.
        """
        now_suspects = self.stale_runs(stale_after=STALE_AFTER_SECONDS)
        confirmed = [run_id for run_id, sign in now_suspects.items() if suspects.get(run_id) == sign]
        taken = self.recover_stale(run_ids=confirmed, stale_after=STALE_AFTER_SECONDS, trust_own_pid=True, sweep_orphans=True)
        return taken, {run_id: sign for run_id, sign in now_suspects.items() if run_id not in taken}

    def watch_stale_runs(self) -> bool:
        """Keep taking over runs whose owner is gone, every ``RECOVERY_WATCH_SECONDS``, while this workspace is open.

        For a long-running process (the server; ``mark_interrupted`` starts
        it): a run the start-up check could not judge yet — another
        machine/container with a recent heartbeat, e.g. the container an
        update replaced, or a CLI run killed later — is recovered once its
        owner is gone, without a restart. Idempotent; returns whether a new
        watcher started. ``close()`` stops it.
        """
        with _LOCK:
            if self._closed or (self._watcher is not None and self._watcher.is_alive()):
                return False
            stop = self._watch_stop = threading.Event()
            self._watcher = threading.Thread(target=self._watch_loop, args=(stop,), name="insia-run-recovery", daemon=True)
            self._watcher.start()
        return True

    def _watch_loop(self, stop: threading.Event) -> None:
        suspects: dict[str, str] = {}
        while not stop.wait(RECOVERY_WATCH_SECONDS):
            try:
                taken, suspects = self._recovery_tick(suspects)
            except WorkspaceError:  # closed
                return
            except Exception:  # noqa: BLE001 - e.g. the database stayed locked: try again next round
                log.warning("멈춘 실행을 확인하지 못했어요", exc_info=True)
                taken = []
            if taken:
                log.warning("실행하던 프로세스가 멈춘 실행 %d개를 '중단됨'으로 정리했어요: %s", len(taken), ", ".join(taken))
            self._publish_recovery_tick()

    def _publish_recovery_tick(self) -> list[str]:
        """The watcher's publish part: close ``sending`` attempts whose worker is gone (``recover_publish_attempts``)
        and hand their ids to ``publish_recovery_hook`` (the publishing service deletes their public images)."""
        try:
            closed = self.recover_publish_attempts()
        except WorkspaceError:  # closed
            return []
        except Exception:  # noqa: BLE001 - try again next round
            log.warning("멈춘 게시 시도를 확인하지 못했어요", exc_info=True)
            return []
        if closed:
            log.warning("게시하던 프로세스가 멈춘 게시 시도 %d개를 정리했어요: %s", len(closed), ", ".join(closed))
            hook = self.publish_recovery_hook
            if hook is not None:
                try:
                    hook(closed)
                except Exception:  # noqa: BLE001 - cleanup is best effort; the records are already right
                    log.warning("정리한 게시 시도의 파일을 지우지 못했어요", exc_info=True)
        return closed

    def mark_interrupted(self, *, watch: bool = True) -> int:
        """On startup: take over runs left ``running`` by a process that is gone (``recover_stale``).

        Returns the number of runs marked ``interrupted``. Runs another live
        process is working on (a cron ``insia run-due`` while the server
        starts) are left alone. With ``watch`` (the default) the check keeps
        running in the background (``watch_stale_runs``), so runs whose owner
        goes away later are recovered too.
        """
        taken = len(self.recover_stale())
        if watch:
            self.watch_stale_runs()
        return taken

    def _run_summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        brief = _loads(row["brief"], {}) or {}
        items = conn.execute("SELECT id, channel, score FROM items WHERE run_id = ? ORDER BY rowid", (row["id"],)).fetchall()
        events = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (row["id"],)).fetchone()[0]
        return {
            "run_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "topic": brief.get("topic", ""),
            "channels": list(brief.get("channels") or []),
            "mode": row["mode"],
            "model": row["model"],
            "parent_item_id": row["parent_item_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"] or None,
            "cost_usd": round(float(row["cost_usd"] or 0.0), 6),
            "error": row["error"] or None,
            "events": int(events),
            "items": {item["channel"]: item["id"] for item in items},
            "scores": {item["channel"]: item["score"] for item in items} if items else None,
        }

    def get_run(self, run_id: str) -> dict | None:
        """Run detail as a JSON-serializable dict (brief, options, profile snapshot, plan, research, progress,
        owner = ``{pid, host, heartbeat_at}`` of the process that runs / last ran it, …)."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            data = self._run_summary(conn, row)
        data.update({
            "brief": _loads(row["brief"], {}),
            "options": _loads(row["options"], {}) or {},
            "profile": _loads(row["profile"]),
            "plan": _loads(row["plan"]),
            "research": _loads(row["research"]),
            "progress": _loads(row["progress"], {}) or {},
            "owner": {"pid": int(row["owner_pid"] or 0) or None, "host": row["owner_host"] or None,
                      "heartbeat_at": row["heartbeat_at"] or None},
        })
        return data

    def list_runs(self, limit: int = 50, *, kind: str | None = None, status: str | None = None,
                  parent_item_id: str | None = None) -> list[dict]:
        """Run summaries, newest first (optionally filtered by ``kind`` / ``status`` / ``parent_item_id``:
        the jobs run on one content item)."""
        where, params = [], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        if parent_item_id:
            where.append("parent_item_id = ?")
            params.append(str(parent_item_id).strip())
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit or 50), 1000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM runs {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
            return [self._run_summary(conn, row) for row in rows]

    # -- events ------------------------------------------------------------------
    def append_event(self, run_id: str, event: dict) -> None:
        """Store one event (the dict emitted by ``EventBus``). A missing ``seq`` gets the next number.

        Refused with ``RunTakenOverError`` when this process holds the run's
        lease but another process took the run over.
        """
        if not isinstance(event, dict):
            raise WorkspaceError("이벤트는 객체여야 해요")
        lease = self._lease_for(run_id)
        with self._tx() as conn:
            self._check_owner(conn, run_id, lease)
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                last = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
                event = {**event, "seq": int(last) + 1}
            event = {"run_id": run_id, "ts": utc_now(), "t": 0.0, "agent": "system", "data": {}, **event}
            try:
                conn.execute("INSERT INTO events (run_id, seq, type, agent, t, ts, event) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (run_id, event["seq"], str(event.get("type", "")), str(event["agent"]), float(event["t"] or 0.0),
                              str(event["ts"]), _dumps(event)))
            except sqlite3.IntegrityError as exc:
                if not conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
                    raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요 (이벤트 저장 전 create_run 필요)") from None
                raise WorkspaceError(f"이벤트 번호가 겹쳐요: {run_id} #{event['seq']} ({exc})") from None

    def list_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute("SELECT event FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                                (run_id, max(0, int(after_seq or 0)))).fetchall()
        return [json.loads(row["event"]) for row in rows]

    def last_event(self, run_id: str) -> dict | None:
        """The run's last stored event (resume continues its ``seq`` and ``t``)."""
        with self._read() as conn:
            row = conn.execute("SELECT event FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        return json.loads(row["event"]) if row else None

    # -- content items -------------------------------------------------------------
    @staticmethod
    def _item(row: sqlite3.Row) -> ContentItem:
        passed = row["passed"]
        return ContentItem(
            id=row["id"], run_id=row["run_id"], channel=row["channel"], title=row["title"], status=row["status"],
            version=max(1, int(row["version"] or 0)), score=row["score"], passed=None if passed is None else bool(passed),
            scheduled_at=row["scheduled_at"], published_at=row["published_at"], published_url=row["published_url"],
            note=row["note"], created_at=row["created_at"], updated_at=row["updated_at"],
            approved_version=int(row["approved_version"] or 0), approval_forced=bool(row["approval_forced"]),
            approved_score=row["approved_score"], approved_at=row["approved_at"] or "",
            published_via=row["published_via"] or "", published_external_id=row["published_external_id"] or "",
        )

    @staticmethod
    def _version(row: sqlite3.Row) -> DraftVersion:
        review = _loads(row["review"])
        return DraftVersion(
            id=row["id"], item_id=row["item_id"], version=row["version"], source=row["source"],
            draft=Draft.model_validate_json(row["draft"]), review=Review.model_validate(review) if review else None,
            instructions=row["instructions"], created_at=row["created_at"],
        )

    def _item_row(self, conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"콘텐츠 {item_id}를 찾을 수 없어요")
        return row

    @staticmethod
    def _check_not_publishing(conn: sqlite3.Connection, item_id: str) -> None:
        """Refuse any change to an item's versions or status while an API publish attempt of it is live (``sending``
        or ``unknown``): what a person confirmed must stay exactly what gets (or got) published. Every write path
        goes through the internal functions that call this (``_insert_version``, ``_refresh_item``,
        ``_put_back_on_top``, ``attach_review``, ``set_item_status``), so the pipeline, jobs, ``import-run`` and the
        dashboard are all stopped here. Title, note and scheduled date (``update_item``) stay editable."""
        row = conn.execute("SELECT id, platform, status FROM publish_attempts WHERE item_id = ? AND status IN ('sending', 'unknown') "
                           "ORDER BY created_at DESC LIMIT 1", (item_id,)).fetchone()
        if row is not None:
            raise ItemLockedError(attempt_id=row["id"], platform=row["platform"], status=row["status"])

    def _insert_item(self, conn: sqlite3.Connection, item_id: str, channel: str, title: str, *, run_id: str = "",
                     brief: Brief | None = None, status: str = "draft", scheduled_at: str = "", note: str = "") -> None:
        if channel not in ALL_CHANNELS:
            raise WorkspaceError(f"알 수 없는 채널이에요: {channel!r}")
        if not _ITEM_ID.match(item_id):
            raise WorkspaceError(f"콘텐츠 id 형식이 올바르지 않아요: {item_id!r}")
        if status not in CONTENT_STATUSES:
            raise WorkspaceError(f"알 수 없는 상태예요: {status!r}")
        now = utc_now()
        conn.execute(
            "INSERT INTO items (id, run_id, channel, title, status, scheduled_at, note, brief, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, run_id or "", channel, title or "", status, _check_when(scheduled_at), (note or "")[:MAX_NOTE_CHARS],
             brief.model_dump_json() if brief is not None else None, now, now),
        )

    def create_item(self, channel: str, title: str, *, run_id: str = "", brief: Brief | None = None,
                    item_id: str | None = None, status: str = "draft", scheduled_at: str = "", note: str = "") -> ContentItem:
        """An empty content item (versions come with ``add_version``). Pipeline outputs use
        ``upsert_item_from_result`` or the pipeline's progressive recorder instead."""
        brief = _as_model(Brief, brief, "브리프")
        item_id = item_id or _hex_id("it")
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                raise WorkspaceError(f"이미 있는 콘텐츠 id예요: {item_id}")
            self._insert_item(conn, item_id, channel, title, run_id=run_id, brief=brief, status=status,
                              scheduled_at=scheduled_at, note=note)
            return self._item(self._item_row(conn, item_id))

    def ensure_item(self, item_id: str, channel: str, title: str, *, run_id: str = "", brief: Brief | None = None) -> ContentItem:
        """Create the item if it does not exist yet; return it either way.

        Refused (``RunTakenOverError``) when this process held ``run_id`` but another process took it over.
        """
        brief = _as_model(Brief, brief, "브리프")
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                self._insert_item(conn, item_id, channel, title, run_id=run_id, brief=brief)
                row = self._item_row(conn, item_id)
            return self._item(row)

    def _insert_version(self, conn: sqlite3.Connection, item_id: str, draft: Draft, *, source: str, review: Review | None,
                        instructions: str, run_id: str, role: str) -> DraftVersion:
        if source not in VERSION_SOURCES:
            raise WorkspaceError(f"버전 출처는 agent 또는 human이어야 해요 (받은 값: {source!r})")
        item = self._item_row(conn, item_id)
        self._check_not_publishing(conn, item_id)
        if draft.channel != item["channel"]:
            raise WorkspaceError(f"채널이 달라요: 콘텐츠는 {item['channel']}, 초안은 {draft.channel}")
        number = conn.execute("SELECT COALESCE(MAX(version), 0) FROM versions WHERE item_id = ?", (item_id,)).fetchone()[0] + 1
        version_id = _hex_id("dv")
        now = utc_now()
        conn.execute(
            "INSERT INTO versions (id, item_id, version, source, run_id, role, round, draft, review, instructions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, item_id, number, source, run_id or "", role, int(draft.round), draft.model_dump_json(),
             review.model_dump_json() if review is not None else None, instructions or "", now),
        )
        return DraftVersion(id=version_id, item_id=item_id, version=number, source=source, draft=draft, review=review,  # type: ignore[arg-type]
                            instructions=instructions or "", created_at=now)

    def _refresh_item(self, conn: sqlite3.Connection, item_id: str, *, keep_status: bool = False) -> None:
        """Recompute title/version/score/passed/status from the latest version.

        - ``draft`` / ``needs_changes`` follow the latest review (failed → needs_changes).
        - ``approved`` / ``scheduled`` fall back to draft/needs_changes when a newer
          version than the approved one appears (approval is for specific content).
        - ``published`` / ``archived`` never change here.

        ``keep_status``: the status stays as it is (the latest version is a copy of
        the version that was current, put back on top, so nothing a person decided
        about it changes).
        """
        item = self._item_row(conn, item_id)
        self._check_not_publishing(conn, item_id)
        latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
        if latest is None:
            return
        draft = Draft.model_validate_json(latest["draft"])
        review_data = _loads(latest["review"])
        review = Review.model_validate(review_data) if review_data else None
        failed = review is not None and not review.passed
        status = item["status"]
        if keep_status:
            pass
        elif status in ("draft", "needs_changes"):
            status = "needs_changes" if failed else "draft"
        elif status in ("approved", "scheduled") and int(latest["version"]) != int(item["approved_version"]):
            status = "needs_changes" if failed else "draft"
        conn.execute(
            "UPDATE items SET title = ?, version = ?, score = ?, passed = ?, status = ?, updated_at = ? WHERE id = ?",
            (draft.title or item["title"], int(latest["version"]), review.score if review else None,
             (1 if review.passed else 0) if review else None, status, utc_now(), item_id),
        )

    def add_version(self, item_id: str, draft: Draft, *, source: str, review: Review | None = None, instructions: str = "",
                    run_id: str = "") -> DraftVersion:
        """Append a version (numbered 1, 2, …) and refresh the item's summary and status.

        ``run_id`` is the run (pipeline or job) that produced it; the pipeline
        uses it to find its own rounds again on resume. Refused
        (``RunTakenOverError``) when this process held that run but another
        process took it over.
        """
        draft = _as_model(Draft, draft, "초안")
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            if source == "agent" and run_id and item_id == pipeline_item_id(run_id, draft.channel):
                # a run's round on its own item (pipeline, import): never buries a person's edit or decision
                return self._add_run_version(conn, item_id, draft, review=review, instructions=instructions,
                                             run_id=run_id).version
            version = self._insert_version(conn, item_id, draft, source=source, review=review, instructions=instructions,
                                           run_id=run_id, role="round")
            self._refresh_item(conn, item_id)
        return version

    def add_run_version(self, item_id: str, draft: Draft, *, run_id: str, review: Review | None = None,
                        instructions: str = "") -> RunVersion:
        """Store a run's draft round on the run's own item (``it_<run_id>_<channel>``) without burying newer work.

        Normally the round is appended and becomes the current version, like
        ``add_version``. But when the item moved on after the run's last
        stored round — a person saved an edit (while the run was stopped, or
        from the dashboard while a CLI run was going), another job saved a
        version, or a person approved, scheduled or published the item — the
        round is only kept in the history (resume still finds it) and the
        version that was current is put back on top as a copy (role
        ``restored``, same content and review, a change-log note), the way
        ``add_job_version`` does for revise jobs. The item's status stays as
        the person left it (an approval moves to the copy, which has the same
        content). The check and the inserts are one transaction.
        """
        draft = _as_model(Draft, draft, "초안")
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            return self._add_run_version(conn, item_id, draft, review=review, instructions=instructions, run_id=run_id)

    def _add_run_version(self, conn: sqlite3.Connection, item_id: str, draft: Draft, *, review: Review | None,
                         instructions: str, run_id: str) -> RunVersion:
        head = self._run_head(conn, item_id, run_id)
        version = self._insert_version(conn, item_id, draft, source="agent", review=review, instructions=instructions,
                                       run_id=run_id, role="round")
        if head.keep is None:
            self._refresh_item(conn, item_id)
            return RunVersion(version)
        restored = self._put_back_on_top(conn, item_id, head.keep, self._held_note(conn, item_id, head, draft.round,
                                                                                   version.version), run_id)
        self._refresh_item(conn, item_id, keep_status=True)
        return RunVersion(version, restored, head.by_human, head.reason)

    @staticmethod
    def _same_text(a: sqlite3.Row, b: sqlite3.Row) -> bool:
        """Whether two stored versions hold the same draft from the same source (the change log aside: a copy put
        back on top starts with a note)."""
        if a["source"] != b["source"]:
            return False
        da, db_ = _loads(a["draft"], {}) or {}, _loads(b["draft"], {}) or {}
        da.pop("change_log", None)
        db_.pop("change_log", None)
        return bool(da) and da == db_

    def _is_copy_of(self, row: sqlite3.Row, original: sqlite3.Row) -> bool:
        """``row`` is a copy of ``original`` put back on top later (role ``restored``, same text and source), e.g. a
        run kept a 수정 요청 job's revision current over its own round, or a job kept a run's round current."""
        return row["role"] == "restored" and int(row["version"]) > int(original["version"]) and self._same_text(row, original)

    def _own_row(self, conn: sqlite3.Connection, item_id: str, run_id: str) -> sqlite3.Row | None:
        """The newest version run ``run_id`` itself stored on the item (a round or its final copy)."""
        return conn.execute("SELECT * FROM versions WHERE item_id = ? AND run_id = ? AND source = 'agent' "
                            "AND role IN ('round', 'final') ORDER BY version DESC LIMIT 1", (item_id, run_id)).fetchone()

    def _run_head(self, conn: sqlite3.Connection, item_id: str, run_id: str) -> _RunHead:
        """Whether run ``run_id`` may put a new version of its item on top (``keep`` is None) or must keep it in
        the history only: the item's latest version is not the run's own agent work (a person's edit, another
        job's version, or a copy the run already put back on top), or a person approved, scheduled or published
        the item. A copy of the run's own newest version that another job put back on top counts as the run's
        own work (the text is the run's)."""
        latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
        if latest is None:
            return _RunHead(None, 0, False, "")
        own_row = self._own_row(conn, item_id, run_id)
        own = int(own_row["version"]) if own_row is not None else 0
        by_human = conn.execute("SELECT 1 FROM versions WHERE item_id = ? AND version > ? AND source = 'human' LIMIT 1",
                                (item_id, own)).fetchone() is not None
        moved = int(latest["version"]) != own and not (own_row is not None and self._is_copy_of(latest, own_row))
        held_status = self._item_row(conn, item_id)["status"] in HELD_STATUSES
        if not moved and not held_status:
            return _RunHead(None, own, False, "")
        if not moved:  # the run's own text is current but a person approved, scheduled or published it
            by_human = False
        reason = HELD_HUMAN_EDIT if by_human else (HELD_STATUS if held_status else HELD_NEWER_VERSION)
        return _RunHead(latest, own, by_human, reason)

    def _held_note(self, conn: sqlite3.Connection, item_id: str, head: _RunHead, round_: int, number: int) -> str:
        kept = int(head.keep["version"]) if head.keep is not None else 0
        tail = f"(에이전트의 R{int(round_)} 결과는 기록에만 남겨요: v{number})"
        if head.reason == HELD_STATUS:
            label = STATUS_LABELS.get(self._item_row(conn, item_id)["status"], "")
            return f"이 콘텐츠가 '{label}' 상태라 v{kept} 버전을 그대로 현재 버전으로 두었어요 {tail}"
        who = "사람이 고친 " if head.reason == HELD_HUMAN_EDIT else ""
        return f"이 실행이 멈췄거나 진행되는 동안 {who}v{kept} 버전이 저장돼서, 그 내용을 다시 현재 버전으로 올렸어요 {tail}"

    def _put_back_on_top(self, conn: sqlite3.Connection, item_id: str, kept_row: sqlite3.Row, note: str,
                         run_id: str) -> DraftVersion:
        """Append a copy of ``kept_row`` (role ``restored``) so it is the latest version again; an approval of it
        moves to the copy (same content). The caller refreshes the item with ``keep_status=True``."""
        self._check_not_publishing(conn, item_id)
        kept = self._version(kept_row)
        change_log = list(kept.draft.change_log)
        if kept_row["role"] == "restored" and change_log:  # a copy of a copy: replace its note instead of stacking notes
            change_log = change_log[1:]
        copy = kept.draft.model_copy(update={"change_log": [note, *change_log]})
        restored = self._insert_version(conn, item_id, copy, source=kept.source, review=kept.review,
                                        instructions=kept.instructions, run_id=run_id, role="restored")
        item = self._item_row(conn, item_id)
        if item["status"] in ("approved", "scheduled") and int(item["approved_version"]) == kept.version:
            conn.execute("UPDATE items SET approved_version = ? WHERE id = ?", (restored.version, item_id))
        return restored

    def add_job_version(self, item_id: str, draft: Draft, *, base_version: int, source: str = "agent",
                        review: Review | None = None, instructions: str = "", run_id: str = "",
                        label: str = "수정 요청") -> tuple[DraftVersion, DraftVersion | None]:
        """Append a job's result that was made from version ``base_version``; returns ``(new, restored)``.

        When the item's latest version is still ``base_version`` this is
        ``add_version`` (``restored`` is None). When the item moved on while the
        job ran (a person saved an edit, or another job added a version), the
        job's version is kept in the history but does not silently become the
        current one: the version that was current is appended again right
        after it (role ``restored``, same content, source and review, with a
        change-log note), so the latest version — what exports and approval
        use — is still the newer work, and the item's status (and approval) stays
        as it was. The check and both inserts are one transaction (refused like
        ``add_version`` when the job was taken over).
        """
        draft = _as_model(Draft, draft, "초안")
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            version = self._insert_version(conn, item_id, draft, source=source, review=review, instructions=instructions,
                                           run_id=run_id, role="round")
            restored: DraftVersion | None = None
            if latest is not None and int(latest["version"]) != int(base_version):
                note = (f"{label}이 진행되는 동안 v{int(latest['version'])} 버전이 저장돼서, 그 내용을 다시 현재 버전으로 "
                        f"올렸어요 ({label} 결과는 v{version.version}, v{int(base_version)} 기준)")
                restored = self._put_back_on_top(conn, item_id, latest, note, run_id)
            self._refresh_item(conn, item_id, keep_status=restored is not None)
        return version, restored

    def attach_review(self, version_id: str, review: Review, *, run_id: str = "") -> None:
        """Store ``review`` on a version. ``run_id`` is the run (pipeline or job) that made the review: refused
        (``RunTakenOverError``) when this process held it but another process took it over. The item's summary
        and status follow only when it is the item's current (latest) version.

        Copies of the version that were put back on top while the review was being made (``add_run_version`` /
        ``add_job_version``: e.g. a CLI run kept a 수정 요청 job's revision current over its own round) show the
        same text, so they get the review too — unless a copy already has a different review of its own. Without
        this the current version would stay unreviewed and approval would need "그래도 승인"."""
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"버전 {version_id}를 찾을 수 없어요")
            item_id, before = row["item_id"], _loads(row["review"])
            self._check_not_publishing(conn, item_id)
            conn.execute("UPDATE versions SET review = ? WHERE id = ?", (review.model_dump_json(), version_id))
            touched = {int(row["version"])}
            for copy in conn.execute("SELECT * FROM versions WHERE item_id = ? AND version > ? AND role = 'restored'",
                                     (item_id, int(row["version"]))).fetchall():
                if self._is_copy_of(copy, row) and _loads(copy["review"]) == before:  # the copy still mirrors it
                    conn.execute("UPDATE versions SET review = ? WHERE id = ?", (review.model_dump_json(), copy["id"]))
                    touched.add(int(copy["version"]))
            latest = conn.execute("SELECT MAX(version) FROM versions WHERE item_id = ?", (item_id,)).fetchone()[0]
            if int(latest or 0) in touched:
                self._refresh_item(conn, item_id)

    def get_version(self, version_id: str) -> DraftVersion | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return self._version(row) if row else None

    def get_item(self, item_id: str) -> ContentItemDetail | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            versions = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version", (item_id,)).fetchall()
            brief_text = row["brief"]
            if not brief_text and row["run_id"]:
                run = conn.execute("SELECT brief FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
                brief_text = run["brief"] if run else None
        brief_data = _loads(brief_text)
        brief = None
        if brief_data:
            try:
                brief = Brief.model_validate(brief_data)
            except ValidationError:
                brief = None
        return ContentItemDetail(item=self._item(row), versions=[self._version(v) for v in versions], brief=brief)

    def list_items(self, *, status: str | None = None, channel: str | None = None, limit: int = 200) -> list[ContentItem]:
        where, params = [], []
        if status:
            if status not in CONTENT_STATUSES:
                raise WorkspaceError(f"알 수 없는 상태예요: {status!r}")
            where.append("status = ?")
            params.append(status)
        if channel:
            if channel not in ALL_CHANNELS:
                raise WorkspaceError(f"알 수 없는 채널이에요: {channel!r}")
            where.append("channel = ?")
            params.append(channel)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit or 200), 5000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM items {clause} ORDER BY updated_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._item(row) for row in rows]

    def update_item(self, item_id: str, *, title: str | None = None, scheduled_at: str | None = None,
                    note: str | None = None) -> ContentItem:
        """Edit item fields that are not part of the approval flow."""
        values: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise WorkspaceError("제목을 입력해 주세요")
            values["title"] = title.strip()[:300]
        if scheduled_at is not None:
            values["scheduled_at"] = _check_when(scheduled_at)
        if note is not None:
            values["note"] = note.strip()[:MAX_NOTE_CHARS]
        with self._tx() as conn:
            self._item_row(conn, item_id)
            if values:
                values["updated_at"] = utc_now()
                assignments = ", ".join(f"{key} = ?" for key in values)
                conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
            return self._item(self._item_row(conn, item_id))

    def set_item_status(self, item_id: str, status: str, *, scheduled_at: str | None = None,
                        published_url: str | None = None, note: str | None = None, force: bool = False) -> ContentItem:
        """Move an item through 초안 → 승인 → 게시 예정/게시 완료 (or 보관).

        - draft/needs_changes → approved needs the latest version to have a
          passed review, unless ``force=True`` ("그래도 승인").
        - approved → scheduled (needs ``scheduled_at``) | published; scheduled → published.
        - any → archived; archived → draft (restore).
        - published sets ``published_at`` to now when it is empty.

        Every approval is recorded on the item: ``approved_version``,
        ``approved_score`` (its review score, None without a review),
        ``approved_at`` and ``approval_forced`` (true when ``force`` approved a
        version that had not passed review — the dashboard shows '강제 승인').
        """
        if status not in CONTENT_STATUSES:
            raise WorkspaceError(f"알 수 없는 상태예요: {status!r} ({', '.join(CONTENT_STATUSES)} 중 하나)")
        with self._tx() as conn:
            row = self._item_row(conn, item_id)
            self._check_not_publishing(conn, item_id)
            old = row["status"]
            if status != old and status not in TRANSITIONS.get(old, frozenset()):
                hint = " 먼저 승인해 주세요." if status in ("scheduled", "published") and old in ("draft", "needs_changes") else ""
                raise InvalidTransitionError(f"'{STATUS_LABELS[old]}' 상태에서 '{STATUS_LABELS[status]}'(으)로 바꿀 수 없어요.{hint}")
            values: dict[str, Any] = {"status": status}
            if status == "approved" and old in ("draft", "needs_changes"):  # scheduled → approved keeps the approval
                latest = conn.execute("SELECT version, review FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1",
                                      (item_id,)).fetchone()
                if latest is None:
                    raise ApprovalBlockedError("승인할 버전이 없어요. 초안을 먼저 만들어 주세요.", item_id=item_id, version=0, score=None)
                review_data = _loads(latest["review"])
                review = Review.model_validate(review_data) if review_data else None
                if not force and not (review is not None and review.passed):
                    number = int(latest["version"])
                    if review is None:
                        message = (f"최신 버전(v{number})은 아직 검수를 받지 않았어요. 재검수를 먼저 돌리거나, "
                                   "내용을 직접 확인했다면 '그래도 승인'을 눌러 주세요.")
                    else:
                        message = (f"최신 버전(v{number})이 검수를 통과하지 못했어요 ({review.score}점). 수정 요청이나 재검수 뒤 "
                                   "승인하거나, 내용을 직접 확인했다면 '그래도 승인'을 눌러 주세요.")
                    raise ApprovalBlockedError(message, item_id=item_id, version=number, score=review.score if review else None)
                values["approved_version"] = int(latest["version"])
                values["approved_score"] = review.score if review is not None else None
                values["approval_forced"] = 0 if (review is not None and review.passed) else 1
                values["approved_at"] = utc_now()
            if status == "scheduled":
                when = scheduled_at if scheduled_at is not None else row["scheduled_at"]
                if not (when or "").strip():
                    raise WorkspaceError("게시 예정일(scheduled_at)을 알려 주세요 (예: 2026-10-05)")
                values["scheduled_at"] = _check_when(when)
            elif scheduled_at is not None:
                values["scheduled_at"] = _check_when(scheduled_at)
            if published_url is not None:
                url = published_url.strip()
                if url and not _URL.match(url):
                    raise WorkspaceError("게시 URL은 http:// 또는 https://로 시작해야 해요")
                values["published_url"] = url
            if status == "published" and not row["published_at"]:
                values["published_at"] = utc_now()
            if status == "published" and old != "published":  # 게시 완료 표시 by a person
                values.update(self._published_by_hand(conn, row, url_given=bool((published_url or "").strip())))
            if note is not None:
                values["note"] = note.strip()[:MAX_NOTE_CHARS]
            values["updated_at"] = utc_now()
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
            return self._item(self._item_row(conn, item_id))

    @staticmethod
    def _published_by_hand(conn: sqlite3.Connection, row: sqlite3.Row, *, url_given: bool) -> dict[str, Any]:
        """The API fields when a person marks an item published: they must say how *this* version went up.

        - The current version has a published API attempt (the item was moved back — 보관 → 복원 → 승인 — after that
          post, or recording the item failed): ``published_via`` is that API post's (the attempt's ``item_via``, the
          item's own value, else the platform's), ``published_external_id`` its id, ``published_at`` its time and
          its permalink the address (unless one is given here; an older post's address is never kept for it). An
          accidental 보관 → 복원 → 게시 완료 표시 thus comes back to the same record.
        - Otherwise the person posted this version by hand (DESIGN.md 5-4): ``published_via`` and
          ``published_external_id`` are ``''``; an address and time left from an older version's API post are not
          kept (a new address given here stays)."""
        version = max(1, int(row["version"] or 0))
        attempt = conn.execute("SELECT * FROM publish_attempts WHERE item_id = ? AND version = ? AND status = 'published' "
                               "ORDER BY finished_at DESC, rowid DESC LIMIT 1", (row["id"], version)).fetchone()
        if attempt is None:
            out: dict[str, Any] = {"published_via": "", "published_external_id": ""}
            if row["published_via"]:  # the address and time belong to an older version's API post
                out["published_at"] = utc_now()
                if not url_given:
                    out["published_url"] = ""
            return out
        state = _loads(attempt["state"], {}) or {}
        via = (str(state.get("item_via") or "") or str(row["published_via"] or "")
               or PUBLISHED_VIA_BY_PLATFORM.get(str(attempt["platform"]), ""))
        out = {"published_via": via if via in PUBLISHED_VIA_VALUES else "",
               "published_external_id": str(attempt["external_id"] or "")}
        link = str(attempt["permalink"] or "")
        if not url_given and link and _URL.match(link):
            out["published_url"] = link
        elif not url_given and row["published_url"] and conn.execute(
                "SELECT 1 FROM publish_attempts WHERE item_id = ? AND id <> ? AND permalink = ? LIMIT 1",
                (row["id"], attempt["id"], row["published_url"])).fetchone() is not None:
            out["published_url"] = ""  # an older post's address, not this one's
        if attempt["finished_at"]:
            out["published_at"] = str(attempt["finished_at"])
        return out

    def published_history(self, channel: str | None = None, limit: int = 50) -> list[ContentItem]:
        """Published items, newest first (the planner uses them to avoid repeating topics). Items "published" by the
        fake publishing mode (``published_via='fake'``, tests and demos) were never posted and are left out."""
        params: list[Any] = []
        clause = ""
        if channel:
            if channel not in ALL_CHANNELS:
                raise WorkspaceError(f"알 수 없는 채널이에요: {channel!r}")
            clause = "AND channel = ?"
            params.append(channel)
        limit = max(1, min(int(limit or 50), 1000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM items WHERE status = 'published' AND published_via <> 'fake' {clause} "
                                "ORDER BY published_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._item(row) for row in rows]

    def list_run_versions(self, run_id: str, channel: str) -> list[DraftVersion]:
        """The review-loop rounds a pipeline run stored for one channel, in round order."""
        item_id = pipeline_item_id(run_id, channel)
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM versions WHERE item_id = ? AND run_id = ? AND role = 'round' "
                                "ORDER BY round, version", (item_id, run_id)).fetchall()
        return [self._version(row) for row in rows]

    def run_item_superseded(self, run_id: str, channel: str) -> dict[str, Any] | None:
        """Whether run ``run_id``'s work is not the current version of its item for ``channel``.

        None when the item's latest version is the run's own (or there is no
        item). Otherwise ``{"version": the run's newest version, "current_version":
        the item's current one, "superseded_by_human_edit": a person saved a
        version after the run's, "status": the item's status}`` — e.g. a person
        edited the item while the run was stopped or going (``add_run_version``).
        """
        item_id = pipeline_item_id(run_id, channel)
        with self._read() as conn:
            item = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
            if item is None:
                return None
            latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            own_row = self._own_row(conn, item_id, run_id)
            if latest is None or own_row is None or int(latest["version"]) == int(own_row["version"]) \
                    or self._is_copy_of(latest, own_row):  # a copy of the run's text that a job put back on top
                return None
            own = int(own_row["version"])
            by_human = conn.execute("SELECT 1 FROM versions WHERE item_id = ? AND version > ? AND source = 'human' LIMIT 1",
                                    (item_id, own)).fetchone() is not None
        return {"version": own, "current_version": int(latest["version"]), "superseded_by_human_edit": by_human,
                "status": item["status"]}

    def version_is_current(self, item_id: str, version: int) -> bool:
        """Whether version ``version`` holds the item's current text: it is the latest version, or the latest is a
        copy of it put back on top (``add_run_version`` / ``add_job_version`` keep a version current that way, e.g.
        a CLI run keeps a 수정 요청 job's revision current over its own round). For a job deciding whether its
        result was superseded."""
        with self._read() as conn:
            latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
            if latest is None:
                return False
            if int(latest["version"]) == int(version):
                return True
            row = conn.execute("SELECT * FROM versions WHERE item_id = ? AND version = ?", (item_id, int(version))).fetchone()
            return row is not None and self._is_copy_of(latest, row)

    def upsert_item_from_result(self, run_id: str, result: ChannelResult, brief: Brief) -> ContentItem:
        """Create/update item ``it_<run_id>_<channel>`` from a finished channel.

        Every draft round becomes a version (source=agent) with its review;
        rounds already stored by this run are not duplicated (safe to call
        again, e.g. after a resume). When the newest version this run stored
        is not the final draft — the best-scoring round is an earlier one, or
        a later round was written but its review failed — the final draft is
        added once more as the newest version (role ``final``, with its
        review), so "the latest version" is always the final text. Refused
        (``RunTakenOverError``) when this process held the run but another
        process took it over.

        When the item moved on after the run's last stored round (a person's
        edit or decision, another job's version: see ``add_run_version``),
        the run's work stays in the history only: no final copy is added,
        rounds stored now are followed by a copy of the current version, and
        the item's status is left as it is (``run_item_superseded`` reports it).
        """
        result = _as_model(ChannelResult, result, "채널 결과")
        brief = _as_model(Brief, brief, "브리프")
        item_id = pipeline_item_id(run_id, result.channel)
        reviews_by_round = {review.round: review for review in result.reviews}
        with self._tx() as conn:
            self._guard_run(conn, run_id)
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
                self._insert_item(conn, item_id, result.channel, result.final.title, run_id=run_id, brief=brief)
            head = self._run_head(conn, item_id, run_id)
            stored = {
                row["round"]: row
                for row in conn.execute("SELECT id, round, review FROM versions WHERE item_id = ? AND run_id = ? "
                                        "AND role = 'round' AND source = 'agent'", (item_id, run_id)).fetchall()
            }
            added: DraftVersion | None = None
            for draft in result.drafts:
                review = reviews_by_round.get(draft.round)
                row = stored.get(draft.round)
                if row is None:
                    added = self._insert_version(conn, item_id, draft, source="agent", review=review, instructions="",
                                                 run_id=run_id, role="round")
                elif review is not None and (_loads(row["review"]) != review.model_dump(mode="json")):
                    conn.execute("UPDATE versions SET review = ? WHERE id = ?", (review.model_dump_json(), row["id"]))
            if head.keep is not None:  # someone else's version (or decision) stays current
                if added is not None:
                    note = self._held_note(conn, item_id, head, added.draft.round, added.version)
                    self._put_back_on_top(conn, item_id, head.keep, note, run_id)
                self._refresh_item(conn, item_id, keep_status=True)
                return self._item(self._item_row(conn, item_id))
            newest = conn.execute("SELECT round, role, draft, review FROM versions WHERE item_id = ? AND run_id = ? "
                                  "AND source = 'agent' AND role IN ('round', 'final') ORDER BY version DESC LIMIT 1",
                                  (item_id, run_id)).fetchone()
            if newest is not None and not self._is_final_copy(newest, result.final):
                later = int(newest["round"]) > result.final.round and newest["review"] is None and newest["role"] == "round"
                if later:  # e.g. the reviewer failed on the last revision: fall back to the best reviewed round
                    note = (f"R{int(newest['round'])} 수정본은 검수를 마치지 못해서, 검수를 받은 R{result.final.round} 버전을 "
                            "최종본으로 다시 올렸어요")
                else:
                    note = f"R{result.final.round} 버전이 가장 점수가 높아 최종본으로 골랐어요"
                final = result.final.model_copy(update={"change_log": [note, *result.final.change_log]})
                self._insert_version(conn, item_id, final, source="agent", review=reviews_by_round.get(result.final.round),
                                     instructions="", run_id=run_id, role="final")
            self._refresh_item(conn, item_id)
            return self._item(self._item_row(conn, item_id))

    @staticmethod
    def _is_final_copy(row: sqlite3.Row, final: Draft) -> bool:
        """Whether a stored version holds ``final``'s text (its change log may differ: a final copy has a note)."""
        if int(row["round"]) != int(final.round):
            return False
        try:
            stored = Draft.model_validate_json(row["draft"])
        except ValidationError:
            return False
        return (stored.title, stored.content, stored.hashtags) == (final.title, final.content, final.hashtags)

    def item_plan(self, item_id: str) -> Plan | None:
        """The plan of the pipeline run that produced the item (if any)."""
        with self._read() as conn:
            row = conn.execute("SELECT r.plan FROM items i JOIN runs r ON r.id = i.run_id WHERE i.id = ?", (item_id,)).fetchone()
        data = _loads(row["plan"]) if row else None
        return Plan.model_validate(data) if data else None

    def item_research(self, item_id: str) -> ResearchPack | None:
        """The newest research pack of the item's own run or of a job on the item."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT r.research FROM runs r WHERE r.research IS NOT NULL AND "
                "(r.id = (SELECT run_id FROM items WHERE id = ?) OR r.parent_item_id = ?) "
                "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1", (item_id, item_id)).fetchall()
        data = _loads(rows[0]["research"]) if rows else None
        return ResearchPack.model_validate(data) if data else None

    # -- usage ---------------------------------------------------------------------
    def record_usage(self, record: UsageRecord) -> None:
        record = _as_model(UsageRecord, record, "사용량 기록")
        cost = _finite(record.cost_usd)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO usage (run_id, agent, task, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "web_search_requests, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.run_id or "", record.agent or "", record.task or "", record.model or "", max(0, record.input_tokens),
                 max(0, record.output_tokens), max(0, record.cache_read_tokens), max(0, record.cache_write_tokens),
                 max(0, record.web_search_requests), cost, _normalize_ts(record.created_at)),
            )
            if record.run_id and cost:
                conn.execute("UPDATE runs SET cost_usd = cost_usd + ? WHERE id = ?", (cost, record.run_id))

    def usage_summary(self, *, since: str | None = None, until: str | None = None) -> dict:
        """Cost/usage totals. ``since``/``until``: ``YYYY-MM-DD`` (Korean calendar days, inclusive) or ISO date-times.

        ``{"total_usd", "calls", <token totals>, "runs": [{run_id, kind, topic, usd, calls, <tokens>, first_at, last_at}],
        "by_task": {task: {usd, calls, input_tokens, output_tokens}}, "by_day": [{date, usd, calls}]}`` — runs newest first,
        days oldest first (dates in KST).
        """
        where, params = [], []
        if since:
            op, bound = _time_bound(since, end=False)
            where.append(f"u.created_at {op} ?")
            params.append(bound)
        if until:
            op, bound = _time_bound(until, end=True)
            where.append(f"u.created_at {op} ?")
            params.append(bound)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._read() as conn:
            rows = conn.execute(f"SELECT u.*, r.kind AS run_kind, r.brief AS run_brief FROM usage u "
                                f"LEFT JOIN runs r ON r.id = u.run_id {clause} ORDER BY u.created_at, u.id", params).fetchall()
        token_keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "web_search_requests")
        totals: dict[str, Any] = {"total_usd": 0.0, "calls": 0, **{key: 0 for key in token_keys}}
        runs: dict[str, dict[str, Any]] = {}
        by_task: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            cost = float(row["cost_usd"] or 0.0)
            totals["total_usd"] += cost
            totals["calls"] += 1
            for key in token_keys:
                totals[key] += int(row[key] or 0)
            run_id = row["run_id"] or ""
            entry = runs.get(run_id)
            if entry is None:
                brief = _loads(row["run_brief"], {}) or {}
                entry = runs[run_id] = {"run_id": run_id, "kind": row["run_kind"] or ("other" if not run_id else ""),
                                        "topic": brief.get("topic", ""), "usd": 0.0, "calls": 0,
                                        **{key: 0 for key in token_keys}, "first_at": row["created_at"], "last_at": row["created_at"]}
            entry["usd"] += cost
            entry["calls"] += 1
            entry["last_at"] = row["created_at"]
            for key in token_keys:
                entry[key] += int(row[key] or 0)
            task = by_task.setdefault(row["task"] or "other", {"usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})
            task["usd"] += cost
            task["calls"] += 1
            task["input_tokens"] += int(row["input_tokens"] or 0)
            task["output_tokens"] += int(row["output_tokens"] or 0)
            day = by_day.setdefault(kst_date(row["created_at"]), {"usd": 0.0, "calls": 0})
            day["usd"] += cost
            day["calls"] += 1
        for entry in runs.values():
            entry["usd"] = round(entry["usd"], 6)
        for task in by_task.values():
            task["usd"] = round(task["usd"], 6)
        totals["total_usd"] = round(totals["total_usd"], 6)
        return {
            "since": since or None,
            "until": until or None,
            **totals,
            "runs": sorted(runs.values(), key=lambda e: e["last_at"], reverse=True),
            "by_task": by_task,
            "by_day": [{"date": day, "usd": round(v["usd"], 6), "calls": v["calls"]} for day, v in sorted(by_day.items())],
        }

    def run_cost(self, run_id: str) -> float:
        with self._read() as conn:
            value = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE run_id = ?", (run_id,)).fetchone()[0]
        return round(float(value or 0.0), 6)

    # -- calendar ------------------------------------------------------------------
    @staticmethod
    def _slot(row: sqlite3.Row) -> CalendarSlot:
        return CalendarSlot(id=row["id"], date=row["date"], channel=row["channel"], topic=row["topic"], angle=row["angle"],
                            keywords=_loads(row["keywords"], []) or [], goal=row["goal"], status=row["status"],
                            item_id=row["item_id"], run_id=row["run_id"], created_at=row["created_at"])

    @staticmethod
    def _sorted_slots(slots: list[CalendarSlot]) -> list[CalendarSlot]:
        order = {channel: i for i, channel in enumerate(ALL_CHANNELS)}
        return sorted(slots, key=lambda s: (s.date, order.get(s.channel, 9), s.created_at, s.id))

    def add_slots(self, slots: Sequence[PlannedSlot]) -> list[CalendarSlot]:
        planned = [_as_model(PlannedSlot, slot, "캘린더 슬롯") for slot in slots]
        now = utc_now()
        created: list[CalendarSlot] = []
        with self._tx() as conn:
            for slot in planned:
                topic = slot.topic.strip()
                if not topic:
                    raise WorkspaceError("슬롯 주제가 비어 있어요")
                keywords = [k.strip() for k in slot.keywords if k and k.strip()]
                entry = CalendarSlot(id=_hex_id("sl"), date=_check_date(slot.date, "게시 예정일"), channel=slot.channel, topic=topic,
                                     angle=slot.angle.strip(), keywords=keywords, goal=slot.goal.strip(), status="planned",
                                     created_at=now)
                conn.execute("INSERT INTO slots (id, date, channel, topic, angle, keywords, goal, status, created_at, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                             (entry.id, entry.date, entry.channel, entry.topic, entry.angle, _dumps(entry.keywords), entry.goal, now, now))
                created.append(entry)
        return created

    def get_slot(self, slot_id: str) -> CalendarSlot | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return self._slot(row) if row else None

    def list_slots(self, *, date_from: str | None = None, date_to: str | None = None) -> list[CalendarSlot]:
        where, params = [], []
        if date_from:
            where.append("date >= ?")
            params.append(_check_date(date_from, "시작일"))
        if date_to:
            where.append("date <= ?")
            params.append(_check_date(date_to, "종료일"))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM slots {clause}", params).fetchall()
        return self._sorted_slots([self._slot(row) for row in rows])

    _SLOT_FIELDS = frozenset({"date", "channel", "topic", "angle", "keywords", "goal", "status", "item_id", "run_id"})

    def update_slot(self, slot_id: str, **fields: Any) -> CalendarSlot:
        unknown = set(fields) - self._SLOT_FIELDS
        if unknown:
            raise TypeError(f"update_slot: unknown field(s) {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                continue
            if key == "date":
                values["date"] = _check_date(str(value), "게시 예정일")
            elif key == "channel":
                if value not in ALL_CHANNELS:
                    raise WorkspaceError(f"알 수 없는 채널이에요: {value!r}")
                values["channel"] = value
            elif key == "status":
                if value not in SLOT_STATUSES:
                    raise WorkspaceError(f"슬롯 상태는 {', '.join(SLOT_STATUSES)} 중 하나여야 해요 (받은 값: {value!r})")
                values["status"] = value
            elif key == "keywords":
                items = value.split(",") if isinstance(value, str) else list(value)
                values["keywords"] = _dumps([str(k).strip() for k in items if str(k).strip()])
            elif key == "topic":
                if not str(value).strip():
                    raise WorkspaceError("슬롯 주제가 비어 있어요")
                values["topic"] = str(value).strip()
            else:
                values[key] = str(value).strip()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM slots WHERE id = ?", (slot_id,)).fetchone() is None:
                raise NotFoundError(f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
            if values:
                values["updated_at"] = utc_now()
                assignments = ", ".join(f"{key} = ?" for key in values)
                conn.execute(f"UPDATE slots SET {assignments} WHERE id = ?", (*values.values(), slot_id))
            return self._slot(conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone())

    def _slot_run_busy(self, conn: sqlite3.Connection, slot: sqlite3.Row, run_id: str, now: datetime) -> str:
        """The id of another run that is working on this slot right now ("" if none).

        On the way, a ``running`` run whose owner is gone is taken over (marked
        ``interrupted``) and a slot left ``generating`` by a run that is gone
        (or ended/never started ``ORPHAN_SLOT_SECONDS`` ago) is released like
        ``recover_stale`` does; the caller must read the slot again.
        """
        other_id = slot["run_id"] or ""
        if other_id and other_id == run_id:
            return ""
        stamp = _fmt(now)
        other = conn.execute("SELECT * FROM runs WHERE id = ?", (other_id,)).fetchone() if other_id else None
        if other is not None and other["status"] == "running":
            if self._owner_alive(other, now, STALE_AFTER_SECONDS, trust_own_pid=True):
                return other_id
            self._close_interrupted(conn, other, stamp)
            if slot["status"] == "generating":
                self._release_slot(conn, slot, stamp)
            return ""
        if slot["status"] == "generating":
            updated = _parse_ts(slot["updated_at"])
            if not other_id or updated is None or (now - updated).total_seconds() > ORPHAN_SLOT_SECONDS:
                self._release_slot(conn, slot, stamp)
        return ""

    def claim_slot(self, slot_id: str, run_id: str, *, force: bool = False) -> CalendarSlot:
        """Atomically mark a slot ``generating`` for ``run_id``.

        Refuses a slot already generating or drafted unless ``force``, and
        always refuses a slot another live run is working on (``force`` is for
        regenerating a draft, not for two generations at once). A slot left
        ``generating`` by a run whose process is gone is recovered first, so it
        can be generated again without ``force``.
        """
        now = datetime.now(timezone.utc)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
            busy = self._slot_run_busy(conn, row, run_id, now)
            if busy:
                raise WorkspaceError(f"이 슬롯은 지금 다른 실행({busy})이 초안을 만드는 중이에요. 그 실행이 끝난 뒤 다시 시도해 주세요.")
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row["status"] == "generating" and not force:
                raise WorkspaceError("이 슬롯은 이미 초안을 만드는 중이에요")
            if row["status"] == "drafted" and row["item_id"] and not force:
                raise WorkspaceError(f"이미 초안이 있어요 (보관함 {row['item_id']}). 다시 만들려면 force로 요청해 주세요.")
            if row["status"] == "skipped" and not force:
                raise WorkspaceError("건너뛰기로 표시한 슬롯이에요. 먼저 '계획'으로 되돌리거나 force로 요청해 주세요.")
            conn.execute("UPDATE slots SET status = 'generating', run_id = ?, updated_at = ? WHERE id = ?",
                         (run_id, _fmt(now), slot_id))
            return self._slot(conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone())

    def refresh_slot(self, slot_id: str) -> CalendarSlot | None:
        """Read a slot, first recovering it when the run generating it is gone (its process died → the run is
        marked ``interrupted`` and the slot released; a slot orphaned by an ended run → released).

        For request handlers that check ``status == 'generating'`` before starting a job.
        """
        now = datetime.now(timezone.utc)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "generating":
                self._slot_run_busy(conn, row, "", now)
                row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            return self._slot(row)

    def reclaim_slot(self, slot_id: str, run_id: str) -> CalendarSlot | None:
        """For resuming a calendar-slot run: mark its slot ``generating`` for ``run_id`` again.

        Returns the slot as it was (to restore it when the resume fails), or
        ``None`` when the slot is left alone: it no longer exists, or a draft
        made after this run started already fills it (the resumed run then
        finishes its own item without taking the slot back). Raises
        ``WorkspaceError`` when another live run is generating the slot.
        """
        now = datetime.now(timezone.utc)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                return None
            busy = self._slot_run_busy(conn, row, run_id, now)
            if busy:
                raise WorkspaceError(f"이 슬롯은 지금 다른 실행({busy})이 초안을 만드는 중이에요. 그 실행이 끝난 뒤 보관함을 확인해 주세요.")
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row["status"] == "drafted" and row["item_id"] and (row["run_id"] or "") != run_id:
                # ">=": timestamps have millisecond precision; a draft made in the same millisecond as this run
                # counts as newer (leaving the slot alone never unlinks someone else's draft)
                newer = conn.execute("SELECT 1 FROM items i JOIN runs mine ON mine.id = ? WHERE i.id = ? AND i.created_at >= mine.created_at",
                                     (run_id, row["item_id"])).fetchone()
                if newer is not None:
                    return None
            before = self._slot(row)
            conn.execute("UPDATE slots SET status = 'generating', run_id = ?, updated_at = ? WHERE id = ?",
                         (run_id, _fmt(now), slot_id))
            return before

    @staticmethod
    def _still_claims(conn: sqlite3.Connection, slot: sqlite3.Row, run_id: str, owner_token: str | None) -> bool:
        """Whether ``slot`` is still ``run_id``'s claim and, with ``owner_token``, the run still that process's."""
        if not run_id or (slot["run_id"] or "") != run_id:
            return False
        if owner_token is not None:
            run = conn.execute("SELECT owner_token FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is not None and run["owner_token"] != owner_token:
                return False
        return True

    def release_slot(self, slot_id: str, run_id: str, *, status: str = "planned", item_id: str | None = None,
                     set_run_id: str | None = None, owner_token: str | None = None) -> CalendarSlot | None:
        """Hand back a slot ``run_id`` was generating, after that run failed or stopped.

        ``status`` is ``planned`` (so it can be generated again) or the state
        it had before a regeneration or resume (``drafted`` with ``item_id``,
        ``skipped``); ``set_run_id`` restores the slot's earlier run id. It
        only happens while the slot is still that run's claim (``generating``
        for ``run_id``) and, with ``owner_token`` (the lease token of the
        process that claimed it), while the run still belongs to that process.
        When another process took the run over (``recover_stale``, a forced
        resume) or another run claimed the slot meanwhile, the slot is left to
        them: returns None. Otherwise returns the updated slot.
        """
        if status not in SLOT_STATUSES or status == "generating":
            raise WorkspaceError(f"슬롯을 돌려놓을 상태가 올바르지 않아요: {status!r}")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None or row["status"] != "generating" or not self._still_claims(conn, row, run_id, owner_token):
                return None
            values: dict[str, Any] = {"status": status, "updated_at": utc_now()}
            if item_id is not None:
                values["item_id"] = item_id
            if set_run_id is not None:
                values["run_id"] = set_run_id
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(f"UPDATE slots SET {assignments} WHERE id = ?", (*values.values(), slot_id))
            return self._slot(conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone())

    def link_slot(self, slot_id: str, run_id: str, item_id: str, *, owner_token: str | None = None) -> CalendarSlot | None:
        """After ``run_id`` finished a slot's draft: mark the slot ``drafted`` with ``item_id`` and give the item the
        slot date as ``scheduled_at`` (when it has none).

        Only while the slot is still that run's (same checks as
        ``release_slot``; a slot the user skipped meanwhile stays skipped).
        Returns the slot, unchanged when it was left alone, or None when it no
        longer exists.
        """
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                return None
            if row["status"] != "skipped" and self._still_claims(conn, row, run_id, owner_token) and \
                    conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is not None:
                now = utc_now()
                conn.execute("UPDATE items SET scheduled_at = ?, updated_at = ? WHERE id = ? AND scheduled_at = ''",
                             (row["date"], now, item_id))
                conn.execute("UPDATE slots SET status = 'drafted', item_id = ?, updated_at = ? WHERE id = ?",
                             (item_id, now, slot_id))
                row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            return self._slot(row)

    def due_slots(self, until_date: str) -> list[CalendarSlot]:
        """Planned slots on or before ``until_date`` (for ``insia run-due``)."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM slots WHERE status = 'planned' AND date <= ?",
                                (_check_date(until_date, "기준일"),)).fetchall()
        return self._sorted_slots([self._slot(row) for row in rows])

    # -- API publishing (LinkedIn / Instagram): connections, previews, attempts ----------------------------------
    # The workspace keeps records only; tokens and app secrets live in credentials/secrets.sqlite
    # (insia_agents.publishers.store). The publishing package is the only caller of these methods.

    @staticmethod
    def _now_dt(now: datetime | None) -> datetime:
        return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    @staticmethod
    def _publish_connection(row: sqlite3.Row) -> PublishConnection:
        return PublishConnection(
            platform=row["platform"], account_id=row["account_id"], account_name=row["account_name"],
            scopes=[s for s in (row["scopes"] or "").split() if s], status=row["status"],
            status_reason=row["status_reason"], token_expires_at=row["token_expires_at"],
            expires_estimated=bool(row["expires_estimated"]), token_issued_at=row["token_issued_at"],
            token_refreshed_at=row["token_refreshed_at"], api_version=row["api_version"],
            connected_at=row["connected_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _publish_preview(row: sqlite3.Row) -> PublishPreview:
        return PublishPreview(
            id=row["id"], item_id=row["item_id"], version=int(row["version"]), platform=row["platform"],
            account_id=row["account_id"], payload=_loads(row["payload"], {}) or {}, payload_hash=row["payload_hash"],
            created_via=row["created_via"], requested_by=row["requested_by"], created_at=row["created_at"],
            expires_at=row["expires_at"], used_at=row["used_at"],
        )

    @staticmethod
    def _publish_attempt(row: sqlite3.Row) -> PublishAttempt:
        return PublishAttempt(
            id=row["id"], item_id=row["item_id"], version=int(row["version"]), platform=row["platform"],
            preview_id=row["preview_id"], payload_hash=row["payload_hash"], account_id=row["account_id"],
            status=row["status"], step=row["step"], external_id=row["external_id"], permalink=row["permalink"],
            state=_loads(row["state"], {}) or {}, error_code=row["error_code"], error=row["error"],
            requested_by=row["requested_by"], resolved_by=row["resolved_by"], created_at=row["created_at"],
            updated_at=row["updated_at"], finished_at=row["finished_at"],
        )

    @staticmethod
    def _check_platform(platform: str) -> str:
        if platform not in PUBLISH_PLATFORMS:
            raise WorkspaceError(f"API 게시 플랫폼은 linkedin 또는 instagram이어야 해요 (받은 값: {platform!r})")
        return platform

    @staticmethod
    def _merged_state(current: str | None, patch: Mapping[str, Any] | None) -> str:
        state = _loads(current, {}) or {}
        if patch:
            bad = sorted(k for k in patch if str(k).lower() in _STATE_FORBIDDEN_KEYS)
            if bad:
                raise WorkspaceError(f"게시 시도 기록에 넣을 수 없는 값이에요: {', '.join(bad)}")
            state.update(dict(patch))
        return _dumps(state)

    # connections (display copy; the authoritative account id sits next to the token in secrets.sqlite)
    def get_publish_connection(self, platform: str) -> PublishConnection | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM publish_connections WHERE platform = ?", (platform,)).fetchone()
        return self._publish_connection(row) if row else None

    def list_publish_connections(self) -> list[PublishConnection]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM publish_connections ORDER BY platform").fetchall()
        return [self._publish_connection(row) for row in rows]

    def save_publish_connection(self, connection: PublishConnection | Mapping[str, Any]) -> PublishConnection:
        """Insert or replace the connection row of ``connection.platform`` (``connected_at`` kept when it existed)."""
        value = _as_model(PublishConnection, connection, "게시 연결 정보")
        self._check_platform(value.platform)
        if value.status not in PUBLISH_CONNECTION_STATUSES:
            raise WorkspaceError(f"연결 상태가 올바르지 않아요: {value.status!r}")
        now = utc_now()
        with self._tx() as conn:
            old = conn.execute("SELECT connected_at FROM publish_connections WHERE platform = ?", (value.platform,)).fetchone()
            connected_at = value.connected_at or (old["connected_at"] if old else "") or now
            conn.execute(
                "INSERT INTO publish_connections (platform, account_id, account_name, scopes, status, status_reason, "
                "token_expires_at, expires_estimated, token_issued_at, token_refreshed_at, api_version, connected_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (platform) DO UPDATE SET "
                "account_id = excluded.account_id, account_name = excluded.account_name, scopes = excluded.scopes, "
                "status = excluded.status, status_reason = excluded.status_reason, token_expires_at = excluded.token_expires_at, "
                "expires_estimated = excluded.expires_estimated, token_issued_at = excluded.token_issued_at, "
                "token_refreshed_at = excluded.token_refreshed_at, api_version = excluded.api_version, "
                "connected_at = excluded.connected_at, updated_at = excluded.updated_at",
                (value.platform, value.account_id, value.account_name, " ".join(value.scopes), value.status,
                 value.status_reason, value.token_expires_at, 1 if value.expires_estimated else 0, value.token_issued_at,
                 value.token_refreshed_at, value.api_version, connected_at, now))
            row = conn.execute("SELECT * FROM publish_connections WHERE platform = ?", (value.platform,)).fetchone()
        return self._publish_connection(row)

    def set_publish_connection_status(self, platform: str, status: str, reason: str = "") -> None:
        if status not in PUBLISH_CONNECTION_STATUSES:
            raise WorkspaceError(f"연결 상태가 올바르지 않아요: {status!r}")
        with self._tx() as conn:
            conn.execute("UPDATE publish_connections SET status = ?, status_reason = ?, updated_at = ? WHERE platform = ?",
                         (status, (reason or "")[:500], utc_now(), platform))

    def delete_publish_connection(self, platform: str) -> bool:
        """Remove the connection row (the service first refuses while an attempt is ``sending``/``unknown``)."""
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM publish_connections WHERE platform = ?", (platform,))
        return cursor.rowcount > 0

    # previews
    def create_publish_preview(self, item_id: str, version: int, platform: str, account_id: str, payload: Mapping[str, Any],
                               payload_hash: str, *, created_via: str, confirm_code_hash: str = "", requested_by: str = "",
                               ttl_seconds: float = 1800, now: datetime | None = None) -> PublishPreview:
        """Store exactly what would be sent (``payload``, canonical JSON, never a token) and its hash for 30 minutes.

        ``created_via`` (``dashboard``/``cli``): a preview can only be sent from where it was made. A CLI send preview
        stores only the sha256 of its random confirm code (``confirm_code_hash``); the code itself is never stored.
        """
        self._check_platform(platform)
        if created_via not in PUBLISH_PREVIEW_VIA:
            raise WorkspaceError(f"미리보기를 만든 곳은 dashboard 또는 cli여야 해요 (받은 값: {created_via!r})")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", payload_hash or ""):
            raise WorkspaceError("미리보기 해시 형식이 올바르지 않아요")
        if confirm_code_hash and not re.fullmatch(r"[0-9a-f]{64}", confirm_code_hash):
            raise WorkspaceError("확인 코드 해시 형식이 올바르지 않아요")
        now_dt = self._now_dt(now)
        preview_id = f"pv_{secrets.token_hex(12)}"
        text = json.dumps(to_jsonable(dict(payload)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        created, expires = _fmt(now_dt), _fmt(now_dt + timedelta(seconds=float(ttl_seconds)))
        with self._tx() as conn:
            self._item_row(conn, item_id)
            conn.execute(
                "INSERT INTO publish_previews (id, item_id, version, platform, account_id, payload, payload_hash, created_via, "
                "confirm_code_hash, requested_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (preview_id, item_id, int(version), platform, account_id or "", text, payload_hash, created_via,
                 confirm_code_hash or "", (requested_by or "")[:200], created, expires))
            row = conn.execute("SELECT * FROM publish_previews WHERE id = ?", (preview_id,)).fetchone()
        return self._publish_preview(row)

    def get_publish_preview(self, preview_id: str) -> PublishPreview | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM publish_previews WHERE id = ?", (str(preview_id or ""),)).fetchone()
        return self._publish_preview(row) if row else None

    def check_confirm_code(self, preview_id: str, code: str) -> bool:
        """Whether ``code`` (what the person typed, trimmed and upper-cased) matches the preview's stored hash.
        False when the preview has no code (a dashboard preview or ``insia publish preview``)."""
        with self._read() as conn:
            row = conn.execute("SELECT confirm_code_hash FROM publish_previews WHERE id = ?", (str(preview_id or ""),)).fetchone()
        stored = row["confirm_code_hash"] if row else ""
        if not stored:
            return False
        typed = hashlib.sha256((code or "").strip().upper().encode("utf-8")).hexdigest()
        return hmac.compare_digest(typed, stored)

    def mark_publish_preview_used(self, preview_id: str, *, now: datetime | None = None) -> bool:
        """Burn a preview without sending (e.g. a wrong CLI confirm code): it can never be sent afterwards."""
        with self._tx() as conn:
            cursor = conn.execute("UPDATE publish_previews SET used_at = ? WHERE id = ? AND used_at = ''",
                                  (_fmt(self._now_dt(now)), str(preview_id or "")))
        return cursor.rowcount == 1

    def purge_publish_previews(self, now: str | datetime | None = None) -> list[str]:
        """Ids of previews that are expired or used (their staged images can go), oldest first. Unused previews that
        expired more than a day ago are deleted; used ones stay as the record of what a person confirmed."""
        now_dt = now if isinstance(now, datetime) else (_parse_ts(now or "") or datetime.now(timezone.utc))
        stamp, day_before = _fmt(now_dt), _fmt(now_dt - timedelta(days=1))
        with self._tx() as conn:
            rows = conn.execute("SELECT id FROM publish_previews WHERE used_at <> '' OR expires_at <= ? ORDER BY created_at",
                                (stamp,)).fetchall()
            conn.execute("DELETE FROM publish_previews WHERE used_at = '' AND expires_at <= ?", (day_before,))
        return [row["id"] for row in rows]

    # attempts — ownership
    def _register_publish_worker(self, attempt_id: str, token: str) -> None:
        with _PUBLISH_WORKERS_LOCK:
            _PUBLISH_WORKERS[(self._key, attempt_id)] = token

    def release_publish_worker(self, attempt_id: str) -> None:
        """Forget this process's worker for ``attempt_id`` (the worker ended; safe to call more than once)."""
        with _PUBLISH_WORKERS_LOCK:
            _PUBLISH_WORKERS.pop((self._key, attempt_id), None)

    def holds_publish_attempt(self, attempt_id: str) -> bool:
        """Whether a worker of this process holds the attempt right now."""
        with _PUBLISH_WORKERS_LOCK:
            return (self._key, attempt_id) in _PUBLISH_WORKERS

    def _live_attempt_error(self, conn: sqlite3.Connection, item_id: str, version: int, platform: str) -> Exception | None:
        row = conn.execute("SELECT id, status, permalink FROM publish_attempts WHERE item_id = ? AND version = ? AND platform = ? "
                           "AND status IN ('sending', 'published', 'unknown') ORDER BY created_at DESC LIMIT 1",
                           (item_id, int(version), platform)).fetchone()
        if row is None:
            return None
        if row["status"] == "published":
            return PublishStateError("이미 게시한 버전이에요.", reason="already_published", attempt_id=row["id"],
                                     permalink=row["permalink"])
        return ItemLockedError(attempt_id=row["id"], platform=platform, status=row["status"])

    @staticmethod
    def _blocked_by(item: sqlite3.Row, version: int | None = None) -> str:
        """Why an item cannot be published through the API right now ("" when it can): not_approved / version_changed /
        published / archived. ``version``: the version a preview was made of (must still be the approved latest)."""
        status = item["status"]
        if status == "archived":
            return "archived"
        if status == "published":
            return "published"
        if status not in ("approved", "scheduled"):
            return "not_approved"
        current = max(1, int(item["version"] or 0))
        if int(item["approved_version"] or 0) != current or (version is not None and int(version) != current):
            return "version_changed"
        return ""

    def begin_publish_attempt(self, preview_id: str, preview_hash: str, *, via: str, requested_by: str,
                              state: Mapping[str, Any] | None = None, now: datetime | None = None) -> tuple[PublishAttempt, str]:
        """Start a confirmed publish, all in one transaction; returns ``(attempt, owner_token)``.

        Checks: the preview exists, is unused and unexpired, was made by ``via`` and its hash equals
        ``preview_hash``; the item has no live attempt, is ``approved``/``scheduled`` and its approved version is its
        latest version and the preview's; that version was not published through the API already; the platform's
        connection is ``connected`` with the preview's account. Then a ``sending`` attempt owned by this process
        (fresh ``owner_token``, heartbeat now) is inserted, the preview is marked used and the worker is registered.
        The partial unique index turns a concurrent second attempt into ``ItemLockedError`` / ``already_published``.
        ``owner_token`` must stay in the worker's memory. Failures raise ``PublishStateError`` (``reason``) or
        ``ItemLockedError``; nothing is sent then.
        """
        now_dt = self._now_dt(now)
        stamp = _fmt(now_dt)
        registered = False
        attempt_id = ""
        try:
            with self._tx() as conn:
                pv = conn.execute("SELECT * FROM publish_previews WHERE id = ?", (str(preview_id or ""),)).fetchone()
                if pv is None:
                    raise PublishStateError("미리보기를 찾을 수 없어요. 다시 확인해 주세요.", reason="preview_missing")
                if pv["used_at"]:
                    raise PublishStateError("이미 사용한 미리보기예요. 다시 확인해 주세요.", reason="preview_used")
                expires = _parse_ts(pv["expires_at"])
                if expires is None or expires <= now_dt:
                    raise PublishStateError("미리보기가 30분이 지나 만료됐어요. 다시 확인해 주세요.", reason="preview_expired")
                if pv["created_via"] != via:
                    raise PublishStateError("다른 곳에서 만든 미리보기라 여기서 보낼 수 없어요. 다시 확인해 주세요.", reason="preview_via")
                if not hmac.compare_digest(str(pv["payload_hash"]), str(preview_hash or "")):
                    raise PublishStateError("확인한 뒤에 내용이 바뀌었어요. 다시 확인해 주세요.", reason="hash_mismatch")
                item_id, version, platform = pv["item_id"], int(pv["version"]), pv["platform"]
                item = self._item_row(conn, item_id)
                self._check_not_publishing(conn, item_id)
                blocked = self._blocked_by(item, version)
                if blocked:
                    raise PublishStateError("승인한 최신 버전만 API로 게시할 수 있어요. 다시 확인해 주세요.", reason="not_publishable",
                                            blocked_by=blocked)
                live = self._live_attempt_error(conn, item_id, version, platform)
                if live is not None:
                    raise live
                connection = conn.execute("SELECT status, account_id FROM publish_connections WHERE platform = ?", (platform,)).fetchone()
                if connection is None or connection["status"] != "connected":
                    raise PublishStateError("계정 연결이 끝났거나 해제됐어요. 다시 연결해 주세요.", reason="not_connected")
                if connection["account_id"] != pv["account_id"]:
                    raise PublishStateError("확인한 뒤에 연결된 계정이 바뀌었어요. 다시 확인해 주세요.", reason="account_changed")
                attempt_id = f"pa_{secrets.token_hex(12)}"
                token = secrets.token_hex(16)
                try:
                    conn.execute(
                        "INSERT INTO publish_attempts (id, item_id, version, platform, preview_id, payload_hash, account_id, status, "
                        "state, requested_by, owner_pid, owner_host, owner_boot, owner_token, heartbeat_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (attempt_id, item_id, version, platform, pv["id"], pv["payload_hash"], pv["account_id"],
                         self._merged_state("{}", state), (requested_by or "")[:200], os.getpid(), this_host(), boot_marker(),
                         token, stamp, stamp, stamp))
                except sqlite3.IntegrityError:
                    live = self._live_attempt_error(conn, item_id, version, platform)
                    raise live if live is not None else ItemLockedError(platform=platform) from None
                cursor = conn.execute("UPDATE publish_previews SET used_at = ? WHERE id = ? AND used_at = ''", (stamp, pv["id"]))
                if cursor.rowcount != 1:
                    raise PublishStateError("이미 사용한 미리보기예요. 다시 확인해 주세요.", reason="preview_used")
                row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
                # registered before the commit (still under the workspace lock): a recovery in another thread of this
                # process can never see the new row without its worker
                self._register_publish_worker(attempt_id, token)
                registered = True
        except BaseException:
            if registered:
                self.release_publish_worker(attempt_id)
            raise
        return self._publish_attempt(row), token

    def heartbeat_publish_attempt(self, attempt_id: str, owner_token: str, *, now: datetime | None = None) -> bool:
        """The worker's heartbeat; False when the attempt is no longer this worker's (recovered, finished)."""
        if not owner_token:
            return False
        with self._tx() as conn:
            cursor = conn.execute("UPDATE publish_attempts SET heartbeat_at = ? WHERE id = ? AND status = 'sending' AND owner_token = ?",
                                  (_fmt(self._now_dt(now)), attempt_id, owner_token))
        return cursor.rowcount == 1

    def update_publish_attempt(self, attempt_id: str, owner_token: str, *, step: str | None = None,
                               state_patch: Mapping[str, Any] | None = None, media_token: str | None = None,
                               now: datetime | None = None) -> None:
        """Record progress of a ``sending`` attempt this worker owns (step, merged state, media folder token).
        ``AttemptTakenOverError`` when ownership was lost: the worker must stop without sending."""
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM publish_attempts WHERE id = ? AND status = 'sending' AND owner_token = ? "
                               "AND owner_token <> ''", (attempt_id, owner_token or "")).fetchone()
            if row is None:
                raise AttemptTakenOverError(attempt_id=attempt_id)
            values: dict[str, Any] = {"heartbeat_at": stamp, "updated_at": stamp}
            if step is not None:
                values["step"] = str(step)[:60]
            if state_patch:
                values["state"] = self._merged_state(row["state"], state_patch)
            if media_token is not None:
                if media_token and not re.fullmatch(r"[0-9a-f]{32}", media_token):
                    raise WorkspaceError("미디어 폴더 이름 형식이 올바르지 않아요")
                values["media_token"] = media_token
            assignments = ", ".join(f"{key} = ?" for key in values)
            cursor = conn.execute(f"UPDATE publish_attempts SET {assignments} WHERE id = ? AND status = 'sending' AND owner_token = ?",
                                  (*values.values(), attempt_id, owner_token))
            if cursor.rowcount != 1:
                raise AttemptTakenOverError(attempt_id=attempt_id)

    def claim_publish_write(self, attempt_id: str, owner_token: str, *, step: str = "write", now: datetime | None = None) -> None:
        """Right before the irreversible call (LinkedIn ``POST /rest/posts``, Instagram ``media_publish``): a conditional
        UPDATE that only succeeds while this worker still owns the ``sending`` attempt. ``AttemptTakenOverError``
        otherwise — the caller must not send. After this, an interruption means the outcome is unknown."""
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            cursor = conn.execute("UPDATE publish_attempts SET step = ?, heartbeat_at = ?, updated_at = ? "
                                  "WHERE id = ? AND status = 'sending' AND owner_token = ? AND owner_token <> ''",
                                  (str(step)[:60], stamp, stamp, attempt_id, owner_token or ""))
        if cursor.rowcount != 1:
            raise AttemptTakenOverError(attempt_id=attempt_id)

    # attempts — outcomes
    def _mark_item_published_via_api(self, conn: sqlite3.Connection, item_id: str, version: int, *, via: str, url: str,
                                     external_id: str, stamp: str) -> str:
        """Content side of a confirmed API publish (the attempt is already recorded as published). Deliberately not
        guarded by ``_check_not_publishing``: the only live attempt is this one. Returns "" or the Korean reason why
        the item could not be moved (then it is left as it is).

        The item is approved/scheduled at this very version, so any ``published_at`` / ``published_url`` it still
        holds belong to an earlier post (보관 → 복원 → 고친 뒤 다시 게시): the publish fields are all this post's —
        its time, its permalink (``''`` when the platform gave none, so the address a person fills in later lands
        on the item) and its id."""
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            return PUBLISH_ITEM_UPDATE_ERROR
        if item["status"] not in ("approved", "scheduled") or int(item["approved_version"] or 0) != int(version) \
                or max(1, int(item["version"] or 0)) != int(version):
            return PUBLISH_ITEM_UPDATE_ERROR
        link = url if url and _URL.match(url) else ""
        conn.execute(
            "UPDATE items SET status = 'published', published_at = ?, published_url = ?, published_via = ?, "
            "published_external_id = ?, updated_at = ? WHERE id = ?", (stamp, link, via, external_id or "", stamp, item_id))
        return ""

    def _record_item_update(self, attempt_id: str, item_id: str, version: int, *, via: str, url: str, external_id: str,
                            stamp: str) -> ContentItem | None:
        """Second transaction after an attempt became ``published``: move the item. A failure never undoes the
        attempt; it is noted in ``attempt.state.item_update_error``."""
        try:
            with self._tx() as conn:
                reason = self._mark_item_published_via_api(conn, item_id, version, via=via, url=url,
                                                           external_id=external_id, stamp=stamp)
                if not reason:
                    return self._item(self._item_row(conn, item_id))
        except Exception:  # noqa: BLE001 - the success record must never be lost over the item update
            log.warning("게시 시도 %s의 콘텐츠 상태를 바꾸지 못했어요", attempt_id, exc_info=True)
            reason = PUBLISH_ITEM_UPDATE_ERROR
        try:
            with self._tx() as conn:
                row = conn.execute("SELECT state FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
                if row is not None:  # item_via: what 게시 완료 표시 records later (set_item_status)
                    conn.execute("UPDATE publish_attempts SET state = ?, updated_at = ? WHERE id = ?",
                                 (self._merged_state(row["state"], {"item_update_error": reason, "item_via": via}),
                                  stamp, attempt_id))
        except Exception:  # noqa: BLE001
            log.warning("게시 시도 %s에 메모를 남기지 못했어요", attempt_id, exc_info=True)
        return None

    def finish_publish_success(self, attempt_id: str, owner_token: str, *, external_id: str = "", permalink: str = "",
                               via: str, state_patch: Mapping[str, Any] | None = None,
                               now: datetime | None = None) -> tuple[PublishAttempt, ContentItem | None]:
        """The platform confirmed the post. ① The attempt becomes ``published`` and is committed right away (so the
        fact and its permalink are never lost; the unique index keeps blocking a second post of this version).
        ② The item becomes ``published`` with this post's fields (``published_at`` = now, ``published_url`` = the
        permalink or ``''``, ``published_via`` = ``via``, ``published_external_id``) — if that is impossible the item stays as it is and
        ``attempt.state.item_update_error`` says so; no exception. ``AttemptTakenOverError`` when the worker no
        longer owns the attempt (then the recovery's record stands)."""
        if via not in PUBLISHED_VIA_VALUES or not via:
            raise WorkspaceError(f"published_via 값이 올바르지 않아요: {via!r}")
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ? AND status = 'sending' AND owner_token = ? "
                               "AND owner_token <> ''", (attempt_id, owner_token or "")).fetchone()
            if row is None:
                raise AttemptTakenOverError(attempt_id=attempt_id)
            conn.execute("UPDATE publish_attempts SET status = 'published', external_id = ?, permalink = ?, state = ?, "
                         "error_code = '', error = '', owner_token = '', heartbeat_at = ?, updated_at = ?, finished_at = ? "
                         "WHERE id = ?", (external_id or "", permalink or "", self._merged_state(row["state"], state_patch),
                                          stamp, stamp, stamp, attempt_id))
        self.release_publish_worker(attempt_id)
        item = self._record_item_update(attempt_id, row["item_id"], int(row["version"]), via=via, url=permalink or "",
                                        external_id=external_id or "", stamp=stamp)
        attempt = self.get_publish_attempt(attempt_id)
        assert attempt is not None
        return attempt, item

    def finish_publish_failure(self, attempt_id: str, owner_token: str, *, status: str, error_code: str = "", error: str = "",
                               state_patch: Mapping[str, Any] | None = None, now: datetime | None = None) -> PublishAttempt:
        """Close a ``sending`` attempt this worker owns as ``failed`` (nothing was published) or ``unknown`` (the
        irreversible call went out without an answer: the item stays locked until a person resolves it).
        ``AttemptTakenOverError`` when ownership was lost."""
        if status not in ("failed", "unknown"):
            raise WorkspaceError(f"게시 실패 상태는 failed 또는 unknown이어야 해요 (받은 값: {status!r})")
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM publish_attempts WHERE id = ? AND status = 'sending' AND owner_token = ? "
                               "AND owner_token <> ''", (attempt_id, owner_token or "")).fetchone()
            if row is None:
                raise AttemptTakenOverError(attempt_id=attempt_id)
            conn.execute("UPDATE publish_attempts SET status = ?, error_code = ?, error = ?, state = ?, owner_token = '', "
                         "heartbeat_at = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                         (status, (error_code or "")[:100], (error or "")[:1000], self._merged_state(row["state"], state_patch),
                          stamp, stamp, stamp if status == "failed" else "", attempt_id))
        self.release_publish_worker(attempt_id)
        attempt = self.get_publish_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def set_publish_permalink(self, attempt_id: str, url: str, *, by: str, external_id: str = "",
                              now: datetime | None = None) -> PublishAttempt:
        """A person fills in the post address of a ``published`` attempt that has none; the item's ``published_url``
        follows while the item still shows this attempt's post (``_ITEM_STILL_SHOWS_POST``: no newer version posted
        through the API since) and has no address of its own (``_ITEM_URL_UNSET``: an older post's address counts as
        none). ``external_id`` (the media id of the Instagram candidate the person picked) fills an empty external id
        of the attempt and of that item. The caller checks the platform's hosts and the candidate."""
        url = (url or "").strip()
        external_id = (external_id or "").strip()[:300]
        if not _URL.match(url) or not url.lower().startswith("https://") or len(url) > 2000:
            raise WorkspaceError("게시물 주소는 https://로 시작하는 전체 주소여야 해요")
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"게시 시도 {attempt_id}를 찾을 수 없어요")
            if row["status"] != "published" or row["permalink"]:
                raise PublishStateError("게시가 끝났고 주소가 비어 있는 기록에만 주소를 넣을 수 있어요.", reason="attempt_state")
            conn.execute("UPDATE publish_attempts SET permalink = ?, external_id = CASE WHEN external_id = '' THEN ? "
                         "ELSE external_id END, state = ?, updated_at = ? WHERE id = ?",
                         (url, external_id, self._merged_state(row["state"], {"permalink_by": (by or "")[:200]}), stamp,
                          attempt_id))
            conn.execute("UPDATE items SET published_url = ?, published_external_id = CASE WHEN published_external_id = '' "
                         f"THEN ? ELSE published_external_id END, updated_at = ? WHERE id = ? AND {_ITEM_STILL_SHOWS_POST} "
                         f"AND {_ITEM_URL_UNSET}", (url, external_id, stamp, row["item_id"], int(row["version"]), attempt_id))
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._publish_attempt(row)

    def resolve_publish_attempt(self, attempt_id: str, outcome: str, *, url: str = "", resolved_by: str, via: str = "",
                                now: datetime | None = None) -> tuple[PublishAttempt, ContentItem | None]:
        """A person closes an ``unknown`` attempt: ``published`` (it did go up; optional checked ``url``; the item
        follows like ``finish_publish_success`` ②, ``published_via`` = ``via``) or ``not_published`` →
        ``abandoned`` (item untouched). ``PublishStateError(reason="attempt_state")`` unless the attempt is
        ``unknown``."""
        if outcome not in ("published", "not_published"):
            raise WorkspaceError("결과는 published 또는 not_published여야 해요")
        url = (url or "").strip()
        if url and (not _URL.match(url) or not url.lower().startswith("https://")):
            raise WorkspaceError("게시물 주소는 https://로 시작하는 전체 주소여야 해요")
        if outcome == "published" and (via not in PUBLISHED_VIA_VALUES or not via):
            raise WorkspaceError(f"published_via 값이 올바르지 않아요: {via!r}")
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"게시 시도 {attempt_id}를 찾을 수 없어요")
            if row["status"] != "unknown":
                raise PublishStateError("결과 확인이 필요한 기록만 정리할 수 있어요.", reason="attempt_state")
            status = "published" if outcome == "published" else "abandoned"
            conn.execute("UPDATE publish_attempts SET status = ?, permalink = CASE WHEN ? <> '' THEN ? ELSE permalink END, "
                         "resolved_by = ?, owner_token = '', updated_at = ?, finished_at = ? WHERE id = ?",
                         (status, url, url, (resolved_by or "")[:200], stamp, stamp, attempt_id))
        item = None
        if outcome == "published":
            item = self._record_item_update(attempt_id, row["item_id"], int(row["version"]), via=via,
                                            url=url or row["permalink"], external_id=row["external_id"], stamp=stamp)
        attempt = self.get_publish_attempt(attempt_id)
        assert attempt is not None
        return attempt, item

    def reconcile_publish_attempt(self, attempt_id: str, *, status: str, external_id: str = "", permalink: str = "",
                                  error_code: str = "", error: str = "", via: str = "", resolved_by: str = "",
                                  state_patch: Mapping[str, Any] | None = None,
                                  now: datetime | None = None) -> tuple[PublishAttempt, ContentItem | None]:
        """Close an ``unknown`` attempt from a read-only platform re-check (Instagram ``status_code``): ``published``
        (the item follows like ``finish_publish_success`` ②) or ``failed``. ``status="unknown"`` only merges
        ``state_patch`` / ``error`` (the check could not decide)."""
        if status not in ("published", "failed", "unknown"):
            raise WorkspaceError("재확인 결과는 published, failed, unknown 중 하나여야 해요")
        if status == "published" and (via not in PUBLISHED_VIA_VALUES or not via):
            raise WorkspaceError(f"published_via 값이 올바르지 않아요: {via!r}")
        stamp = _fmt(self._now_dt(now))
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"게시 시도 {attempt_id}를 찾을 수 없어요")
            if row["status"] != "unknown":
                raise PublishStateError("결과 확인이 필요한 기록만 다시 확인할 수 있어요.", reason="attempt_state")
            state = self._merged_state(row["state"], state_patch)
            if status == "unknown":
                conn.execute("UPDATE publish_attempts SET state = ?, error = CASE WHEN ? <> '' THEN ? ELSE error END, updated_at = ? "
                             "WHERE id = ?", (state, error or "", (error or "")[:1000], stamp, attempt_id))
            else:
                conn.execute("UPDATE publish_attempts SET status = ?, external_id = CASE WHEN ? <> '' THEN ? ELSE external_id END, "
                             "permalink = CASE WHEN ? <> '' THEN ? ELSE permalink END, error_code = ?, error = ?, state = ?, "
                             "resolved_by = ?, owner_token = '', updated_at = ?, finished_at = ? WHERE id = ?",
                             (status, external_id or "", external_id or "", permalink or "", permalink or "",
                              "" if status == "published" else (error_code or "")[:100],
                              "" if status == "published" else (error or "")[:1000], state, (resolved_by or "")[:200],
                              stamp, stamp, attempt_id))
        item = None
        if status == "published":
            item = self._record_item_update(attempt_id, row["item_id"], int(row["version"]), via=via,
                                            url=permalink or row["permalink"], external_id=external_id or row["external_id"],
                                            stamp=stamp)
        attempt = self.get_publish_attempt(attempt_id)
        assert attempt is not None
        return attempt, item

    def record_late_publish_success(self, attempt_id: str, *, external_id: str = "", permalink: str = "", via: str,
                                    state_patch: Mapping[str, Any] | None = None,
                                    now: datetime | None = None) -> tuple[PublishAttempt, ContentItem | None]:
        """The platform confirmed the post after the worker had lost the attempt (recovery closed it, a person
        resolved it, or an Instagram re-check closed it). A confirmed success is never dropped:

        - ``unknown`` / ``failed`` / ``abandoned`` → ``published`` (``resolved_by`` = ``system:late_answer``; the
          item follows like ``finish_publish_success`` ②), which also restores the unique index against a second
          post of this version. If another live attempt of the same version already exists (a new send after
          "안 올라갔어요"), the status stays and ``error`` asks the person to check for a duplicate;
        - ``published`` (a person said "올라갔어요") → a missing external id / permalink is filled in, on the item
          too while it still shows this post and has none (like ``set_publish_permalink``).

        ``state.late_success`` always keeps what arrived (``external_id``, ``permalink``, ``at``, ``status_before``)."""
        if via not in PUBLISHED_VIA_VALUES or not via:
            raise WorkspaceError(f"published_via 값이 올바르지 않아요: {via!r}")
        external_id, permalink = (external_id or "")[:300], (permalink or "")[:2000]
        stamp = _fmt(self._now_dt(now))
        moved = False
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"게시 시도 {attempt_id}를 찾을 수 없어요")
            late = {"late_success": {"external_id": external_id, "permalink": permalink, "at": stamp,
                                     "status_before": row["status"], "resolved_by_before": row["resolved_by"] or ""}}
            state = self._merged_state(self._merged_state(row["state"], state_patch), late)
            if row["status"] in ("unknown", "failed", "abandoned"):
                other = conn.execute("SELECT id FROM publish_attempts WHERE item_id = ? AND version = ? AND platform = ? "
                                     "AND status IN ('sending', 'published', 'unknown') AND id <> ? LIMIT 1",
                                     (row["item_id"], row["version"], row["platform"], attempt_id)).fetchone()
                moved = other is None
            if moved:
                conn.execute("UPDATE publish_attempts SET status = 'published', external_id = ?, "
                             "permalink = CASE WHEN ? <> '' THEN ? ELSE permalink END, error_code = '', error = '', "
                             "state = ?, resolved_by = ?, owner_token = '', updated_at = ?, finished_at = ? WHERE id = ?",
                             (external_id, permalink, permalink, state, PUBLISH_LATE_ANSWER, stamp, stamp, attempt_id))
            elif row["status"] == "published":
                conn.execute("UPDATE publish_attempts SET external_id = CASE WHEN external_id = '' THEN ? ELSE external_id END, "
                             "permalink = CASE WHEN permalink = '' THEN ? ELSE permalink END, state = ?, updated_at = ? "
                             "WHERE id = ?", (external_id, permalink, state, stamp, attempt_id))
                conn.execute("UPDATE items SET published_external_id = CASE WHEN published_external_id = '' THEN ? "
                             f"ELSE published_external_id END, published_url = CASE WHEN ? <> '' AND {_ITEM_URL_UNSET} "
                             f"THEN ? ELSE published_url END, updated_at = ? WHERE id = ? AND {_ITEM_STILL_SHOWS_POST}",
                             (external_id, permalink, attempt_id, permalink, stamp, row["item_id"], int(row["version"])))
            else:  # blocked by another live attempt of this version (or, never expected, still 'sending')
                conn.execute("UPDATE publish_attempts SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                             (state, PUBLISH_LATE_SUCCESS_BLOCKED, stamp, attempt_id))
        item = None
        if moved:
            item = self._record_item_update(attempt_id, row["item_id"], int(row["version"]), via=via,
                                            url=permalink or row["permalink"], external_id=external_id, stamp=stamp)
        attempt = self.get_publish_attempt(attempt_id)
        assert attempt is not None
        return attempt, item

    def get_publish_attempt(self, attempt_id: str) -> PublishAttempt | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE id = ?", (str(attempt_id or ""),)).fetchone()
        return self._publish_attempt(row) if row else None

    def list_publish_attempts(self, *, item_id: str | None = None, status: str | None = None,
                              limit: int = 50) -> list[PublishAttempt]:
        """Attempts, newest first (optionally one item's / one status)."""
        where, params = [], []
        if item_id:
            where.append("item_id = ?")
            params.append(str(item_id))
        if status:
            if status not in PUBLISH_ATTEMPT_STATUSES:
                raise WorkspaceError(f"게시 시도 상태는 {', '.join(PUBLISH_ATTEMPT_STATUSES)} 중 하나여야 해요 (받은 값: {status!r})")
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit or 50), 1000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM publish_attempts {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (*params, limit)).fetchall()
        return [self._publish_attempt(row) for row in rows]

    def active_publish_attempt(self, item_id: str) -> PublishAttempt | None:
        """The item's live attempt (``sending`` or ``unknown``), if any — while it exists the item is locked."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM publish_attempts WHERE item_id = ? AND status IN ('sending', 'unknown') "
                               "ORDER BY created_at DESC LIMIT 1", (str(item_id or ""),)).fetchone()
        return self._publish_attempt(row) if row else None

    def active_publish_attempts(self, platform: str | None = None) -> list[PublishAttempt]:
        """All live attempts (``sending``/``unknown``), optionally of one platform."""
        params: list[Any] = []
        clause = ""
        if platform:
            clause, params = "AND platform = ?", [platform]
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM publish_attempts WHERE status IN ('sending', 'unknown') {clause} "
                                "ORDER BY created_at", params).fetchall()
        return [self._publish_attempt(row) for row in rows]

    def publish_attempt_media_token(self, attempt_id: str) -> str:
        """The public media folder name of an attempt ("" when none) — internal, for cleanup."""
        with self._read() as conn:
            row = conn.execute("SELECT media_token FROM publish_attempts WHERE id = ?", (str(attempt_id or ""),)).fetchone()
        return row["media_token"] if row else ""

    def finished_media_tokens(self, *, older_than: str | datetime | None = None) -> list[str]:
        """Public media folders that can go: attempts that ended (``published``/``failed``/``abandoned``) and, with
        ``older_than``, ``unknown`` attempts created before it (containers live 24 hours)."""
        with self._read() as conn:
            if older_than is None:
                rows = conn.execute("SELECT DISTINCT media_token FROM publish_attempts WHERE media_token <> '' "
                                    "AND status IN ('published', 'failed', 'abandoned')").fetchall()
            else:
                bound = older_than if isinstance(older_than, str) else _fmt(older_than)
                rows = conn.execute("SELECT DISTINCT media_token FROM publish_attempts WHERE media_token <> '' AND "
                                    "(status IN ('published', 'failed', 'abandoned') OR (status = 'unknown' AND created_at < ?))",
                                    (bound,)).fetchall()
        return [row["media_token"] for row in rows]

    # attempts — recovery
    def _attempt_owner_alive(self, row: sqlite3.Row, now: datetime, stale_after: float) -> bool:
        """Whether the worker recorded on a ``sending`` attempt may still be working (the runs' rule, with this
        process's worker table instead of run leases):

        1. a worker of this process holds the attempt's owner token → alive;
        2. no heartbeat (nor any write) for ``stale_after`` seconds → gone; another host/container → only this rule;
        3. same host: a reboot since → gone; our own pid without a registered worker → an earlier process → gone;
           otherwise whether that pid still runs.
        """
        with _PUBLISH_WORKERS_LOCK:
            held = _PUBLISH_WORKERS.get((self._key, row["id"]))
        if held and row["owner_token"] and held == row["owner_token"]:
            return True
        moments = [m for m in (_parse_ts(row["heartbeat_at"]), _parse_ts(row["updated_at"])) if m is not None]
        if not moments or (now - max(moments)).total_seconds() > stale_after:
            return False
        pid = int(row["owner_pid"] or 0)
        host = row["owner_host"] or ""
        if pid <= 0 or not host or host != this_host():
            return True
        if _same_boot(row["owner_boot"] or "", boot_marker()) is False:
            return False
        if pid == os.getpid():
            return False
        return pid_alive(pid)

    def recover_publish_attempts(self, *, stale_after: float = PUBLISH_STALE_AFTER_SECONDS,
                                 now: datetime | None = None) -> list[str]:
        """Close ``sending`` attempts whose worker is gone; returns their ids.

        From the write step on (``step`` ``write`` or ``permalink``) the post may exist → ``unknown`` (the item stays locked until a
        person resolves it); before it nothing was sent → ``failed``. The owner token is cleared in the same
        transaction, so a worker that wakes up later fails its next conditional write and never sends. Safe to run
        from several processes (server every minute, every CLI ``publish`` command)."""
        now_dt = self._now_dt(now)
        stamp = _fmt(now_dt)
        closed: list[str] = []
        with self._tx() as conn:
            for row in conn.execute("SELECT * FROM publish_attempts WHERE status = 'sending'").fetchall():
                if self._attempt_owner_alive(row, now_dt, stale_after):
                    continue
                if row["step"] in PUBLISH_AFTER_WRITE_STEPS:
                    status, error = "unknown", PUBLISH_INTERRUPTED_UNKNOWN.get(row["platform"], PUBLISH_INTERRUPTED_FAILED)
                else:
                    status, error = "failed", PUBLISH_INTERRUPTED_FAILED
                cursor = conn.execute(
                    "UPDATE publish_attempts SET status = ?, error_code = 'interrupted', error = ?, owner_token = '', "
                    "updated_at = ?, finished_at = ? WHERE id = ? AND status = 'sending' AND owner_token = ?",
                    (status, error, stamp, stamp if status == "failed" else "", row["id"], row["owner_token"]))
                if cursor.rowcount == 1:
                    closed.append(row["id"])
        for attempt_id in closed:
            self.release_publish_worker(attempt_id)
        return closed
