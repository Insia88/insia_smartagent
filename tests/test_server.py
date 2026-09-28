from __future__ import annotations

import http.client
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from insia_agents import server as server_module
from insia_agents.models import Brief
from insia_agents.server import RequestError, RunManager, make_server, parse_options, parse_range, resolve_static


@pytest.fixture
def web_dir(tmp_path) -> Path:
    root = tmp_path / "site" / "web"
    (root / "assets" / "models").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>INSIA 에이전트 스튜디오</title>", encoding="utf-8")
    (root / "assets" / "models" / "orchestrator.glb").write_bytes(b"glTF" + bytes(range(60)))
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "site" / "secret.txt").write_text("top secret", encoding="utf-8")
    return root


@pytest.fixture
def settings(settings, tmp_path, monkeypatch):
    """The server persists everything: keep its workspace in a temp dir, never ./workspace."""
    for name in ("INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_MAX_LIVE_JOBS", "INSIA_MAX_MOCK_JOBS"):
        monkeypatch.delenv(name, raising=False)  # a token in the developer's shell must not switch on token mode
    return replace(settings, home=tmp_path / "workspace")


@pytest.fixture
def server(settings, web_dir):
    srv = make_server(settings, host="127.0.0.1", port=0, web_dir=web_dir, heartbeat=0.2)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp, payload


def read_sse(srv, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    events, ids = [], []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8").rstrip("\n")
        if line.startswith("id: "):
            ids.append(int(line[4:]))
        if line.startswith("data: "):
            event = json.loads(line[6:])
            events.append(event)
            if event["type"] in ("run.completed", "run.failed"):
                break
    conn.close()
    return events, ids


def test_health_and_sample_brief(server):
    resp, body = request(server, "GET", "/api/health")
    health = json.loads(body)
    assert resp.status == 200 and health["mode"] == "mock" and health["version"] and health["model"]
    resp, body = request(server, "GET", "/api/sample-brief")
    assert resp.status == 200 and json.loads(body)["topic"]


def test_run_lifecycle_with_sse(server):
    brief = {"topic": "테스트 주제", "channels": ["linkedin", "instagram"], "keywords": ["AI 에이전트"],
             "options": {"mode": "mock", "speed": 0, "max_rounds": 2, "pass_score": 80}}
    resp, body = request(server, "POST", "/api/runs", brief)
    assert resp.status == 201, body
    created = json.loads(body)
    run_id = created["run_id"]
    assert created["events_url"] == f"/api/runs/{run_id}/events"

    events, ids = read_sse(server, created["events_url"])
    assert events[0]["type"] == "run.started" and events[-1]["type"] == "run.completed"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)) == ids
    assert {e["data"]["channel"] for e in events if e["type"] == "channel.completed"} == {"linkedin", "instagram"}

    # resume from the middle with Last-Event-ID
    tail, _ = read_sse(server, created["events_url"], headers={"Last-Event-ID": str(len(events) - 3)})
    assert [e["seq"] for e in tail] == [len(events) - 2, len(events) - 1, len(events)]
    # nothing left after completion → 204 so EventSource stops reconnecting
    resp, _ = request(server, "GET", created["events_url"], headers={"Last-Event-ID": str(len(events))})
    assert resp.status == 204

    for _ in range(50):  # the worker thread records the result right after run.completed
        resp, body = request(server, "GET", f"/api/runs/{run_id}")
        detail = json.loads(body)
        if detail["status"] != "running":
            break
        threading.Event().wait(0.05)
    assert detail["status"] == "completed" and detail["result"]["run_id"] == run_id
    assert set(detail["scores"]) == {"linkedin", "instagram"}
    resp, body = request(server, "GET", "/api/runs")
    assert run_id in [r["run_id"] for r in json.loads(body)["runs"]]


def test_brief_wrapped_in_brief_key_is_accepted(server):
    resp, body = request(server, "POST", "/api/runs", {"brief": {"topic": "감싼 브리프", "channels": ["instagram"]},
                                                       "options": {"speed": 0}})
    assert resp.status == 201, body
    events, _ = read_sse(server, json.loads(body)["events_url"])
    assert events[-1]["type"] == "run.completed"


@pytest.mark.parametrize("payload, status", [
    ({"topic": "", "channels": ["linkedin"]}, 400),
    ({"topic": "t", "channels": ["tiktok"]}, 400),
    ({"topic": "t", "channels": []}, 400),
    ({"topic": "t", "options": {"speed": "fast"}}, 400),
    ({"topic": "t", "options": {"max_rounds": 9}}, 400),
    ({"topic": "t", "options": {"mode": "live"}}, 400),  # no credentials in tests
    ([1, 2], 400),
])
def test_invalid_requests(server, payload, status):
    resp, body = request(server, "POST", "/api/runs", payload)
    assert resp.status == status
    assert json.loads(body)["error"]


def test_body_limits_and_unknown_routes(server):
    resp, _ = request(server, "POST", "/api/runs", b"{" + b" " * (64 * 1024 + 10) + b"}")
    assert resp.status == 413
    resp, _ = request(server, "POST", "/api/runs", b"not json")
    assert resp.status == 400
    resp, _ = request(server, "GET", "/api/runs/does-not-exist")
    assert resp.status == 404
    resp, _ = request(server, "GET", "/api/runs/does-not-exist/events")
    assert resp.status == 404
    resp, _ = request(server, "GET", "/api/nope")
    assert resp.status == 404


def test_static_files_and_traversal(server):
    resp, body = request(server, "GET", "/")
    assert resp.status == 200 and "INSIA" in body.decode("utf-8")
    assert resp.getheader("Content-Type").startswith("text/html")
    resp, body = request(server, "GET", "/assets/models/orchestrator.glb")
    assert resp.status == 200 and resp.getheader("Content-Type") == "model/gltf-binary" and body.startswith(b"glTF")
    resp, body = request(server, "GET", "/assets/models/orchestrator.glb", headers={"Range": "bytes=0-3"})
    assert resp.status == 206 and body == b"glTF" and resp.getheader("Content-Range") == "bytes 0-3/64"
    resp, _ = request(server, "HEAD", "/index.html")
    assert resp.status == 200
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt", "/.env", "/assets%5c..%5c..%5csecret.txt"):
        resp, body = request(server, "GET", path)
        assert resp.status in (403, 404), path
        assert b"secret" not in body.lower().replace(b"secret.txt", b""), path
    resp, _ = request(server, "GET", "/missing.js")
    assert resp.status == 404


def test_resolve_static_and_range_helpers(web_dir):
    assert resolve_static(web_dir, "/") == (web_dir / "index.html").resolve()
    assert resolve_static(web_dir, "/../secret.txt") is None
    assert resolve_static(web_dir, "/a/%2e%2e/%2e%2e/secret.txt") is None
    assert resolve_static(web_dir, "/.env") is None
    assert parse_range("bytes=10-", 100) == (10, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=200-300", 100) is None
    assert parse_range("items=0-1", 100) is None


def test_symlinked_index_html_cannot_escape_root(web_dir):
    sub = web_dir / "sub"
    sub.mkdir()
    (sub / "index.html").symlink_to(Path("..") / ".." / "secret.txt")
    assert resolve_static(web_dir, "/sub/") is None
    assert resolve_static(web_dir, "/sub") is None
    inner = web_dir / "inner"
    inner.mkdir()
    (inner / "index.html").symlink_to(Path("..") / "index.html")  # a symlink that stays inside root is fine
    assert resolve_static(web_dir, "/inner/") == (web_dir / "index.html").resolve()


# -- cross-site protection (DNS rebinding / CSRF) ------------------------------


@pytest.mark.parametrize("path", ["/api/health", "/api/runs", "/api/sample-brief", "/api/runs/x/events"])
def test_api_rejects_foreign_host_header(server, path):
    port = server.server_address[1]
    for host in ("attacker.example", f"attacker.example:{port}",
                 f"localhost.attacker.example:{port}", f"evil@127.0.0.1:{port}"):
        resp, body = request(server, "GET", path, headers={"Host": host})
        assert resp.status == 403, (path, host)
        assert json.loads(body)["error"]


def test_api_accepts_loopback_host_names(server):
    port = server.server_address[1]
    # a forwarded port (ssh -L 9999:127.0.0.1:<port>, docker -p) arrives with a different port
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}", f"[::1]:{port}",
                 f"127.0.0.1:{port + 1}", "127.0.0.1", "localhost:9999"):
        resp, _ = request(server, "GET", "/api/health", headers={"Host": host})
        assert resp.status == 200, host
    # the dashboard's static files are not API routes
    resp, _ = request(server, "GET", "/", headers={"Host": "attacker.example"})
    assert resp.status == 200


def test_post_rejects_foreign_host_origin_and_non_json_body(server):
    port = server.server_address[1]
    payload = {"topic": "csrf", "channels": ["linkedin"], "options": {"speed": 0}}
    resp, _ = request(server, "POST", "/api/runs", payload, headers={"Host": f"attacker.example:{port}"})
    assert resp.status == 403
    for origin in ("https://evil.example", "http://evil.example", f"http://127.0.0.1:{port + 1}", "null",
                   f"https://127.0.0.1:{port}"):
        resp, body = request(server, "POST", "/api/runs", payload, headers={"Origin": origin})
        assert resp.status == 403, origin
        assert json.loads(body)["error"]
    # CORS "simple" content types (no preflight) are refused
    raw = json.dumps(payload).encode("utf-8")
    for ctype in ("text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        resp, _ = request(server, "POST", "/api/runs", raw, headers={"Content-Type": ctype})
        assert resp.status == 415, ctype
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/runs", body=raw)  # no Content-Type at all
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 415
    assert json.loads(request(server, "GET", "/api/runs")[1])["runs"] == []  # nothing started


def test_post_from_the_dashboard_origin_is_accepted(server):
    port = server.server_address[1]
    payload = {"topic": "대시보드", "channels": ["instagram"], "options": {"speed": 0}}
    for origin, host in ((f"http://127.0.0.1:{port}", f"127.0.0.1:{port}"), (f"http://localhost:{port}", f"localhost:{port}"),
                         ("http://localhost:9999", "localhost:9999")):  # dashboard opened through a port forward
        resp, body = request(server, "POST", "/api/runs", payload,
                             headers={"Origin": origin, "Host": host, "Content-Type": "application/json; charset=utf-8"})
        assert resp.status == 201, (origin, body)
        events, _ = read_sse(server, json.loads(body)["events_url"])
        assert events[-1]["type"] == "run.completed"


def test_wildcard_bind_allows_ip_literals_but_not_names(settings):
    # a non-loopback bind needs an access token since the workspace API exists
    srv = make_server(settings, host="0.0.0.0", port=0, web_dir=None, token="test-token-1234567890")
    try:
        port = srv.server_address[1]
        assert srv.allows_host("192.168.0.10", port) and srv.allows_host("127.0.0.1", port)
        assert srv.allows_host("localhost", port)
        assert not srv.allows_host("attacker.example", port)
        assert srv.allows_host("192.168.0.10", port + 1)  # forwarded ports are fine; the name decides
    finally:
        srv.server_close()


# -- malformed JSON / numbers ---------------------------------------------------


@pytest.mark.parametrize("raw", [
    b'{"topic": "t", "options": {"max_rounds": Infinity}}',
    b'{"topic": "t", "options": {"pass_score": NaN}}',
    b'{"topic": "t", "options": {"speed": -Infinity}}',
    b'{"topic": "t", "options": {"max_rounds": 1e400}}',
    b'{"topic": "t", "options": {"speed": 1' + b"0" * 400 + b'}}',
    b'{"topic": "t", "options": {"max_rounds": 1' + b"0" * 5000 + b'}}',
    b"[" * 30000,
])
def test_non_finite_and_deeply_nested_json_get_400(server, raw):
    resp, body = request(server, "POST", "/api/runs", raw)
    assert resp.status == 400, body
    assert json.loads(body)["error"]


def test_post_origin_must_match_the_requested_host(server):
    port = server.server_address[1]
    payload = {"topic": "x", "channels": ["linkedin"], "options": {"speed": 0}}
    resp, _ = request(server, "POST", "/api/runs", payload,
                      headers={"Origin": "http://localhost:9999", "Host": f"localhost:{port}"})
    assert resp.status == 403


def test_parse_options_numbers():
    assert parse_options({"speed": 2, "max_rounds": 3.0, "pass_score": 90}) == {"speed": 2.0, "max_rounds": 3, "pass_score": 90}
    for raw in ({"speed": float("inf")}, {"pass_score": float("nan")}, {"max_rounds": 10 ** 400},
                {"speed": 10 ** 400}, {"speed": 1e-6}, {"max_rounds": 1.5}, {"max_rounds": True}):
        with pytest.raises(RequestError) as info:
            parse_options(raw)
        assert info.value.status == 400, raw


# -- concurrency limit ----------------------------------------------------------


def test_run_limit_holds_under_a_burst(settings, monkeypatch):
    max_active, burst = 2, 6
    release = threading.Event()
    barrier = threading.Barrier(burst, timeout=10)
    real_prepare = server_module.prepare_run

    def slow_prepare(*args, **kwargs):
        barrier.wait()  # every request is between "check" and "insert" at the same time
        return real_prepare(*args, **kwargs)

    def blocking_pipeline(*args, **kwargs):
        release.wait(10)
        raise RuntimeError("test stop")

    monkeypatch.setattr(server_module, "prepare_run", slow_prepare)
    monkeypatch.setattr(server_module, "run_pipeline", blocking_pipeline)
    manager = RunManager(settings, max_active=max_active)
    outcomes: list[object] = []
    lock = threading.Lock()

    def start() -> None:
        try:
            record = manager.start(Brief(topic="동시 실행", channels=["linkedin"]), {"speed": 0})
            result: object = record
        except RequestError as exc:
            result = exc.status
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=start) for _ in range(burst)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    try:
        started = [o for o in outcomes if not isinstance(o, int)]
        assert len(outcomes) == burst
        assert len(started) == max_active
        assert sorted(o for o in outcomes if isinstance(o, int)) == [429] * (burst - max_active)
        assert sum(1 for r in manager.list() if r["status"] == "running") == max_active
    finally:
        release.set()
        for record in started:
            record.thread.join(timeout=5)


# -- overflowing JSON numbers (finding 15) ----------------------------------------


@pytest.mark.parametrize("number", [b"1e999", b"-1e999", b"1" + b"0" * 400, b"-" + b"9" * 17, b"NaN"])
def test_overflowing_numbers_get_400_not_500(server, number):
    raw = b'{"start": "2026-09-28", "counts": {"naver_blog": ' + number + b'}, "options": {"mode": "mock"}}'
    resp, body = request(server, "POST", "/api/calendar/plan", raw)
    assert resp.status == 400, body
    assert "쓸 수 없는 숫자" in json.loads(body)["error"]


def test_parse_json_body_number_rules():
    assert server_module.parse_json_body(b'{"a": 9007199254740991, "b": -9007199254740991, "c": 1.5, "d": 1e308}') == {
        "a": 2 ** 53 - 1, "b": -(2 ** 53 - 1), "c": 1.5, "d": 1e308}
    for raw in (b'{"a": 9007199254740992}', b'{"a": 1e999}', b'[-1e400]', b'{"a": Infinity}', b'{"a": NaN}',
                b'{"a": 1' + b"0" * 5000 + b"}"):
        with pytest.raises(server_module.JsonNumberError):
            server_module.parse_json_body(raw)
    with pytest.raises(ValueError):
        server_module.parse_json_body(b"{nope")


# -- malformed or abandoned requests: no 500, no traceback (finding 16) -----------


def _no_error_logs(caplog, capfd):
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage()[:200] for r in errors]
    assert "Traceback" not in capfd.readouterr().err


def test_long_static_paths_are_404_without_a_traceback(server, caplog, capfd):
    with caplog.at_level(logging.DEBUG, logger="insia_agents.server"):
        for length in (300, 60_000):  # one segment longer than NAME_MAX (ENAMETOOLONG), then a 60 KB path
            resp, body = request(server, "GET", "/" + "a" * length)
            assert resp.status == 404 and json.loads(body)["error"] == "파일을 찾을 수 없어요", length
        resp, body = request(server, "GET", "/assets/" + "b" * 300 + "/x.js")
        assert resp.status == 404
        resp, body = request(server, "GET", "/api/" + "a" * 70_000)  # over the stdlib request-line limit
        assert resp.status == 414 and json.loads(body) == {"error": "주소(URL)가 너무 길어요.", "status": 414}
    _no_error_logs(caplog, capfd)


def _raw(server, data: bytes, *, shutdown_write: bool = False, wait: float = 10.0) -> bytes:
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=wait)
    sock.sendall(data)
    if shutdown_write:
        sock.shutdown(socket.SHUT_WR)
    chunks = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks)


def test_stalled_request_body_gets_408(server, monkeypatch, caplog, capfd):
    monkeypatch.setattr(server_module.InsiaHandler, "timeout", 0.5)
    head = b"POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n"
    with caplog.at_level(logging.DEBUG, logger="insia_agents.server"):
        answer = _raw(server, head + b'{"topic"')  # the rest never comes
        assert answer.startswith(b"HTTP/1.0 408"), answer[:200]
        assert "제시간에 도착하지 않았어요".encode() in answer
        answer = _raw(server, head + b'{"topic": "t"}', shutdown_write=True)  # the client hung up early
        assert answer.startswith(b"HTTP/1.0 400") and "Content-Length보다 짧아요".encode() in answer
    _no_error_logs(caplog, capfd)
    assert json.loads(request(server, "GET", "/api/runs")[1])["runs"] == []


def test_client_that_hangs_up_is_not_an_error(server, monkeypatch, caplog, capfd):
    def gone(self, *args, **kwargs):
        raise BrokenPipeError(32, "Broken pipe")

    with caplog.at_level(logging.DEBUG, logger="insia_agents.server"):
        # a scoped patch: monkeypatch.undo() would also undo the env isolation of the conftest/settings fixtures
        with monkeypatch.context() as patch:
            patch.setattr(server_module.InsiaHandler, "_error", gone)  # every error answer finds the client gone
            for path in ("/api/nope", "/nope.js"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", path)
                with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
                    conn.getresponse()
                conn.close()
        for _ in range(20):  # real resets: SO_LINGER 0 closes with RST before the answer is written
            sock = socket.create_connection(("127.0.0.1", server.server_address[1]))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.sendall(b"GET /api/does-not-exist HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            sock.close()
        time.sleep(0.3)
        assert request(server, "GET", "/api/health")[0].status == 200
    _no_error_logs(caplog, capfd)
    assert any("연결이 끊겼어요" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_handle_error_logs_real_errors_but_not_disconnects(server, caplog):
    with caplog.at_level(logging.DEBUG, logger="insia_agents.server"):
        try:
            raise ConnectionResetError(104, "reset")
        except ConnectionResetError:
            server.handle_error(None, ("127.0.0.1", 1))
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            server.handle_error(None, ("127.0.0.1", 1))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "boom" in errors[0].getMessage()


# -- IPv6 binds (finding 17) --------------------------------------------------------


def _ipv6_loopback_works() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


def test_ipv6_literals_pick_the_ipv6_family_and_count_as_loopback():
    assert server_module.address_family_for("::1") == socket.AF_INET6
    assert server_module.address_family_for("[::1]") == socket.AF_INET6
    assert server_module.address_family_for("::") == socket.AF_INET6
    for host in ("127.0.0.1", "0.0.0.0", "", "localhost", "insia.example.com"):
        assert server_module.address_family_for(host) == socket.AF_INET, host
    assert server_module.is_loopback_bind("::1") and server_module.is_loopback_bind("[::1]")
    assert not server_module.is_loopback_bind("::")


def test_ipv6_wildcard_still_needs_a_token(settings):
    with pytest.raises(server_module.ServerConfigError, match="접근 토큰이 필요해요"):
        make_server(settings, host="::", port=0)


TOKEN = "unit-test-token-0123456789"
FAKE_PORT = 48765


class _StandInListenSocket:
    """The listening socket, for machines without IPv6: records what the server asks for. Like the real
    one, an AF_INET socket cannot bind an IPv6 literal (what made ``--host ::1`` crash before)."""

    def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
        self.family, self.type = family, type
        self.options: list[tuple[int, int, int]] = []
        self.bound = None

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, address):
        if self.family != socket.AF_INET6 and ":" in address[0]:
            raise socket.gaierror(-9, "Address family for hostname not supported")
        port = address[1] or FAKE_PORT
        self.bound = (address[0], port, 0, 0) if self.family == socket.AF_INET6 else (address[0], port)

    def getsockname(self):
        return self.bound

    def listen(self, backlog):
        pass

    def fileno(self):
        return -1

    def close(self):
        pass


def _stand_in_server(settings, monkeypatch, host, **kwargs):
    with monkeypatch.context() as patch:  # only the listening socket is a stand-in
        patch.setattr(socket, "socket", _StandInListenSocket)
        return make_server(settings, host=host, port=0, **kwargs)


def _exchange(srv, peer, method, path, *, host, body=None, headers=None):
    """One request handled as if accepted from ``peer`` (an IPv6 4-tuple): the server's own per-request
    thread (``process_request`` → handler → ``shutdown_request``) over a socket pair."""
    ours, theirs = socket.socketpair()
    theirs.settimeout(10)
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Length: {len(data)}\r\n"
    if body is not None:
        head += "Content-Type: application/json\r\n"
    head += "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
    try:
        theirs.sendall(head.encode("latin-1") + b"\r\n" + data)
        srv.process_request(ours, peer)
        resp = http.client.HTTPResponse(theirs)
        resp.begin()
        payload = resp.read()
        return resp.status, resp.getheader("Set-Cookie"), payload
    finally:
        theirs.close()


def test_ipv6_binds_use_an_ipv6_socket_and_the_wildcard_is_dual_stack(settings, monkeypatch):
    """Runs on every machine (a stand-in socket), so the IPv6 bind path is checked even without IPv6."""
    v6only_off = (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    loop = _stand_in_server(settings, monkeypatch, "[::1]")
    try:
        assert loop.socket.family == socket.AF_INET6 and loop.socket.bound == ("::1", FAKE_PORT, 0, 0)
        assert v6only_off not in loop.socket.options  # loopback stays IPv6-only
        assert loop.url == f"http://[::1]:{FAKE_PORT}/" and not loop.token_required
    finally:
        loop.server_close()
    wild = _stand_in_server(settings, monkeypatch, "::", token=TOKEN)
    try:
        assert wild.socket.family == socket.AF_INET6 and wild.socket.bound[0] == "::"
        assert v6only_off in wild.socket.options  # "::" takes IPv4 clients too, like 0.0.0.0
        assert wild.url == f"http://[::]:{FAKE_PORT}/" and wild.token_required
    finally:
        wild.server_close()
    plain = _stand_in_server(settings, monkeypatch, "127.0.0.1")
    try:
        assert plain.socket.family == socket.AF_INET and plain.url == f"http://127.0.0.1:{FAKE_PORT}/"
    finally:
        plain.server_close()


def test_requests_from_ipv6_clients_on_a_dual_stack_bind(settings, monkeypatch):
    """The request side of ``--host ::`` with IPv6 peers: Host rules, login, and one limiter key per /64."""
    srv = _stand_in_server(settings, monkeypatch, "::", token=TOKEN)
    try:
        def peer(address):
            return (address, 50000, 0, 0)

        def health(host, auth=True):
            headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
            return _exchange(srv, peer("2001:db8:1:2::1"), "GET", "/api/health", host=host, headers=headers)

        status, _, payload = health("[2001:db8::10]:8765")  # an IP literal on a wildcard bind
        assert status == 200 and json.loads(payload)["token_required"] is True
        assert health("[::1]:8765")[0] == 200 and health("[::1]:8765", auth=False)[0] == 401
        assert health("evil.example")[0] == 403

        def login(address, token="wrong-token-000000"):
            return _exchange(srv, peer(address), "POST", "/api/login", host="[::1]:8765", body={"token": token})

        statuses = [login(f"2001:db8:1:2::{i:x}")[0] for i in range(1, 11)]  # a new source address each time
        assert statuses.count(401) == 9 and statuses[-1] == 429
        assert login("2001:db8:1:2:abcd::99", TOKEN)[0] == 429  # the same /64 stays locked out
        status, cookie, _ = login("2001:db8:1:3::1", TOKEN)  # another /64 logs in
        assert status == 200 and cookie and cookie.startswith(f"{server_module.COOKIE_NAME}=")
        for _ in range(10):
            login("::ffff:198.51.100.7")  # an IPv4 client, as the dual-stack socket reports it
        assert srv.limiter.retry_after("198.51.100.7") > 0
    finally:
        srv.server_close()


def test_ipv6_loopback_bind(settings):
    if not _ipv6_loopback_works():
        # this machine has no IPv6: a clear Korean refusal instead of a misleading "port in use"
        with pytest.raises(server_module.ServerConfigError, match="IPv6") as info:
            make_server(settings, host="::1", port=0)
        assert "--host 127.0.0.1" in str(info.value)
        return
    srv = make_server(settings, host="::1", port=0, heartbeat=0.2)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        assert srv.address_family == socket.AF_INET6 and srv.url == f"http://[::1]:{port}/"
        conn = http.client.HTTPConnection("::1", port, timeout=10)
        conn.request("GET", "/api/health", headers={"Host": f"[::1]:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read())["token_required"] is False
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
    # "::" on a real socket: IPv4 clients get in too (dual stack) and count as their IPv4 address
    wild = make_server(settings, host="::", port=0, heartbeat=0.2, token=TOKEN)
    thread = threading.Thread(target=wild.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port = wild.server_address[1]
        for _ in range(server_module.LOGIN_MAX_FAILURES):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/login", body=json.dumps({"token": "wrong-token-000000"}),
                         headers={"Content-Type": "application/json"})
            assert conn.getresponse().status in (401, 429)
            conn.close()
        assert wild.limiter.retry_after("127.0.0.1") > 0
        conn = http.client.HTTPConnection("::1", port, timeout=10)  # ::1 is another client (key "::/64")
        conn.request("GET", "/api/health", headers={"Host": f"[::1]:{port}", "Authorization": f"Bearer {TOKEN}"})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        wild.shutdown()
        wild.server_close()


# ---------------------------------------------------------------------------
# Graceful shutdown: SIGTERM (docker stop) takes the Ctrl+C path
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not hasattr(signal, "SIGTERM") or sys.platform == "win32", reason="POSIX signals")
def test_sigterm_as_interrupt_raises_keyboard_interrupt_in_the_main_thread_only():
    before = signal.getsignal(signal.SIGTERM)
    with server_module.sigterm_as_interrupt() as installed:
        assert installed is True and signal.getsignal(signal.SIGTERM) is not before
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is before  # restored

    seen = []

    def worker():
        with server_module.sigterm_as_interrupt() as installed:
            seen.append(installed)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert seen == [False] and signal.getsignal(signal.SIGTERM) is before


SERVE_COMMANDS = {
    "cli": ["-m", "insia_agents", "serve", "--port", "0", "--mode", "mock"],
    "library": ["-c", "from insia_agents.config import Settings; from insia_agents.server import serve; "
                      "serve(Settings.from_env(), port=0)"],
}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery is POSIX")
@pytest.mark.parametrize("how", sorted(SERVE_COMMANDS))
def test_sigterm_stops_insia_serve_cleanly_and_leaves_no_run_running(tmp_path, how):
    """docker stop sends SIGTERM: the server cancels its live run (saved as 'cancelled', resumable) and exits 0."""
    from insia_agents.db import Workspace

    home = tmp_path / "ws"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "INSIA_HOME": str(home), "INSIA_MODE": "mock",
           "PYTHONUNBUFFERED": "1"}
    for name in ("INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    proc = subprocess.Popen([sys.executable, *SERVE_COMMANDS[how]], cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        port = None
        deadline = time.monotonic() + 30
        while port is None and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "http://127.0.0.1:" in line:
                port = int(line.split("http://127.0.0.1:", 1)[1].split("/", 1)[0])
        assert port, "server did not start"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"topic": "종료 테스트", "channels": ["linkedin", "instagram"], "options": {"speed": 1}})
        conn.request("POST", "/api/runs", body=body.encode("utf-8"), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        run_id = json.loads(resp.read())["run_id"]
        conn.close()
        assert resp.status == 201
        time.sleep(0.5)  # the run is under way (recorded pace: minutes)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert proc.returncode == 0, out
    assert "서버를 종료해요" in out and "진행 중인 작업 1개를 멈추는 중" in out and "Traceback" not in out
    ws = Workspace(home)
    try:
        run = ws.get_run(run_id)
        assert run["status"] == "cancelled", run["status"]
        assert ws.list_runs(status="running") == [] and ws.stale_runs() == {}
        assert ws.list_events(run_id)[-1]["type"] == "run.failed"
    finally:
        ws.close()


def test_docker_compose_gives_the_server_time_to_stop_its_runs():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+init: true\s*$", compose, re.M)
    grace = re.search(r"^\s+stop_grace_period: (\d+)s\s*$", compose, re.M)
    assert grace and int(grace.group(1)) > server_module.SHUTDOWN_GRACE
