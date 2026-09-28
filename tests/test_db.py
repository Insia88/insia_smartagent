from __future__ import annotations

import json
import sqlite3
import threading
import time

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
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, extra])
    for _ in range(2):
        with Workspace(home) as workspace:
            assert workspace.schema_version == 2
            assert workspace._conn.execute("SELECT COUNT(*) FROM extra").fetchone()[0] == 1


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    home = tmp_path / "ws"
    Workspace(home).close()
    broken = "CREATE TABLE half_done (x INTEGER);\nINSERT INTO missing_table VALUES (1);"
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, broken])
    with pytest.raises(WorkspaceError, match="되돌렸어요"):
        Workspace(home)
    conn = sqlite3.connect(home / "insia.db")
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
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
