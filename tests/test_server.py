from __future__ import annotations

import http.client
import json
import threading
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
    srv = make_server(settings, host="0.0.0.0", port=0, web_dir=None)
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
