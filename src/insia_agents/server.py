"""Dashboard + HTTP API server (stdlib ``ThreadingHTTPServer``, no framework).

Everything the dashboard does goes through this API; ``docs/api.md`` is the
full Korean reference. All state lives in the workspace (``Settings.home``,
SQLite): runs and item jobs keep running in background threads, their events
are stored as they happen, and a restarted server serves finished runs'
events from the database (``mark_interrupted`` closes runs a previous process
left behind, so they can be resumed).

Routes (JSON unless noted; errors are ``{"error": "<Korean>", "status": N}``)::

    GET    /api/health                      mode, model, workspace, profile_complete, live_available, budget_usd, capabilities
    POST   /api/login  {token}              sets the insia_token cookie (token mode)
    POST   /api/logout                      clears it
    GET    /api/sample-brief
    GET    /api/profile        PUT /api/profile
    GET    /api/documents      POST /api/documents {title, text, kind?, filename?}
    GET    /api/documents/<id> DELETE /api/documents/<id>
    GET    /api/runs?kind=&status=&limit=   POST /api/runs (Brief + options)
    GET    /api/runs/<id>                   GET /api/runs/<id>/events (SSE)   GET /api/runs/<id>/export (zip)
    POST   /api/runs/<id>/resume            POST /api/runs/<id>/cancel
    GET    /api/items?status=&channel=      GET /api/items/<id>
    PUT    /api/items/<id>/draft            POST /api/items/<id>/review | /revise | /status
    GET    /api/items/<id>/export?format=md|txt|html|docx|zip[&version=N][&info=1]
    GET    /api/calendar?from=&to=          POST /api/calendar/plan
    POST   /api/calendar/<slot>             POST /api/calendar/<slot>/generate
    GET    /api/usage?since=&until=

Security model (unchanged from the local-only version, plus an access token):

- Every ``/api`` request must carry an allowed ``Host`` (loopback names, the
  bind address, ``--public-host`` names; IP literals when bound to every
  interface). This blocks DNS rebinding.
- State-changing requests (POST/PUT/DELETE) need a same-origin ``Origin``
  (when sent; it must name exactly the requested host:port) and POST/PUT a
  ``Content-Type: application/json`` body, which forces a CORS preflight this
  server never approves, so other web pages cannot change anything.
- A non-loopback bind refuses to start without an access token
  (``INSIA_ACCESS_TOKEN`` / ``--token``). With a token every ``/api`` route
  except ``/api/login`` and ``/api/logout`` needs ``Authorization: Bearer
  <token>`` or the ``insia_token`` cookie (HttpOnly, SameSite=Strict; Secure
  behind an HTTPS proxy with ``--trust-proxy``). Comparisons are constant-time
  and failed attempts are rate limited per client IP. Static dashboard files
  stay public (the dashboard shows a login form when ``/api/health`` is 401).
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
import traceback
import urllib.parse
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from pydantic import ValidationError

from . import __version__
from . import actions
from .backends import create_backend
from .backends.base import BackendError, RunContext
from .config import MIN_SPEED, Settings, has_credentials, load_sample_brief, resolve_mode
from .db import (RUN_STATUSES, ApprovalBlockedError, InvalidTransitionError, NotFoundError, Workspace, WorkspaceError,
                 pipeline_item_id, profile_is_empty)
from .events import TERMINAL_TYPES, EventBus, SimClock
from .exporters import (CHANNEL_FORMATS, ExportError, ExportFile, MissingDependencyError, capabilities, export_item,
                        export_run_zip, format_label, formats_for)
from .models import Brief, Profile, RunResult
from .pipeline import (RESUMABLE_KINDS, BudgetExceeded, PipelineError, RunCancelled, SimRunner, ThreadRunner,
                       build_context, continue_numbering, failure_status, load_resume_state, new_run_id, prepare_run,
                       run_pipeline, resume_run)
from .planner import PlanningError, plan_week

log = logging.getLogger(__name__)

# -- limits -------------------------------------------------------------------
MAX_BODY = 64 * 1024  # default JSON body limit
MAX_DOCUMENT_BODY = 8 * 1024 * 1024  # documents: up to 2,000,000 characters of (Korean) text as UTF-8 JSON
MAX_DRAFT_BODY = 1024 * 1024  # human edits: long 사업계획서 drafts (100,000 characters max)
MAX_PROFILE_BODY = 256 * 1024
MAX_LOGIN_BODY = 4 * 1024
MAX_RECORDS = 50  # finished runs kept in memory (for their full RunResult / JobResult)
DEFAULT_MAX_LIVE = 2
DEFAULT_MAX_MOCK = 4
MAX_COST_OPTION = 10_000.0

# -- auth ---------------------------------------------------------------------
COOKIE_NAME = "insia_token"
COOKIE_MAX_AGE = 30 * 24 * 3600
MIN_TOKEN_CHARS = 12
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = 60.0
SESSION_CONTEXT = b"insia-session-v1"

ID_SEGMENT = r"([A-Za-z0-9][A-Za-z0-9._-]{0,120})"
HOST_HEADER = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9.-]+))(?::(?P<port>[0-9]{1,5}))?$")
HOST_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
DOC_ID = re.compile(r"^u\d{1,9}$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})

# Fields the dashboard's completeness meter counts (web/js/brand.js COMPLETE).
PROFILE_REQUIRED: tuple[tuple[str, str], ...] = (
    ("company_name", "회사명"), ("service_name", "서비스명"), ("one_liner", "한 줄 소개"), ("description", "서비스 설명"),
    ("target_customers", "타깃 고객"), ("problem", "고객이 겪는 문제"), ("solution", "해결 방법"),
    ("differentiators", "차별점"), ("business_model", "비즈니스 모델"), ("team", "팀 역할"), ("tone", "톤앤매너"),
    ("cta", "기본 행동 유도 문구"),
)

SETTINGS_OPTION_KEYS = ("mode", "speed", "max_rounds", "pass_score", "max_cost_usd", "use_profile")

NO_KEY_MESSAGE = "API 키가 없어 live 모드를 쓸 수 없어요. ANTHROPIC_API_KEY를 설정한 뒤 서버를 다시 시작해 주세요."
LOGIN_REQUIRED = "로그인이 필요해요. 서버를 켤 때 정한 접근 토큰을 입력해 주세요."

MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".glb": "model/gltf-binary",  # the 3D capybara models
    ".gltf": "model/gltf+json",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}

NO_WEB_PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><title>INSIA 에이전트 스튜디오</title>
<body style="font-family:sans-serif;background:#0b1220;color:#e2e8f0;padding:32px">
<h1>대시보드 파일을 찾을 수 없어요</h1>
<p>저장소 루트에서 <code>insia serve</code>를 실행하거나 <code>--web-dir</code>로 web 폴더를 지정해 주세요.</p>
<p>API는 동작 중이에요: <a style="color:#5eead4" href="/api/health">/api/health</a></p></body></html>"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RequestError(Exception):
    """An HTTP error answered as ``{"error": message, "status": status, **extra}``."""

    def __init__(self, status: int, message: str, *, extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}


class ServerConfigError(ValueError):
    """The server cannot start with these settings. ``str(exc)`` is a Korean message for the terminal."""


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def parse_options(raw: Any) -> dict[str, Any]:
    """Validate run/job ``options``. Only keys that were given are returned.

    ``mode`` (auto|live|mock), ``speed`` (0 or 0.1–100), ``max_rounds`` (0–5),
    ``pass_score`` (0–100), ``max_cost_usd`` (0–10000, 0 = no cap),
    ``use_profile`` (bool), ``docs`` ("all" | "none" | ["u1", …] | "u1,u2").
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RequestError(400, "options는 객체여야 해요")
    out: dict[str, Any] = {}
    if raw.get("mode") is not None:
        if raw["mode"] not in ("auto", "live", "mock"):
            raise RequestError(400, "options.mode는 auto, live, mock 중 하나여야 해요")
        out["mode"] = raw["mode"]
    for key, kind, lo, hi in (("speed", float, 0, 100), ("max_rounds", int, 0, 5), ("pass_score", int, 0, 100),
                              ("max_cost_usd", float, 0, MAX_COST_OPTION)):
        if raw.get(key) is None:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestError(400, f"options.{key}는 숫자여야 해요")
        if isinstance(value, float) and not math.isfinite(value):
            raise RequestError(400, f"options.{key}는 유한한 숫자여야 해요")
        # Range first: int/float comparison is exact, so a huge int never reaches float().
        if not lo <= value <= hi:
            raise RequestError(400, f"options.{key}는 {lo}~{hi:g} 사이여야 해요")
        if key == "speed" and 0 < value < MIN_SPEED:
            raise RequestError(400, f"options.speed는 0(기다리지 않음) 또는 {MIN_SPEED}~100 사이여야 해요")
        if kind is int and isinstance(value, float) and not value.is_integer():
            raise RequestError(400, f"options.{key}는 정수여야 해요")
        out[key] = kind(value)
    if raw.get("use_profile") is not None:
        if not isinstance(raw["use_profile"], bool):
            raise RequestError(400, "options.use_profile은 true 또는 false여야 해요")
        out["use_profile"] = raw["use_profile"]
    if raw.get("docs") is not None:
        out["docs"] = parse_docs(raw["docs"])
    return out


def parse_docs(value: Any) -> str | list[str]:
    """``"all"`` / ``"none"`` / ``["u1", "u3"]`` / ``"u1,u3"`` → ``"all"`` | ``"none"`` | list of ids."""
    message = 'options.docs는 "all", "none" 또는 자료 id 목록(예: ["u1", "u3"])이어야 해요'
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("all", "none", ""):
            return text or "none"
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or len(value) > 200:
        raise RequestError(400, message)
    ids: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not DOC_ID.match(entry.strip()):
            raise RequestError(400, message)
        if entry.strip() not in ids:
            ids.append(entry.strip())
    return ids


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"JSON constant {name} is not allowed")


def split_host(value: str, default_port: int = 80) -> tuple[str, int] | None:
    """Parse a ``Host`` value (``name[:port]`` / ``[v6][:port]``) → ``(lowercase name, port)``."""
    match = HOST_HEADER.match(value.strip())
    if not match:
        return None
    name = (match.group("v6") or match.group("name")).lower()
    port = int(match.group("port")) if match.group("port") else default_port
    return name, port


def is_loopback_bind(host: str) -> bool:
    """True for ``127.0.0.1``, ``::1``, ``localhost`` and other 127.x addresses."""
    name = str(host or "").strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_public_hosts(hosts: Sequence[str] | str | None) -> tuple[str, ...]:
    """Validate ``--public-host`` names (host names or IP literals, no scheme/port)."""
    if not hosts:
        return ()
    if isinstance(hosts, str):
        hosts = [hosts]
    out: list[str] = []
    for raw in hosts:
        name = str(raw or "").strip().lower().rstrip(".")
        if not name:
            continue
        literal = name.strip("[]")
        try:
            ipaddress.ip_address(literal)
            name = literal
        except ValueError:
            if not HOST_NAME.match(name):
                raise ServerConfigError(
                    f"--public-host 값 '{raw}'은(는) 쓸 수 없어요. https://나 포트 없이 이름만 적어 주세요 (예: insia.example.com).")
        if name not in out:
            out.append(name)
    return tuple(out)


def check_token(token: str | None) -> str | None:
    """Validate an access token (printable ASCII, no spaces, at least 12 characters)."""
    if token is None:
        return None
    token = str(token).strip()
    if not token:
        return None
    hint = ' 예: python -c "import secrets; print(secrets.token_urlsafe(24))"로 만든 값을 쓰세요.'
    if len(token) < MIN_TOKEN_CHARS:
        raise ServerConfigError(f"접근 토큰이 너무 짧아요 ({len(token)}자). {MIN_TOKEN_CHARS}자 이상으로 정해 주세요.{hint}")
    if not all(33 <= ord(ch) <= 126 for ch in token):
        raise ServerConfigError(f"접근 토큰에는 공백 없는 영문·숫자·기호만 쓸 수 있어요.{hint}")
    return token


def _profile_missing(profile: Profile) -> list[str]:
    missing = []
    for key, label in PROFILE_REQUIRED:
        value = getattr(profile, key, None)
        if isinstance(value, list):
            filled = any((v.role if hasattr(v, "role") else str(v)).strip() for v in value)
        else:
            filled = bool(str(value or "").strip())
        if not filled:
            missing.append(label)
    return missing


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def _query_int(query: dict[str, list[str]], key: str, default: int, lo: int, hi: int) -> int:
    raw = _query_value(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RequestError(400, f"{key}는 정수여야 해요") from None
    if not lo <= value <= hi:
        raise RequestError(400, f"{key}는 {lo}~{hi} 사이여야 해요")
    return value


def _query_bool(query: dict[str, list[str]], key: str) -> bool:
    return (_query_value(query, key) or "").lower() in ("1", "true", "yes", "on")


def _opt_str(body: dict[str, Any], key: str, *, limit: int = 2000, what: str | None = None) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(400, f"{what or key}은(는) 문자열이어야 해요")
    if len(value) > limit:
        raise RequestError(400, f"{what or key}이(가) 너무 길어요 (최대 {limit:,}자)")
    return value


def _opt_bool(body: dict[str, Any], key: str) -> bool:
    value = body.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestError(400, f"{key}는 true 또는 false여야 해요")
    return value


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancellableSimRunner(SimRunner):
    """Mock-mode runner that the cancel endpoint can stop (``ThreadRunner`` already can).

    ``cancelled`` makes the pipeline's checkpoint raise ``RunCancelled`` before
    the next backend call; ``run_parallel`` re-raises it when a channel was
    stopped, so the run ends ``cancelled`` instead of "completed with errors".
    """

    def __init__(self, clock: SimClock, cancel_event: threading.Event) -> None:
        super().__init__(clock)
        self._cancel_event = cancel_event

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run_parallel(self, gens):  # type: ignore[override]
        results = super().run_parallel(gens)
        if self._cancel_event.is_set() and any(isinstance(v, BaseException) for v in results.values()):
            raise RunCancelled()
        return results


class CancellableBackend:
    """Backend proxy: every agent-facing call raises ``RunCancelled`` once the
    job was cancelled. Item jobs build their own runner, so this is how the
    cancel endpoint reaches them; attribute reads and writes go to the real
    backend (``context``, ``on_usage``, ``model`` …)."""

    METHODS = frozenset({"plan", "research", "draft", "review", "revise", "plan_calendar"})

    def __init__(self, backend: Any, cancel_event: threading.Event) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_cancel_event", cancel_event)

    def __getattr__(self, name: str) -> Any:
        value = getattr(object.__getattribute__(self, "_backend"), name)
        if name in CancellableBackend.METHODS and callable(value):
            cancel_event = object.__getattribute__(self, "_cancel_event")

            @functools.wraps(value)
            def guarded(*args: Any, **kwargs: Any) -> Any:
                if cancel_event.is_set():
                    raise RunCancelled()
                return value(*args, **kwargs)

            return guarded
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_backend"), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(object.__getattribute__(self, "_backend"), name)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """A run or item job started by this server process (in memory while it runs).

    The workspace row is the source of truth; this record adds the live event
    bus, the cancel switch and the full result object for recent runs.
    """

    run_id: str
    kind: str
    bus: EventBus
    mode: str
    model: str
    brief: Brief | None = None
    options: dict[str, Any] = field(default_factory=dict)
    item_id: str = ""
    slot_id: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    runner: Any = None
    created_at: str = field(default_factory=_iso_now)
    status: str = "running"
    finished_at: str | None = None
    result: RunResult | None = None
    job: dict[str, Any] | None = None
    error: str | None = None
    thread: threading.Thread | None = None
    created_row: bool = False
    cancel_requested: bool = False

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.cancel_event.set()
        cancel = getattr(self.runner, "cancel", None)
        if callable(cancel):
            cancel()

    def summary(self) -> dict[str, Any]:
        """Summary in the workspace's shape (for a job whose row does not exist yet)."""
        brief = self.brief
        return {
            "run_id": self.run_id, "kind": self.kind, "status": self.status,
            "topic": brief.topic if brief else "", "channels": list(brief.channels) if brief else [],
            "mode": self.mode, "model": self.model, "parent_item_id": self.item_id,
            "created_at": self.created_at, "updated_at": self.finished_at or self.created_at,
            "finished_at": self.finished_at, "cost_usd": 0.0, "error": self.error, "events": len(self.bus),
            "items": {}, "scores": None,
        }


class RunManager:
    """Starts runs and item jobs in background threads and answers run queries.

    ``workspace`` defaults to ``Workspace.from_settings(settings)`` (owned: closed
    by ``shutdown``). On construction runs left ``running`` by a previous
    process are marked ``interrupted`` (``interrupted_on_start`` = how many).
    Concurrency: at most ``max_live`` live and ``max_mock`` mock jobs at once
    (env ``INSIA_MAX_LIVE_JOBS`` / ``INSIA_MAX_MOCK_JOBS``; ``max_active`` sets both).
    """

    def __init__(self, settings: Settings, max_active: int | None = None, *, workspace: Workspace | None = None,
                 max_live: int | None = None, max_mock: int | None = None, recover: bool = True) -> None:
        self.settings = settings
        self._owns_workspace = workspace is None
        self.workspace = workspace if workspace is not None else Workspace.from_settings(settings)
        self.max_live = max(1, int(max_live or max_active or _env_int("INSIA_MAX_LIVE_JOBS", DEFAULT_MAX_LIVE)))
        self.max_mock = max(1, int(max_mock or max_active or _env_int("INSIA_MAX_MOCK_JOBS", DEFAULT_MAX_MOCK)))
        self._active: dict[str, RunRecord] = {}
        self._recent: OrderedDict[str, RunRecord] = OrderedDict()
        self._planning = {"live": 0, "mock": 0}
        self._lock = threading.Lock()
        self.interrupted_on_start = self.workspace.mark_interrupted() if recover else 0
        if self.interrupted_on_start:
            log.warning("이전 실행 중 중단된 작업 %d개를 'interrupted'로 표시했어요", self.interrupted_on_start)

    @property
    def max_active(self) -> int:
        return max(self.max_live, self.max_mock)

    # -- info --------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        mode, _ = resolve_mode(self.settings.mode)
        with self._lock:
            active = {"live": sum(1 for r in self._active.values() if r.mode == "live"),
                      "mock": sum(1 for r in self._active.values() if r.mode != "live")}
        profile = self.workspace.get_profile()
        return {
            "mode": mode, "model": self.settings.model, "version": __version__,
            "default_mode": self.settings.mode, "live_available": has_credentials(),
            "workspace": str(self.workspace.home.resolve()),
            "profile_complete": not _profile_missing(profile),
            "budget_usd": float(self.settings.max_cost_usd or 0.0),
            "max_document_chars": self.settings.max_document_chars,
            "capabilities": capabilities(),
            "formats": {channel: list(fmts) for channel, fmts in CHANNEL_FORMATS.items()},
            "active_jobs": active,
            "limits": {"live": self.max_live, "mock": self.max_mock},
        }

    # -- records -------------------------------------------------------------------
    def get(self, run_id: str) -> RunRecord | None:
        """The in-memory record (active or recently finished), if this process started the run."""
        with self._lock:
            return self._active.get(run_id) or self._recent.get(run_id)

    def active_record(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._active.get(run_id)

    def list(self, limit: int = 50, *, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.workspace.list_runs(limit, kind=kind, status=status if status != "running" else None)
        with self._lock:
            active = dict(self._active)
        known = {row["run_id"] for row in rows}
        pending = [r.summary() for r in active.values() if r.run_id not in known
                   and (not kind or r.kind == kind)]
        out = []
        for row in pending + rows:
            record = active.get(row["run_id"])
            row = {**row, "active": record is not None}
            if record is not None:
                row["status"] = "running"
            if status and row["status"] != status:
                continue
            row["resumable"] = record is None and self._list_resumable(row)
            out.append(row)
        return out[:limit]

    def detail(self, run_id: str) -> dict[str, Any] | None:
        row = self.workspace.get_run(run_id)
        record = self.get(run_id)
        if row is None and record is None:
            return None
        if row is None:
            assert record is not None
            data = record.summary()
            data.update({"brief": _dump(record.brief), "options": record.options, "profile": None, "plan": None,
                         "research": None, "progress": {}})
        else:
            data = dict(row)
        active = record is not None and self.active_record(run_id) is not None
        data["active"] = active
        if active:
            data["status"] = "running"
        data["cancel_requested"] = bool(record and record.cancel_requested)
        data["result"] = record.result.model_dump(mode="json") if record is not None and record.result is not None else None
        data["job"] = record.job if record is not None else None
        data["resumable"] = (not active) and self._resumable(data)
        data["events_url"] = f"/api/runs/{run_id}/events"
        return data

    @staticmethod
    def _list_resumable(row: dict[str, Any]) -> bool:
        """``resumable`` for a list row (no ``progress``): a stopped pipeline/slot run, or a
        completed one with a channel that produced no item (it failed)."""
        if row.get("kind") not in RESUMABLE_KINDS or row.get("status") == "running":
            return False
        if row.get("status") != "completed":
            return True
        done = row.get("items") or {}
        return any(ch not in done for ch in row.get("channels") or [])

    @staticmethod
    def _resumable(run: dict[str, Any]) -> bool:
        if run.get("kind") not in RESUMABLE_KINDS or run.get("status") == "running":
            return False
        if run.get("status") != "completed":
            return True
        done = (run.get("progress") or {}).get("channels") or {}
        return any(done.get(ch) != "completed" for ch in run.get("channels") or [])

    # -- capacity --------------------------------------------------------------------
    def _check_capacity(self, mode: str) -> None:
        """Raise 429 when another ``mode`` job does not fit (call with ``_lock`` held)."""
        live = mode == "live"
        limit = self.max_live if live else self.max_mock
        count = sum(1 for r in self._active.values() if (r.mode == "live") == live)
        count += self._planning["live" if live else "mock"]
        if count >= limit:
            label = "live" if live else "mock"
            raise RequestError(429, f"동시에 실행할 수 있는 작업 수({label} {limit}개)를 넘었어요. "
                                    "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.", headers={"Retry-After": "10"})

    def _check_conflicts(self, record: RunRecord) -> None:
        if record.run_id in self._active:
            raise RequestError(409, "이 실행은 이미 진행 중이에요. 실시간 화면에서 진행 상황을 볼 수 있어요.")
        for other in self._active.values():
            if record.item_id and other.item_id == record.item_id and other.kind in ("review", "revise"):
                raise RequestError(409, f"이 콘텐츠는 이미 작업 중이에요 (실행 {other.run_id}). 끝난 뒤 다시 시도해 주세요.",
                                   extra={"run_id": other.run_id})
            if record.slot_id and other.slot_id == record.slot_id:
                raise RequestError(409, f"이 슬롯은 이미 초안을 만드는 중이에요 (실행 {other.run_id}).",
                                   extra={"run_id": other.run_id})

    # -- launching ---------------------------------------------------------------------
    def _prepare(self, options: dict[str, Any], *, run_id: str | None = None, run_mode: str | None = None,
                 ) -> tuple[Settings, Any, EventBus, str, threading.Event]:
        """Settings for this job + (cancellable backend, bus with an interruptible mock clock, mode note, cancel switch)."""
        opts = {k: options[k] for k in SETTINGS_OPTION_KEYS if options.get(k) is not None}
        if opts.get("mode") == "live" and not has_credentials():
            raise RequestError(400, NO_KEY_MESSAGE)
        try:
            settings = self.settings.with_options(**opts)
            if run_mode in ("live", "mock") and "mode" not in opts and settings.mode == "auto":
                settings = settings.with_options(mode=run_mode)
            mode, _ = resolve_mode(settings.mode)
            if mode == "live" and not has_credentials():
                raise RequestError(400, NO_KEY_MESSAGE)
            backend, bus, note = prepare_run(settings, run_id=run_id or new_run_id())
        except (ValueError, BackendError) as exc:
            raise RequestError(400, str(exc)) from exc
        cancel_event = threading.Event()
        if isinstance(bus.clock, SimClock):  # mock playback sleeps end at once when the job is cancelled
            bus = EventBus(bus.run_id, clock=SimClock(bus.clock.speed, sleep=cancel_event.wait))
        return settings, CancellableBackend(backend, cancel_event), bus, note, cancel_event

    @staticmethod
    def _runner(settings: Settings, bus: EventBus, cancel_event: threading.Event) -> Any:
        if isinstance(bus.clock, SimClock):
            return CancellableSimRunner(bus.clock, cancel_event)
        return ThreadRunner(settings.max_workers)

    def _launch(self, record: RunRecord, target: Callable[[], Any], *, create_row: Callable[[], None] | None = None,
                ) -> RunRecord:
        record.thread = threading.Thread(target=self._work, args=(record, target), daemon=True,
                                         name=f"insia-{record.kind}-{record.run_id}")
        with self._lock:
            # Check and insert in one critical section so a burst cannot exceed the limit.
            self._check_conflicts(record)
            self._check_capacity(record.mode)
            self._active[record.run_id] = record
        try:
            if create_row is not None:
                create_row()
                record.created_row = True
        except BaseException:
            with self._lock:
                self._active.pop(record.run_id, None)
            raise
        record.thread.start()
        return record

    def _work(self, record: RunRecord, target: Callable[[], Any]) -> None:
        outcome_status = "completed"
        try:
            outcome = target()
            if isinstance(outcome, RunResult):
                record.result = outcome
                record.model = outcome.model
            elif isinstance(outcome, actions.JobResult):
                record.job = outcome.to_dict()
                record.result = outcome.result
        except BaseException as exc:  # noqa: BLE001 - the thread must record every failure
            outcome_status, message = failure_status(exc)
            record.error = message
            if isinstance(exc, BudgetExceeded) and exc.result is not None:
                record.result = exc.result
            try:
                self._settle(record, outcome_status, message)
            except Exception:  # noqa: BLE001
                log.exception("실행 %s의 상태를 정리하지 못했어요", record.run_id)
        finally:
            record.finished_at = _iso_now()
            try:
                record.bus.close()  # wakes SSE readers even when no terminal event was emitted
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                record.status = outcome_status
                self._active.pop(record.run_id, None)
                self._recent[record.run_id] = record
                while len(self._recent) > MAX_RECORDS:
                    self._recent.popitem(last=False)

    def _settle(self, record: RunRecord, status: str, message: str) -> None:
        """Make sure a failed job's run row and event stream end, whatever path it failed on."""
        ws = self.workspace
        run = ws.get_run(record.run_id)
        bus = record.bus
        if run is None:  # failed before the job created its run: tell stream readers (memory only)
            if not bus.closed:
                try:
                    bus.emit("run.failed", "system", {"error": message, "kind": record.kind})
                except RuntimeError:
                    pass
            return
        if not (record.created_row or len(bus) > 0):
            return  # never started its own stream (e.g. another process resumed this run first)
        if run["status"] != "running":
            return  # the pipeline / job already recorded how it ended
        if not bus.closed:
            try:
                event = bus.emit("run.failed", "system", {"error": message, "kind": record.kind})
            except RuntimeError:
                event = None
            if event is not None:
                last = ws.last_event(record.run_id)
                if last is None or int(last.get("seq", 0)) < event["seq"]:
                    ws.append_event(record.run_id, event)
        ws.update_run(record.run_id, status=status, error=message)

    # -- run kinds -----------------------------------------------------------------------
    def start(self, brief: Brief, options: dict[str, Any]) -> RunRecord:
        """Start a pipeline run (``POST /api/runs``)."""
        settings, backend, bus, note, cancel_event = self._prepare(options)
        docs = options.get("docs", "all")
        try:
            context = build_context(self.workspace, settings, docs=docs)
        except WorkspaceError as exc:
            raise RequestError(400, str(exc)) from None
        runner = self._runner(settings, bus, cancel_event)
        record = RunRecord(run_id=bus.run_id, kind="pipeline", bus=bus, mode=backend.name, model=backend.model,
                           brief=brief, options=options, cancel_event=cancel_event, runner=runner)
        stored_options = {k: v for k, v in options.items() if k != "docs"}
        stored_options["doc_ids"] = [d.id for d in context.documents]

        def create_row() -> None:
            self.workspace.create_run(bus.run_id, brief, kind="pipeline", options=stored_options, mode=backend.name,
                                      model=backend.model, profile=context.profile)

        def target() -> RunResult:
            return run_pipeline(brief, backend, bus, settings, runner=runner, out_dir=settings.out_dir, mode_note=note,
                                workspace=self.workspace, context=context)

        return self._launch(record, target, create_row=create_row)

    def resume(self, run_id: str, *, force: bool = False, options: dict[str, Any] | None = None) -> RunRecord:
        """Continue an interrupted / failed / cancelled / budget-stopped pipeline or slot run (same run id)."""
        options = dict(options or {})
        run = self.workspace.get_run(run_id)
        if run is None:
            raise RequestError(404, "해당 실행을 찾을 수 없어요")
        if self.active_record(run_id) is not None:
            raise RequestError(409, "이 실행은 이미 진행 중이에요. 실시간 화면에서 진행 상황을 볼 수 있어요.")
        if run["kind"] not in RESUMABLE_KINDS:
            raise RequestError(400, f"'{run['kind']}' 작업은 이어서 실행할 수 없어요. 같은 작업을 다시 시작해 주세요.")
        if run["status"] == "running" and not force:
            raise RequestError(409, "다른 곳(CLI 등)에서 실행 중으로 표시된 작업이에요. 정말 멈춘 작업이라면 "
                                    "force: true로 다시 요청하거나 서버를 다시 시작해 주세요.", extra={"can_force": True})
        if run["status"] == "completed":
            state = load_resume_state(self.workspace, run_id)
            if all(state.channels.get(ch) and state.channels[ch].completed for ch in run.get("channels") or []):
                raise RequestError(409, "이미 모든 채널을 마친 실행이에요. 결과는 보관함에서 볼 수 있어요.")
        settings, backend, bus, note, cancel_event = self._prepare(options, run_id=run_id, run_mode=run.get("mode"))
        continue_numbering(self.workspace, bus)  # stream readers see the stored events first, then this bus
        runner = self._runner(settings, bus, cancel_event)
        brief = Brief.model_validate(run["brief"]) if run.get("brief") else None
        slot_id = str((run.get("options") or {}).get("slot_id") or "")
        record = RunRecord(run_id=run_id, kind=run["kind"], bus=bus, mode=backend.name, model=backend.model, brief=brief,
                           options={**options, "force": force, "resumed": True}, slot_id=slot_id,
                           cancel_event=cancel_event, runner=runner)

        def target() -> RunResult:
            return resume_run(run_id, settings, self.workspace, backend=backend, bus=bus, runner=runner,
                              out_dir=settings.out_dir, force=force)

        return self._launch(record, target)

    def cancel(self, run_id: str) -> RunRecord:
        record = self.active_record(run_id)
        if record is None:
            run = self.workspace.get_run(run_id)
            if run is None:
                raise RequestError(404, "해당 실행을 찾을 수 없어요")
            if run["status"] == "running":
                raise RequestError(409, "이 서버에서 실행 중인 작업이 아니에요. 다른 곳(CLI 등)에서 실행 중이거나 "
                                        "비정상 종료된 작업이에요. 서버를 다시 시작하면 '중단됨'으로 정리돼요.")
            raise RequestError(409, "이미 끝난 실행이에요.", extra={"run_status": run["status"]})
        record.request_cancel()
        return record

    def _item_for_job(self, item_id: str, what: str):
        detail = self.workspace.get_item(item_id)
        if detail is None:
            raise RequestError(404, f"콘텐츠 {item_id}를 찾을 수 없어요")
        if not detail.versions:
            raise RequestError(400, f"{what} 버전이 없어요. 초안을 먼저 만들어 주세요.")
        return detail

    def review(self, item_id: str, options: dict[str, Any] | None = None) -> RunRecord:
        """재검수 job (``POST /api/items/<id>/review``)."""
        options = dict(options or {})
        detail = self._item_for_job(item_id, "검수할")
        settings, backend, bus, _note, cancel_event = self._prepare(options)
        channel = detail.item.channel
        brief = (detail.brief or Brief(topic=detail.item.title or "콘텐츠", channels=[channel])).model_copy(
            update={"channels": [channel]})
        record = RunRecord(run_id=bus.run_id, kind="review", bus=bus, mode=backend.name, model=backend.model,
                           brief=brief, options=options, item_id=item_id, cancel_event=cancel_event)

        def target() -> actions.JobResult:
            return actions.review_item(self.workspace, item_id, settings=settings, backend=backend, bus=bus)

        return self._launch(record, target)

    def revise(self, item_id: str, instructions: str = "", options: dict[str, Any] | None = None) -> RunRecord:
        """수정 요청 job (``POST /api/items/<id>/revise``)."""
        options = dict(options or {})
        instructions = (instructions or "").strip()
        if len(instructions) > actions.MAX_INSTRUCTIONS_CHARS:
            raise RequestError(400, f"수정 지시가 너무 길어요 ({len(instructions):,}자). "
                                    f"{actions.MAX_INSTRUCTIONS_CHARS:,}자 이하로 줄여 주세요.")
        detail = self._item_for_job(item_id, "수정할")
        settings, backend, bus, _note, cancel_event = self._prepare(options)
        channel = detail.item.channel
        brief = (detail.brief or Brief(topic=detail.item.title or "콘텐츠", channels=[channel])).model_copy(
            update={"channels": [channel]})
        record = RunRecord(run_id=bus.run_id, kind="revise", bus=bus, mode=backend.name, model=backend.model,
                           brief=brief, options={**options, "instructions": instructions}, item_id=item_id,
                           cancel_event=cancel_event)

        def target() -> actions.JobResult:
            return actions.revise_item(self.workspace, item_id, instructions, settings=settings, backend=backend, bus=bus)

        return self._launch(record, target)

    def generate_slot(self, slot_id: str, *, force: bool = False, options: dict[str, Any] | None = None) -> RunRecord:
        """캘린더 슬롯 초안 job (``POST /api/calendar/<slot>/generate``)."""
        options = dict(options or {})
        slot = self.workspace.get_slot(slot_id)
        if slot is None:
            raise RequestError(404, f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
        if slot.status == "generating" and not force:
            raise RequestError(409, "이 슬롯은 이미 초안을 만드는 중이에요", extra={"run_id": slot.run_id})
        if slot.status == "drafted" and slot.item_id and not force:
            raise RequestError(409, "이미 초안이 있어요. 다시 만들려면 force: true로 요청해 주세요.",
                               extra={"item_id": slot.item_id, "can_force": True})
        if slot.status == "skipped" and not force:
            raise RequestError(409, "건너뛰기로 표시한 슬롯이에요. 먼저 '계획'으로 되돌리거나 force: true로 요청해 주세요.",
                               extra={"can_force": True})
        settings, backend, bus, _note, cancel_event = self._prepare(options)
        try:
            context = build_context(self.workspace, settings, docs=options.get("docs", "all"))
        except WorkspaceError as exc:
            raise RequestError(400, str(exc)) from None
        brief = actions.slot_brief(slot, context.profile)
        record = RunRecord(run_id=bus.run_id, kind="slot", bus=bus, mode=backend.name, model=backend.model, brief=brief,
                           options={**options, "slot_id": slot_id, "force": force}, slot_id=slot_id,
                           cancel_event=cancel_event)

        def target() -> actions.JobResult:
            return actions.generate_slot(self.workspace, slot_id, settings=settings, backend=backend, bus=bus,
                                         context=context, force=force)

        return self._launch(record, target)

    def plan_week(self, theme: str, start: str, end: str, counts: Any, options: dict[str, Any] | None = None):
        """Plan the calendar synchronously (counts toward the concurrency limit while the backend works)."""
        options = dict(options or {})
        opts = {k: options[k] for k in ("mode",) if options.get(k) is not None}
        if opts.get("mode") == "live" and not has_credentials():
            raise RequestError(400, NO_KEY_MESSAGE)
        try:
            settings = self.settings.with_options(**opts)
            mode, _ = resolve_mode(settings.mode)
            if mode == "live" and not has_credentials():
                raise RequestError(400, NO_KEY_MESSAGE)
            backend = create_backend(mode, settings)
        except (ValueError, BackendError) as exc:
            raise RequestError(400, str(exc)) from exc
        key = "live" if backend.name == "live" else "mock"
        with self._lock:
            self._check_capacity(backend.name)
            self._planning[key] += 1
        try:
            profile = self.workspace.get_profile() if settings.use_profile else Profile()
            try:
                backend.context = RunContext(profile=None if profile_is_empty(profile) else profile, documents=[],
                                             today=settings.today)
            except Exception:  # noqa: BLE001 - a backend without the attribute still plans
                pass
            plan = plan_week(self.workspace, backend, theme, start, end, counts, profile=profile)
        finally:
            with self._lock:
                self._planning[key] -= 1
        return plan, backend.name

    # -- events ------------------------------------------------------------------------
    def iter_events(self, run_id: str, after: int = 0, heartbeat: float | None = 15.0,
                    poll: float = 0.5) -> Iterator[dict[str, Any] | None]:
        """Stored events after ``after``, then live ones; ``None`` = heartbeat.

        Ends after the run's final terminal event. A resumed run's stored stream
        holds the earlier attempt's ``run.failed`` too; that superseded terminal
        event is skipped (its ``seq`` is left out), so clients that close on
        ``run.completed`` / ``run.failed`` keep following the resumed attempt.
        Runs started by another process (CLI) on the same workspace are
        followed by polling the database.
        """
        ws = self.workspace
        heartbeat = heartbeat if heartbeat and heartbeat > 0 else 15.0
        poll = max(0.05, min(poll, heartbeat))
        while True:
            record = self.active_record(run_id)
            if record is None and ws.get_run(run_id) is None:
                record = self.get(run_id)  # a job that failed before creating its run row
                if record is None:
                    return
            if record is not None:
                bus = record.bus
                first = bus.first_seq
                if after < first - 1:
                    for event in ws.list_events(run_id, after):
                        if event["seq"] >= first:
                            break
                        after = event["seq"]
                        if event["type"] not in TERMINAL_TYPES:  # stored endings are superseded by this attempt
                            yield event
                    after = max(after, first - 1)
                last: dict[str, Any] | None = None
                for event in bus.subscribe(after_seq=after, heartbeat=heartbeat):
                    if event is None:
                        yield None
                        continue
                    yield event
                    after = event["seq"]
                    last = event
                if last is not None and last["type"] in TERMINAL_TYPES:
                    return
                if ws.get_run(run_id) is None:
                    return
                # The bus closed without a terminal event: let the worker finish, then read the database.
                if record.thread is not None and record.thread.is_alive():
                    record.thread.join(timeout=heartbeat)
                    if record.thread.is_alive():
                        yield None
                continue
            # Database mode: a finished run, or one another process is running.
            run = ws.get_run(run_id)
            if run is None:
                return
            running = run["status"] == "running"
            events = ws.list_events(run_id, after)
            for index, event in enumerate(events):
                after = event["seq"]
                if event["type"] in TERMINAL_TYPES and (index < len(events) - 1 or running):
                    continue  # superseded: the run went on (resumed) after this ending
                yield event
            if not running and self.active_record(run_id) is None:
                return
            waited = 0.0
            while waited < heartbeat:
                time.sleep(poll)
                waited += poll
                if self.active_record(run_id) is not None:
                    break
                last_event = ws.last_event(run_id)
                if last_event is not None and int(last_event.get("seq", 0)) > after:
                    break
                current = ws.get_run(run_id)
                if current is None or current["status"] != "running":
                    break
            else:
                yield None

    # -- shutdown ----------------------------------------------------------------------
    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel active jobs, wait up to ``timeout`` seconds, close an owned workspace."""
        with self._lock:
            records = list(self._active.values())
        for record in records:
            record.request_cancel()
        deadline = time.monotonic() + max(0.0, timeout)
        for record in records:
            if record.thread is not None:
                record.thread.join(max(0.0, deadline - time.monotonic()))
        if self._owns_workspace:
            self.workspace.close()


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("%s=%r는 정수가 아니라서 기본값 %d을 써요", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Access token
# ---------------------------------------------------------------------------


class LoginLimiter:
    """Failed login / token attempts per client key (IP) in a sliding window."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        self.max_failures = max(1, int(max_failures))
        self.window = float(window)
        self.clock = clock
        self.max_keys = max_keys
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float] | None:
        entries = self._failures.get(key)
        if entries is None:
            return None
        while entries and now - entries[0] >= self.window:
            entries.popleft()
        if not entries:
            del self._failures[key]
            return None
        return entries

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again (0 = allowed now)."""
        with self._lock:
            now = self.clock()
            entries = self._recent(key, now)
            if entries is None or len(entries) < self.max_failures:
                return 0.0
            return max(1.0, self.window - (now - entries[-self.max_failures]))

    def fail(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            entries = self._recent(key, now)
            if entries is None:
                entries = self._failures[key] = deque(maxlen=self.max_failures * 2)
            entries.append(now)
            self._failures.move_to_end(key)
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class InsiaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: RunManager, web_root: Path | None, heartbeat: float = 15.0,
                 quiet: bool = True, *, token: str | None = None, public_hosts: Sequence[str] = (),
                 trust_proxy: bool = False) -> None:
        self.bind_host = str(address[0]).strip("[]").lower()
        # Set before binding: a failed bind (port in use) calls server_close(), which needs it.
        self.manager = manager
        super().__init__(address, InsiaHandler)
        self.web_root = web_root.resolve() if web_root is not None and web_root.is_dir() else None
        self.heartbeat = heartbeat
        self.quiet = quiet
        self.token = token
        self.public_hosts = frozenset(public_hosts)
        self.trust_proxy = bool(trust_proxy)
        self.limiter = LoginLimiter()
        self._session = (hmac.new(token.encode("utf-8"), SESSION_CONTEXT, hashlib.sha256).hexdigest()
                         if token else None)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if ":" in str(host):
            host = f"[{host}]"
        return f"http://{host}:{port}/"

    @property
    def token_required(self) -> bool:
        return self.token is not None

    @property
    def session_value(self) -> str | None:
        """What the ``insia_token`` cookie holds: an HMAC of the token (never the token itself)."""
        return self._session

    @staticmethod
    def _same(given: str, expected: str) -> bool:
        """Constant-time comparison that does not reveal the expected length either."""
        digest = lambda text: hashlib.sha256(text.encode("utf-8", "replace")).digest()  # noqa: E731
        return hmac.compare_digest(digest(given), digest(expected))

    def check_bearer(self, value: str) -> bool:
        if self.token is None:
            return True
        return self._same(value, self.token)

    def check_cookie(self, value: str) -> bool:
        if self._session is None:
            return True
        return self._same(value, self._session)

    def allows_host(self, name: str, port: int | None = None) -> bool:
        """True for loopback names, the bind address or a ``--public-host`` name.

        The port is not compared: a browser always sends the port it actually
        connected to, which differs from ours behind ``ssh -L`` / ``docker -p``
        port forwards and reverse proxies, and DNS rebinding is already stopped
        by the name check (a rebinding page carries the attacker's hostname).
        When listening on every interface (``0.0.0.0``), any IP literal is also
        accepted so LAN access keeps working.
        """
        del port  # kept for callers; see docstring
        if name in LOOPBACK_HOSTS or name in self.public_hosts or name in (self.bind_host, str(self.server_address[0]).lower()):
            return True
        if self.bind_host in WILDCARD_HOSTS:
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return False
            return True
        return False

    def server_close(self) -> None:
        super().server_close()
        try:
            self.manager.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("작업 정리 중 오류")


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: str
    auth: bool = True


def _route(method: str, pattern: str, handler: str, *, auth: bool = True) -> Route:
    return Route(method, re.compile(f"^{pattern}/?$"), handler, auth)


ROUTES: tuple[Route, ...] = (
    _route("GET", r"/api/health", "health"),
    _route("POST", r"/api/login", "login", auth=False),
    _route("POST", r"/api/logout", "logout", auth=False),
    _route("GET", r"/api/sample-brief", "sample_brief"),
    _route("GET", r"/api/profile", "get_profile"),
    _route("PUT", r"/api/profile", "put_profile"),
    _route("GET", r"/api/documents", "list_documents"),
    _route("POST", r"/api/documents", "add_document"),
    _route("GET", rf"/api/documents/{ID_SEGMENT}", "get_document"),
    _route("DELETE", rf"/api/documents/{ID_SEGMENT}", "delete_document"),
    _route("GET", r"/api/runs", "list_runs"),
    _route("POST", r"/api/runs", "create_run"),
    _route("GET", rf"/api/runs/{ID_SEGMENT}", "get_run"),
    _route("GET", rf"/api/runs/{ID_SEGMENT}/events", "run_events"),
    _route("GET", rf"/api/runs/{ID_SEGMENT}/export", "export_run"),
    _route("POST", rf"/api/runs/{ID_SEGMENT}/resume", "resume_run"),
    _route("POST", rf"/api/runs/{ID_SEGMENT}/cancel", "cancel_run"),
    _route("GET", r"/api/items", "list_items"),
    _route("GET", rf"/api/items/{ID_SEGMENT}", "get_item"),
    _route("PUT", rf"/api/items/{ID_SEGMENT}/draft", "edit_item"),
    _route("POST", rf"/api/items/{ID_SEGMENT}/review", "review_item"),
    _route("POST", rf"/api/items/{ID_SEGMENT}/revise", "revise_item"),
    _route("POST", rf"/api/items/{ID_SEGMENT}/status", "item_status"),
    _route("GET", rf"/api/items/{ID_SEGMENT}/export", "export_item"),
    _route("GET", r"/api/calendar", "list_calendar"),
    _route("POST", r"/api/calendar/plan", "plan_calendar"),
    _route("POST", rf"/api/calendar/{ID_SEGMENT}/generate", "generate_slot"),
    _route("POST", rf"/api/calendar/{ID_SEGMENT}", "update_slot"),
    _route("GET", r"/api/usage", "usage"),
)


class InsiaHandler(BaseHTTPRequestHandler):
    server: InsiaServer
    server_version = f"INSIA/{__version__}"
    timeout = 120  # seconds a client may stay silent while sending a request

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.server.quiet:
            super().log_message(format, *args)

    # -- response helpers ------------------------------------------------------
    def _send_json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, extra: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None) -> None:
        payload = {"error": message, "status": status}
        payload.update(extra or {})
        self._send_json(status, payload, headers)

    def _send_download(self, exported: ExportFile) -> None:
        self.send_response(200)
        self.send_header("Content-Type", exported.content_type)
        self.send_header("Content-Length", str(len(exported.data)))
        self.send_header("Content-Disposition", exported.content_disposition())
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        if exported.notes:
            self.send_header("X-Insia-Notes", urllib.parse.quote("\n".join(exported.notes), safe=""))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(exported.data)

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urllib.parse.urlsplit(self.path)
        return parts.path, urllib.parse.parse_qs(parts.query)

    # -- request checks --------------------------------------------------------
    def _check_host(self) -> None:
        """Reject requests whose ``Host`` is not allowed (DNS rebinding)."""
        parsed = split_host(self.headers.get("Host") or "")
        if parsed is None or not self.server.allows_host(*parsed):
            self.close_connection = True
            raise RequestError(403, "허용되지 않은 주소(Host)로 들어온 요청이에요. "
                                    "http://127.0.0.1:<포트>/ 또는 http://localhost:<포트>/ 로 접속해 주세요. "
                                    "도메인으로 접속한다면 서버를 --public-host <도메인>으로 시작해 주세요.")

    def _forwarded_https(self) -> bool:
        if not self.server.trust_proxy:
            return False
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _client_ip(self) -> str:
        peer = str(self.client_address[0]) if self.client_address else ""
        if self.server.trust_proxy:
            forwarded = self.headers.get_all("X-Forwarded-For") or []
            if forwarded:
                last = forwarded[-1].split(",")[-1].strip()  # the hop our trusted proxy added
                try:
                    ipaddress.ip_address(last)
                    return last
                except ValueError:
                    pass
        return peer

    def _check_origin(self, *, require_json: bool) -> None:
        """Same-origin ``Origin`` (when sent) and a JSON body type, so other sites cannot change anything."""
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site not in ("same-origin", "none"):
            self.close_connection = True
            raise RequestError(403, "다른 사이트에서 보낸 요청은 받을 수 없어요. 대시보드에서 실행해 주세요.")
        origin = self.headers.get("Origin")
        if origin is not None:
            scheme = "https" if self._forwarded_https() else "http"
            prefix = f"{scheme}://"
            default_port = 443 if scheme == "https" else 80
            # same-origin: Origin must name exactly the host:port this request was sent to
            parsed = split_host(origin[len(prefix):], default_port) if origin.lower().startswith(prefix) else None
            requested = split_host(self.headers.get("Host") or "", default_port)
            if parsed is None or parsed != requested or not self.server.allows_host(parsed[0]):
                self.close_connection = True
                hint = ""
                if origin.lower().startswith("https://") and scheme == "http":
                    hint = " HTTPS 리버스 프록시 뒤에서 쓰고 있다면 서버를 --trust-proxy로 시작해 주세요."
                raise RequestError(403, "다른 사이트에서 보낸 요청은 받을 수 없어요. 대시보드에서 실행해 주세요." + hint)
        if require_json:
            media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self.close_connection = True
                raise RequestError(415, "요청 본문은 Content-Type: application/json으로 보내 주세요")

    def _cookie(self, name: str) -> str | None:
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                key, sep, value = part.strip().partition("=")
                if sep and key.strip() == name:
                    return value.strip().strip('"')
        return None

    def _cookie_header(self, value: str, max_age: int) -> str:
        parts = [f"{COOKIE_NAME}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
        if self._forwarded_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _require_auth(self) -> None:
        srv = self.server
        if srv.token is None:
            return
        key = self._client_ip()
        wait = srv.limiter.retry_after(key)
        if wait:
            raise RequestError(429, f"로그인 시도가 너무 많아요. {math.ceil(wait)}초 뒤에 다시 시도해 주세요.",
                               headers={"Retry-After": str(math.ceil(wait))})
        unauthorized = {"WWW-Authenticate": 'Bearer realm="insia"'}
        auth = self.headers.get("Authorization")
        if auth:
            scheme, _, value = auth.strip().partition(" ")
            if scheme.lower() == "bearer" and value.strip() and srv.check_bearer(value.strip()):
                return
            srv.limiter.fail(key)
            raise RequestError(401, "접근 토큰이 맞지 않아요.", extra={"login": True}, headers=unauthorized)
        cookie = self._cookie(COOKIE_NAME)
        if cookie:
            if srv.check_cookie(cookie):
                return
            srv.limiter.fail(key)
            headers = {**unauthorized, "Set-Cookie": self._cookie_header("", 0)}
            raise RequestError(401, "로그인이 만료됐어요. 접근 토큰을 다시 입력해 주세요.", extra={"login": True}, headers=headers)
        raise RequestError(401, LOGIN_REQUIRED, extra={"login": True}, headers=unauthorized)

    def _read_json(self, limit: int = MAX_BODY, *, required: bool = False, empty_message: str = "JSON 본문을 보내 주세요",
                   type_message: str = "요청 본문은 JSON 객체여야 해요") -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            raise RequestError(411, "Content-Length가 필요해요")
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.close_connection = True
            raise RequestError(400, "Content-Length가 올바르지 않아요") from None
        if length > limit:
            self.close_connection = True
            size = f"{limit // (1024 * 1024)}MB" if limit >= 1024 * 1024 else f"{limit // 1024}KB"
            raise RequestError(413, f"요청 본문이 너무 커요 (최대 {size})")
        if length <= 0:
            if required:
                raise RequestError(400, empty_message)
            return {}
        raw = self.rfile.read(length)
        try:
            # NaN/Infinity are not JSON; deep nesting raises RecursionError.
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):  # includes UnicodeDecodeError and JSONDecodeError
            raise RequestError(400, "JSON 형식이 올바르지 않아요") from None
        if not isinstance(body, dict):
            raise RequestError(400, type_message)
        return body

    # -- verbs -----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS headers: a cross-site preflight always fails.
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        try:
            path, query = self._path()
            if not (path == "/api" or path.startswith("/api/")):
                if method not in ("GET", "HEAD"):
                    raise RequestError(405, "허용되지 않는 요청이에요", headers={"Allow": "GET, HEAD"})
                self._serve_static(path)
                return
            self._check_host()
            lookup = "GET" if method == "HEAD" else method
            matched: tuple[Route, tuple[str, ...]] | None = None
            allowed: list[str] = []
            for route in ROUTES:
                match = route.pattern.match(path)
                if match is None:
                    continue
                allowed.append(route.method)
                if route.method == lookup and matched is None:
                    matched = (route, match.groups())
            if matched is None or matched[0].auth:
                self._require_auth()  # unknown paths need auth too, so they reveal nothing
            if matched is None:
                if allowed:
                    raise RequestError(405, "이 경로에서 지원하지 않는 메서드예요", headers={"Allow": ", ".join(sorted(set(allowed)))})
                raise RequestError(404, "없는 API 경로예요")
            route, params = matched
            if method in ("POST", "PUT", "DELETE"):
                self._check_origin(require_json=method != "DELETE")
            getattr(self, f"_h_{route.handler}")(*params, query=query)
        except RequestError as exc:
            self._error(exc.status, exc.message, exc.extra, exc.headers)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:  # noqa: BLE001 - map library errors, answer 500 instead of dropping the connection
            mapped = self._map_error(exc)
            if mapped is None:
                log.error("%s %s failed:\n%s", method, self.path.split("?", 1)[0], traceback.format_exc())
                self.close_connection = True
                mapped = RequestError(500, "서버에서 요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.")
            try:
                self._error(mapped.status, mapped.message, mapped.extra, mapped.headers)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    @staticmethod
    def _map_error(exc: Exception) -> RequestError | None:
        """Library exceptions → HTTP errors (Korean messages pass through)."""
        if isinstance(exc, ApprovalBlockedError):
            return RequestError(409, str(exc), extra={
                "blocked": True, "can_force": True, "item_id": exc.item_id, "version": exc.version, "score": exc.score,
                "hint": "내용을 직접 확인했다면 force: true로 다시 보내면 그래도 승인돼요 (그래도 승인)."})
        if isinstance(exc, InvalidTransitionError):
            return RequestError(409, str(exc))
        if isinstance(exc, NotFoundError):
            return RequestError(404, str(exc))
        if isinstance(exc, MissingDependencyError):
            return RequestError(501, str(exc), extra={"package": exc.package, "extra": exc.extra})
        if isinstance(exc, ValidationError):
            fields = ", ".join(".".join(str(p) for p in err["loc"]) or "(본문)" for err in exc.errors()[:5])
            return RequestError(400, f"형식이 올바르지 않아요: {fields}")
        if isinstance(exc, (ExportError, PlanningError, WorkspaceError)):
            return RequestError(400, str(exc))
        if isinstance(exc, BackendError):
            return RequestError(502, str(exc) or "AI 백엔드 호출에 실패했어요")
        if isinstance(exc, PipelineError):
            return RequestError(409, str(exc))
        return None

    # -- auth routes -------------------------------------------------------------
    def _h_login(self, *, query: dict[str, list[str]]) -> None:
        srv = self.server
        if srv.token is None:
            self._read_json(MAX_LOGIN_BODY)
            self._send_json(200, {"ok": True, "token_required": False})
            return
        key = self._client_ip()
        wait = srv.limiter.retry_after(key)
        if wait:
            raise RequestError(429, f"로그인 시도가 너무 많아요. {math.ceil(wait)}초 뒤에 다시 시도해 주세요.",
                               headers={"Retry-After": str(math.ceil(wait))})
        body = self._read_json(MAX_LOGIN_BODY, required=True, empty_message="토큰을 입력해 주세요")
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise RequestError(400, "토큰을 입력해 주세요")
        if not srv.check_bearer(token.strip()):
            srv.limiter.fail(key)
            wait = srv.limiter.retry_after(key)
            if wait:
                raise RequestError(429, f"로그인 시도가 너무 많아요. {math.ceil(wait)}초 뒤에 다시 시도해 주세요.",
                                   headers={"Retry-After": str(math.ceil(wait))})
            raise RequestError(401, "토큰이 맞지 않아요.", extra={"login": True})
        srv.limiter.reset(key)
        assert srv.session_value is not None
        self._send_json(200, {"ok": True, "token_required": True},
                        {"Set-Cookie": self._cookie_header(srv.session_value, COOKIE_MAX_AGE)})

    def _h_logout(self, *, query: dict[str, list[str]]) -> None:
        self._read_json(MAX_LOGIN_BODY)
        self._send_json(200, {"ok": True}, {"Set-Cookie": self._cookie_header("", 0)})

    # -- info routes ---------------------------------------------------------------
    def _h_health(self, *, query: dict[str, list[str]]) -> None:
        data = self.server.manager.health()
        data["token_required"] = self.server.token_required
        self._send_json(200, data)

    def _h_sample_brief(self, *, query: dict[str, list[str]]) -> None:
        self._send_json(200, load_sample_brief(self.server.manager.settings).model_dump(mode="json"))

    # -- profile & documents ---------------------------------------------------------
    def _profile_payload(self, profile: Profile) -> dict[str, Any]:
        missing = _profile_missing(profile)
        return {"profile": profile.model_dump(mode="json"), "profile_complete": not missing, "missing": missing,
                "completeness": round(100 * (len(PROFILE_REQUIRED) - len(missing)) / len(PROFILE_REQUIRED))}

    def _h_get_profile(self, *, query: dict[str, list[str]]) -> None:
        self._send_json(200, self._profile_payload(self.server.manager.workspace.get_profile()))

    def _h_put_profile(self, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_PROFILE_BODY, required=True, empty_message="프로필 JSON을 보내 주세요")
        data = body["profile"] if isinstance(body.get("profile"), dict) else body
        data = {k: v for k, v in data.items() if k != "updated_at"}
        profile = Profile.model_validate(data)
        saved = self.server.manager.workspace.save_profile(profile)
        self._send_json(200, self._profile_payload(saved))

    def _h_list_documents(self, *, query: dict[str, list[str]]) -> None:
        docs = self.server.manager.workspace.list_documents()
        with_text = _query_value(query, "text") not in ("0", "false", "no")
        payload = []
        for doc in docs:
            entry = doc.model_dump(mode="json")
            if not with_text:
                entry.pop("text", None)
            payload.append(entry)
        self._send_json(200, {"documents": payload, "total_chars": sum(d.chars for d in docs),
                              "max_document_chars": self.server.manager.settings.max_document_chars})

    def _h_add_document(self, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_DOCUMENT_BODY, required=True, empty_message="자료 JSON을 보내 주세요")
        text = body.get("text")
        if not isinstance(text, str):
            raise RequestError(400, "자료 내용(text)을 문자열로 보내 주세요")
        title = _opt_str(body, "title", limit=1000, what="제목") or ""
        kind = _opt_str(body, "kind", limit=20, what="자료 종류") or "text"
        filename = _opt_str(body, "filename", limit=500, what="파일 이름") or ""
        doc = self.server.manager.workspace.add_document(title, text, kind=kind, filename=filename)
        self._send_json(201, {"document": doc.model_dump(mode="json")})

    def _h_get_document(self, doc_id: str, *, query: dict[str, list[str]]) -> None:
        doc = self.server.manager.workspace.get_document(doc_id)
        if doc is None:
            raise RequestError(404, f"자료 {doc_id}를 찾을 수 없어요")
        self._send_json(200, {"document": doc.model_dump(mode="json")})

    def _h_delete_document(self, doc_id: str, *, query: dict[str, list[str]]) -> None:
        if not self.server.manager.workspace.delete_document(doc_id):
            raise RequestError(404, f"자료 {doc_id}를 찾을 수 없어요")
        self._send_json(200, {"deleted": True, "id": doc_id})

    # -- runs ------------------------------------------------------------------------
    def _h_list_runs(self, *, query: dict[str, list[str]]) -> None:
        limit = _query_int(query, "limit", 50, 1, 200)
        status = _query_value(query, "status")
        if status is not None and status not in RUN_STATUSES:
            raise RequestError(400, f"status는 {', '.join(RUN_STATUSES)} 중 하나여야 해요")
        kind = _query_value(query, "kind")
        if kind is not None and not re.fullmatch(r"[a-z][a-z_]{0,31}", kind):
            raise RequestError(400, "kind 형식이 올바르지 않아요")
        self._send_json(200, {"runs": self.server.manager.list(limit, kind=kind, status=status)})

    def _h_create_run(self, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY, required=True, empty_message="브리프 JSON을 보내 주세요",
                               type_message="브리프는 JSON 객체여야 해요")
        options = parse_options(body.pop("options", None))
        brief_data = body.get("brief") if isinstance(body.get("brief"), dict) else body
        try:
            brief = Brief.model_validate(brief_data)
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors()[:5])
            raise RequestError(400, f"브리프 형식이 올바르지 않아요: {fields}") from None
        if not brief.topic.strip():
            raise RequestError(400, "주제(topic)를 입력해 주세요")
        if not brief.channels:
            raise RequestError(400, "채널을 하나 이상 골라 주세요")
        record = self.server.manager.start(brief, options)
        self._send_json(201, self._run_links(record))

    @staticmethod
    def _run_links(record: RunRecord) -> dict[str, Any]:
        return {
            "run_id": record.run_id,
            "kind": record.kind,
            "mode": record.mode,
            "events_url": f"/api/runs/{record.run_id}/events",
            "status_url": f"/api/runs/{record.run_id}",
            "cancel_url": f"/api/runs/{record.run_id}/cancel",
        }

    def _h_get_run(self, run_id: str, *, query: dict[str, list[str]]) -> None:
        detail = self.server.manager.detail(run_id)
        if detail is None:
            raise RequestError(404, "해당 실행을 찾을 수 없어요")
        self._send_json(200, detail)

    def _h_resume_run(self, run_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY)
        options = parse_options(body.get("options"))
        record = self.server.manager.resume(run_id, force=_opt_bool(body, "force"), options=options)
        self._send_json(202, {**self._run_links(record), "resumed": True})

    def _h_cancel_run(self, run_id: str, *, query: dict[str, list[str]]) -> None:
        self._read_json(MAX_BODY)
        record = self.server.manager.cancel(run_id)
        self._send_json(202, {"run_id": record.run_id, "status": "cancelling", "cancel_requested": True,
                              "events_url": f"/api/runs/{record.run_id}/events"})

    def _h_export_run(self, run_id: str, *, query: dict[str, list[str]]) -> None:
        manager = self.server.manager
        if manager.workspace.get_run(run_id) is None:
            raise RequestError(404, "해당 실행을 찾을 수 없어요")
        exported = export_run_zip(manager.workspace, run_id)
        if _query_bool(query, "info"):
            self._send_json(200, self._export_info(exported))
            return
        self._send_download(exported)

    def _h_run_events(self, run_id: str, *, query: dict[str, list[str]]) -> None:
        manager = self.server.manager
        after_text = self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0]
        try:
            after = max(0, int(after_text))
        except ValueError:
            after = 0
        row = manager.workspace.get_run(run_id)
        record = manager.get(run_id)
        if row is None and record is None:
            raise RequestError(404, "해당 실행을 찾을 수 없어요")
        active = manager.active_record(run_id) is not None
        if not active and (row is None or row["status"] != "running"):
            pending = manager.workspace.list_events(run_id, after) if row is not None else []
            if row is None and record is not None:
                pending = [e for e in record.bus.events if e["seq"] > after]
            if not pending:
                # Nothing left to send; 204 tells EventSource to stop reconnecting.
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            for event in manager.iter_events(run_id, after, heartbeat=self.server.heartbeat):
                if event is None:
                    chunk = b": ping\n\n"
                else:
                    data = json.dumps(event, ensure_ascii=False)
                    chunk = f"id: {event['seq']}\ndata: {data}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            return
        except WorkspaceError:  # the server is shutting down
            return

    # -- items -------------------------------------------------------------------------
    def _h_list_items(self, *, query: dict[str, list[str]]) -> None:
        limit = _query_int(query, "limit", 200, 1, 5000)
        items = self.server.manager.workspace.list_items(status=_query_value(query, "status"),
                                                         channel=_query_value(query, "channel"), limit=limit)
        self._send_json(200, {"items": [i.model_dump(mode="json") for i in items]})

    def _exports_for(self, item_id: str, channel: str) -> list[dict[str, Any]]:
        caps = capabilities()
        out = []
        for fmt in formats_for(channel):
            available = caps.get("docx", False) if fmt == "docx" else True
            entry: dict[str, Any] = {"format": fmt, "label": format_label(fmt, channel), "available": available,
                                     "url": f"/api/items/{urllib.parse.quote(item_id)}/export?format={fmt}"}
            if fmt == "docx" and not available:
                entry["hint"] = 'Word 파일을 만들려면 pip install "insia-smartagent[export]"로 python-docx를 설치해 주세요.'
            if fmt == "zip" and channel == "instagram" and not caps.get("png", False):
                entry["hint"] = "Playwright가 없어서 PNG 대신 인쇄용 slides.html을 넣어요."
            out.append(entry)
        return out

    def _h_get_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        detail = self.server.manager.workspace.get_item(item_id)
        if detail is None:
            raise RequestError(404, f"콘텐츠 {item_id}를 찾을 수 없어요")
        payload = detail.model_dump(mode="json")
        payload["exports"] = self._exports_for(item_id, detail.item.channel)
        self._send_json(200, payload)

    def _h_edit_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_DRAFT_BODY, required=True, empty_message="수정한 제목과 본문을 보내 주세요")
        manager = self.server.manager
        if manager.workspace.get_item(item_id) is None:
            raise RequestError(404, f"콘텐츠 {item_id}를 찾을 수 없어요")
        title = body.get("title")
        content = body.get("content")
        if not isinstance(title, str) or not isinstance(content, str):
            raise RequestError(400, "제목(title)과 본문(content)을 문자열로 보내 주세요")
        hashtags = body.get("hashtags")
        if hashtags is not None and not (isinstance(hashtags, str) or (
                isinstance(hashtags, list) and all(isinstance(t, str) for t in hashtags) and len(hashtags) <= 100)):
            raise RequestError(400, "해시태그(hashtags)는 문자열 목록이어야 해요")
        result = actions.edit_item(manager.workspace, item_id, title, content, hashtags, settings=manager.settings)
        self._send_json(200, result.to_dict())

    def _job_options(self, body: dict[str, Any]) -> dict[str, Any]:
        return parse_options(body.get("options"))

    def _h_review_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY)
        record = self.server.manager.review(item_id, self._job_options(body))
        self._send_json(201, {**self._run_links(record), "item_id": item_id})

    def _h_revise_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY)
        instructions = _opt_str(body, "instructions", limit=actions.MAX_INSTRUCTIONS_CHARS * 2, what="수정 지시") or ""
        record = self.server.manager.revise(item_id, instructions, self._job_options(body))
        self._send_json(201, {**self._run_links(record), "item_id": item_id})

    def _h_item_status(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY, required=True, empty_message="바꿀 상태(status)를 보내 주세요")
        status = body.get("status")
        if not isinstance(status, str) or not status.strip():
            raise RequestError(400, "바꿀 상태(status)를 보내 주세요")
        item = self.server.manager.workspace.set_item_status(
            item_id, status.strip(),
            scheduled_at=_opt_str(body, "scheduled_at", limit=40, what="게시 예정일"),
            published_url=_opt_str(body, "published_url", limit=2000, what="게시 URL"),
            note=_opt_str(body, "note", limit=2000, what="메모"),
            force=_opt_bool(body, "force"))
        self._send_json(200, {"item": item.model_dump(mode="json")})

    @staticmethod
    def _export_info(exported: ExportFile) -> dict[str, Any]:
        return {"filename": exported.filename, "content_type": exported.content_type, "size": exported.size,
                "notes": list(exported.notes)}

    def _h_export_item(self, item_id: str, *, query: dict[str, list[str]]) -> None:
        manager = self.server.manager
        detail = manager.workspace.get_item(item_id)
        if detail is None:
            raise RequestError(404, f"콘텐츠 {item_id}를 찾을 수 없어요")
        fmt = _query_value(query, "format") or formats_for(detail.item.channel)[0]
        version = _query_value(query, "version")
        number: int | None = None
        if version is not None:
            try:
                number = int(version.lstrip("vV"))
            except ValueError:
                raise RequestError(400, "version은 버전 번호(예: 2)여야 해요") from None
        profile = manager.workspace.get_profile()
        exported = export_item(detail, fmt, None if profile_is_empty(profile) else profile, version=number)
        if _query_bool(query, "info"):
            self._send_json(200, self._export_info(exported))
            return
        self._send_download(exported)

    # -- calendar ----------------------------------------------------------------------
    def _h_list_calendar(self, *, query: dict[str, list[str]]) -> None:
        ws = self.server.manager.workspace
        date_from, date_to = _query_value(query, "from"), _query_value(query, "to")
        slots = ws.list_slots(date_from=date_from, date_to=date_to)
        linked = {s.item_id for s in slots if s.item_id}
        statuses: dict[str, str] = {}
        if linked:
            statuses = {item.id: item.status for item in ws.list_items(limit=5000) if item.id in linked}
        payload = []
        for slot in slots:
            entry = slot.model_dump(mode="json")
            entry["item_status"] = statuses.get(slot.item_id) if slot.item_id else None
            payload.append(entry)
        self._send_json(200, {"slots": payload, "from": date_from, "to": date_to})

    def _h_plan_calendar(self, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY, required=True, empty_message="계획 조건(theme, start, counts)을 보내 주세요")
        theme = _opt_str(body, "theme", limit=1000, what="주제") or ""
        start = body.get("start")
        if not isinstance(start, str) or not start.strip():
            raise RequestError(400, "시작일(start)을 YYYY-MM-DD 형식으로 보내 주세요")
        end = body.get("end")
        if end is not None and not isinstance(end, str):
            raise RequestError(400, "종료일(end)은 YYYY-MM-DD 문자열이어야 해요")
        if not end:
            days = body.get("days", 7)
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
                raise RequestError(400, "days는 1~31 사이의 정수여야 해요")
            try:
                end = (date.fromisoformat(start.strip()) + timedelta(days=days - 1)).isoformat()
            except ValueError:
                raise RequestError(400, f"시작일 '{start}'을(를) 읽을 수 없어요. YYYY-MM-DD 형식으로 적어 주세요.") from None
        counts = body.get("counts")
        if not isinstance(counts, dict) or not counts:
            raise RequestError(400, '채널별 개수(counts)를 {"naver_blog": 2, "linkedin": 1} 같은 형식으로 보내 주세요')
        plan, mode = self.server.manager.plan_week(theme, start.strip(), end.strip(), counts, parse_options(body.get("options")))
        self._send_json(201, {"summary": plan.summary, "slots": [s.model_dump(mode="json") for s in plan.slots],
                              "notices": list(plan.notices), "mode": mode, "start": start.strip(), "end": end.strip()})

    def _h_generate_slot(self, slot_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY)
        manager = self.server.manager
        record = manager.generate_slot(slot_id, force=_opt_bool(body, "force"), options=self._job_options(body))
        slot = manager.workspace.get_slot(slot_id)
        payload: dict[str, Any] = {**self._run_links(record), "slot_id": slot_id}
        if slot is not None:
            # The job claims the slot as it starts; the item id is known in advance.
            payload["item_id"] = pipeline_item_id(record.run_id, slot.channel)
            payload["slot"] = slot.model_copy(update={"status": "generating", "run_id": record.run_id}).model_dump(mode="json")
        self._send_json(201, payload)

    def _h_update_slot(self, slot_id: str, *, query: dict[str, list[str]]) -> None:
        body = self._read_json(MAX_BODY, required=True, empty_message="바꿀 내용(date, topic, status …)을 보내 주세요")
        ws = self.server.manager.workspace
        allowed = {"date", "topic", "angle", "keywords", "goal", "status"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise RequestError(400, f"바꿀 수 없는 항목이에요: {', '.join(unknown)} (가능: {', '.join(sorted(allowed))})")
        slot = ws.get_slot(slot_id)
        if slot is None:
            raise RequestError(404, f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
        if slot.status == "generating":
            raise RequestError(409, "초안을 만드는 중인 슬롯은 바꿀 수 없어요. 끝난 뒤 다시 시도해 주세요.")
        fields: dict[str, Any] = {}
        for key in ("date", "topic", "angle", "goal"):
            value = _opt_str(body, key, limit=500)
            if value is not None:
                fields[key] = value
        if body.get("keywords") is not None:
            keywords = body["keywords"]
            if isinstance(keywords, str):
                keywords = [k for k in keywords.split(",")]
            if not isinstance(keywords, list) or len(keywords) > 20 or not all(isinstance(k, str) and len(k) <= 100 for k in keywords):
                raise RequestError(400, "keywords는 문자열 목록이어야 해요 (최대 20개)")
            fields["keywords"] = keywords
        status = body.get("status")
        if status is not None:
            if status not in ("planned", "skipped"):
                raise RequestError(400, "슬롯 상태는 planned(계획) 또는 skipped(건너뛰기)로만 바꿀 수 있어요")
            if status == "planned" and slot.status == "drafted":
                raise RequestError(409, "이미 초안이 있는 슬롯이에요. 다시 만들려면 초안 만들기를 force: true로 요청해 주세요.")
            fields["status"] = status
        updated = ws.update_slot(slot_id, **fields)
        self._send_json(200, {"slot": updated.model_dump(mode="json")})

    # -- usage -------------------------------------------------------------------------
    def _h_usage(self, *, query: dict[str, list[str]]) -> None:
        manager = self.server.manager
        summary = manager.workspace.usage_summary(since=_query_value(query, "since"), until=_query_value(query, "until"))
        for entry in summary.get("runs") or []:
            run = manager.workspace.get_run(entry["run_id"]) if entry.get("run_id") else None
            entry["started_at"] = run["created_at"] if run else entry.get("first_at")
            entry["status"] = run["status"] if run else None
            entry["mode"] = run["mode"] if run else None
        summary["budget_usd"] = float(manager.settings.max_cost_usd or 0.0)
        summary["currency"] = "USD"
        self._send_json(200, summary)

    # -- static files ------------------------------------------------------------------
    def _static_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "same-origin")

    def _serve_static(self, url_path: str) -> None:
        root = self.server.web_root
        if root is None:
            if url_path in ("/", "/index.html"):
                body = NO_WEB_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._static_headers()
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            raise RequestError(404, "파일을 찾을 수 없어요")
        target = resolve_static(root, url_path)
        if target is None:
            raise RequestError(403, "접근할 수 없는 경로예요")
        if not target.is_file():
            raise RequestError(404, "파일을 찾을 수 없어요")
        self._send_file(target)

    def _send_file(self, target: Path) -> None:
        size = target.stat().st_size
        ctype = MIME_OVERRIDES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            parsed = parse_range(range_header, size)
            if parsed is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self._static_headers()
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def resolve_static(root: Path, url_path: str) -> Path | None:
    """Map a URL path to a file under ``root``; ``None`` if it would escape.

    Rejects ``..`` segments (also percent-encoded), backslashes, NUL bytes,
    dotfiles and anything that resolves outside ``root`` (e.g. via symlink).
    """
    decoded = urllib.parse.unquote(url_path)
    if "\x00" in decoded or "\\" in decoded:
        return None
    parts = [p for p in decoded.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        return None
    root = root.resolve()
    target = root.joinpath(*parts) if parts else root
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved != root and not resolved.is_relative_to(root):
        return None
    if resolved.is_dir():
        # index.html itself may be a symlink, so resolve and check containment again.
        try:
            resolved = (resolved / "index.html").resolve()
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(root):
            return None
    return resolved


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.group(1), match.group(2)
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:  # suffix range: last N bytes
        count = int(last)
        if count == 0:
            return None
        start, end = max(0, size - count), size - 1
    if start > end or start >= size:
        return None
    return start, end


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765, web_dir: str | Path | None = None,
                heartbeat: float = 15.0, quiet: bool = True, *, token: str | None = None,
                public_hosts: Sequence[str] | str = (), trust_proxy: bool = False, workspace: Workspace | None = None,
                max_live: int | None = None, max_mock: int | None = None) -> InsiaServer:
    """Build the server (not started: call ``serve_forever``).

    ``token`` defaults to env ``INSIA_ACCESS_TOKEN``; empty ``public_hosts`` to
    env ``INSIA_PUBLIC_HOSTS`` (comma-separated) and ``trust_proxy=False`` to env
    ``INSIA_TRUST_PROXY`` (``1``/``true``), so a container can be configured
    with environment variables alone. Raises ``ServerConfigError`` (a
    ``ValueError`` with a Korean message) when a non-loopback ``host`` or any
    ``public_hosts`` entry has no token, the token is too weak, a ``public_hosts`` entry is invalid or the
    workspace cannot be opened; ``OSError`` when the port cannot be bound.
    """
    token = check_token(token if token is not None and str(token).strip() else os.environ.get("INSIA_ACCESS_TOKEN"))
    if not public_hosts:
        public_hosts = [h for h in (os.environ.get("INSIA_PUBLIC_HOSTS") or "").split(",") if h.strip()]
    if not trust_proxy:
        trust_proxy = (os.environ.get("INSIA_TRUST_PROXY") or "").strip().lower() in ("1", "true", "yes", "on")
    hosts = normalize_public_hosts(public_hosts)
    if not is_loopback_bind(host) and token is None:
        raise ServerConfigError(
            f"{host or '모든 네트워크 주소'}에서 서버를 열려면 접근 토큰이 필요해요. INSIA_ACCESS_TOKEN 환경 변수나 --token으로 "
            f"{MIN_TOKEN_CHARS}자 이상의 토큰을 정해 주세요. 내 PC에서만 쓴다면 --host 127.0.0.1(기본값)로 실행하세요.")
    if hosts and token is None:  # a reverse proxy makes even a loopback bind reachable from outside
        raise ServerConfigError(
            "도메인(--public-host / INSIA_PUBLIC_HOSTS)으로 열면 리버스 프록시를 거쳐 바깥에서 접속할 수 있어서 접근 토큰이 "
            f"꼭 필요해요. INSIA_ACCESS_TOKEN 환경 변수나 --token으로 {MIN_TOKEN_CHARS}자 이상의 토큰을 정해 주세요.")
    web_root = Path(web_dir) if web_dir is not None else settings.web_dir
    try:
        manager = RunManager(settings, workspace=workspace, max_live=max_live, max_mock=max_mock)
    except WorkspaceError as exc:
        raise ServerConfigError(str(exc)) from None
    try:
        return InsiaServer((host, port), manager, web_root, heartbeat=heartbeat, quiet=quiet, token=token,
                           public_hosts=hosts, trust_proxy=trust_proxy)
    except BaseException:
        manager.shutdown(timeout=0)
        raise


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765, web_dir: str | Path | None = None,
          quiet: bool = True, *, token: str | None = None, public_hosts: Sequence[str] | str = (),
          trust_proxy: bool = False) -> None:
    """Run the server until Ctrl+C (raises ``ServerConfigError`` / ``OSError`` like ``make_server``)."""
    server = make_server(settings, host, port, web_dir, quiet=quiet, token=token, public_hosts=public_hosts,
                         trust_proxy=trust_proxy)
    health = server.manager.health()
    print(f"INSIA 에이전트 스튜디오: {server.url}")
    print(f"기본 모드: {health['mode']} · 모델: {health['model']} · 대시보드 폴더: {server.web_root or '(없음)'}")
    print(f"워크스페이스: {health['workspace']}")
    if server.token_required:
        print("접근 토큰: 켜짐 (대시보드에서 토큰으로 로그인해요)")
    if server.public_hosts:
        print(f"허용한 도메인: {', '.join(sorted(server.public_hosts))}")
    if server.manager.interrupted_on_start:
        print(f"지난번에 끝나지 못한 실행 {server.manager.interrupted_on_start}개를 '중단됨'으로 정리했어요. "
              "대시보드나 'insia resume <run_id>'로 이어서 실행할 수 있어요.")
    print("종료하려면 Ctrl+C를 누르세요.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n서버를 종료해요.")
    finally:
        server.server_close()
