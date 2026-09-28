from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from insia_agents.server import make_server, parse_range, resolve_static


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
