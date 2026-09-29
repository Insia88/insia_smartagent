"""Public images for Instagram (DESIGN.md 3-3, 4-2-3): staging, the public folder, the media-only listener.

Instagram has no image upload: it fetches every carousel image from a public HTTPS URL. INSIA therefore

* renders the JPEGs once at preview time into ``publish/staging/<preview_id>/NN.jpg`` (never re-rendered: the
  sha256 of those bytes is part of the confirmed preview);
* when a confirmed send starts, copies them to ``publish/public/<token>/NN.jpg`` (``token`` = 128 random bits, 32
  lower-case hex characters, ASCII only) and writes ``.expires`` (unix seconds, now + 24 h) **last**;
* serves them from the **media-only listener** (``start_media_listener``): ``GET``/``HEAD`` of exactly
  ``/pub/m/<32 hex>/<NN>.jpg`` while ``.expires`` lies in the future — decided from the file system alone, with no
  ``Workspace``, no cookies, no API (a tunnel that exposes the whole port still exposes nothing else). Every other
  path is an empty 404 (other methods 405, malformed requests an empty 4xx), more than 30 such error answers a
  minute from one client get 429, and quiet or slow clients are dropped (socket timeout, send deadline, connection
  cap);
* when the attempt ends, first sets ``.expires`` to ``0`` (so the files stop being served even if deleting fails)
  and then deletes the folder; ``unknown`` attempts keep theirs for 24 hours (a container's lifetime).

``jpeg_info`` checks the JPEG markers without any imaging library (baseline only, no progressive, no MPO).
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .base import MEDIA_PATH_PATTERN
from .redact import get_logger

log = get_logger(__name__)

MEDIA_TTL_SECONDS = 24 * 3600            # public folder lifetime (= Instagram container lifetime)
STAGING_MAX_AGE_SECONDS = 2 * 3600       # staged preview images older than this are always removed
UNFINISHED_PUBLISH_GRACE_SECONDS = 120  # a public folder without .expires younger than this is still being written
NOT_FOUND_LIMIT = 30                     # error answers (404, 405, malformed) per client per minute on the listener
NOT_FOUND_WINDOW_SECONDS = 60.0
# The listener faces the internet (through a tunnel or proxy): a client that goes quiet mid-request is dropped after
# MEDIA_SOCKET_TIMEOUT_SECONDS of silence, one answer may take at most MEDIA_SEND_DEADLINE_SECONDS, and at most
# MEDIA_MAX_CONNECTIONS connections are served at once (more are closed unanswered), so idle or slow clients can never
# pile up threads.
MEDIA_SOCKET_TIMEOUT_SECONDS = 10.0
MEDIA_SEND_DEADLINE_SECONDS = 60.0
MEDIA_SEND_CHUNK_BYTES = 64 * 1024
MEDIA_MAX_CONNECTIONS = 64
EXPIRES_FILE = ".expires"
_MEDIA_PATH = re.compile(MEDIA_PATH_PATTERN)
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_PREVIEW_ID = re.compile(r"^pv_[0-9a-f]{24}$")


# ---------------------------------------------------------------------------
# JPEG check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JpegInfo:
    width: int
    height: int
    baseline: bool          # SOF0/SOF1 (sequential DCT)
    progressive: bool       # SOF2 and other non-baseline frames
    has_mpf: bool           # APP2 "MPF" segment: an MPO (multi-picture) file

    @property
    def ratio(self) -> float:
        return self.width / self.height if self.height else 0.0


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_STANDALONE = {0x01, *range(0xD0, 0xD8)}


def jpeg_info(data: bytes) -> JpegInfo:
    """Parse the JPEG markers up to the first scan. ``ValueError`` (Korean) when ``data`` is not a JPEG or has no
    frame header. Instagram accepts baseline JPEG only (no MPO/JPS, IG §4.2)."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("JPEG 파일이 아니에요.")
    pos = 2
    size: tuple[int, int] | None = None
    marker_kind: int | None = None
    has_mpf = False
    length = len(data)
    while pos < length:
        if data[pos] != 0xFF:
            raise ValueError("JPEG 마커가 올바르지 않아요.")
        while pos < length and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= length:
            break
        marker = data[pos]
        pos += 1
        if marker in _STANDALONE:
            continue
        if marker == 0xD9:  # EOI
            break
        if pos + 2 > length:
            raise ValueError("JPEG 파일이 잘렸어요.")
        seg_len = (data[pos] << 8) | data[pos + 1]
        if seg_len < 2 or pos + seg_len > length:
            raise ValueError("JPEG 파일이 잘렸어요.")
        segment = data[pos + 2: pos + seg_len]
        if marker == 0xE2 and segment[:4] == b"MPF\x00":
            has_mpf = True
        elif marker in _SOF_MARKERS and size is None:
            if len(segment) < 5:
                raise ValueError("JPEG 프레임 정보가 올바르지 않아요.")
            height = (segment[1] << 8) | segment[2]
            width = (segment[3] << 8) | segment[4]
            size, marker_kind = (width, height), marker
        if marker == 0xDA:  # start of scan: the headers are over
            break
        pos += seg_len
    if size is None or marker_kind is None:
        raise ValueError("JPEG 프레임 정보(SOF)를 찾지 못했어요.")
    baseline = marker_kind in (0xC0, 0xC1)
    return JpegInfo(width=size[0], height=size[1], baseline=baseline, progressive=not baseline, has_mpf=has_mpf)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def new_media_token() -> str:
    """128 random bits as 32 lower-case hex characters (the public folder name)."""
    return secrets.token_hex(16)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    _write_private(tmp, text.encode("ascii"))
    os.replace(tmp, path)


def image_name(n: int) -> str:
    return f"{int(n):02d}.jpg"


def public_url(base_url: str, token: str, n: int) -> str:
    """``<base>/pub/m/<token>/<NN>.jpg`` (ASCII only, IG §1 #12)."""
    return f"{base_url.rstrip('/')}/pub/m/{token}/{image_name(n)}"


class PublicMediaHost:
    """Staging and public folders under ``<workspace>/publish`` (created on first use, 0700)."""

    def __init__(self, publish_dir: Path | str, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(publish_dir)
        self.staging_root = self.root / "staging"
        self.public_root = self.root / "public"
        self._now = now

    # staging (preview images, 0700)
    def staging_dir(self, preview_id: str) -> Path:
        if not _PREVIEW_ID.match(preview_id or ""):
            raise ValueError("미리보기 id 형식이 올바르지 않아요.")
        return self.staging_root / preview_id

    def write_staging(self, preview_id: str, images: Sequence[bytes]) -> list[dict[str, Any]]:
        """Store the rendered JPEGs as ``01.jpg`` …; returns ``[{n, sha256, bytes}]``."""
        folder = self.staging_dir(preview_id)
        _mkdir_private(self.root)
        _mkdir_private(self.staging_root)
        _mkdir_private(folder)
        out = []
        for n, data in enumerate(images, start=1):
            _write_private(folder / image_name(n), data)
            out.append({"n": n, "sha256": sha256_hex(data), "bytes": len(data)})
        return out

    def staged_image(self, preview_id: str, n: int) -> bytes | None:
        try:
            path = self.staging_dir(preview_id) / image_name(n)
        except ValueError:
            return None
        if not 1 <= int(n) <= 99:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def remove_staging(self, preview_id: str) -> bool:
        try:
            folder = self.staging_dir(preview_id)
        except ValueError:
            return False
        if not folder.exists():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()

    # public (served to Instagram)
    def publish(self, preview_id: str, token: str, expected_sha256: Sequence[str], *,
                ttl_seconds: float = MEDIA_TTL_SECONDS) -> int:
        """Copy the staged images to ``public/<token>/`` after checking each sha256, then write ``.expires`` last.
        ``ValueError`` (Korean) when a staged image is missing or changed; nothing is served then."""
        if not _TOKEN.match(token or ""):
            raise ValueError("공개 폴더 이름 형식이 올바르지 않아요.")
        folder = self.public_root / token
        _mkdir_private(self.root)
        _mkdir_private(self.public_root)
        _mkdir_private(folder)
        for n, digest in enumerate(expected_sha256, start=1):
            data = self.staged_image(preview_id, n)
            if data is None or sha256_hex(data) != digest:
                shutil.rmtree(folder, ignore_errors=True)
                raise ValueError(f"미리보기 이미지 {n}번이 없거나 바뀌었어요. 다시 확인해 주세요.")
            _write_private(folder / image_name(n), data)
        _atomic_write(folder / EXPIRES_FILE, str(int(self._now() + ttl_seconds)))
        return len(expected_sha256)

    def expire(self, token: str) -> bool:
        """Stop serving at once (``.expires`` → ``0``), then delete the folder. True when the folder is gone."""
        if not _TOKEN.match(token or ""):
            return False
        folder = self.public_root / token
        if not folder.exists():
            return True
        try:
            _atomic_write(folder / EXPIRES_FILE, "0")
        except OSError:
            log.warning("공개 이미지 폴더의 만료 표시를 바꾸지 못했어요: %s…", token[:6])
        shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()

    def cleanup(self, *, finished_tokens: Sequence[str] = (), dead_previews: Sequence[str] = (),
                keep_previews: Sequence[str] = ()) -> dict[str, int]:
        """Remove public folders of finished attempts and every public folder whose ``.expires`` passed, staging
        folders of expired/used previews (``dead_previews``, except ``keep_previews``) and staging folders older than
        ``STAGING_MAX_AGE_SECONDS``. Returns counts."""
        removed = {"staging": 0, "public": 0}
        now = self._now()
        for token in dict.fromkeys(finished_tokens):
            if (self.public_root / token).exists() and self.expire(token):
                removed["public"] += 1
        if self.public_root.is_dir():
            for folder in self.public_root.iterdir():
                if not folder.is_dir() or not _TOKEN.match(folder.name):
                    continue
                expires = _read_expires(folder / EXPIRES_FILE)
                if expires is None:
                    # publish() writes the images first and ``.expires`` last: a young folder without it is being
                    # filled right now (a send in progress), so leave it; an old one is a leftover from a crash
                    try:
                        age = now - folder.stat().st_mtime
                    except OSError:
                        continue
                    if age < UNFINISHED_PUBLISH_GRACE_SECONDS:
                        continue
                if expires is None or expires <= now:
                    if self.expire(folder.name):
                        removed["public"] += 1
        keep = set(keep_previews)
        for preview_id in dict.fromkeys(dead_previews):
            if preview_id not in keep and self.remove_staging(preview_id):
                removed["staging"] += 1
        if self.staging_root.is_dir():
            for folder in self.staging_root.iterdir():
                if not folder.is_dir() or folder.name in keep:
                    continue
                try:
                    age = now - folder.stat().st_mtime
                except OSError:
                    continue
                if age > STAGING_MAX_AGE_SECONDS:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed["staging"] += 1
        return removed


def _read_expires(path: Path) -> int | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(21)
    except OSError:
        return None
    if len(raw) > 20:
        return None
    try:
        return int(raw.strip() or b"0")
    except ValueError:
        return None


def resolve_public_file(public_root: Path | str, url_path: str, now: float | None = None) -> Path | None:
    """The file to serve for ``url_path`` or ``None`` (→ 404 without body). File system only: the path must match
    ``/pub/m/<32 hex>/<NN>.jpg`` exactly, the image must be a regular file (no symlink) and the folder's ``.expires``
    must lie in the future."""
    match = _MEDIA_PATH.match(url_path or "")
    if match is None:
        return None
    token, number = match.group(1), match.group(2)
    folder = Path(public_root) / token
    path = folder / f"{number}.jpg"
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    expires = _read_expires(folder / EXPIRES_FILE)
    if expires is None or expires <= (time.time() if now is None else now):
        return None
    return path


# ---------------------------------------------------------------------------
# Media-only listener
# ---------------------------------------------------------------------------


def client_key(address: str) -> str:
    """Rate-limit key: the IPv4 address, or the /64 prefix of an IPv6 address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class NotFoundLimiter:
    """At most ``limit`` 404 answers per client key in a sliding ``window``; 200 answers are never counted."""

    def __init__(self, limit: int = NOT_FOUND_LIMIT, window: float = NOT_FOUND_WINDOW_SECONDS,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self._now = limit, window, now
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # forget quiet clients
                for other in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(other, None)
            return True


_MEDIA_HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                  ("X-Robots-Tag", "noindex, nofollow"), ("Referrer-Policy", "no-referrer"))


class MediaRequestHandler(BaseHTTPRequestHandler):
    """Serves one kind of file and nothing else. No workspace, no cookies, no API, no directory listing."""

    server_version = "INSIA-media"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = MEDIA_SOCKET_TIMEOUT_SECONDS  # StreamRequestHandler: every read and write on the socket (TimeoutError → dropped)

    def version_string(self) -> str:
        return self.server_version

    def _empty(self, status: int, extra: Sequence[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        for name, value in (*_MEDIA_HEADERS, *extra):
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _refuse(self, status: int, extra: Sequence[tuple[str, str]] = ()) -> None:
        """Every error answer (404, 405, malformed request) counts against the client's limit (over it → 429) and
        closes the connection (an unread request body never becomes the next request)."""
        limiter: NotFoundLimiter = self.server.limiter  # type: ignore[attr-defined]
        if not limiter.allow(client_key(self.client_address[0])):
            status, extra = 429, (("Retry-After", "60"),)
        self._empty(status, (*extra, ("Connection", "close")))
        self.close_connection = True

    def _send_body(self, data: bytes) -> None:
        """Write in chunks (each write waits at most the socket timeout) within an overall deadline, so a client
        that reads very slowly cannot keep the thread for long."""
        deadline = time.monotonic() + MEDIA_SEND_DEADLINE_SECONDS
        view = memoryview(data)
        for start in range(0, len(view), MEDIA_SEND_CHUNK_BYTES):
            if time.monotonic() > deadline:
                self.close_connection = True
                return
            self.wfile.write(view[start:start + MEDIA_SEND_CHUNK_BYTES])

    def _serve(self, head: bool) -> None:
        if self.request_version == "HTTP/0.9":  # no status line or headers would go out: answer nothing
            self.close_connection = True
            return
        path = self.path.split("?", 1)[0]
        target = resolve_public_file(self.server.media_root, path)  # type: ignore[attr-defined]
        if target is None:
            self._refuse(404)
            return
        try:
            with open(target, "rb") as handle:
                data = handle.read()
        except OSError:
            self._empty(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        for name, value in _MEDIA_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self._send_body(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._serve(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head=True)

    def _not_allowed(self) -> None:
        self._refuse(405, (("Allow", "GET, HEAD"),))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed  # noqa: N815

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """No HTML error pages on this socket: malformed requests (bad request line or version, too long a URI or
        header) get an empty answer and the connection is closed; an unknown method is a 405 like every other
        method but GET/HEAD. Both count against the client's error limit."""
        if self.request_version in ("", "HTTP/0.9"):
            # the request line never got as far as a valid version (http.server's default is HTTP/0.9, which
            # would silence the status line): answer as HTTP/1.1
            self.request_version = self.protocol_version
        try:
            if code == 501:
                self._refuse(405, (("Allow", "GET, HEAD"),))
            else:
                self.close_connection = True
                self._refuse(code)
        except OSError:
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        """Only the first 6 characters of a token ever reach the log. ``path`` and ``command`` are not set yet
        when a malformed request line is answered."""
        path = getattr(self, "path", "")
        match = _MEDIA_PATH.match(path.split("?", 1)[0]) if isinstance(path, str) else None
        shown = f"/pub/m/{match.group(1)[:6]}…/{match.group(2)}.jpg" if match else "(other)"
        code = args[1] if format.startswith('"%s"') and len(args) > 1 else ""
        log.debug("media %s %s %s", getattr(self, "command", "") or "-", shown, code)


class _MediaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False  # close() never waits for a client; the handler threads are daemons

    def __init__(self, address: tuple[str, int], media_root: Path, family: int) -> None:
        self.address_family = family
        self.media_root = media_root
        self.limiter = NotFoundLimiter()
        self.slots = threading.BoundedSemaphore(MEDIA_MAX_CONNECTIONS)
        super().__init__(address, MediaRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):  # too many open connections: close this one unanswered
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Never print a traceback to stderr for what a client did (dropped connections, resets); a real bug is one
        warning line without request details (the path may hold a token) plus the traceback at debug level."""
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            log.warning("미디어 요청을 처리하지 못했어요: %s", type(exc).__name__)
        log.debug("media request failed", exc_info=True)


class MediaListener:
    """A running media-only listener (``close()`` stops it)."""

    def __init__(self, server: _MediaServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                       name="insia-media-listener", daemon=True)
        self.thread.start()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def close(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()


def start_media_listener(host: str, port: int, media_root: Path | str) -> MediaListener:
    """Open the media-only listener on ``host:port`` serving ``media_root`` (``publish/public``). ``OSError`` when the
    port cannot be opened (the caller then marks Instagram unavailable and the server keeps running)."""
    bind = (host or "127.0.0.1").strip("[]")
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    server = _MediaServer((bind, int(port)), Path(media_root), family)
    return MediaListener(server)


__all__ = [
    "EXPIRES_FILE", "MEDIA_MAX_CONNECTIONS", "MEDIA_SEND_DEADLINE_SECONDS", "MEDIA_SOCKET_TIMEOUT_SECONDS",
    "MEDIA_TTL_SECONDS", "NOT_FOUND_LIMIT", "STAGING_MAX_AGE_SECONDS",
    "JpegInfo", "MediaListener", "MediaRequestHandler", "NotFoundLimiter", "PublicMediaHost", "client_key",
    "image_name", "jpeg_info", "new_media_token", "public_url", "resolve_public_file", "sha256_hex",
    "start_media_listener",
]
