"""HTTP API over a temporary workspace (mock backend): every route, SSE replay, resume, token mode."""

from __future__ import annotations

import http.client
import io
import json
import threading
import time
import urllib.parse
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from insia_agents import server as server_module
from insia_agents.db import Workspace, pipeline_item_id
from insia_agents.exporters import capabilities
from insia_agents.models import Brief
from insia_agents.server import COOKIE_NAME, LoginLimiter, ServerConfigError, client_key, make_server

TOKEN = "unit-test-token-0123456789"
TERMINAL = ("run.completed", "run.failed")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(settings, tmp_path):
    return replace(settings, home=tmp_path / "workspace")


@pytest.fixture(autouse=True)
def no_png_rendering(monkeypatch):
    """Carousel exports fall back to slides.html (no headless browser in these tests)."""
    monkeypatch.setenv("INSIA_RENDER", "0")
    for name in ("INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_MAX_LIVE_JOBS", "INSIA_MAX_MOCK_JOBS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def servers():
    started = []

    def start(settings, **kwargs):
        kwargs.setdefault("heartbeat", 0.2)
        srv = make_server(settings, host=kwargs.pop("host", "127.0.0.1"), port=0, web_dir=kwargs.pop("web_dir", None), **kwargs)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started.append(srv)
        return srv

    def stop(srv):
        if srv in started:
            started.remove(srv)
            srv.shutdown()
            srv.server_close()

    start.stop = stop  # type: ignore[attr-defined]
    yield start
    for srv in list(started):
        stop(srv)


@pytest.fixture
def srv(servers, settings):
    return servers(settings)


def request(srv, method, path, body=None, headers=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, (dict, list)) else body
    hdrs = {"Content-Type": "application/json"} if data is not None or method in ("POST", "PUT") else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    if raw:
        return resp, payload
    ctype = resp.getheader("Content-Type") or ""
    return resp, (json.loads(payload) if payload and "json" in ctype else payload)


def ok(srv, method, path, body=None, status=200, headers=None):
    resp, data = request(srv, method, path, body, headers)
    assert resp.status == status, (method, path, resp.status, data)
    return data


def read_sse(srv, path, headers=None, stop_on_terminal=True):
    """Events of an SSE stream; stops at the first terminal event (or when the server closes)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    assert resp.status == 200, resp.status
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    events = []
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8").rstrip("\n")
        if line.startswith("data: "):
            event = json.loads(line[6:])
            events.append(event)
            if stop_on_terminal and event["type"] in TERMINAL:
                break
    conn.close()
    return events


def wait_run(srv, run_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = ok(srv, "GET", f"/api/runs/{run_id}")
        if not detail["active"] and detail["status"] != "running":
            return detail
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def start_run(srv, channels=("linkedin",), **options):
    options.setdefault("speed", 0)
    body = {"topic": "API 테스트 주제", "channels": list(channels), "keywords": ["AI 에이전트"], "options": options}
    return ok(srv, "POST", "/api/runs", body, status=201)


def finished_run(srv, channels=("linkedin",), **options):
    created = start_run(srv, channels, **options)
    events = read_sse(srv, created["events_url"])
    assert events[-1]["type"] == "run.completed", events[-1]
    detail = wait_run(srv, created["run_id"])
    return created["run_id"], detail


# ---------------------------------------------------------------------------
# health, profile, documents
# ---------------------------------------------------------------------------


def test_health_reports_workspace_and_capabilities(srv, settings):
    health = ok(srv, "GET", "/api/health")
    assert health["mode"] == "mock" and health["live_available"] is False
    assert Path(health["workspace"]) == settings.home.resolve()
    assert health["profile_complete"] is False and health["budget_usd"] == 0.0
    assert set(health["capabilities"]) == {"docx", "png"} and health["token_required"] is False
    assert health["limits"] == {"live": 2, "mock": 4} and health["formats"]["instagram"][0] == "zip"


def test_profile_get_put_and_validation(srv):
    empty = ok(srv, "GET", "/api/profile")
    assert empty["profile"]["company_name"] == "" and empty["profile_complete"] is False and "회사명" in empty["missing"]
    saved = ok(srv, "PUT", "/api/profile", {"company_name": "인시아", "service_name": "스마트에이전트", "banned_words": ["최고"]})
    assert saved["profile"]["company_name"] == "인시아" and saved["profile"]["updated_at"]
    assert 0 < saved["completeness"] < 100
    full = {"company_name": "인시아", "service_name": "스마트에이전트", "one_liner": "한 줄", "description": "설명",
            "target_customers": "1인 창업자", "problem": "문제", "solution": "해결", "differentiators": ["차별점"],
            "business_model": "구독", "team": [{"role": "대표", "name": "홍길동"}], "tone": "친근", "cta": "문의"}
    wrapped = ok(srv, "PUT", "/api/profile", {"profile": full})
    assert wrapped["profile_complete"] is True and wrapped["missing"] == []
    assert ok(srv, "GET", "/api/profile")["profile"]["team"][0]["name"] == "홍길동"
    assert ok(srv, "GET", "/api/health")["profile_complete"] is True
    resp, data = request(srv, "PUT", "/api/profile", {"team": "not a list"})
    assert resp.status == 400 and "team" in data["error"]
    resp, _ = request(srv, "PUT", "/api/profile", json.dumps(full).encode(), headers={"Content-Type": "text/plain"})
    assert resp.status == 415
    resp, _ = request(srv, "DELETE", "/api/profile")
    assert resp.status == 405


def test_documents_crud_and_limits(srv):
    doc = ok(srv, "POST", "/api/documents", {"title": "회사 소개서", "text": "베타 사용자 120명 (2026-08 기준)"}, status=201)["document"]
    assert doc["id"] == "u1" and doc["chars"] > 0
    big_text = "가" * 100_000  # 300 KB of UTF-8: above the default 64 KB JSON limit, fine for documents
    big = ok(srv, "POST", "/api/documents", {"title": "긴 자료", "text": big_text, "kind": "markdown", "filename": "../x/긴.md"},
             status=201)["document"]
    assert big["id"] == "u2" and big["chars"] == 100_000 and big["filename"] == "긴.md"
    listed = ok(srv, "GET", "/api/documents")
    assert [d["id"] for d in listed["documents"]] == ["u1", "u2"] and listed["documents"][0]["text"].startswith("베타")
    assert "text" not in ok(srv, "GET", "/api/documents?text=0")["documents"][0]
    assert ok(srv, "GET", "/api/documents/u2")["document"]["text"] == big_text
    assert ok(srv, "DELETE", "/api/documents/u1") == {"deleted": True, "id": "u1"}
    resp, data = request(srv, "DELETE", "/api/documents/u1")
    assert resp.status == 404 and data["error"]
    assert ok(srv, "POST", "/api/documents", {"title": "새 자료", "text": "내용"}, status=201)["document"]["id"] == "u3"
    for body in ({"title": "빈 자료", "text": "   "}, {"title": "종류", "text": "x", "kind": "exe"}, {"title": "숫자", "text": 3}):
        resp, data = request(srv, "POST", "/api/documents", body)
        assert resp.status == 400 and data["error"], body
    # Content-Length is checked before the body is read
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.putrequest("POST", "/api/documents")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(9 * 1024 * 1024))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413 and "8MB" in json.loads(resp.read())["error"]
    conn.close()
    resp, _ = request(srv, "POST", "/api/calendar/plan", {"theme": "x" * (70 * 1024), "start": "2026-10-05", "counts": {"linkedin": 1}})
    assert resp.status == 413


# ---------------------------------------------------------------------------
# runs: persistence, SSE replay after restart, resume, cancel
# ---------------------------------------------------------------------------


def test_run_is_persisted_listed_and_linked_to_items(srv):
    run_id, detail = finished_run(srv, ("linkedin", "instagram"))
    assert detail["status"] == "completed" and detail["kind"] == "pipeline" and detail["active"] is False
    assert set(detail["items"]) == {"linkedin", "instagram"} and detail["result"]["run_id"] == run_id
    assert detail["plan"] and detail["research"] and detail["resumable"] is False
    assert detail["options"]["doc_ids"] == []
    runs = ok(srv, "GET", "/api/runs?kind=pipeline&status=completed")["runs"]
    assert runs[0]["run_id"] == run_id and runs[0]["active"] is False and runs[0]["resumable"] is False
    assert ok(srv, "GET", "/api/runs?status=failed")["runs"] == []
    resp, _ = request(srv, "GET", "/api/runs?status=bogus")
    assert resp.status == 400
    items = ok(srv, "GET", "/api/items")["items"]
    assert {i["id"] for i in items} == {pipeline_item_id(run_id, "linkedin"), pipeline_item_id(run_id, "instagram")}


def test_runs_use_profile_and_documents_options(srv):
    ok(srv, "POST", "/api/documents", {"title": "소개서", "text": "사용자 제공 사실"}, status=201)
    ok(srv, "PUT", "/api/profile", {"company_name": "인시아"})
    _, detail = finished_run(srv, docs=["u1"], use_profile=True)
    assert detail["options"]["doc_ids"] == ["u1"] and detail["profile"]["company_name"] == "인시아"
    _, detail = finished_run(srv, docs="none", use_profile=False)
    assert detail["options"]["doc_ids"] == [] and detail["profile"] is None
    for options in ({"docs": ["u9"]}, {"docs": ["../etc"]}, {"use_profile": "yes"}, {"max_cost_usd": -1}):
        resp, data = request(srv, "POST", "/api/runs", {"topic": "t", "channels": ["linkedin"], "options": options})
        assert resp.status == 400 and data["error"], options


def test_finished_run_events_replay_from_db_after_restart(servers, settings):
    first = servers(settings)
    run_id, _ = finished_run(first, ("naver_blog",))
    live_events = read_sse(first, f"/api/runs/{run_id}/events")
    servers.stop(first)

    second = servers(settings)  # a new server object on the same workspace
    detail = ok(second, "GET", f"/api/runs/{run_id}")
    assert detail["status"] == "completed" and detail["active"] is False and detail["result"] is None
    replayed = read_sse(second, f"/api/runs/{run_id}/events")
    assert [e["seq"] for e in replayed] == list(range(1, len(replayed) + 1))
    assert replayed == live_events and replayed[-1]["type"] == "run.completed"
    tail = read_sse(second, f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(len(replayed) - 2)})
    assert [e["seq"] for e in tail] == [len(replayed) - 1, len(replayed)]
    resp, _ = request(second, "GET", f"/api/runs/{run_id}/events?after={len(replayed)}")
    assert resp.status == 204
    resp, _ = request(second, "GET", "/api/runs/nope-123/events")
    assert resp.status == 404


def _interrupted_run(settings, brief: Brief) -> str:
    """A pipeline run a crashed process left 'running' (one stored event, no plan yet)."""
    ws = Workspace(settings.home)
    try:
        run_id = "20260928-010203-dead"
        ws.create_run(run_id, brief, kind="pipeline", options={"max_rounds": 1, "pass_score": 80}, mode="mock", model="mock")
        ws.append_event(run_id, {"seq": 1, "t": 0.0, "type": "run.started", "agent": "system", "data": {"brief": brief.model_dump()}})
    finally:
        ws.close()
    return run_id


def test_resume_interrupted_run_keeps_run_id_and_streams_across_the_interruption(servers, settings):
    run_id = _interrupted_run(settings, Brief(topic="중단된 실행", channels=["linkedin"]))
    srv = servers(settings)
    assert srv.manager.interrupted_on_start == 1
    detail = ok(srv, "GET", f"/api/runs/{run_id}")
    assert detail["status"] == "interrupted" and detail["resumable"] is True
    listed = ok(srv, "GET", "/api/runs")["runs"]
    assert [(r["run_id"], r["status"], r["resumable"]) for r in listed] == [(run_id, "interrupted", True)]  # the studio's 이어서 실행
    stored = read_sse(srv, f"/api/runs/{run_id}/events")
    assert [e["type"] for e in stored] == ["run.started", "run.failed"]
    assert stored[-1]["data"]["interrupted"] is True

    resumed = ok(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 0}}, status=202)
    assert resumed["run_id"] == run_id and resumed["resumed"] is True
    events = read_sse(srv, f"/api/runs/{run_id}/events")
    # the interrupted attempt's run.failed (seq 2) is superseded: left out, so the stream does not end there
    assert [e["seq"] for e in events] == [1, *range(3, len(events) + 2)]
    assert events[1]["type"] == "run.started" and events[1]["data"]["resumed"] is True
    assert events[-1]["type"] == "run.completed"
    assert [e["type"] for e in events].count("run.failed") == 0
    detail = wait_run(srv, run_id)
    assert detail["status"] == "completed" and detail["items"] == {"linkedin": pipeline_item_id(run_id, "linkedin")}
    stored_events = srv.manager.workspace.list_events(run_id)
    assert stored_events[1]["type"] == "run.failed" and len(stored_events) == len(events) + 1  # kept in the workspace
    replay = read_sse(srv, f"/api/runs/{run_id}/events", stop_on_terminal=False)
    assert replay == events

    resp, data = request(srv, "POST", f"/api/runs/{run_id}/resume", {})
    assert resp.status == 409 and "이미 모든 채널" in data["error"]
    resp, _ = request(srv, "POST", "/api/runs/unknown-run/resume", {})
    assert resp.status == 404


def sse_stream(srv, path, headers=None):
    """Incremental SSE reader: yields events as they arrive."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    assert resp.status == 200, resp.status
    try:
        while True:
            line = resp.readline()
            if not line:
                return
            line = line.decode("utf-8").rstrip("\n")
            if line.startswith("data: "):
                yield json.loads(line[6:])
    finally:
        conn.close()


def test_sse_of_an_active_resumed_run_sends_stored_events_first(servers, settings):
    run_id = _interrupted_run(settings, Brief(topic="이어서 보기", channels=["linkedin", "instagram"]))
    srv = servers(settings)
    ok(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 1}}, status=202)  # slow: still active below
    stream = sse_stream(srv, f"/api/runs/{run_id}/events")
    head = [next(stream) for _ in range(2)]
    assert [e["type"] for e in head] == ["run.started", "run.started"] and [e["seq"] for e in head] == [1, 3]
    assert head[1]["data"]["resumed"] is True
    detail = ok(srv, "GET", f"/api/runs/{run_id}")
    assert detail["active"] is True and detail["status"] == "running" and detail["resumable"] is False
    running = ok(srv, "GET", "/api/runs?status=running")["runs"]
    assert [r["run_id"] for r in running] == [run_id] and running[0]["active"] is True
    resp, data = request(srv, "POST", f"/api/runs/{run_id}/resume", {})
    assert resp.status == 409
    ok(srv, "POST", f"/api/runs/{run_id}/cancel", {}, status=202)
    rest = list(stream)
    assert rest[-1]["type"] == "run.failed" and rest[-1]["data"]["error"]
    assert wait_run(srv, run_id)["status"] == "cancelled"


def test_sse_follows_a_run_another_process_writes(srv, settings):
    """A CLI run on the same workspace (another Workspace connection) is streamed by polling the DB."""
    other = Workspace(settings.home)
    run_id = "20260928-020304-beef"
    try:
        other.create_run(run_id, Brief(topic="CLI 실행", channels=["linkedin"]), kind="pipeline", mode="mock")
        other.append_event(run_id, {"seq": 1, "type": "run.started", "agent": "system", "data": {}})
        detail = ok(srv, "GET", f"/api/runs/{run_id}")
        assert detail["status"] == "running" and detail["active"] is False
        stream = sse_stream(srv, f"/api/runs/{run_id}/events")
        assert next(stream)["seq"] == 1

        def later() -> None:
            time.sleep(0.3)
            other.append_event(run_id, {"seq": 2, "type": "log", "agent": "system", "data": {"level": "info", "message": "진행 중"}})
            other.append_event(run_id, {"seq": 3, "type": "run.completed", "agent": "system", "data": {}})
            other.update_run(run_id, status="completed")

        writer = threading.Thread(target=later)
        writer.start()
        rest = list(stream)
        writer.join()
        assert [e["seq"] for e in rest] == [2, 3] and rest[-1]["type"] == "run.completed"
    finally:
        other.close()


def test_resume_refuses_running_and_non_resumable_runs(srv):
    run_id, _ = finished_run(srv)
    item_id = pipeline_item_id(run_id, "linkedin")
    job = ok(srv, "POST", f"/api/items/{item_id}/review", {"options": {"speed": 0}}, status=201)
    read_sse(srv, job["events_url"])
    wait_run(srv, job["run_id"])
    resp, data = request(srv, "POST", f"/api/runs/{job['run_id']}/resume", {})
    assert resp.status == 400 and "이어서 실행할 수 없어요" in data["error"]
    ws = srv.manager.workspace
    ws.create_run("20260928-000000-busy", Brief(topic="다른 프로세스", channels=["linkedin"]), kind="pipeline")
    resp, data = request(srv, "POST", "/api/runs/20260928-000000-busy/resume", {})
    assert resp.status == 409 and data["can_force"] is True
    resp, data = request(srv, "POST", "/api/runs/20260928-000000-busy/cancel", {})
    assert resp.status == 409 and "이 서버에서 실행 중인 작업이 아니에요" in data["error"]
    # an imported Claude Code run (insia import-run) is a record, never something to resume
    ws.create_run("20260928-000001-import", Brief(topic="가져온 실행", channels=["linkedin"]), kind="import")
    ws.update_run("20260928-000001-import", status="failed")
    assert ok(srv, "GET", "/api/runs/20260928-000001-import")["resumable"] is False
    listed = {r["run_id"]: r for r in ok(srv, "GET", "/api/runs")["runs"]}
    assert listed["20260928-000001-import"]["resumable"] is False
    resp, data = request(srv, "POST", "/api/runs/20260928-000001-import/resume", {})
    assert resp.status == 400 and "이어서 실행할 수 없어요" in data["error"]


def test_cancel_stops_a_running_job_and_it_can_be_resumed(srv):
    created = start_run(srv, ("linkedin", "instagram"), speed=1)  # recorded pace: minutes unless cancelled
    run_id = created["run_id"]
    assert ok(srv, "GET", f"/api/runs/{run_id}")["status"] == "running"
    time.sleep(0.3)
    cancelled = ok(srv, "POST", created["cancel_url"], {}, status=202)
    assert cancelled["status"] == "cancelling"
    started = time.monotonic()
    events = read_sse(srv, created["events_url"])
    assert events[-1]["type"] == "run.failed" and time.monotonic() - started < 10
    detail = wait_run(srv, run_id)
    assert detail["status"] == "cancelled" and detail["resumable"] is True and detail["cancel_requested"] is True
    resp, data = request(srv, "POST", created["cancel_url"], {})
    assert resp.status == 409 and data["run_status"] == "cancelled"
    resp, _ = request(srv, "POST", "/api/runs/missing-run/cancel", {})
    assert resp.status == 404
    ok(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 0}}, status=202)
    events = read_sse(srv, f"/api/runs/{run_id}/events")
    assert events[-1]["type"] == "run.completed" and [e["type"] for e in events].count("run.started") == 2
    assert not [e for e in events if e["type"] == "run.failed"]  # the cancelled attempt's ending is superseded
    assert wait_run(srv, run_id)["status"] == "completed"


def test_concurrency_limit_answers_429(servers, settings):
    srv = servers(settings, max_mock=1)
    slow = start_run(srv, speed=1)
    resp, data = request(srv, "POST", "/api/runs", {"topic": "두 번째", "channels": ["linkedin"], "options": {"speed": 0}})
    assert resp.status == 429 and "동시에 실행할 수 있는 작업 수" in data["error"] and resp.getheader("Retry-After")
    resp, _ = request(srv, "POST", "/api/calendar/plan", {"theme": "t", "start": "2026-10-05", "counts": {"linkedin": 1}})
    assert resp.status == 429
    ok(srv, "POST", slow["cancel_url"], {}, status=202)
    wait_run(srv, slow["run_id"])
    start_run(srv)  # capacity is back


# ---------------------------------------------------------------------------
# items: library, human edit, review/revise jobs, status flow
# ---------------------------------------------------------------------------


def test_item_detail_edit_and_status_flow(srv):
    run_id, _ = finished_run(srv, ("linkedin",))
    item_id = pipeline_item_id(run_id, "linkedin")
    assert ok(srv, "GET", "/api/items?channel=linkedin")["items"][0]["id"] == item_id
    assert ok(srv, "GET", "/api/items?channel=bizplan")["items"] == []
    resp, _ = request(srv, "GET", "/api/items?status=weird")
    assert resp.status == 400
    detail = ok(srv, "GET", f"/api/items/{item_id}")
    assert detail["item"]["id"] == item_id and detail["versions"] and detail["brief"]["topic"] == "API 테스트 주제"
    assert [e["format"] for e in detail["exports"]] == ["txt", "md", "docx", "zip"]
    resp, _ = request(srv, "GET", "/api/items/it_missing")
    assert resp.status == 404

    long_content = "직접 고친 본문이에요.\n\n" + "가" * 70_000  # ~210 KB body: over the default JSON limit
    edited = ok(srv, "PUT", f"/api/items/{item_id}/draft", {"title": "사람이 고친 제목", "content": long_content,
                                                          "hashtags": ["AI 에이전트", "#창업"]})
    assert edited["kind"] == "edit" and edited["version"]["source"] == "human"
    assert edited["version"]["draft"]["hashtags"] == ["#AI에이전트", "#창업"]
    assert edited["format_checks"] and all({"id", "passed", "value", "expected"} <= set(c) for c in edited["format_checks"])
    assert edited["item"]["status"] == "draft" and edited["item"]["title"] == "사람이 고친 제목"
    for body in ({"title": "", "content": "x"}, {"title": "t"}, {"title": "t", "content": "x", "hashtags": [1]}):
        resp, data = request(srv, "PUT", f"/api/items/{item_id}/draft", body)
        assert resp.status == 400 and data["error"], body

    resp, data = request(srv, "POST", f"/api/items/{item_id}/status", {"status": "approved"})
    assert resp.status == 409 and data["blocked"] is True and data["can_force"] is True and "그래도 승인" in data["error"]
    assert ok(srv, "POST", f"/api/items/{item_id}/status", {"status": "approved", "force": True})["item"]["status"] == "approved"
    resp, data = request(srv, "POST", f"/api/items/{item_id}/status", {"status": "scheduled"})
    assert resp.status == 400 and "scheduled_at" in data["error"]
    scheduled = ok(srv, "POST", f"/api/items/{item_id}/status", {"status": "scheduled", "scheduled_at": "2026-10-05"})["item"]
    assert scheduled["status"] == "scheduled" and scheduled["scheduled_at"] == "2026-10-05"
    resp, data = request(srv, "POST", f"/api/items/{item_id}/status", {"status": "published", "published_url": "ftp://x"})
    assert resp.status == 400
    published = ok(srv, "POST", f"/api/items/{item_id}/status",
                   {"status": "published", "published_url": "https://www.linkedin.com/posts/1", "note": "게시함"})["item"]
    assert published["status"] == "published" and published["published_at"] and published["note"] == "게시함"
    resp, data = request(srv, "POST", f"/api/items/{item_id}/status", {"status": "approved"})
    assert resp.status == 409 and data["error"]
    for body in ({}, {"status": 3}, {"status": "draft", "force": "yes"}):
        resp, _ = request(srv, "POST", f"/api/items/{item_id}/status", body)
        assert resp.status == 400, body
    assert ok(srv, "POST", f"/api/items/{item_id}/status", {"status": "archived"})["item"]["status"] == "archived"


def test_review_and_revise_jobs_stream_events(srv):
    run_id, _ = finished_run(srv, ("naver_blog",))
    item_id = pipeline_item_id(run_id, "naver_blog")
    before = ok(srv, "GET", f"/api/items/{item_id}")

    review = ok(srv, "POST", f"/api/items/{item_id}/review", {"options": {"speed": 0}}, status=201)
    assert review["kind"] == "review" and review["item_id"] == item_id
    events = read_sse(srv, review["events_url"])
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and "review.completed" in types and types[-1] == "run.completed"
    assert events[0]["data"]["kind"] == "review" and events[0]["data"]["item_id"] == item_id
    job = wait_run(srv, review["run_id"])
    assert job["kind"] == "review" and job["parent_item_id"] == item_id and job["job"]["review"]["score"] >= 0

    revise = ok(srv, "POST", f"/api/items/{item_id}/revise", {"instructions": "도입부를 더 짧게", "options": {"speed": 0}},
                status=201)
    events = read_sse(srv, revise["events_url"])
    assert events[-1]["type"] == "run.completed" and "revision.requested" in [e["type"] for e in events]
    wait_run(srv, revise["run_id"])
    after = ok(srv, "GET", f"/api/items/{item_id}")
    assert len(after["versions"]) == len(before["versions"]) + 1
    assert after["versions"][-1]["instructions"] == "도입부를 더 짧게" and after["versions"][-1]["review"]

    resp, data = request(srv, "POST", f"/api/items/{item_id}/revise", {"instructions": "x" * 5000})
    assert resp.status == 400 and "수정 지시가 너무 길어요" in data["error"]
    resp, _ = request(srv, "POST", "/api/items/it_nope/review", {})
    assert resp.status == 404
    empty = srv.manager.workspace.create_item("linkedin", "빈 콘텐츠")
    resp, data = request(srv, "POST", f"/api/items/{empty.id}/review", {})
    assert resp.status == 400 and "버전이 없어요" in data["error"]


def test_second_job_on_the_same_item_is_refused_while_one_runs(srv):
    run_id, _ = finished_run(srv, ("linkedin",))
    item_id = pipeline_item_id(run_id, "linkedin")
    first = ok(srv, "POST", f"/api/items/{item_id}/review", {"options": {"speed": 1}}, status=201)
    resp, data = request(srv, "POST", f"/api/items/{item_id}/revise", {"instructions": "짧게"})
    assert resp.status == 409 and data["run_id"] == first["run_id"]
    ok(srv, "POST", first["cancel_url"], {}, status=202)
    events = read_sse(srv, first["events_url"])
    assert events[-1]["type"] == "run.failed"
    assert wait_run(srv, first["run_id"])["status"] == "cancelled"


@pytest.mark.parametrize("job, body", [("revise", {"instructions": "더 짧게"}), ("review", {})])
def test_human_edit_is_refused_while_a_review_or_revise_job_runs(srv, job, body):
    """A job started from the previous version would bury the edit (finding 4): 409, then save afterwards."""
    run_id, _ = finished_run(srv, ("linkedin",))
    item_id = pipeline_item_id(run_id, "linkedin")
    versions = len(ok(srv, "GET", f"/api/items/{item_id}")["versions"])
    started = ok(srv, "POST", f"/api/items/{item_id}/{job}", {**body, "options": {"speed": 1}}, status=201)
    edit = {"title": "사람이 고친 제목", "content": "사람이 직접 고친 본문이에요. " * 5}
    resp, data = request(srv, "PUT", f"/api/items/{item_id}/draft", edit)
    assert resp.status == 409, data
    assert data["run_id"] == started["run_id"] and data["job"] == job and data["item_id"] == item_id
    assert "에이전트가 이 콘텐츠를" in data["error"] and "다시 저장해 주세요" in data["error"]
    assert len(ok(srv, "GET", f"/api/items/{item_id}")["versions"]) == versions  # nothing was saved
    ok(srv, "POST", started["cancel_url"], {}, status=202)
    wait_run(srv, started["run_id"])
    saved = ok(srv, "PUT", f"/api/items/{item_id}/draft", edit)  # the job is over: the edit goes through
    assert saved["version"]["source"] == "human" and saved["item"]["title"] == "사람이 고친 제목"
    assert srv.manager._editing == {}


def test_a_job_cannot_start_while_a_human_edit_is_being_saved(srv):
    run_id, _ = finished_run(srv, ("linkedin",))
    item_id = pipeline_item_id(run_id, "linkedin")
    with srv.manager.human_edit(item_id):
        for job in ("review", "revise"):
            resp, data = request(srv, "POST", f"/api/items/{item_id}/{job}", {"options": {"speed": 0}})
            assert resp.status == 409 and "직접 수정한 내용을 저장하는 중" in data["error"], data
        with srv.manager.human_edit(item_id):  # two edits one after another are still fine
            assert srv.manager._editing[item_id] == 2
    assert srv.manager._editing == {}
    created = ok(srv, "POST", f"/api/items/{item_id}/review", {"options": {"speed": 0}}, status=201)
    assert wait_run(srv, created["run_id"])["status"] == "completed"


@pytest.fixture
def held_review(monkeypatch):
    """Holds the first review a run stores until released: by then the run has saved v1 of its item
    and is still active (deterministic, no dependence on the mock clock)."""
    reached, release = threading.Event(), threading.Event()
    original = Workspace.attach_review

    def attach_review(self, *args, **kwargs):
        if not reached.is_set():
            reached.set()
            release.wait(15)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Workspace, "attach_review", attach_review)
    yield reached, release
    release.set()


def test_items_of_a_run_that_is_still_writing_them_cannot_be_edited_or_given_a_job(srv, held_review):
    """Finding 4 through the item's own run: its next round or final copy would become the current
    version on top of a human edit saved meanwhile, so the edit (and review/revise jobs) wait for it."""
    reached, release = held_review
    run_id = start_run(srv, ("linkedin",))["run_id"]
    assert reached.wait(10)
    item_id = pipeline_item_id(run_id, "linkedin")
    before = ok(srv, "GET", f"/api/items/{item_id}")["versions"]
    assert len(before) == 1 and ok(srv, "GET", f"/api/runs/{run_id}")["active"] is True
    edit = {"title": "사람이 고친 제목", "content": "사람이 직접 고친 본문이에요. " * 5}
    resp, data = request(srv, "PUT", f"/api/items/{item_id}/draft", edit)
    assert resp.status == 409, data
    assert data["run_id"] == run_id and data["job"] == "pipeline" and data["item_id"] == item_id
    assert "에이전트가 아직 이 콘텐츠를" in data["error"] and "다시 저장해 주세요" in data["error"]
    for job, body in (("review", {}), ("revise", {"instructions": "더 짧게"})):
        resp, data = request(srv, "POST", f"/api/items/{item_id}/{job}", {**body, "options": {"speed": 0}})
        assert resp.status == 409 and data["run_id"] == run_id and "아직 진행 중" in data["error"], (job, data)
    assert ok(srv, "GET", f"/api/items/{item_id}")["versions"] == before  # nothing was saved
    release.set()
    assert wait_run(srv, run_id)["status"] == "completed"
    saved = ok(srv, "PUT", f"/api/items/{item_id}/draft", edit)  # the run is over: the edit goes through
    detail = ok(srv, "GET", f"/api/items/{item_id}")
    assert saved["version"]["source"] == "human" and detail["versions"][-1]["source"] == "human"
    assert detail["item"]["version"] == saved["version"]["version"] and detail["item"]["title"] == "사람이 고친 제목"
    assert srv.manager._editing == {} and srv.manager._editing_runs == {}


def test_a_run_cannot_resume_while_one_of_its_items_is_edited_or_has_a_job(srv):
    run_id, _ = finished_run(srv, ("linkedin",))
    item_id = pipeline_item_id(run_id, "linkedin")
    srv.manager.workspace.update_run(run_id, status="interrupted")  # resumable again
    with srv.manager.human_edit(item_id):  # the run is looked up from the item
        assert srv.manager._editing_runs == {run_id: 1}
        resp, data = request(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 0}})
        assert resp.status == 409 and "직접 수정한 내용을 저장하는 중" in data["error"], data
    assert srv.manager._editing_runs == {}
    job = ok(srv, "POST", f"/api/items/{item_id}/review", {"options": {"speed": 1}}, status=201)
    resp, data = request(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 0}})
    assert resp.status == 409 and data["run_id"] == job["run_id"] and data["item_id"] == item_id, data
    assert "재검수하는 중" in data["error"]
    ok(srv, "POST", job["cancel_url"], {}, status=202)
    wait_run(srv, job["run_id"])
    ok(srv, "POST", f"/api/runs/{run_id}/resume", {"options": {"speed": 0}}, status=202)
    assert wait_run(srv, run_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------


def _download(srv, path):
    resp, data = request(srv, "GET", path, raw=True)
    assert resp.status == 200, (path, resp.status, data[:300])
    disposition = resp.getheader("Content-Disposition")
    assert disposition.startswith("attachment; filename=") and "filename*=UTF-8''" in disposition
    assert resp.getheader("Cache-Control") == "no-store" and resp.getheader("X-Content-Type-Options") == "nosniff"
    return resp, data


def test_item_exports_have_download_headers(srv):
    run_id, _ = finished_run(srv, ("bizplan", "naver_blog", "linkedin", "instagram"))
    ids = {ch: pipeline_item_id(run_id, ch) for ch in ("bizplan", "naver_blog", "linkedin", "instagram")}

    resp, data = _download(srv, f"/api/items/{ids['linkedin']}/export?format=txt")
    assert resp.getheader("Content-Type") == "text/plain; charset=utf-8" and data.decode("utf-8").strip()
    name = urllib.parse.unquote(resp.getheader("Content-Disposition").split("filename*=UTF-8''", 1)[1])
    assert name.endswith(".txt") and "_linkedin_" in name
    resp, data = _download(srv, f"/api/items/{ids['naver_blog']}/export?format=html")
    assert resp.getheader("Content-Type").startswith("text/html") and b"<" in data
    resp, data = _download(srv, f"/api/items/{ids['bizplan']}/export?format=md")
    assert resp.getheader("Content-Type").startswith("text/markdown")
    resp, data = _download(srv, f"/api/items/{ids['instagram']}/export")  # default = the channel's first format (zip)
    assert resp.getheader("Content-Type") == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "caption.txt" in names and "alt-text.txt" in names
    notes = urllib.parse.unquote(resp.getheader("X-Insia-Notes") or "")
    assert "slides.html" in notes and "slides.html" in names  # PNG rendering is off here
    info = ok(srv, "GET", f"/api/items/{ids['instagram']}/export?format=zip&info=1")
    assert info["filename"].endswith(".zip") and info["size"] == len(data) and isinstance(info["notes"], list)

    resp, data = request(srv, "GET", f"/api/items/{ids['bizplan']}/export?format=docx", raw=True)
    if capabilities()["docx"]:
        assert resp.status == 200 and data.startswith(b"PK")
    else:
        assert resp.status == 501 and "pip install" in json.loads(data)["error"]
    for query, status in (("format=pdf", 400), ("format=html", 400), ("format=md&version=99", 400), ("format=md&version=x", 400)):
        resp, data = request(srv, "GET", f"/api/items/{ids['linkedin']}/export?{query}")
        assert resp.status == status and data["error"], query
    assert request(srv, "GET", f"/api/items/{ids['linkedin']}/export?format=md&version=1", raw=True)[0].status == 200
    resp, _ = request(srv, "GET", "/api/items/it_unknown/export?format=md")
    assert resp.status == 404

    resp, data = _download(srv, f"/api/runs/{run_id}/export")
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert any(n.endswith("sources.md") for n in archive.namelist()) and any(n.endswith("README.txt") for n in archive.namelist())
    resp, _ = request(srv, "GET", "/api/runs/unknown-run/export")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# calendar & usage
# ---------------------------------------------------------------------------


def test_calendar_plan_generate_and_link(srv):
    plan = ok(srv, "POST", "/api/calendar/plan", {"theme": "AI로 콘텐츠 운영 줄이기", "start": "2026-10-05", "days": 5,
                                                  "counts": {"naver_blog": 1, "linkedin": 2}}, status=201)
    assert plan["mode"] == "mock" and plan["end"] == "2026-10-09" and isinstance(plan["notices"], list)
    assert sorted(s["channel"] for s in plan["slots"]) == ["linkedin", "linkedin", "naver_blog"]
    listed = ok(srv, "GET", "/api/calendar?from=2026-10-05&to=2026-10-11")["slots"]
    assert {s["id"] for s in listed} == {s["id"] for s in plan["slots"]}
    assert all(s["status"] == "planned" and s["item_status"] is None for s in listed)
    slot = next(s for s in listed if s["channel"] == "naver_blog")

    started = ok(srv, "POST", f"/api/calendar/{slot['id']}/generate", {"options": {"speed": 0}}, status=201)
    assert started["slot_id"] == slot["id"] and started["item_id"] == pipeline_item_id(started["run_id"], "naver_blog")
    assert started["slot"]["status"] == "generating"
    events = read_sse(srv, started["events_url"])
    assert events[-1]["type"] == "run.completed"
    job = wait_run(srv, started["run_id"])
    assert job["kind"] == "slot" and job["job"]["slot"]["status"] == "drafted"
    linked = next(s for s in ok(srv, "GET", "/api/calendar")["slots"] if s["id"] == slot["id"])
    assert linked["status"] == "drafted" and linked["item_id"] == started["item_id"]
    assert linked["item_status"] in ("draft", "needs_changes")
    item = ok(srv, "GET", f"/api/items/{started['item_id']}")["item"]
    assert item["scheduled_at"] == slot["date"]
    resp, data = request(srv, "POST", f"/api/calendar/{slot['id']}/generate", {})
    assert resp.status == 409 and data["item_id"] == started["item_id"] and data["can_force"] is True

    other = next(s for s in listed if s["channel"] == "linkedin")
    moved = ok(srv, "POST", f"/api/calendar/{other['id']}", {"date": "2026-10-08", "topic": "바뀐 주제"})["slot"]
    assert moved["date"] == "2026-10-08" and moved["topic"] == "바뀐 주제"
    assert ok(srv, "POST", f"/api/calendar/{other['id']}", {"status": "skipped"})["slot"]["status"] == "skipped"
    resp, _ = request(srv, "POST", f"/api/calendar/{other['id']}/generate", {})
    assert resp.status == 409
    for body, status in (({"status": "drafted"}, 400), ({"channel": "bizplan"}, 400), ({"date": "10/08"}, 400),
                         ({"status": "planned"}, 200)):
        resp, _ = request(srv, "POST", f"/api/calendar/{other['id']}", body)
        assert resp.status == status, body
    resp, _ = request(srv, "POST", f"/api/calendar/{slot['id']}", {"status": "planned"})
    assert resp.status == 409
    resp, _ = request(srv, "POST", "/api/calendar/sl_missing", {"date": "2026-10-06"})
    assert resp.status == 404
    resp, _ = request(srv, "POST", "/api/calendar/sl_missing/generate", {})
    assert resp.status == 404
    for body in ({"theme": "t", "start": "2026-13-01", "counts": {"linkedin": 1}}, {"theme": "t", "start": "2026-10-05"},
                 {"theme": "t", "start": "2026-10-05", "counts": {"tiktok": 1}},
                 {"theme": "t", "start": "2026-10-05", "end": "2026-10-01", "counts": {"linkedin": 1}}):
        resp, data = request(srv, "POST", "/api/calendar/plan", body)
        assert resp.status == 400 and data["error"], body
    resp, _ = request(srv, "GET", "/api/calendar?from=yesterday")
    assert resp.status == 400


def test_usage_summary_with_budget(servers, settings):
    srv = servers(replace(settings, max_cost_usd=3.5))
    run_id, _ = finished_run(srv)
    usage = ok(srv, "GET", "/api/usage?since=2020-01-01")
    assert usage["budget_usd"] == 3.5 and usage["currency"] == "USD" and usage["total_usd"] == 0
    entry = next(r for r in usage["runs"] if r["run_id"] == run_id)
    assert entry["calls"] > 0 and entry["started_at"] and entry["status"] == "completed"
    assert entry["mode"] == "mock"  # the dashboard explains why mock runs cost $0
    assert usage["by_day"] and usage["by_task"]
    assert ok(srv, "GET", "/api/health")["budget_usd"] == 3.5
    resp, data = request(srv, "GET", "/api/usage?since=last-week")
    assert resp.status == 400 and data["error"]


# ---------------------------------------------------------------------------
# access token
# ---------------------------------------------------------------------------


def test_non_loopback_bind_needs_a_strong_token(settings, monkeypatch):
    with pytest.raises(ServerConfigError, match="접근 토큰이 필요해요"):
        make_server(settings, host="0.0.0.0", port=0)
    with pytest.raises(ServerConfigError, match="너무 짧아요"):
        make_server(settings, host="0.0.0.0", port=0, token="short")
    with pytest.raises(ServerConfigError, match="영문"):
        make_server(settings, host="127.0.0.1", port=0, token="토큰토큰토큰토큰토큰토큰토큰")
    with pytest.raises(ServerConfigError, match="public-host"):
        make_server(settings, host="127.0.0.1", port=0, public_hosts=["https://insia.example.com"])
    with pytest.raises(ServerConfigError, match="리버스 프록시"):  # loopback behind a proxy is still public
        make_server(settings, host="127.0.0.1", port=0, public_hosts=["insia.example.com"])
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", "insia.example.com")
    with pytest.raises(ServerConfigError, match="리버스 프록시"):
        make_server(settings, host="127.0.0.1", port=0)
    monkeypatch.setenv("INSIA_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("INSIA_PUBLIC_HOSTS", "insia.example.com, Other.Example.org")
    monkeypatch.setenv("INSIA_TRUST_PROXY", "1")
    srv = make_server(settings, host="0.0.0.0", port=0)  # a container configured by environment variables only
    try:
        assert srv.token_required and srv.session_value and srv.session_value != TOKEN
        assert srv.public_hosts == {"insia.example.com", "other.example.org"} and srv.trust_proxy is True
    finally:
        srv.server_close()


def test_token_mode_login_cookie_bearer_and_logout(servers, settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>INSIA</title>", encoding="utf-8")
    srv = servers(settings, token=TOKEN, web_dir=web)
    resp, data = request(srv, "GET", "/api/health")
    assert resp.status == 401 and data["login"] is True and data["error"]
    assert resp.getheader("WWW-Authenticate", "").startswith("Bearer")
    resp, data = request(srv, "GET", "/api/does-not-exist")
    assert resp.status == 401  # unknown routes reveal nothing before login
    resp, body = request(srv, "GET", "/", raw=True)
    assert resp.status == 200 and b"INSIA" in body and resp.getheader("X-Frame-Options") == "DENY"
    resp, _ = request(srv, "POST", "/api/runs", {"topic": "무단", "channels": ["linkedin"]})
    assert resp.status == 401

    resp, data = request(srv, "POST", "/api/login", {"token": "wrong-token-000000"})
    assert resp.status == 401 and data["login"] is True and resp.getheader("Set-Cookie") is None
    resp, data = request(srv, "POST", "/api/login", {})
    assert resp.status == 400
    resp, data = request(srv, "POST", "/api/login", {"token": TOKEN})
    assert resp.status == 200 and data == {"ok": True, "token_required": True}
    cookie = resp.getheader("Set-Cookie")
    for part in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={30 * 24 * 3600}"):
        assert part in cookie
    assert "Secure" not in cookie and TOKEN not in cookie
    value = cookie.split(";", 1)[0]
    assert value.startswith(f"{COOKIE_NAME}=")

    health = ok(srv, "GET", "/api/health", headers={"Cookie": f"theme=dark; {value}"})
    assert health["token_required"] is True
    assert ok(srv, "GET", "/api/profile", headers={"Authorization": f"Bearer {TOKEN}"})["profile"] is not None
    resp, _ = request(srv, "GET", "/api/profile", headers={"Authorization": "Bearer nope-nope-nope"})
    assert resp.status == 401
    resp, _ = request(srv, "GET", "/api/profile", headers={"Authorization": f"Basic {TOKEN}"})
    assert resp.status == 401
    resp, _ = request(srv, "GET", "/api/profile", headers={"Cookie": f"{COOKIE_NAME}=forged"})
    assert resp.status == 401 and "Max-Age=0" in resp.getheader("Set-Cookie")
    # a logged-in browser still cannot be driven by another site (CSRF)
    resp, _ = request(srv, "POST", "/api/documents", {"title": "x", "text": "y"},
                      headers={"Cookie": value, "Origin": "http://evil.example"})
    assert resp.status == 403
    created = ok(srv, "POST", "/api/runs", {"topic": "토큰으로", "channels": ["linkedin"], "options": {"speed": 0}},
                 status=201, headers={"Cookie": value})
    events = read_sse(srv, created["events_url"], headers={"Cookie": value})  # EventSource sends the cookie
    assert events[-1]["type"] == "run.completed"

    resp, data = request(srv, "POST", "/api/logout", {}, headers={"Cookie": value})
    assert resp.status == 200 and "Max-Age=0" in resp.getheader("Set-Cookie")


def test_secure_cookie_only_behind_a_trusted_https_proxy(servers, settings):
    plain = servers(settings, token=TOKEN)
    resp, _ = request(plain, "POST", "/api/login", {"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
    assert resp.status == 200 and "Secure" not in resp.getheader("Set-Cookie")
    servers.stop(plain)
    proxied = servers(settings, token=TOKEN, trust_proxy=True, public_hosts=["insia.example.com"])
    headers = {"X-Forwarded-Proto": "https", "Host": "insia.example.com", "Origin": "https://insia.example.com",
               "X-Forwarded-For": "203.0.113.7"}
    resp, _ = request(proxied, "POST", "/api/login", {"token": TOKEN}, headers=headers)
    assert resp.status == 200 and "; Secure" in resp.getheader("Set-Cookie")
    resp, _ = request(proxied, "POST", "/api/login", {"token": TOKEN}, headers={**headers, "Host": "other.example.com"})
    assert resp.status == 403


def test_public_host_and_https_origin_rules(servers, settings):
    srv = servers(settings, token=TOKEN, public_hosts=["insia.example.com"])
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert ok(srv, "GET", "/api/health", headers={**auth, "Host": "insia.example.com"})["token_required"]
    resp, _ = request(srv, "GET", "/api/health", headers={**auth, "Host": "evil.example.com"})
    assert resp.status == 403
    body = {"title": "도메인", "text": "본문"}
    ok(srv, "POST", "/api/documents", body, status=201,
       headers={**auth, "Host": "insia.example.com:8443", "Origin": "http://insia.example.com:8443"})
    resp, data = request(srv, "POST", "/api/documents", body,
                         headers={**auth, "Host": "insia.example.com", "Origin": "https://insia.example.com",
                                  "X-Forwarded-Proto": "https"})
    assert resp.status == 403 and "--trust-proxy" in data["error"]  # https Origin without a trusted proxy
    resp, _ = request(srv, "POST", "/api/documents", body, headers={**auth, "Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403


def test_failed_logins_are_rate_limited(servers, settings):
    srv = servers(settings, token=TOKEN)
    for _ in range(10):
        resp, _ = request(srv, "POST", "/api/login", {"token": "wrong-token-000000"})
        assert resp.status in (401, 429)
    resp, data = request(srv, "POST", "/api/login", {"token": TOKEN})  # blocked even with the right token
    assert resp.status == 429 and int(resp.getheader("Retry-After")) > 0 and data["error"]
    resp, _ = request(srv, "GET", "/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status == 429


def test_login_limiter_window():
    now = [0.0]
    limiter = LoginLimiter(max_failures=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.retry_after("1.2.3.4") == 0
        limiter.fail("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == pytest.approx(60)
    assert limiter.retry_after("5.6.7.8") == 0
    now[0] = 61.0
    assert limiter.retry_after("1.2.3.4") == 0
    limiter.fail("1.2.3.4")
    limiter.reset("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") == 0


def test_client_key_groups_ipv6_by_64_and_unmaps_ipv4():
    assert client_key("203.0.113.9") == "203.0.113.9"
    assert client_key("::ffff:203.0.113.9") == "203.0.113.9"  # how a dual-stack "::" socket shows IPv4 clients
    assert client_key("2001:db8:1:2::1") == client_key("2001:db8:1:2:ffff:1:2:3") == "2001:db8:1:2::/64"
    assert client_key("[2001:db8:1:3::1]") == "2001:db8:1:3::/64" != client_key("2001:db8:1:2::1")
    assert client_key("fe80::1%eth0") == "fe80::/64"
    assert client_key("") == "" and client_key("not-an-ip") == "not-an-ip"


def test_login_limit_covers_an_ipv6_clients_whole_64(servers, settings):
    """Keyed by the full address, one IPv6 subscriber could rotate through its /64 for fresh guesses."""
    srv = servers(settings, token=TOKEN, trust_proxy=True)

    def login(client, token="wrong-token-000000"):
        return request(srv, "POST", "/api/login", {"token": token}, headers={"X-Forwarded-For": client})[0].status

    statuses = [login(f"2001:db8:1:2::{i:x}") for i in range(1, 11)]  # a new source address for every guess
    assert statuses.count(401) == 9 and statuses[-1] == 429
    assert login("2001:db8:1:2:ffff:ffff:ffff:ffff", TOKEN) == 429  # same /64: still locked out
    assert login("2001:db8:1:3::1", TOKEN) == 200  # another /64 is another client
    for _ in range(10):
        login("::ffff:198.51.100.7")
    assert login("198.51.100.7", TOKEN) == 429  # an IPv4-mapped address is the IPv4 client


def test_login_without_a_token_configured(srv):
    assert ok(srv, "POST", "/api/login", {"token": "anything"}) == {"ok": True, "token_required": False}


def test_trust_proxy_needs_a_token(settings, monkeypatch):
    """--trust-proxy means a reverse proxy is in front, so 127.0.0.1 is reachable from outside (finding 14)."""
    with pytest.raises(ServerConfigError, match="리버스 프록시") as info:
        make_server(settings, host="127.0.0.1", port=0, trust_proxy=True)
    assert "접근 토큰" in str(info.value) and "INSIA_ACCESS_TOKEN" in str(info.value)
    monkeypatch.setenv("INSIA_TRUST_PROXY", "yes")
    with pytest.raises(ServerConfigError, match="trust-proxy"):
        make_server(settings, host="127.0.0.1", port=0)
    srv = make_server(settings, host="127.0.0.1", port=0, token=TOKEN)
    try:
        assert srv.trust_proxy is True and srv.token_required
    finally:
        srv.server_close()


def _raw_login_burst(srv, guesses):
    """Send every login's headers first (all handlers pass the pre-body check), then all bodies at once."""
    import socket as socket_module

    port = srv.server_address[1]
    socks = []
    bodies = []
    for guess in guesses:
        body = json.dumps({"token": guess}).encode()
        s = socket_module.create_connection(("127.0.0.1", port), timeout=20)
        s.sendall((f"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode())
        socks.append(s)
        bodies.append(body)
    time.sleep(0.5)  # every handler now waits for its body, after the lockout pre-check
    for s, body in zip(socks, bodies):
        s.sendall(body)
    answers = []
    for s in socks:
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        answers.append((int(head.split(" ", 2)[1]), "set-cookie:" in head.lower()))
    return answers


def test_parallel_logins_compare_at_most_the_limit(servers, settings):
    """Finding 2: guesses held back until they are all in flight still get only LOGIN_MAX_FAILURES comparisons."""
    srv = servers(settings, token=TOKEN)
    compared = []
    real_check = srv.check_bearer

    def counting_check(value):
        compared.append(value)
        return real_check(value)

    srv.check_bearer = counting_check
    guesses = [f"wrong-guess-{i:06d}" for i in range(29)] + [TOKEN]  # the right token arrives last
    answers = _raw_login_burst(srv, guesses)
    assert 0 < len(compared) <= server_module.LOGIN_MAX_FAILURES  # before the fix: all 30 were compared
    statuses = [status for status, _ in answers]
    assert set(statuses) <= {200, 401, 429} and statuses.count(401) < server_module.LOGIN_MAX_FAILURES
    assert statuses.count(429) >= len(guesses) - server_module.LOGIN_MAX_FAILURES
    if TOKEN in compared:  # it happened to get one of the slots
        assert answers[-1] == (200, True)
    else:  # the slots went to wrong guesses: the right token was refused without being compared
        assert answers[-1] == (429, False)
    assert sum(1 for _, cookie in answers if cookie) == statuses.count(200) <= 1


def test_login_limiter_attempt_is_atomic_under_a_thread_burst():
    limiter = LoginLimiter(max_failures=5, window=60)
    barrier = threading.Barrier(40, timeout=10)
    granted = []

    def guess():
        barrier.wait()
        if limiter.attempt("203.0.113.9") == 0:
            granted.append(1)

    threads = [threading.Thread(target=guess) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(granted) == 5  # exactly the limit, however the threads interleave
    assert limiter.retry_after("203.0.113.9") > 0
    assert limiter.attempt("203.0.113.9") > 0  # refused attempts do not extend the lockout...
    assert len(limiter._failures["203.0.113.9"]) == 5
    limiter.release("203.0.113.9")  # ...and a correct credential gives its reserved slot back
    assert limiter.retry_after("203.0.113.9") == 0 and limiter.attempt("203.0.113.9") == 0
    limiter.reset("203.0.113.9")
    limiter.release("203.0.113.9")  # nothing to release: no error
    assert limiter.retry_after("203.0.113.9") == 0


def test_bearer_guesses_are_capped_and_valid_tokens_do_not_use_up_the_limit(servers, settings):
    srv = servers(settings, token=TOKEN)
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    for _ in range(server_module.LOGIN_MAX_FAILURES - 1):
        resp, _ = request(srv, "GET", "/api/health", headers={"Authorization": "Bearer wrong-guess-0000"})
        assert resp.status == 401
    for _ in range(30):  # each valid request reserves an attempt and gives it back
        ok(srv, "GET", "/api/health", headers=bearer)
    resp, _ = request(srv, "GET", "/api/health", headers={"Authorization": "Bearer"})  # an empty token is a failure too
    assert resp.status == 401
    resp, _ = request(srv, "GET", "/api/health", headers=bearer)
    assert resp.status == 429 and int(resp.getheader("Retry-After")) > 0


def test_basic_authorization_header_falls_back_to_the_cookie(servers, settings):
    """Finding 3: an nginx/Caddy basic-auth header in front must neither block the cookie nor count as a failure."""
    srv = servers(settings, token=TOKEN)
    resp, _ = request(srv, "POST", "/api/login", {"token": TOKEN})
    cookie = resp.getheader("Set-Cookie").split(";", 1)[0]
    basic = {"Authorization": "Basic dXNlcjpwYXNz"}
    for _ in range(server_module.LOGIN_MAX_FAILURES + 5):
        assert ok(srv, "GET", "/api/profile", headers={**basic, "Cookie": cookie})["profile"] is not None
    for _ in range(server_module.LOGIN_MAX_FAILURES + 5):  # without the cookie: "log in", but no failed attempt
        resp, data = request(srv, "GET", "/api/profile", headers=basic)
        assert resp.status == 401 and data["error"] == server_module.LOGIN_REQUIRED
    ok(srv, "GET", "/api/profile", headers={"Cookie": cookie})
    ok(srv, "GET", "/api/profile", headers={"Authorization": f"Bearer {TOKEN}"})
    resp, _ = request(srv, "GET", "/api/profile", headers={"Authorization": "Bearer wrong-guess-0000", "Cookie": cookie})
    assert resp.status == 401  # a Bearer header is still the credential that counts


def test_run_manager_keeps_legacy_constructor(settings):
    manager = server_module.RunManager(settings, max_active=3)
    try:
        assert manager.max_live == manager.max_mock == 3 and manager.list() == []
    finally:
        manager.shutdown()


def test_resumable_flag_in_run_list_for_stopped_and_partial_runs(srv):
    ws = srv.manager.workspace
    brief = Brief(topic="목록의 이어서 실행", channels=["linkedin", "instagram"])
    ws.create_run("20260928-000001-aaaa", brief, kind="pipeline", mode="mock", model="mock")
    ws.update_run("20260928-000001-aaaa", status="cancelled")
    ws.create_run("20260928-000002-bbbb", brief, kind="review", mode="mock", model="mock")
    ws.update_run("20260928-000002-bbbb", status="failed")
    ws.create_run("20260928-000003-cccc", brief, kind="pipeline", mode="mock", model="mock")
    ws.update_run("20260928-000003-cccc", status="completed")  # finished without any channel item
    rows = {r["run_id"]: r["resumable"] for r in ok(srv, "GET", "/api/runs")["runs"]}
    assert rows == {"20260928-000001-aaaa": True, "20260928-000002-bbbb": False, "20260928-000003-cccc": True}
    assert ok(srv, "GET", "/api/runs/20260928-000003-cccc")["resumable"] is True


def test_port_in_use_raises_oserror_without_a_cleanup_traceback(srv, settings, caplog):
    other = replace(settings, home=settings.home.parent / "other-workspace")
    with caplog.at_level("ERROR", logger="insia_agents.server"):
        with pytest.raises(OSError):
            make_server(other, host="127.0.0.1", port=srv.server_address[1])
    assert not [r for r in caplog.records if "작업 정리" in r.getMessage()]
