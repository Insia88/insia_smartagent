"""Public media for Instagram (package A): folders, ``.expires``, cleanup, the media-only listener and the public
URL self-check (DESIGN.md 3-3, 4-2-3). Listener tests bind port 0 on loopback."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from insia_agents.publishers import FakeTransport, HumanConfirmation, Response, TransportError, json_response
import insia_agents.publishers.media as media_module
from insia_agents.publishers.media import (
    EXPIRES_FILE,
    MEDIA_SOCKET_TIMEOUT_SECONDS,
    NOT_FOUND_LIMIT,
    MediaRequestHandler,
    PublicMediaHost,
    new_media_token,
    public_url,
    resolve_public_file,
    start_media_listener,
)

pytestmark = pytest.mark.usefixtures("no_network")

PREVIEW = "pv_" + "a" * 24


def _host(tmp_path, images=(b"\xff\xd8one", b"\xff\xd8two")):
    host = PublicMediaHost(tmp_path / "publish")
    rows = host.write_staging(PREVIEW, list(images))
    token = new_media_token()
    host.publish(PREVIEW, token, [r["sha256"] for r in rows])
    return host, token


def test_token_and_url_shape(tmp_path):
    token = new_media_token()
    assert re.fullmatch(r"[0-9a-f]{32}", token) and new_media_token() != token
    assert public_url("https://media.example.com/", token, 3) == f"https://media.example.com/pub/m/{token}/03.jpg"


def test_publish_checks_the_staged_bytes(tmp_path):
    host = PublicMediaHost(tmp_path / "publish")
    rows = host.write_staging(PREVIEW, [b"\xff\xd8one"])
    (host.staging_dir(PREVIEW) / "01.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="바뀌었어요"):
        host.publish(PREVIEW, new_media_token(), [rows[0]["sha256"]])
    assert not any((host.public_root).iterdir())  # nothing is served for a changed image


PATH_TRICKS = ("/pub/m/{t}/../01.jpg", "/pub/m/{T}/01.jpg", "/pub/m/{t}0/01.jpg", "/pub/m/{t}/1.jpg", "/pub/m/{t}/001.jpg",
               "/pub/m/%2e%2e/01.jpg", "/pub/m/{t}/%2e%2e", "/pub/m/{t}/01.png", "/pub/m/{t}/.expires", "/pub/{t}/01.jpg",
               "/pub/m/{t}/01.jpg/", "//pub/m/{t}/01.jpg")


def test_path_tricks_are_refused(tmp_path):
    host, token = _host(tmp_path)
    assert resolve_public_file(host.public_root, f"/pub/m/{token}/01.jpg") is not None
    for path in PATH_TRICKS:
        assert resolve_public_file(host.public_root, path.format(t=token, T=token.upper())) is None, path


def test_expired_folders_are_not_served_and_expire_first_marks_zero(tmp_path, monkeypatch):
    host, token = _host(tmp_path)
    good = f"/pub/m/{token}/02.jpg"
    assert resolve_public_file(host.public_root, good, now=time.time()) is not None
    assert resolve_public_file(host.public_root, good, now=time.time() + 25 * 3600) is None  # 24 hours at most
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # deleting fails (e.g. a Windows file lock)
    assert host.expire(token) is False
    assert (host.public_root / token / EXPIRES_FILE).read_text() == "0"
    assert resolve_public_file(host.public_root, good) is None  # …but it is already not served


def test_cleanup_removes_finished_expired_and_old_staging(tmp_path):
    clock = {"now": time.time()}
    host = PublicMediaHost(tmp_path / "publish", now=lambda: clock["now"])
    rows = host.write_staging(PREVIEW, [b"\xff\xd8a"])
    finished, live, old = new_media_token(), new_media_token(), new_media_token()
    for token in (finished, live):
        host.publish(PREVIEW, token, [rows[0]["sha256"]])
    host.publish(PREVIEW, old, [rows[0]["sha256"]], ttl_seconds=-1)
    other = "pv_" + "b" * 24
    host.write_staging(other, [b"x"])
    removed = host.cleanup(finished_tokens=[finished], dead_previews=[other], keep_previews=[PREVIEW])
    assert removed == {"staging": 1, "public": 2}
    assert sorted(p.name for p in host.public_root.iterdir()) == [live]
    clock["now"] += 3 * 3600  # staged images older than two hours always go (unless kept for a running send)
    assert host.cleanup(keep_previews=[PREVIEW])["staging"] == 0
    assert host.cleanup()["staging"] == 1
    clock["now"] += 24 * 3600
    assert host.cleanup()["public"] == 1 and not any(host.public_root.iterdir())


def _get(url, method="GET"):
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test server
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def listener(tmp_path, monkeypatch):
    import insia_agents.db as db

    def no_workspace(*args, **kwargs):
        raise AssertionError("the media listener must never open the workspace")

    monkeypatch.setattr(db.Workspace, "__init__", no_workspace)
    host, token = _host(tmp_path)
    running = start_media_listener("127.0.0.1", 0, host.public_root)
    yield f"http://127.0.0.1:{running.address[1]}", token
    running.close()


def test_media_listener_serves_only_public_images(listener):
    base, token = listener
    status, headers, body = _get(f"{base}/pub/m/{token}/01.jpg")
    assert status == 200 and body == b"\xff\xd8one" and headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"] == "no-store" and headers["X-Robots-Tag"] == "noindex, nofollow"
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"
    status, headers, body = _get(f"{base}/pub/m/{token}/02.jpg", method="HEAD")
    assert status == 200 and body == b"" and headers["Content-Length"] == "5"
    for path in ("/api/health", "/", "/oauth/linkedin/callback?code=x&state=y", "/index.html", f"/pub/m/{token}/09.jpg"):
        status, _, body = _get(base + path)
        assert (status, body) == (404, b""), path
    for method in ("POST", "PUT", "DELETE", "PROPFIND"):
        status, headers, body = _get(f"{base}/pub/m/{token}/01.jpg", method=method)
        assert (status, body) == (405, b"") and headers["Allow"] == "GET, HEAD"


def test_media_listener_limits_404s(listener):
    base, token = listener
    statuses = [_get(f"{base}/nothing-{n}")[0] for n in range(NOT_FOUND_LIMIT + 1)]
    assert statuses[:NOT_FOUND_LIMIT] == [404] * NOT_FOUND_LIMIT and statuses[-1] == 429
    status, headers, _ = _get(f"{base}/again")
    assert status == 429 and headers["Retry-After"] == "60"
    assert _get(f"{base}/pub/m/{token}/01.jpg")[0] == 200  # found files are never limited


def _raw(address, data: bytes = b"", *, timeout: float = 3.0) -> bytes:
    """Send raw bytes and read until the server closes the connection (``TimeoutError`` if it never does). A reset
    (the server closed with request bytes still unread) ends the answer like a normal close."""
    out = b""
    with socket.create_connection(address, timeout=timeout) as sock:
        try:
            if data:
                sock.sendall(data)
            while chunk := sock.recv(65536):
                out += chunk
        except (BrokenPipeError, ConnectionResetError):
            pass
    return out


@pytest.fixture
def media_server(tmp_path):
    host, token = _host(tmp_path)
    running = start_media_listener("127.0.0.1", 0, host.public_root)
    yield running, token
    running.close()


def test_malformed_requests_get_an_empty_answer_without_a_traceback(media_server, caplog, capfd):
    running, token = media_server
    caplog.set_level(logging.DEBUG, logger="insia_agents.publishers.media")
    cases = [(b"GET /x FOO/1.1\r\n\r\n", 400), (b"GARBAGE\r\n\r\n", 400), (b"GET / HTTP/2.0\r\n\r\n", 505),
             (b"GET /" + b"a" * 70_000 + b" HTTP/1.1\r\n\r\n", 414), (b"FOO /pub/m/x HTTP/1.1\r\nHost: a\r\n\r\n", 405),
             (b"GET /pub/m/" + token.encode() + b"/01.jpg HTTP/1.1\r\nHost: a\r\n" + b"X: y\r\n" * 120 + b"\r\n", 431)]
    for request, code in cases:
        answer = _raw(running.address, request)
        head, _, body = answer.partition(b"\r\n\r\n")
        assert head.startswith(f"HTTP/1.1 {code} ".encode()), (request[:30], answer[:80])
        assert b"Content-Length: 0" in head and body == b"" and b"<" not in answer, request[:30]
        assert b"Server: INSIA-media\r\n" in head
    assert _raw(running.address, f"GET /pub/m/{token}/01.jpg\r\n\r\n".encode()) == b""  # HTTP/0.9: nothing at all
    assert "Traceback" not in capfd.readouterr().err
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert token not in caplog.text and token[:6] in caplog.text  # the log shows 6 characters of a token, never more


def test_malformed_requests_count_against_the_error_limit(media_server):
    running, token = media_server
    for _ in range(NOT_FOUND_LIMIT):
        assert _raw(running.address, b"GARBAGE\r\n\r\n").startswith(b"HTTP/1.1 400 ")
    assert _raw(running.address, b"GET /x HTTP/1.1\r\nConnection: close\r\n\r\n").startswith(b"HTTP/1.1 429 ")
    answer = _raw(running.address, f"GET /pub/m/{token}/01.jpg HTTP/1.1\r\nConnection: close\r\n\r\n".encode())
    assert answer.startswith(b"HTTP/1.1 200 ") and answer.endswith(b"\xff\xd8one")


def test_quiet_clients_are_dropped_and_connections_are_capped(tmp_path, monkeypatch):
    assert MediaRequestHandler.timeout == MEDIA_SOCKET_TIMEOUT_SECONDS and 0 < MEDIA_SOCKET_TIMEOUT_SECONDS <= 30
    monkeypatch.setattr(MediaRequestHandler, "timeout", 0.25)
    host, token = _host(tmp_path)
    running = start_media_listener("127.0.0.1", 0, host.public_root)
    running.server.slots = threading.BoundedSemaphore(2)
    try:
        quiet = [socket.create_connection(running.address, timeout=3) for _ in range(2)]
        for sock in quiet:
            sock.sendall(b"GET /pub/m/")  # half a request line, then silence
        started = time.monotonic()
        request = f"GET /pub/m/{token}/01.jpg HTTP/1.1\r\nConnection: close\r\n\r\n".encode()
        assert _raw(running.address, request) == b""  # a third connection is closed unanswered while two are open
        for sock in quiet:
            assert sock.recv(100) == b""  # dropped after the socket timeout (before: held open forever)
            sock.close()
        assert time.monotonic() - started < 2.0
        assert _raw(running.address, request).startswith(b"HTTP/1.1 200 ")  # the slots came back
        monkeypatch.setattr(media_module, "MEDIA_SEND_DEADLINE_SECONDS", -1.0)  # past the deadline: no body
        answer = _raw(running.address, f"GET /pub/m/{token}/01.jpg HTTP/1.1\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 200 ") and answer.endswith(b"\r\n\r\n")
    finally:
        running.close()


# ---------------------------------------------------------------------------
# self-check and the files of a send
# ---------------------------------------------------------------------------


def _ig(kit, **env):
    fake = FakeTransport()
    service = kit.service({"INSIA_PUBLISH_SKIP_SELF_CHECK": "0", **env}, transport=fake, instagram=True)
    kit.connect_instagram(service)
    fake.add("GET", r"/v25\.0/me$", json_response(200, {"user_id": kit.IG_ID, "username": "a"}), repeat=True)
    fake.add("GET", r"/content_publishing_limit$",
             json_response(200, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}), repeat=True)
    return service, fake


def test_self_check(publish_kit):
    service, fake = _ig(publish_kit)
    data = b"\xff\xd8real"
    url = "https://media.example.com/pub/m/" + "c" * 32 + "/01.jpg"
    digest = hashlib.sha256(data).hexdigest()
    cases = [
        (Response(200, {"content-type": "image/jpeg"}, data), True),
        (Response(302, {"location": "https://elsewhere.example/x.jpg"}), False),  # redirects fail (IG-4 unverified)
        (Response(200, {"content-type": "text/html"}, b"<html>"), False),
        (Response(200, {"content-type": "image/jpeg"}, b"\xff\xd8else"), False),
        (Response(404), False),
        (TransportError("refused", sent="no"), False),
    ]
    for answer, ok in cases:
        fake.add("GET", r"https://media\.example\.com/pub/m/", answer)
        if ok:
            service.instagram._self_check([(url, digest)])
            continue
        with pytest.raises(Exception) as caught:
            service.instagram._self_check([(url, digest)])
        assert caught.value.outcome.error_code == "hosting" and "https://media.example.com" in caught.value.outcome.error


def _send(kit, service, fake, *, serve=True):
    item_id = kit.item("instagram")
    preview = service.preview(item_id, options={"is_ai_generated": False}, via="dashboard", requested_by="d")
    home = kit.home

    def serve_media(request):
        match = re.search(r"/pub/m/([0-9a-f]{32})/(\d\d)\.jpg$", request.url)
        path = home / "publish" / "public" / match.group(1) / f"{match.group(2)}.jpg"
        return Response(200, {"content-type": "image/jpeg"}, path.read_bytes())

    fake.add("GET", r"https://media\.example\.com/pub/m/", serve_media if serve else Response(403), repeat=True)
    return service.send(HumanConfirmation(via="dashboard", requested_by="d", preview_id=preview.preview_id,
                                          preview_hash=preview.preview_hash), background=False)


def test_failed_self_check_creates_nothing_and_deletes_the_files(publish_kit):
    service, fake = _ig(publish_kit)
    attempt = _send(publish_kit, service, fake, serve=False)
    assert attempt.status == "failed" and attempt.error_code == "hosting"
    assert fake.calls("POST") == []  # no container was created
    assert not any((publish_kit.home / "publish" / "public").iterdir())
    assert not any((publish_kit.home / "publish" / "staging").iterdir())


def test_failed_send_after_the_self_check_deletes_the_public_files(publish_kit):
    service, fake = _ig(publish_kit)
    fake.add("POST", r"/media$", json_response(400, {"error": {"code": 25, "error_subcode": 2207050}}))
    attempt = _send(publish_kit, service, fake)
    assert attempt.status == "failed" and "제한된 상태" in attempt.error
    assert len(fake.calls("GET", r"media\.example\.com")) == 7  # every image was checked first
    assert not any((publish_kit.home / "publish" / "public").iterdir())


def test_skip_self_check(publish_kit):
    service, fake = _ig(publish_kit, INSIA_PUBLISH_SKIP_SELF_CHECK="1")
    fake.add("POST", r"/media$", json_response(400, {"error": {"code": 25, "error_subcode": 2207050}}))
    _send(publish_kit, service, fake)
    assert fake.calls("GET", r"media\.example\.com") == []


def test_config_b_serves_from_the_main_port_only_there(publish_kit):
    main = publish_kit.service({"INSIA_PUBLISH_INSTAGRAM": "1", "INSIA_MEDIA_BASE_URL": "https://dash.example.com"},
                               public_hosts=("dash.example.com",))
    assert main.settings.media_mode == "main"
    host = main.media
    rows = host.write_staging(PREVIEW, [b"\xff\xd8b"])
    token = new_media_token()
    host.publish(PREVIEW, token, [rows[0]["sha256"]])
    assert main.public_media_file(f"/pub/m/{token}/01.jpg") is not None
    assert main.public_media_file(f"/pub/m/{token}/01.jpg?x") is None
    listener_mode = publish_kit.service(instagram=True)
    assert listener_mode.settings.media_mode == "listener"
    assert listener_mode.public_media_file(f"/pub/m/{token}/01.jpg") is None  # config A: never on the main port
    address = listener_mode.start_media_listener("127.0.0.1")
    assert address is not None and listener_mode.status()["media"]["listening"] is True
