from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from insia_agents import db as dbmod
from insia_agents.db import (ApprovalBlockedError, InvalidTransitionError, NotFoundError, Workspace, WorkspaceError,
                             normalize_hashtags, pipeline_item_id, profile_is_empty)
from insia_agents.models import (Brief, ChannelResult, Draft, PlannedSlot, Plan, Profile, ResearchPack, Review, RubricScore,
                                 TeamMember, UsageRecord)


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    yield workspace
    workspace.close()


def _brief(channels=("linkedin",)) -> Brief:
    return Brief(topic="테스트 주제", channels=list(channels), keywords=["AI 마케팅 자동화"])


def _draft(channel="linkedin", round=0, title="제목", content="본문") -> Draft:
    return Draft(channel=channel, round=round, title=title, content=content, hashtags=["#a", "#b", "#c"])


def _review(channel="linkedin", round=0, score=85, passed=True) -> Review:
    return Review(channel=channel, round=round, score=score, passed=passed,
                  rubric=[RubricScore(id="hook", label="훅", score=20, max=25, comment="")], issues=[], summary="요약")


# ---------------------------------------------------------------------------
# Layout, migrations, pragmas
# ---------------------------------------------------------------------------


def test_layout_and_migrations_are_idempotent(tmp_path):
    home = tmp_path / "ws"
    with Workspace(home) as first:
        assert first.schema_version == len(dbmod.MIGRATIONS)
        first.save_profile(Profile(company_name="인시아"))
    for sub in ("exports", "uploads", "logs"):
        assert (home / sub).is_dir()
    assert (home / "insia.db").is_file()
    with Workspace(home) as again:  # reopening runs no migration twice and keeps the data
        assert again.schema_version == len(dbmod.MIGRATIONS)
        assert again.get_profile().company_name == "인시아"
        rows = again._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert rows == 1


def test_forward_migration_applies_once(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    Workspace(home).close()
    extra = "CREATE TABLE IF NOT EXISTS extra (x INTEGER);\nINSERT INTO extra (x) VALUES (1);"
    shipped = len(dbmod.MIGRATIONS)
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, extra])
    for _ in range(2):
        with Workspace(home) as workspace:
            assert workspace.schema_version == shipped + 1
            assert workspace._conn.execute("SELECT COUNT(*) FROM extra").fetchone()[0] == 1


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    Workspace(home).close()
    broken = "CREATE TABLE half_done (x INTEGER);\nINSERT INTO missing_table VALUES (1);"
    shipped = len(dbmod.MIGRATIONS)
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, broken])
    with pytest.raises(WorkspaceError, match="되돌렸어요"):
        Workspace(home)
    conn = sqlite3.connect(home / "insia.db")
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == shipped
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    finally:
        conn.close()


def test_newer_database_is_refused(tmp_path):
    home = tmp_path / "ws"
    Workspace(home).close()
    conn = sqlite3.connect(home / "insia.db")
    conn.execute("UPDATE schema_version SET version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(WorkspaceError, match="업데이트"):
        Workspace(home)


def test_wal_foreign_keys_and_from_settings(tmp_path, settings):
    from dataclasses import replace

    with Workspace.from_settings(replace(settings, home=tmp_path / "home_ws")) as workspace:
        assert workspace.home == tmp_path / "home_ws"
        assert workspace._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert workspace._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(WorkspaceError, match="닫혔"):
        workspace.get_profile()


# ---------------------------------------------------------------------------
# Profile & documents
# ---------------------------------------------------------------------------


def test_profile_roundtrip(ws):
    assert ws.get_profile() == Profile()
    assert profile_is_empty(ws.get_profile())
    saved = ws.save_profile(Profile(company_name="인시아", team=[TeamMember(role="대표", name="김대표")],
                                    banned_words=["최고"]))
    assert saved.updated_at.endswith("Z") and len(saved.updated_at) == len("2026-09-28T00:00:00.000Z")
    loaded = ws.get_profile()
    assert loaded == saved and not profile_is_empty(loaded)
    ws.save_profile({"service_name": "스마트에이전트"})  # a dict works too and replaces the whole profile
    assert ws.get_profile().company_name == "" and ws.get_profile().service_name == "스마트에이전트"


def test_delete_document_removes_only_its_uploaded_originals(ws):
    first = ws.add_document("소개서", "본문 1", filename="intro.md")
    for _ in range(9):
        ws.add_document("채우기", "본문")
    tenth = ws.get_document("u10")
    assert first.id == "u1" and tenth is not None
    ws.uploads_dir.mkdir(parents=True, exist_ok=True)
    mine = ws.uploads_dir / "u1_intro.md"
    other = ws.uploads_dir / "u10_deck.pdf"
    mine.write_text("x", encoding="utf-8")
    other.write_text("y", encoding="utf-8")
    assert ws.delete_document("u1") is True
    assert not mine.exists() and other.exists()  # "u1_" must not match "u10_"
    assert ws.delete_document("u1") is False and other.exists()


def test_document_ids_are_monotonic_and_never_reused(ws):
    first = ws.add_document("회사 소개서", "우리 회사는 …", kind="markdown", filename="C:\\docs\\intro.md")
    second = ws.add_document("", "IR 자료 본문", filename="ir.txt")
    assert (first.id, second.id) == ("u1", "u2")
    assert first.filename == "intro.md" and first.chars == len("우리 회사는 …")
    assert second.title == "ir"  # falls back to the file name
    assert ws.delete_document("u2") is True
    assert ws.delete_document("u2") is False
    third = ws.add_document("새 자료", "내용")
    assert third.id == "u3"
    assert [d.id for d in ws.list_documents()] == ["u1", "u3"]
    assert ws.get_document("u1").text == "우리 회사는 …"
    assert ws.get_document("u2") is None
    with pytest.raises(WorkspaceError, match="비어"):
        ws.add_document("빈 자료", "   ")
    with pytest.raises(WorkspaceError, match="종류"):
        ws.add_document("자료", "내용", kind="hwp")


# ---------------------------------------------------------------------------
# Runs & events
# ---------------------------------------------------------------------------


def test_run_lifecycle(ws):
    ws.create_run("run-1", _brief(), options={"max_rounds": 2}, mode="mock", model="m", profile=Profile(company_name="인시아"))
    with pytest.raises(WorkspaceError, match="이미"):
        ws.create_run("run-1", _brief())
    with pytest.raises(WorkspaceError):
        ws.create_run("../evil", _brief())
    run = ws.get_run("run-1")
    json.dumps(run, ensure_ascii=False)
    assert run["status"] == "running" and run["kind"] == "pipeline" and run["finished_at"] is None
    assert run["profile"]["company_name"] == "인시아" and run["options"] == {"max_rounds": 2}
    assert run["plan"] is None and run["research"] is None and run["progress"] == {}

    plan = Plan(summary="요약", key_messages=["a"], questions=[], outlines=[])
    ws.update_run("run-1", plan=plan, research=ResearchPack(findings=[], sources=[], gaps=["x"]), progress={"channels": {}})
    ws.update_run("run-1", status="failed", error="실패")
    run = ws.get_run("run-1")
    assert run["plan"]["summary"] == "요약" and run["research"]["gaps"] == ["x"]
    assert run["status"] == "failed" and run["error"] == "실패" and run["finished_at"]
    ws.update_run("run-1", status="running")  # resume clears the error and finish time
    run = ws.get_run("run-1")
    assert run["error"] is None and run["finished_at"] is None
    with pytest.raises(TypeError):
        ws.update_run("run-1", nonsense=1)
    with pytest.raises(WorkspaceError):
        ws.update_run("run-1", status="done")
    with pytest.raises(NotFoundError):
        ws.update_run("nope", status="failed")

    assert ws.claim_run("run-1", "failed") is False  # it is running, not failed
    ws.update_run("run-1", status="interrupted")
    assert ws.claim_run("run-1", "interrupted") is True and ws.claim_run("run-1", "interrupted") is False
    assert ws.get_run("run-1")["status"] == "running"
    ws.create_run("run-2", _brief(), kind="review", parent_item_id="it_x")
    assert [r["run_id"] for r in ws.list_runs()] == ["run-2", "run-1"]
    assert [r["run_id"] for r in ws.list_runs(kind="review")] == ["run-2"]
    assert ws.get_run("nope") is None


def test_events_append_and_list(ws):
    ws.create_run("run-1", _brief())
    for seq in (1, 2, 3):
        ws.append_event("run-1", {"seq": seq, "t": float(seq), "ts": "2026-09-28T00:00:00.000Z", "run_id": "run-1",
                                  "type": "log", "agent": "system", "data": {"message": str(seq)}})
    ws.append_event("run-1", {"type": "log", "data": {"message": "auto"}})  # no seq → next number
    events = ws.list_events("run-1")
    assert [e["seq"] for e in events] == [1, 2, 3, 4] and events[3]["data"]["message"] == "auto"
    assert [e["seq"] for e in ws.list_events("run-1", after_seq=2)] == [3, 4]
    assert ws.last_event("run-1")["seq"] == 4
    assert ws.get_run("run-1")["events"] == 4
    with pytest.raises(WorkspaceError, match="겹쳐"):
        ws.append_event("run-1", {"seq": 2, "type": "log"})
    with pytest.raises(NotFoundError):
        ws.append_event("unknown-run", {"seq": 1, "type": "log"})


def test_mark_interrupted(ws):
    ws.create_run("run-a", _brief())
    ws.append_event("run-a", {"seq": 1, "t": 3.5, "type": "run.started", "agent": "system", "data": {}})
    ws.create_run("run-b", _brief())
    ws.update_run("run-b", status="completed")
    slot = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic="주제", angle="사례", keywords=["a"], goal="인지")])[0]
    ws.update_slot(slot.id, status="generating")
    assert ws.mark_interrupted() == 1
    run = ws.get_run("run-a")
    assert run["status"] == "interrupted" and "이어서" in run["error"]
    last = ws.last_event("run-a")
    assert last["type"] == "run.failed" and last["seq"] == 2 and last["t"] == 3.5 and last["data"]["interrupted"] is True
    assert ws.get_run("run-b")["status"] == "completed"
    assert ws.get_slot(slot.id).status == "planned"
    assert ws.mark_interrupted() == 0


def test_concurrent_writes_from_threads(ws):
    ws.create_run("run-c", _brief())
    errors: list[BaseException] = []
    per_thread = 25

    def worker(n: int) -> None:
        try:
            for i in range(per_thread):
                ws.add_document(f"자료 {n}-{i}", "내용")
                ws.record_usage(UsageRecord(run_id="run-c", task="draft", cost_usd=0.01, input_tokens=10))
                ws.append_event("run-c", {"type": "log", "agent": "system", "data": {"n": n, "i": i}})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    total = 8 * per_thread
    assert sorted(int(d.id[1:]) for d in ws.list_documents()) == list(range(1, total + 1))
    assert [e["seq"] for e in ws.list_events("run-c")] == list(range(1, total + 1))
    assert ws.run_cost("run-c") == pytest.approx(0.01 * total)
    assert ws.get_run("run-c")["cost_usd"] == pytest.approx(0.01 * total)
    assert ws.usage_summary()["input_tokens"] == 10 * total


# ---------------------------------------------------------------------------
# Content items
# ---------------------------------------------------------------------------


def test_versions_follow_reviews(ws):
    item = ws.create_item("linkedin", "첫 제목", brief=_brief())
    assert item.id.startswith("it_") and item.status == "draft"
    v1 = ws.add_version(item.id, _draft(title="R0 제목"), source="agent")
    detail = ws.get_item(item.id)
    assert v1.version == 1 and detail.item.title == "R0 제목" and detail.item.score is None and detail.item.passed is None
    ws.attach_review(v1.id, _review(score=60, passed=False))
    assert ws.get_item(item.id).item.status == "needs_changes"
    v2 = ws.add_version(item.id, _draft(round=1), source="agent", review=_review(round=1, score=90), instructions="짧게")
    detail = ws.get_item(item.id)
    assert v2.version == 2 and detail.item.version == 2 and detail.item.score == 90 and detail.item.passed is True
    assert detail.item.status == "draft"
    assert [v.version for v in detail.versions] == [1, 2] and detail.versions[1].instructions == "짧게"
    assert detail.brief.topic == "테스트 주제"
    assert ws.get_version(v1.id).review.score == 60
    with pytest.raises(WorkspaceError, match="채널"):
        ws.add_version(item.id, _draft(channel="instagram"), source="agent")
    with pytest.raises(WorkspaceError):
        ws.add_version(item.id, _draft(), source="robot")
    with pytest.raises(NotFoundError):
        ws.add_version("it_missing", _draft(), source="agent")
    with pytest.raises(NotFoundError):
        ws.attach_review("dv_missing", _review())


def test_status_transitions_with_approval_gate(ws):
    item = ws.create_item("linkedin", "제목")
    with pytest.raises(ApprovalBlockedError, match="버전이 없어요"):
        ws.set_item_status(item.id, "approved")
    version = ws.add_version(item.id, _draft(), source="agent", review=_review(score=72, passed=False))
    with pytest.raises(ApprovalBlockedError, match="72점") as blocked:
        ws.set_item_status(item.id, "approved")
    assert blocked.value.version == 1 and blocked.value.score == 72
    with pytest.raises(InvalidTransitionError, match="먼저 승인"):
        ws.set_item_status(item.id, "published")
    approved = ws.set_item_status(item.id, "approved", force=True, note="대표가 직접 확인")
    assert approved.status == "approved" and approved.note == "대표가 직접 확인"
    with pytest.raises(WorkspaceError, match="게시 예정일"):
        ws.set_item_status(item.id, "scheduled")
    with pytest.raises(WorkspaceError, match="YYYY-MM-DD"):
        ws.set_item_status(item.id, "scheduled", scheduled_at="10월 5일")
    scheduled = ws.set_item_status(item.id, "scheduled", scheduled_at="2026-10-05")
    assert scheduled.status == "scheduled" and scheduled.scheduled_at == "2026-10-05"
    # un-scheduling keeps the (forced) approval: no second gate
    assert ws.set_item_status(item.id, "approved").status == "approved"
    ws.set_item_status(item.id, "scheduled", scheduled_at="2026-10-05T09:00:00+09:00")
    with pytest.raises(WorkspaceError, match="http"):
        ws.set_item_status(item.id, "published", published_url="blog.naver.com/x")
    published = ws.set_item_status(item.id, "published", published_url="https://www.linkedin.com/posts/1")
    assert published.published_at.endswith("Z") and published.published_url.endswith("/1")
    first_published_at = published.published_at
    again = ws.set_item_status(item.id, "published", published_url="https://www.linkedin.com/posts/2")
    assert again.published_at == first_published_at and again.published_url.endswith("/2")
    assert [i.id for i in ws.published_history()] == [item.id]
    assert ws.published_history("instagram") == []
    with pytest.raises(InvalidTransitionError):
        ws.set_item_status(item.id, "draft")
    assert ws.set_item_status(item.id, "archived").status == "archived"
    assert ws.set_item_status(item.id, "draft").status == "draft"
    with pytest.raises(WorkspaceError, match="알 수 없는 상태"):
        ws.set_item_status(item.id, "deleted")
    with pytest.raises(NotFoundError):
        ws.set_item_status("it_missing", "archived")
    assert version.id  # the version stays


def test_approval_is_for_specific_content(ws):
    item = ws.create_item("instagram", "제목")
    ws.add_version(item.id, _draft(channel="instagram"), source="agent", review=_review(channel="instagram"))
    assert ws.set_item_status(item.id, "approved").status == "approved"  # passed review → no force needed
    ws.attach_review(ws.get_item(item.id).versions[-1].id, _review(channel="instagram", score=95))  # re-review keeps approval
    assert ws.get_item(item.id).item.status == "approved"
    ws.add_version(item.id, _draft(channel="instagram", round=1), source="human")  # new content → approval dropped
    assert ws.get_item(item.id).item.status == "draft"
    ws.add_version(item.id, _draft(channel="instagram", round=2), source="agent",
                   review=_review(channel="instagram", round=2, score=50, passed=False))
    assert ws.get_item(item.id).item.status == "needs_changes"


def test_update_item_and_list_filters(ws):
    a = ws.create_item("linkedin", "링크드인")
    b = ws.create_item("instagram", "인스타")
    time.sleep(0.01)
    ws.update_item(a.id, scheduled_at="2026-10-02", note="메모", title="새 제목")
    got = ws.get_item(a.id).item
    assert (got.scheduled_at, got.note, got.title) == ("2026-10-02", "메모", "새 제목")
    assert [i.id for i in ws.list_items(channel="instagram")] == [b.id]
    assert {i.id for i in ws.list_items(status="draft")} == {a.id, b.id}
    assert ws.list_items()[0].id == a.id  # most recently updated first
    with pytest.raises(WorkspaceError):
        ws.list_items(status="bogus")
    with pytest.raises(WorkspaceError):
        ws.create_item("tiktok", "x")
    assert ws.get_item("it_missing") is None


def test_upsert_from_result_is_idempotent_and_final_is_latest(ws):
    ws.create_run("run-u", _brief(("naver_blog",)))
    drafts = [_draft("naver_blog", r, title=f"R{r}") for r in range(3)]
    reviews = [_review("naver_blog", 0, 70, False), _review("naver_blog", 1, 78, False), _review("naver_blog", 2, 75, False)]
    result = ChannelResult(channel="naver_blog", final=drafts[1], drafts=drafts, reviews=reviews, passed=False, rounds=2)
    item = ws.upsert_item_from_result("run-u", result, _brief(("naver_blog",)))
    assert item.id == pipeline_item_id("run-u", "naver_blog") and item.run_id == "run-u"
    again = ws.upsert_item_from_result("run-u", result, _brief(("naver_blog",)))
    detail = ws.get_item(item.id)
    assert [v.draft.round for v in detail.versions] == [0, 1, 2, 1]  # every round + the best (R1) as the latest
    assert [v.review.score for v in detail.versions] == [70, 78, 75, 78]
    assert "가장 점수가 높아" in detail.versions[-1].draft.change_log[0]
    assert again.version == 4 and again.score == 78 and again.passed is False and again.status == "needs_changes"
    assert again.title == "R1"
    assert ws.get_run("run-u")["items"] == {"naver_blog": item.id}
    assert [v.draft.round for v in ws.list_run_versions("run-u", "naver_blog")] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_usage_summary_math(ws):
    ws.create_run("run-a", _brief())
    ws.create_run("run-b", _brief(), kind="review")
    records = [
        UsageRecord(run_id="run-a", task="plan", cost_usd=0.10, input_tokens=1000, output_tokens=200,
                    created_at="2026-09-27T14:59:59Z"),  # KST 2026-09-27 23:59:59
        UsageRecord(run_id="run-a", task="draft", cost_usd=0.25, input_tokens=3000, output_tokens=900, cache_read_tokens=500,
                    created_at="2026-09-27T15:00:00Z"),  # KST 2026-09-28 00:00:00
        UsageRecord(run_id="run-b", task="review", cost_usd=0.05, input_tokens=800, output_tokens=100, web_search_requests=2,
                    created_at="2026-09-28T03:00:00+00:00"),
        UsageRecord(task="plan_calendar", cost_usd=0.01, input_tokens=100, created_at="2026-09-28T04:00:00Z"),
        UsageRecord(run_id="run-a", task="draft", cost_usd=float("nan"), created_at="2026-09-28T05:00:00Z"),
        UsageRecord(run_id="run-a", task="draft", cost_usd=-3.0, created_at="2026-09-28T05:00:01Z"),
    ]
    for record in records:
        ws.record_usage(record)
    summary = ws.usage_summary()
    json.dumps(summary)
    assert summary["total_usd"] == pytest.approx(0.41)
    assert summary["calls"] == 6
    assert summary["input_tokens"] == 4900 and summary["output_tokens"] == 1200
    assert summary["cache_read_tokens"] == 500 and summary["web_search_requests"] == 2
    assert summary["by_day"] == [{"date": "2026-09-27", "usd": 0.1, "calls": 1},
                                 {"date": "2026-09-28", "usd": 0.31, "calls": 5}]
    assert summary["by_task"]["plan"] == {"usd": 0.1, "calls": 1, "input_tokens": 1000, "output_tokens": 200}
    assert summary["by_task"]["draft"]["usd"] == pytest.approx(0.25) and summary["by_task"]["draft"]["calls"] == 3
    assert summary["by_task"]["plan_calendar"]["usd"] == pytest.approx(0.01)
    runs = {r["run_id"]: r for r in summary["runs"]}
    assert runs["run-a"]["usd"] == pytest.approx(0.35) and runs["run-a"]["calls"] == 4 and runs["run-a"]["kind"] == "pipeline"
    assert runs["run-a"]["topic"] == "테스트 주제"
    assert runs["run-b"]["web_search_requests"] == 2 and runs[""]["kind"] == "other"
    assert summary["runs"][0]["run_id"] == "run-a"  # newest activity first
    assert ws.run_cost("run-a") == pytest.approx(0.35)
    assert ws.get_run("run-a")["cost_usd"] == pytest.approx(0.35)
    assert ws.usage_summary(since="2026-09-28")["total_usd"] == pytest.approx(0.31)
    assert ws.usage_summary(until="2026-09-27")["total_usd"] == pytest.approx(0.10)
    assert ws.usage_summary(since="2026-09-28T03:00:00Z", until="2026-09-28T04:00:00Z")["calls"] == 2
    with pytest.raises(WorkspaceError):
        ws.usage_summary(since="어제")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_slots(ws):
    slots = ws.add_slots([
        PlannedSlot(date="2026-10-02", channel="instagram", topic="캐러셀", angle="체크리스트", keywords=["a", " "], goal="저장"),
        PlannedSlot(date="2026-09-30", channel="linkedin", topic="관점 글", angle="경험", keywords=["b"], goal="인지"),
        PlannedSlot(date="2026-09-30", channel="naver_blog", topic="블로그", angle="가이드", keywords=["c"], goal="검색"),
    ])
    assert all(s.id.startswith("sl_") and s.status == "planned" for s in slots)
    assert slots[0].keywords == ["a"]
    listed = ws.list_slots()
    assert [(s.date, s.channel) for s in listed] == [("2026-09-30", "naver_blog"), ("2026-09-30", "linkedin"),
                                                     ("2026-10-02", "instagram")]
    assert [s.channel for s in ws.list_slots(date_from="2026-10-01", date_to="2026-10-31")] == ["instagram"]
    moved = ws.update_slot(slots[1].id, date="2026-10-03", status="skipped", keywords="x, y")
    assert (moved.date, moved.status, moved.keywords) == ("2026-10-03", "skipped", ["x", "y"])
    assert [s.channel for s in ws.due_slots("2026-10-02")] == ["naver_blog", "instagram"]  # skipped slots are not due
    assert ws.due_slots("2026-09-29") == []
    with pytest.raises(WorkspaceError):
        ws.update_slot(slots[0].id, status="done")
    with pytest.raises(WorkspaceError):
        ws.update_slot(slots[0].id, date="2026/10/01")
    with pytest.raises(TypeError):
        ws.update_slot(slots[0].id, color="red")
    with pytest.raises(NotFoundError):
        ws.update_slot("sl_missing", status="skipped")
    assert ws.get_slot("sl_missing") is None
    claimed = ws.claim_slot(slots[0].id, "run-x")
    assert (claimed.status, claimed.run_id) == ("generating", "run-x")
    with pytest.raises(WorkspaceError, match="만드는 중"):
        ws.claim_slot(slots[0].id, "run-y")
    ws.update_slot(slots[0].id, status="drafted", item_id="it_x")
    with pytest.raises(WorkspaceError, match="이미 초안"):
        ws.claim_slot(slots[0].id, "run-y")
    assert ws.claim_slot(slots[0].id, "run-y", force=True).run_id == "run-y"
    with pytest.raises(NotFoundError):
        ws.claim_slot("sl_missing", "run-z")
    with pytest.raises(WorkspaceError):
        ws.add_slots([PlannedSlot(date="내일", channel="linkedin", topic="x", angle="", keywords=[], goal="")])


def test_normalize_hashtags():
    assert normalize_hashtags(["AI 마케팅", "#창업", "창업", "", "#"]) == ["#AI마케팅", "#창업"]
    assert normalize_hashtags("#AI 마케팅 #창업, 스타트업") == ["#AI마케팅", "#창업", "#스타트업"]
    assert normalize_hashtags(None) == []


# ---------------------------------------------------------------------------
# Upgrade from the first schema, run ownership, recovery (review findings 0, 8, 19)
# ---------------------------------------------------------------------------


def _v1_workspace(home, *, run_updated_at: str):
    """A workspace written by the first INSIA release (schema version 1) with one run left 'running'."""
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "insia.db")
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.executescript(dbmod.MIGRATIONS[0])
        brief = _brief().model_dump_json()
        conn.execute("INSERT INTO runs (id, kind, status, brief, created_at, updated_at) VALUES (?, 'pipeline', 'running', ?, ?, ?)",
                     ("old-run", brief, run_updated_at, run_updated_at))
        conn.execute("INSERT INTO items (id, run_id, channel, title, status, version, approved_version, score, passed, created_at, "
                     "updated_at) VALUES ('it_old_linkedin', 'old-run', 'linkedin', '옛 글', 'approved', 1, 1, 90, 1, ?, ?)",
                     (run_updated_at, run_updated_at))
        conn.execute("INSERT INTO slots (id, date, channel, topic, status, run_id, created_at, updated_at) "
                     "VALUES ('sl_old', '2026-09-28', 'linkedin', '옛 슬롯', 'generating', 'old-run', ?, ?)",
                     (run_updated_at, run_updated_at))
        conn.commit()
    finally:
        conn.close()


def test_first_schema_workspace_upgrades_and_recovers_its_stale_run(tmp_path):
    home = tmp_path / "old"
    _v1_workspace(home, run_updated_at="2026-09-01T00:00:00.000Z")  # no heartbeat for weeks
    with Workspace(home) as ws:
        assert ws.schema_version == len(dbmod.MIGRATIONS)
        columns = {row[1] for row in ws._conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert {"owner_pid", "owner_host", "owner_boot", "owner_token", "heartbeat_at"} <= columns
        item = ws.get_item("it_old_linkedin").item  # old rows read fine with the new fields' defaults
        assert (item.status, item.approved_version, item.approval_forced, item.approved_score) == ("approved", 1, False, None)
        assert ws.get_run("old-run")["owner"] == {"pid": None, "host": None, "heartbeat_at": None}
        assert ws.mark_interrupted() == 1  # an old run without an owner: its last update is weeks old
        assert ws.get_run("old-run")["status"] == "interrupted"
        assert ws.get_slot("sl_old").status == "planned"
    with Workspace(home) as again:  # reopening does not migrate twice
        assert again.schema_version == len(dbmod.MIGRATIONS)


def test_first_schema_run_updated_recently_is_left_to_its_heartbeat(tmp_path):
    home = tmp_path / "old"
    _v1_workspace(home, run_updated_at=dbmod.utc_now())  # an older INSIA process may still be working on it
    with Workspace(home) as ws:
        assert ws.mark_interrupted() == 0
        assert ws.get_run("old-run")["status"] == "running" and ws.get_slot("sl_old").status == "generating"
        assert ws.recover_stale(stale_after=0) == ["old-run"]


def _set_owner(ws, run_id, *, pid, heartbeat_at=None, host=None, boot=None, token="other-process"):
    """Make ``run_id`` look owned by another process; ``heartbeat_at`` = its last sign of life (heartbeat and last write)."""
    beat = heartbeat_at or dbmod.utc_now()
    with ws._tx() as conn:
        conn.execute("UPDATE runs SET owner_pid = ?, owner_host = ?, owner_boot = ?, owner_token = ?, heartbeat_at = ?, "
                     "updated_at = ? WHERE id = ?",
                     (pid, dbmod.this_host() if host is None else host, dbmod.boot_marker() if boot is None else boot, token,
                      beat, beat, run_id))


def _dead_pid() -> int:
    import subprocess
    import sys

    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                              check=True).stdout)


@pytest.fixture
def live_process():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    yield proc.pid
    proc.stdin.close()
    proc.wait(timeout=10)


def test_recovery_takes_over_only_runs_whose_owner_is_gone(ws, live_process):
    slots = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic=f"주제 {n}", angle="", keywords=[], goal="")
                          for n in range(5)])
    old_beat = "2026-09-01T00:00:00.000Z"
    cases = {  # run id → (owner setup, taken over?)
        "held-here": (None, False),                                      # this process holds the lease
        "live-pid": (dict(pid=live_process), False),                     # another live process on this machine
        "dead-pid": (dict(pid=_dead_pid()), True),                       # that process is gone
        "asleep": (dict(pid=live_process, heartbeat_at=old_beat), True),  # alive, but no heartbeat for weeks
        "rebooted": (dict(pid=live_process, boot="another-boot"), True),  # recorded before this machine restarted
    }
    for (run_id, (owner, _)), slot in zip(cases.items(), slots):
        ws.create_run(run_id, _brief(), kind="slot", options={"slot_id": slot.id})
        ws.append_event(run_id, {"seq": 1, "t": 2.0, "type": "run.started", "agent": "system", "data": {}})
        ws.claim_slot(slot.id, run_id)
        if owner is None:
            ws.acquire_run(run_id)
        else:
            _set_owner(ws, run_id, **owner)
    ws.create_run("other-host", _brief())
    _set_owner(ws, "other-host", pid=live_process, host="some-container")  # cannot check its pid: heartbeat decides

    taken = ws.mark_interrupted()
    expected = [run_id for run_id, (_, gone) in cases.items() if gone]
    assert taken == len(expected)
    for (run_id, (_, gone)), slot in zip(cases.items(), slots):
        run = ws.get_run(run_id)
        assert run["status"] == ("interrupted" if gone else "running"), run_id
        assert ws.get_slot(slot.id).status == ("planned" if gone else "generating"), run_id  # only its own slot
        events = ws.list_events(run_id)
        assert [e["type"] for e in events] == (["run.started", "run.failed"] if gone else ["run.started"])
    assert ws.get_run("other-host")["status"] == "running"
    # with no grace at all, every run without a lease held here counts as gone
    assert sorted(ws.recover_stale(stale_after=0)) == ["live-pid", "other-host"]
    assert ws.get_run("held-here")["status"] == "running"
    dbmod._LEASES[(ws._key, "held-here")].release()


def test_recover_stale_with_zero_threshold_still_respects_held_leases(ws, live_process):
    ws.create_run("mine", _brief())
    lease = ws.acquire_run("mine")
    ws.create_run("theirs", _brief())
    _set_owner(ws, "theirs", pid=live_process)
    assert ws.recover_stale(stale_after=0) == ["theirs"]  # heartbeat 'too old' for everyone but our own lease
    assert ws.get_run("mine")["status"] == "running"
    lease.release()
    assert ws.recover_stale(trust_own_pid=True) == []  # our pid, no lease: a long-running process may be starting it
    assert ws.recover_stale() == ["mine"]  # a fresh process: our pid without our lease is an earlier process


def test_interrupted_message_depends_on_the_kind(ws):
    ws.create_run("job-rev", _brief(), kind="revise", parent_item_id="it_x")
    ws.create_run("job-import", _brief(), kind="import")
    ws.create_run("run-p", _brief())
    assert ws.mark_interrupted() == 3
    revise = ws.get_run("job-rev")
    assert "수정 요청 작업이 중단됐어요" in revise["error"] and "보관함에서 같은 작업(수정 요청)을 다시 시작" in revise["error"]
    assert "이어서 실행" not in revise["error"]
    closing = ws.last_event("job-rev")["data"]
    assert closing == {"error": revise["error"], "interrupted": True, "resumable": False}
    assert "insia import-run" in ws.get_run("job-import")["error"]
    assert ws.get_run("run-p")["error"] == dbmod.INTERRUPTED_MESSAGE and ws.last_event("run-p")["data"]["resumable"] is True


def test_taken_over_lease_is_lost_and_its_writes_are_refused(ws, monkeypatch):
    monkeypatch.setattr(dbmod, "HEARTBEAT_SECONDS", 0.05)
    ws.create_run("run-t", _brief())
    lease = ws.acquire_run("run-t")
    assert ws.acquire_run("run-t") is lease  # acquiring again in the same process: same lease
    first_beat = ws.get_run("run-t")["owner"]["heartbeat_at"]
    deadline = time.monotonic() + 5
    while ws.get_run("run-t")["owner"]["heartbeat_at"] == first_beat and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ws.get_run("run-t")["owner"]["heartbeat_at"] > first_beat  # the heartbeat thread keeps it fresh
    ws.append_event("run-t", {"type": "log", "data": {"message": "mine"}})
    assert not ws.claim_run("run-t", "running")  # this process runs it: a claim from the same process is refused
    _set_owner(ws, "run-t", pid=1, host="other-host", token="forced-resume")  # another process forces a resume: theirs now
    while not lease.lost and time.monotonic() < deadline:
        time.sleep(0.02)
    assert lease.lost
    with pytest.raises(dbmod.RunTakenOverError, match="넘겨받았어요"):
        ws.append_event("run-t", {"type": "log", "data": {"message": "stale owner"}})
    with pytest.raises(dbmod.RunTakenOverError):
        ws.update_run("run-t", status="completed")
    lease.release()
    ws.update_run("run-t", status="completed")  # without the lease this process writes normally again
    assert [e["data"]["message"] for e in ws.list_events("run-t")] == ["mine"]


def test_pid_and_boot_helpers():
    import os

    assert dbmod.pid_alive(os.getpid()) and not dbmod.pid_alive(_dead_pid()) and not dbmod.pid_alive(0)
    assert dbmod._same_boot("abc", "abc") is True and dbmod._same_boot("abc", "def") is False
    assert dbmod._same_boot("", "abc") is None
    assert dbmod._same_boot("t:1000", "t:1060") is True and dbmod._same_boot("t:1000", "t:5000") is False


def test_claim_slot_respects_live_runs_and_recovers_dead_ones(ws, live_process):
    slot = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic="주제", angle="", keywords=[], goal="")])[0]
    ws.create_run("busy-run", _brief(), kind="slot", options={"slot_id": slot.id})
    ws.claim_slot(slot.id, "busy-run")
    _set_owner(ws, "busy-run", pid=live_process)
    for force in (False, True):  # force regenerates a draft; it never runs two generations at once
        with pytest.raises(WorkspaceError, match="다른 실행\\(busy-run\\)"):
            ws.claim_slot(slot.id, "new-run", force=force)
    _set_owner(ws, "busy-run", pid=_dead_pid())
    claimed = ws.claim_slot(slot.id, "new-run")  # its process is gone: recovered, no force needed
    assert (claimed.status, claimed.run_id) == ("generating", "new-run")
    assert ws.get_run("busy-run")["status"] == "interrupted"
    # a 'planned' slot whose run is still running elsewhere (the state an old resume left) is refused too
    ws.update_slot(slot.id, status="planned")
    ws.create_run("resuming", _brief(), kind="slot", options={"slot_id": slot.id})
    _set_owner(ws, "resuming", pid=live_process)
    ws.update_slot(slot.id, run_id="resuming")
    with pytest.raises(WorkspaceError, match="다른 실행"):
        ws.claim_slot(slot.id, "third-run")


def test_orphan_generating_slots_are_released_after_a_grace_period(ws, monkeypatch):
    slots = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic=f"주제 {n}", angle="", keywords=[], goal="")
                          for n in range(3)])
    ws.update_slot(slots[0].id, status="generating")                  # no run id at all: released at once
    ws.claim_slot(slots[1].id, "never-created")                       # the process died before creating its run
    ws.create_run("finished", _brief(), kind="slot")
    ws.update_run("finished", status="failed")
    ws.claim_slot(slots[2].id, "finished")                            # the run ended but the slot was not put back
    ws.mark_interrupted()
    assert [ws.get_slot(s.id).status for s in slots] == ["planned", "generating", "generating"]  # may still be starting
    monkeypatch.setattr(dbmod, "ORPHAN_SLOT_SECONDS", 0.0)
    time.sleep(0.01)
    ws.mark_interrupted()
    assert [ws.get_slot(s.id).status for s in slots] == ["planned", "planned", "planned"]


def test_recovered_slot_links_a_finished_run_or_keeps_an_earlier_draft(ws):
    slots = ws.add_slots([PlannedSlot(date="2026-10-0%d" % n, channel="linkedin", topic=f"주제 {n}", angle="", keywords=[],
                                      goal="") for n in (1, 2)])
    # 1: the run finished (item saved) but the process died before linking the slot
    ws.create_run("done-run", _brief(), kind="slot")
    ws.claim_slot(slots[0].id, "done-run")
    ws.ensure_item(pipeline_item_id("done-run", "linkedin"), "linkedin", "제목", run_id="done-run")
    ws.add_version(pipeline_item_id("done-run", "linkedin"), _draft(), source="agent", run_id="done-run")
    ws.update_run("done-run", status="completed")
    # 2: a forced regeneration died; the slot had a draft before
    earlier = ws.create_item("linkedin", "예전 초안")
    ws.update_slot(slots[1].id, status="drafted", item_id=earlier.id)
    ws.create_run("regen-run", _brief(), kind="slot")
    ws.claim_slot(slots[1].id, "regen-run", force=True)
    with ws._tx() as conn:
        conn.execute("UPDATE slots SET updated_at = '2026-09-01T00:00:00.000Z'")
    assert ws.mark_interrupted() == 1  # regen-run (done-run already completed)
    first, second = ws.get_slot(slots[0].id), ws.get_slot(slots[1].id)
    assert (first.status, first.item_id) == ("drafted", pipeline_item_id("done-run", "linkedin"))
    assert ws.get_item(first.item_id).item.scheduled_at == "2026-10-01"
    assert (second.status, second.item_id) == ("drafted", earlier.id)


def test_reclaim_slot_for_a_resumed_run(ws, live_process):
    slot = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic="주제", angle="", keywords=[], goal="")])[0]
    ws.create_run("slot-run", _brief(), kind="slot", options={"slot_id": slot.id})
    ws.update_run("slot-run", status="interrupted")
    ws.update_slot(slot.id, run_id="slot-run")
    before = ws.reclaim_slot(slot.id, "slot-run")
    assert before.status == "planned" and ws.get_slot(slot.id).status == "generating"
    assert ws.due_slots("2026-12-31") == []
    assert ws.reclaim_slot("sl_missing", "slot-run") is None
    # another live run generating the slot: refused
    ws.create_run("other", _brief(), kind="slot")
    _set_owner(ws, "other", pid=live_process)
    ws.update_slot(slot.id, status="generating", run_id="other")
    with pytest.raises(WorkspaceError, match="다른 실행\\(other\\)"):
        ws.reclaim_slot(slot.id, "slot-run")
    # a newer run already drafted the slot: left alone
    ws.update_run("other", status="completed")
    newer = ws.create_item("linkedin", "새 초안", run_id="other")
    ws.update_slot(slot.id, status="drafted", item_id=newer.id)
    assert ws.reclaim_slot(slot.id, "slot-run") is None
    assert ws.get_slot(slot.id).status == "drafted"


# ---------------------------------------------------------------------------
# Forced-approval audit, job versions over human edits, final version after a failed review
# ---------------------------------------------------------------------------


def test_forced_approval_is_recorded_on_the_item(ws):
    item = ws.create_item("linkedin", "제목")
    ws.add_version(item.id, _draft(), source="agent", review=_review(score=68, passed=False))
    forced = ws.set_item_status(item.id, "approved", force=True)
    assert (forced.approval_forced, forced.approved_version, forced.approved_score) == (True, 1, 68)
    assert forced.approved_at and ws.get_item(item.id).item.approval_forced is True
    assert ws.set_item_status(item.id, "published").approval_forced is True  # the record stays after publishing

    unreviewed = ws.create_item("linkedin", "검수 전")
    ws.add_version(unreviewed.id, _draft(), source="human")
    approved = ws.set_item_status(unreviewed.id, "approved", force=True)
    assert (approved.approval_forced, approved.approved_score) == (True, None)

    passed = ws.create_item("linkedin", "통과")
    ws.add_version(passed.id, _draft(), source="agent", review=_review(score=91))
    normal = ws.set_item_status(passed.id, "approved", force=True)  # force did not bypass anything
    assert (normal.approval_forced, normal.approved_version, normal.approved_score) == (False, 1, 91)
    data = normal.model_dump(mode="json")
    assert {"approved_version", "approval_forced", "approved_score", "approved_at"} <= set(data)


def test_job_version_does_not_replace_a_newer_version(ws):
    item = ws.create_item("linkedin", "제목")
    base = ws.add_version(item.id, _draft(title="에이전트 v1"), source="agent", review=_review(score=70, passed=False))
    same, restored = ws.add_job_version(item.id, _draft(round=1, title="수정본"), base_version=base.version, run_id="job-1")
    assert restored is None and ws.get_item(item.id).item.version == same.version == 2  # nothing moved: normal append

    human = ws.add_version(item.id, _draft(title="사람이 고친 제목", content="사람 본문"), source="human")
    job, restored = ws.add_job_version(item.id, _draft(round=2, title="v2 기준 수정본"), base_version=2, run_id="job-2",
                                       review=_review(score=88))
    detail = ws.get_item(item.id)
    assert [v.version for v in detail.versions] == [1, 2, 3, 4, 5]
    assert (job.version, restored.version, restored.source) == (4, 5, "human")
    current = detail.versions[-1]
    assert (current.draft.title, current.draft.content) == ("사람이 고친 제목", "사람 본문")
    assert "v3 버전이 저장돼서" in current.draft.change_log[0] and "수정 요청 결과는 v4, v2 기준" in current.draft.change_log[0]
    assert detail.item.version == 5 and detail.item.title == "사람이 고친 제목" and detail.item.status == "draft"
    assert detail.versions[3].review.score == 88 and human.version == 3


def test_final_copy_follows_an_unreviewed_later_round(ws):
    run_id = "run-f"
    ws.create_run(run_id, _brief())
    item_id = pipeline_item_id(run_id, "linkedin")
    r0, r1 = _draft(round=0, title="R0"), _draft(round=1, title="R1 검수 실패")
    ws.ensure_item(item_id, "linkedin", "R0", run_id=run_id)
    ws.add_version(item_id, r0, source="agent", run_id=run_id, review=_review(round=0, score=68, passed=False))
    ws.add_version(item_id, r1, source="agent", run_id=run_id)  # its review call failed
    result = ChannelResult(channel="linkedin", final=r0, drafts=[r0], reviews=[_review(round=0, score=68, passed=False)],
                           passed=False, rounds=0)
    for _ in range(2):  # idempotent
        item = ws.upsert_item_from_result(run_id, result, _brief())
    detail = ws.get_item(item_id)
    assert [(v.draft.round, v.review.score if v.review else None) for v in detail.versions] == [(0, 68), (1, None), (0, 68)]
    assert detail.versions[-1].draft.content == r0.content and "R1 수정본은 검수를 마치지 못해서" in detail.versions[-1].draft.change_log[0]
    assert (item.version, item.status, item.score) == (3, "needs_changes", 68)


def test_list_runs_by_parent_item(ws):
    ws.create_run("p-1", _brief())
    ws.create_run("j-1", _brief(), kind="review", parent_item_id="it_a")
    ws.create_run("j-2", _brief(), kind="revise", parent_item_id="it_a")
    ws.create_run("j-3", _brief(), kind="review", parent_item_id="it_b")
    assert [r["run_id"] for r in ws.list_runs(parent_item_id="it_a")] == ["j-2", "j-1"]
    assert [r["run_id"] for r in ws.list_runs(parent_item_id="it_a", kind="review")] == ["j-1"]
    assert ws.list_runs(parent_item_id="it_none") == []


def test_refresh_slot_recovers_before_a_status_check(ws, live_process):
    slot = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic="주제", angle="", keywords=[], goal="")])[0]
    ws.create_run("gen-run", _brief(), kind="slot", options={"slot_id": slot.id})
    ws.claim_slot(slot.id, "gen-run")
    _set_owner(ws, "gen-run", pid=live_process)
    assert ws.refresh_slot(slot.id).status == "generating"  # still being generated: untouched
    _set_owner(ws, "gen-run", pid=_dead_pid())
    assert ws.refresh_slot(slot.id).status == "planned" and ws.get_run("gen-run")["status"] == "interrupted"
    assert ws.refresh_slot("sl_missing") is None


# ---------------------------------------------------------------------------
# Verification round: taken-over owners stop writing, background recovery, zombie owners
# ---------------------------------------------------------------------------


def test_taken_over_owner_learns_it_at_once_and_its_item_writes_are_refused(ws, live_process):
    ws.create_run("run-o", _brief(), kind="slot")
    lease = ws.acquire_run("run-o")
    item_id = pipeline_item_id("run-o", "linkedin")
    ws.ensure_item(item_id, "linkedin", "제목", run_id="run-o")
    first = ws.add_version(item_id, _draft(), source="agent", run_id="run-o")
    assert lease.verify() and not lease.lost
    _set_owner(ws, "run-o", pid=live_process, token="new-owner")  # another process took it over (recovery / --force)
    assert not lease.lost  # the heartbeat has not noticed yet (every 30 s) ...
    assert lease.verify() is False and lease.lost  # ... but the check before the next paid call reads it now
    for write in (lambda: ws.add_version(item_id, _draft(round=1), source="agent", run_id="run-o"),
                  lambda: ws.attach_review(first.id, _review(), run_id="run-o"),
                  lambda: ws.ensure_item(pipeline_item_id("run-o", "instagram"), "instagram", "제목", run_id="run-o"),
                  lambda: ws.add_job_version(item_id, _draft(round=1), base_version=1, run_id="run-o"),
                  lambda: ws.upsert_item_from_result("run-o", ChannelResult(channel="linkedin", final=_draft(), drafts=[_draft()],
                                                                           reviews=[_review()], passed=True, rounds=0),
                                                     _brief())):
        with pytest.raises(dbmod.RunTakenOverError):
            write()
    detail = ws.get_item(item_id)
    assert [v.version for v in detail.versions] == [1] and detail.versions[0].review is None
    ws.attach_review(first.id, _review())  # writes that are not on behalf of the lost run still work
    lease.release()


def test_release_and_link_slot_only_touch_the_runs_own_claim(ws, live_process):
    slots = ws.add_slots([PlannedSlot(date="2026-10-0%d" % n, channel="linkedin", topic=f"주제 {n}", angle="", keywords=[],
                                      goal="") for n in (1, 2, 3)])
    for n, slot in enumerate(slots):
        ws.create_run(f"run-{n}", _brief(), kind="slot", options={"slot_id": slot.id})
        ws.claim_slot(slot.id, f"run-{n}")
    leases = [ws.acquire_run(f"run-{n}") for n in range(3)]
    item = ws.ensure_item(pipeline_item_id("run-0", "linkedin"), "linkedin", "제목", run_id="run-0")
    # still ours: handed back / linked
    assert ws.release_slot(slots[1].id, "run-1", owner_token=leases[1].token).status == "planned"
    linked = ws.link_slot(slots[0].id, "run-0", item.id, owner_token=leases[0].token)
    assert (linked.status, linked.item_id) == ("drafted", item.id)
    assert ws.get_item(item.id).item.scheduled_at == "2026-10-01"
    # the run was taken over by another process: the slot is theirs, nothing changes
    _set_owner(ws, "run-2", pid=live_process, token="new-owner")
    assert ws.release_slot(slots[2].id, "run-2", owner_token=leases[2].token) is None
    assert ws.link_slot(slots[2].id, "run-2", item.id, owner_token=leases[2].token).status == "generating"
    # another run claimed the slot meanwhile: nothing changes either (even without a token check)
    ws.update_slot(slots[2].id, run_id="run-new")
    assert ws.release_slot(slots[2].id, "run-2") is None
    assert ws.get_slot(slots[2].id).run_id == "run-new"
    # a slot no longer generating is not put back ('drafted' by the new owner stays)
    assert ws.release_slot(slots[0].id, "run-0", owner_token=leases[0].token) is None
    assert ws.get_slot(slots[0].id).status == "drafted"
    with pytest.raises(WorkspaceError):
        ws.release_slot(slots[0].id, "run-0", status="generating")
    for lease in leases:
        lease.release()


def test_background_recovery_confirms_before_taking_over(ws, live_process):
    slots = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic=f"주제 {n}", angle="", keywords=[], goal="")
                          for n in range(2)])
    # the container an update replaced: another host, recent heartbeat, its pid cannot be checked from here
    ws.create_run("old-container", _brief(), kind="slot", options={"slot_id": slots[0].id})
    ws.claim_slot(slots[0].id, "old-container")
    _set_owner(ws, "old-container", pid=1, host="3f2a9c1d7e0b")
    ws.create_run("live-cli", _brief(), kind="slot", options={"slot_id": slots[1].id})
    ws.claim_slot(slots[1].id, "live-cli")
    _set_owner(ws, "live-cli", pid=live_process)
    assert ws.mark_interrupted(watch=False) == 0  # at start-up neither can be judged gone
    assert ws._recovery_tick({}) == ([], {})
    quiet = "2026-09-01T00:00:00.000Z"
    _set_owner(ws, "old-container", pid=1, host="3f2a9c1d7e0b", heartbeat_at=quiet)  # it never beats again
    _set_owner(ws, "live-cli", pid=live_process, heartbeat_at=quiet)  # this one was only asleep (laptop lid closed)
    taken, suspects = ws._recovery_tick({})
    assert taken == [] and set(suspects) == {"old-container", "live-cli"}  # first round: suspected only
    _set_owner(ws, "live-cli", pid=live_process)  # woke up: its heartbeat thread beats before the next round
    taken, suspects = ws._recovery_tick(suspects)
    assert taken == ["old-container"] and suspects == {}
    assert ws.get_run("old-container")["status"] == "interrupted" and ws.get_slot(slots[0].id).status == "planned"
    assert ws.get_run("live-cli")["status"] == "running" and ws.get_slot(slots[1].id).status == "generating"


def test_server_start_keeps_recovering_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "RECOVERY_WATCH_SECONDS", 0.02)
    workspace = Workspace(tmp_path / "ws")
    try:
        workspace.create_run("old-container", _brief())
        _set_owner(workspace, "old-container", pid=1, host="replaced-container")
        assert workspace.mark_interrupted() == 0 and workspace.get_run("old-container")["status"] == "running"
        assert workspace.watch_stale_runs() is False  # already watching
        _set_owner(workspace, "old-container", pid=1, host="replaced-container", heartbeat_at="2026-09-01T00:00:00.000Z")
        deadline = time.monotonic() + 5
        while workspace.get_run("old-container")["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert workspace.get_run("old-container")["status"] == "interrupted"  # no restart needed
    finally:
        workspace.close()
    workspace._watcher.join(timeout=5)
    assert not workspace._watcher.is_alive()  # close() stops it


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the zombie check reads /proc (Linux)")
def test_a_killed_owner_its_parent_has_not_reaped_counts_as_gone(ws):
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert dbmod.pid_alive(proc.pid)
        proc.kill()  # no wait() yet: the process stays a zombie until its parent reaps it

        def state() -> str:
            return Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]

        deadline = time.monotonic() + 5
        while state() != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state() == "Z"
        assert dbmod.pid_alive(proc.pid) is False
        ws.create_run("zombie-run", _brief())
        _set_owner(ws, "zombie-run", pid=proc.pid)
        assert ws.recover_stale() == ["zombie-run"]
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_acquiring_a_run_taken_over_meanwhile_does_not_take_it_back(ws, live_process):
    ws.create_run("run-a", _brief())
    lease = ws.acquire_run("run-a")
    _set_owner(ws, "run-a", pid=live_process, token="new-owner")  # another process took it over; not noticed yet
    with pytest.raises(dbmod.RunTakenOverError):
        ws.acquire_run("run-a")  # e.g. run_pipeline's ensure_run after generate_slot acquired it
    assert lease.lost and ws._owns("run-a", "new-owner")
    lease.release()
