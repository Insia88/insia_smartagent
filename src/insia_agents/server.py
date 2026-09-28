"""Local dashboard server (stdlib ``ThreadingHTTPServer``, no framework).

- ``/`` and other paths: static files from ``web/`` (safe path resolution,
  no traversal, no dotfiles, single-range requests for videos).
- ``GET  /api/health``           → ``{mode, model, version, live_available}``
- ``GET  /api/sample-brief``     → the sample Brief
- ``POST /api/runs``             → start a run: Brief JSON (+ optional ``options``:
  ``mode``, ``speed`` = mock playback multiplier (1 = recorded pace, 2 = twice
  as fast, 0 = no waiting), ``max_rounds``, ``pass_score``)
- ``GET  /api/runs``             → recent runs
- ``GET  /api/runs/<id>``        → status + RunResult when finished
- ``GET  /api/runs/<id>/events`` → Server-Sent Events: replay, then live;
  ``: ping`` heartbeat; closes after ``run.completed`` / ``run.failed``.

Every ``/api`` request must carry a loopback / bind-address ``Host`` header
(blocks DNS rebinding). ``POST`` additionally needs a same-origin ``Origin`` (when
present) and ``Content-Type: application/json``, which forces a CORS preflight
that this server never approves, so other web pages cannot start runs.
"""

from __future__ import annotations

import ipaddress
import json
import math
import mimetypes
import re
import threading
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .backends.base import BackendError
from .config import MIN_SPEED, Settings, has_credentials, load_sample_brief, resolve_mode
from .models import Brief, RunResult
from .pipeline import new_run_id, prepare_run, run_pipeline

MAX_BODY = 64 * 1024
MAX_RECORDS = 50
RUN_PATH = re.compile(r"^/api/runs/([A-Za-z0-9][A-Za-z0-9._-]{0,80})(/events)?/?$")
HOST_HEADER = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9.-]+))(?::(?P<port>[0-9]{1,5}))?$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})

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
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    run_id: str
    brief: Brief
    options: dict[str, Any]
    bus: Any
    mode: str
    model: str
    created_at: str = field(default_factory=_iso_now)
    status: str = "running"
    finished_at: str | None = None
    result: RunResult | None = None
    error: str | None = None
    thread: threading.Thread | None = None

    def summary(self) -> dict[str, Any]:
        scores = None
        if self.result is not None:
            scores = {r.channel: next((v.score for v in r.reviews if v.round == r.final.round), 0) for r in self.result.results}
        return {
            "run_id": self.run_id, "status": self.status, "mode": self.mode, "model": self.model,
            "created_at": self.created_at, "finished_at": self.finished_at, "topic": self.brief.topic,
            "channels": list(self.brief.channels), "events": len(self.bus), "scores": scores, "error": self.error,
        }

    def detail(self) -> dict[str, Any]:
        data = self.summary()
        data.update({
            "brief": self.brief.model_dump(mode="json"),
            "options": self.options,
            "result": self.result.model_dump(mode="json") if self.result is not None else None,
        })
        return data


def parse_options(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RequestError(400, "options는 객체여야 해요")
    out: dict[str, Any] = {}
    if raw.get("mode") is not None:
        if raw["mode"] not in ("auto", "live", "mock"):
            raise RequestError(400, "options.mode는 auto, live, mock 중 하나여야 해요")
        out["mode"] = raw["mode"]
    for key, kind, lo, hi in (("speed", float, 0, 100), ("max_rounds", int, 0, 5), ("pass_score", int, 0, 100)):
        if raw.get(key) is None:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestError(400, f"options.{key}는 숫자여야 해요")
        if isinstance(value, float) and not math.isfinite(value):
            raise RequestError(400, f"options.{key}는 유한한 숫자여야 해요")
        # Range first: int/float comparison is exact, so a huge int never reaches float().
        if not lo <= value <= hi:
            raise RequestError(400, f"options.{key}는 {lo}~{hi} 사이여야 해요")
        if key == "speed" and 0 < value < MIN_SPEED:
            raise RequestError(400, f"options.speed는 0(기다리지 않음) 또는 {MIN_SPEED}~100 사이여야 해요")
        if kind is int and isinstance(value, float) and not value.is_integer():
            raise RequestError(400, f"options.{key}는 정수여야 해요")
        out[key] = kind(value)
    return out


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


class RunManager:
    def __init__(self, settings: Settings, max_active: int = 4) -> None:
        self.settings = settings
        self.max_active = max_active
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        mode, _ = resolve_mode(self.settings.mode)
        return {"mode": mode, "model": self.settings.model, "version": __version__,
                "default_mode": self.settings.mode, "live_available": has_credentials()}

    def start(self, brief: Brief, options: dict[str, Any]) -> RunRecord:
        if options.get("mode") == "live" and not has_credentials():
            raise RequestError(400, "API 키가 없어 live 모드를 쓸 수 없어요. ANTHROPIC_API_KEY를 설정한 뒤 서버를 다시 시작해 주세요.")
        try:
            settings = self.settings.with_options(**options)
            backend, bus, note = prepare_run(settings, run_id=new_run_id())
        except (ValueError, BackendError) as exc:
            raise RequestError(400, str(exc)) from exc
        record = RunRecord(run_id=bus.run_id, brief=brief, options=options, bus=bus, mode=backend.name, model=backend.model)

        def work() -> None:
            try:
                record.result = run_pipeline(brief, backend, bus, settings, out_dir=settings.out_dir, mode_note=note)
                record.model = record.result.model
                record.status = "completed"
            except BaseException as exc:  # noqa: BLE001 - the thread must record every failure
                record.status = "failed"
                record.error = str(exc) or type(exc).__name__
            finally:
                record.finished_at = _iso_now()

        record.thread = threading.Thread(target=work, name=f"insia-run-{record.run_id}", daemon=True)
        with self._lock:
            # Check and insert in one critical section so a burst cannot exceed max_active.
            active = sum(1 for r in self._runs.values() if r.status == "running")
            if active >= self.max_active:
                raise RequestError(429, "동시에 실행할 수 있는 작업 수를 넘었어요. 잠시 후 다시 시도해 주세요.")
            self._runs[record.run_id] = record
            self._trim()
        record.thread.start()
        return record

    def _trim(self) -> None:
        if len(self._runs) <= MAX_RECORDS:
            return
        for run_id in [rid for rid, r in self._runs.items() if r.status != "running"][: len(self._runs) - MAX_RECORDS]:
            del self._runs[run_id]

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._runs.values())
        return [r.summary() for r in reversed(records)]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class InsiaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: RunManager, web_root: Path | None, heartbeat: float = 15.0,
                 quiet: bool = True) -> None:
        self.bind_host = str(address[0]).strip("[]").lower()
        super().__init__(address, InsiaHandler)
        self.manager = manager
        self.web_root = web_root.resolve() if web_root is not None and web_root.is_dir() else None
        self.heartbeat = heartbeat
        self.quiet = quiet

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/"

    def allows_host(self, name: str, port: int | None = None) -> bool:
        """True for loopback names or the bind address.

        The port is not compared: a browser always sends the port it actually
        connected to, which differs from ours behind ``ssh -L`` / ``docker -p``
        port forwards, and DNS rebinding is already stopped by the name check
        (a rebinding page carries the attacker's hostname). When listening on
        every interface (``0.0.0.0``), any IP literal is also accepted so LAN
        access keeps working.
        """
        del port  # kept for callers; see docstring
        if name in LOOPBACK_HOSTS or name in (self.bind_host, str(self.server_address[0]).lower()):
            return True
        if self.bind_host in WILDCARD_HOSTS:
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return False
            return True
        return False


class InsiaHandler(BaseHTTPRequestHandler):
    server: InsiaServer
    server_version = f"INSIA/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.server.quiet:
            super().log_message(format, *args)

    # -- helpers --------------------------------------------------------------
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

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message, "status": status})

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parts = urllib.parse.urlsplit(self.path)
        return parts.path, urllib.parse.parse_qs(parts.query)

    def _check_host(self) -> None:
        """Reject requests whose ``Host`` is not loopback / the bind address (DNS rebinding)."""
        parsed = split_host(self.headers.get("Host") or "")
        if parsed is None or not self.server.allows_host(*parsed):
            self.close_connection = True
            raise RequestError(403, "허용되지 않은 주소(Host)로 들어온 요청이에요. "
                                    "http://127.0.0.1:<포트>/ 또는 http://localhost:<포트>/ 로 접속해 주세요.")

    def _check_post_headers(self) -> None:
        """Same-origin ``Origin`` (when sent) and a JSON body type, so other sites cannot POST."""
        origin = self.headers.get("Origin")
        if origin is not None:
            # same-origin: Origin must name exactly the host:port this request was sent to
            parsed = split_host(origin[len("http://"):]) if origin.lower().startswith("http://") else None
            requested = split_host(self.headers.get("Host") or "")
            if parsed is None or parsed != requested or not self.server.allows_host(parsed[0]):
                self.close_connection = True
                raise RequestError(403, "다른 사이트에서 보낸 요청은 받을 수 없어요. 대시보드에서 실행해 주세요.")
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self.close_connection = True
            raise RequestError(415, "요청 본문은 Content-Type: application/json으로 보내 주세요")

    # -- verbs ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except RequestError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_host()
            path, _ = self._path()
            if path.rstrip("/") != "/api/runs":
                raise RequestError(404, "없는 API 경로예요")
            self._check_post_headers()
            self._create_run()
        except RequestError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001 - answer with 500 instead of dropping the connection
            self.log_error("POST %s failed:\n%s", self.path, traceback.format_exc())
            self.close_connection = True
            self._error(500, "서버에서 요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.end_headers()

    # -- routes ---------------------------------------------------------------
    def _route_get(self) -> None:
        path, query = self._path()
        manager = self.server.manager
        if path == "/api" or path.startswith("/api/"):
            self._check_host()
        if path == "/api/health":
            self._send_json(200, manager.health())
            return
        if path == "/api/sample-brief":
            self._send_json(200, load_sample_brief(manager.settings).model_dump(mode="json"))
            return
        if path.rstrip("/") == "/api/runs":
            self._send_json(200, {"runs": manager.list()})
            return
        match = RUN_PATH.match(path)
        if match:
            record = manager.get(match.group(1))
            if record is None:
                raise RequestError(404, "해당 실행을 찾을 수 없어요")
            if match.group(2):
                self._stream_events(record, query)
            else:
                self._send_json(200, record.detail())
            return
        if path.startswith("/api/"):
            raise RequestError(404, "없는 API 경로예요")
        self._serve_static(path)

    def _create_run(self) -> None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise RequestError(411, "Content-Length가 필요해요")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError(400, "Content-Length가 올바르지 않아요") from None
        if length > MAX_BODY:
            self.close_connection = True
            raise RequestError(413, f"요청 본문이 너무 커요 (최대 {MAX_BODY // 1024}KB)")
        if length <= 0:
            raise RequestError(400, "브리프 JSON을 보내 주세요")
        raw = self.rfile.read(length)
        try:
            # NaN/Infinity are not JSON; deep nesting raises RecursionError.
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):  # includes UnicodeDecodeError and JSONDecodeError
            raise RequestError(400, "JSON 형식이 올바르지 않아요") from None
        if not isinstance(body, dict):
            raise RequestError(400, "브리프는 JSON 객체여야 해요")
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
        self._send_json(201, {
            "run_id": record.run_id,
            "mode": record.mode,
            "events_url": f"/api/runs/{record.run_id}/events",
            "status_url": f"/api/runs/{record.run_id}",
        })

    def _stream_events(self, record: RunRecord, query: dict[str, list[str]]) -> None:
        after_text = self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0]
        try:
            after = max(0, int(after_text))
        except ValueError:
            after = 0
        bus = record.bus
        if bus.closed and after >= bus.last_seq():
            # Nothing left to send; 204 tells EventSource to stop reconnecting.
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            for event in bus.subscribe(after_seq=after, heartbeat=self.server.heartbeat):
                if event is None:
                    chunk = b": ping\n\n"
                else:
                    data = json.dumps(event, ensure_ascii=False)
                    chunk = f"id: {event['seq']}\ndata: {data}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    # -- static files ---------------------------------------------------------
    def _serve_static(self, url_path: str) -> None:
        root = self.server.web_root
        if root is None:
            if url_path in ("/", "/index.html"):
                body = NO_WEB_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
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
        self.send_header("X-Content-Type-Options", "nosniff")
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
                heartbeat: float = 15.0, quiet: bool = True) -> InsiaServer:
    web_root = Path(web_dir) if web_dir is not None else settings.web_dir
    return InsiaServer((host, port), RunManager(settings), web_root, heartbeat=heartbeat, quiet=quiet)


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765, web_dir: str | Path | None = None,
          quiet: bool = True) -> None:
    server = make_server(settings, host, port, web_dir, quiet=quiet)
    health = server.manager.health()
    print(f"INSIA 에이전트 스튜디오: {server.url}")
    print(f"기본 모드: {health['mode']} · 모델: {health['model']} · 대시보드 폴더: {server.web_root or '(없음)'}")
    print("종료하려면 Ctrl+C를 누르세요.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n서버를 종료해요.")
    finally:
        server.server_close()
