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

Schema changes are forward-only: append a new SQL script to ``MIGRATIONS``;
never edit one that has shipped.

All methods raise ``WorkspaceError`` (a ``ValueError``) with a Korean message
fit for the dashboard when the input is invalid; ``NotFoundError`` for unknown
ids, ``InvalidTransitionError`` / ``ApprovalBlockedError`` for status changes.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

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


class ApprovalBlockedError(WorkspaceError):
    """Approval refused because the latest version has not passed review.

    The dashboard offers "그래도 승인" (``force=True``) when it sees this.
    """

    def __init__(self, message: str, *, item_id: str, version: int, score: int | None) -> None:
        super().__init__(message)
        self.item_id = item_id
        self.version = version
        self.score = score


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
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise WorkspaceError(f"워크스페이스 DB를 열 수 없어요: {self.db_path} ({exc})") from None
        self._conn.row_factory = sqlite3.Row
        self._closed = False
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
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE id = ?", (str(doc_id or "").strip(),))
        return cursor.rowcount > 0

    # -- runs --------------------------------------------------------------------
    def create_run(self, run_id: str, brief: Brief, *, kind: str = "pipeline", options: dict | None = None,
                   mode: str = "", model: str = "", profile: Profile | None = None, parent_item_id: str = "") -> None:
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
                "INSERT INTO runs (id, kind, status, brief, options, mode, model, profile, parent_item_id, created_at, updated_at) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, kind, brief.model_dump_json(), _dumps(options or {}), mode or "", model or "",
                 profile.model_dump_json() if profile is not None else None, parent_item_id or "", now, now),
            )

    _RUN_FIELDS = frozenset({"status", "error", "finished_at", "plan", "research", "cost_usd", "mode", "model", "options",
                             "profile", "progress", "parent_item_id"})

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Update run columns: status, error, finished_at, plan (Plan), research (ResearchPack), cost_usd,
        mode, model, options (dict), profile (Profile), progress (dict), parent_item_id.

        A terminal status sets ``finished_at`` (unless given); going back to
        ``running`` (resume) clears ``error`` and ``finished_at``.
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
        with self._tx() as conn:
            cursor = conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*values.values(), run_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요")

    def claim_run(self, run_id: str, expected_status: str) -> bool:
        """Atomically set a run back to ``running`` if its status is still ``expected_status``.

        Two resumes of the same run (a double click) cannot both win.
        """
        now = utc_now()
        with self._tx() as conn:
            cursor = conn.execute("UPDATE runs SET status = 'running', error = '', finished_at = '', updated_at = ? "
                                  "WHERE id = ? AND status = ?", (now, run_id, expected_status))
        return cursor.rowcount == 1

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
        """Run detail as a JSON-serializable dict (brief, options, profile snapshot, plan, research, progress, …)."""
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
        })
        return data

    def list_runs(self, limit: int = 50, *, kind: str | None = None, status: str | None = None) -> list[dict]:
        """Run summaries, newest first (optionally filtered by ``kind`` / ``status``)."""
        where, params = [], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit or 50), 1000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM runs {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
            return [self._run_summary(conn, row) for row in rows]

    # -- events ------------------------------------------------------------------
    def append_event(self, run_id: str, event: dict) -> None:
        """Store one event (the dict emitted by ``EventBus``). A missing ``seq`` gets the next number."""
        if not isinstance(event, dict):
            raise WorkspaceError("이벤트는 객체여야 해요")
        with self._tx() as conn:
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

    def mark_interrupted(self) -> int:
        """On startup: runs still ``running`` belong to a process that died.

        Marks them ``interrupted`` (resumable), closes their event stream with
        a ``run.failed`` event (``data.interrupted = true``) so replays end
        cleanly, and puts calendar slots stuck in ``generating`` back to
        ``planned``. Returns the number of runs marked.
        """
        now = utc_now()
        with self._tx() as conn:
            run_ids = [row["id"] for row in conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()]
            for run_id in run_ids:
                last = conn.execute("SELECT seq, type, t FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
                if last is not None and last["type"] in TERMINAL_TYPES:
                    continue
                seq = (last["seq"] if last else 0) + 1
                t = float(last["t"]) if last else 0.0
                event = {"seq": seq, "t": t, "ts": now, "run_id": run_id, "type": "run.failed",
                         "agent": "system", "data": {"error": INTERRUPTED_MESSAGE, "interrupted": True}}
                conn.execute("INSERT INTO events (run_id, seq, type, agent, t, ts, event) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (run_id, seq, "run.failed", "system", t, event["ts"], _dumps(event)))
            if run_ids:
                conn.execute("UPDATE runs SET status = 'interrupted', error = ?, finished_at = ?, updated_at = ? WHERE status = 'running'",
                             (INTERRUPTED_MESSAGE, now, now))
            conn.execute("UPDATE slots SET status = 'planned', updated_at = ? WHERE status = 'generating'", (now,))
        return len(run_ids)

    # -- content items -------------------------------------------------------------
    @staticmethod
    def _item(row: sqlite3.Row) -> ContentItem:
        passed = row["passed"]
        return ContentItem(
            id=row["id"], run_id=row["run_id"], channel=row["channel"], title=row["title"], status=row["status"],
            version=max(1, int(row["version"] or 0)), score=row["score"], passed=None if passed is None else bool(passed),
            scheduled_at=row["scheduled_at"], published_at=row["published_at"], published_url=row["published_url"],
            note=row["note"], created_at=row["created_at"], updated_at=row["updated_at"],
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
        """Create the item if it does not exist yet; return it either way."""
        brief = _as_model(Brief, brief, "브리프")
        with self._tx() as conn:
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

    def _refresh_item(self, conn: sqlite3.Connection, item_id: str) -> None:
        """Recompute title/version/score/passed/status from the latest version.

        - ``draft`` / ``needs_changes`` follow the latest review (failed → needs_changes).
        - ``approved`` / ``scheduled`` fall back to draft/needs_changes when a newer
          version than the approved one appears (approval is for specific content).
        - ``published`` / ``archived`` never change here.
        """
        item = self._item_row(conn, item_id)
        latest = conn.execute("SELECT * FROM versions WHERE item_id = ? ORDER BY version DESC LIMIT 1", (item_id,)).fetchone()
        if latest is None:
            return
        draft = Draft.model_validate_json(latest["draft"])
        review_data = _loads(latest["review"])
        review = Review.model_validate(review_data) if review_data else None
        failed = review is not None and not review.passed
        status = item["status"]
        if status in ("draft", "needs_changes"):
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
        uses it to find its own rounds again on resume.
        """
        draft = _as_model(Draft, draft, "초안")
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            version = self._insert_version(conn, item_id, draft, source=source, review=review, instructions=instructions,
                                           run_id=run_id, role="round")
            self._refresh_item(conn, item_id)
        return version

    def attach_review(self, version_id: str, review: Review) -> None:
        review = _as_model(Review, review, "검수 결과")
        with self._tx() as conn:
            row = conn.execute("SELECT item_id FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"버전 {version_id}를 찾을 수 없어요")
            conn.execute("UPDATE versions SET review = ? WHERE id = ?", (review.model_dump_json(), version_id))
            self._refresh_item(conn, row["item_id"])

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
        """
        if status not in CONTENT_STATUSES:
            raise WorkspaceError(f"알 수 없는 상태예요: {status!r} ({', '.join(CONTENT_STATUSES)} 중 하나)")
        with self._tx() as conn:
            row = self._item_row(conn, item_id)
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
            if note is not None:
                values["note"] = note.strip()[:MAX_NOTE_CHARS]
            values["updated_at"] = utc_now()
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
            return self._item(self._item_row(conn, item_id))

    def published_history(self, channel: str | None = None, limit: int = 50) -> list[ContentItem]:
        """Published items, newest first (the planner uses them to avoid repeating topics)."""
        params: list[Any] = []
        clause = ""
        if channel:
            if channel not in ALL_CHANNELS:
                raise WorkspaceError(f"알 수 없는 채널이에요: {channel!r}")
            clause = "AND channel = ?"
            params.append(channel)
        limit = max(1, min(int(limit or 50), 1000))
        with self._read() as conn:
            rows = conn.execute(f"SELECT * FROM items WHERE status = 'published' {clause} "
                                "ORDER BY published_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._item(row) for row in rows]

    def list_run_versions(self, run_id: str, channel: str) -> list[DraftVersion]:
        """The review-loop rounds a pipeline run stored for one channel, in round order."""
        item_id = pipeline_item_id(run_id, channel)
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM versions WHERE item_id = ? AND run_id = ? AND role = 'round' "
                                "ORDER BY round, version", (item_id, run_id)).fetchall()
        return [self._version(row) for row in rows]

    def upsert_item_from_result(self, run_id: str, result: ChannelResult, brief: Brief) -> ContentItem:
        """Create/update item ``it_<run_id>_<channel>`` from a finished channel.

        Every draft round becomes a version (source=agent) with its review;
        rounds already stored by this run are not duplicated (safe to call
        again, e.g. after a resume). When the best-scoring round is not the
        last one, the best draft is added once more as the newest version, so
        "the latest version" is always the final text.
        """
        result = _as_model(ChannelResult, result, "채널 결과")
        brief = _as_model(Brief, brief, "브리프")
        item_id = pipeline_item_id(run_id, result.channel)
        reviews_by_round = {review.round: review for review in result.reviews}
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
                self._insert_item(conn, item_id, result.channel, result.final.title, run_id=run_id, brief=brief)
            stored = {
                row["round"]: row
                for row in conn.execute("SELECT id, round, review FROM versions WHERE item_id = ? AND run_id = ? "
                                        "AND role = 'round' AND source = 'agent'", (item_id, run_id)).fetchall()
            }
            for draft in result.drafts:
                review = reviews_by_round.get(draft.round)
                row = stored.get(draft.round)
                if row is None:
                    self._insert_version(conn, item_id, draft, source="agent", review=review, instructions="",
                                         run_id=run_id, role="round")
                elif review is not None and (_loads(row["review"]) != review.model_dump(mode="json")):
                    conn.execute("UPDATE versions SET review = ? WHERE id = ?", (review.model_dump_json(), row["id"]))
            last_round = result.drafts[-1].round if result.drafts else result.final.round
            if result.final.round != last_round:
                exists = conn.execute("SELECT 1 FROM versions WHERE item_id = ? AND run_id = ? AND role = 'final'",
                                      (item_id, run_id)).fetchone()
                if exists is None:
                    note = f"R{result.final.round} 버전이 가장 점수가 높아 최종본으로 골랐어요"
                    final = result.final.model_copy(update={"change_log": [note, *result.final.change_log]})
                    self._insert_version(conn, item_id, final, source="agent", review=reviews_by_round.get(result.final.round),
                                         instructions="", run_id=run_id, role="final")
            self._refresh_item(conn, item_id)
            return self._item(self._item_row(conn, item_id))

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

    def claim_slot(self, slot_id: str, run_id: str, *, force: bool = False) -> CalendarSlot:
        """Atomically mark a slot ``generating`` for ``run_id`` (refuses a slot already
        generating or drafted unless ``force``)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
            if row["status"] == "generating" and not force:
                raise WorkspaceError("이 슬롯은 이미 초안을 만드는 중이에요")
            if row["status"] == "drafted" and row["item_id"] and not force:
                raise WorkspaceError(f"이미 초안이 있어요 (보관함 {row['item_id']}). 다시 만들려면 force로 요청해 주세요.")
            conn.execute("UPDATE slots SET status = 'generating', run_id = ?, updated_at = ? WHERE id = ?",
                         (run_id, utc_now(), slot_id))
            return self._slot(conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone())

    def due_slots(self, until_date: str) -> list[CalendarSlot]:
        """Planned slots on or before ``until_date`` (for ``insia run-due``)."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM slots WHERE status = 'planned' AND date <= ?",
                                (_check_date(until_date, "기준일"),)).fetchall()
        return self._sorted_slots([self._slot(row) for row in rows])
