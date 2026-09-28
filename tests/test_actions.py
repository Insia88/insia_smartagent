"""Item jobs: 재검수 / 수정 요청 / 직접 수정 / 캘린더 슬롯 초안."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from insia_agents import actions
from insia_agents.backends.base import BackendError
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.channels import check_format
from insia_agents.db import NotFoundError, Workspace, WorkspaceError, pipeline_item_id
from insia_agents.events import EventBus, SimClock
from insia_agents.models import PlannedSlot, Profile, UsageRecord
from insia_agents.pipeline import BudgetExceeded, execute_run, new_run_id, prepare_run


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    yield workspace
    workspace.close()


@pytest.fixture
def run_items(settings, brief, ws):
    """A finished 4-channel mock run in the workspace → {channel: item_id}."""
    result, _ = execute_run(brief, settings, workspace=ws)
    return {r.channel: pipeline_item_id(result.run_id, r.channel) for r in result.results}


def _types(events):
    return [e["type"] for e in events]


class InstructedBackend(MockBackend):
    """A backend that accepts human revision instructions (like the live one)."""

    def __init__(self, settings):
        super().__init__(settings)
        self.instructions: list[str] = []
        self.context_instructions: list[str] = []

    def revise(self, brief, plan, research, draft, review, instructions: str = ""):
        self.instructions.append(instructions)
        self.context_instructions.append(getattr(getattr(self, "context", None), "instructions", None))
        out = super().revise(brief, plan, research, draft, review)
        return out.model_copy(update={"change_log": [f"지시 반영: {instructions}", *out.change_log]})


class LegacyBackend(MockBackend):
    """Older backend: revise() has no ``instructions`` keyword."""

    def revise(self, brief, plan, research, draft, review):  # noqa: D401 - signature is the point
        return super().revise(brief, plan, research, draft, review)


def test_review_item_is_a_run_with_events(settings, ws, run_items):
    item_id = run_items["linkedin"]
    before = ws.get_item(item_id)
    result = actions.review_item(ws, item_id, settings=settings)
    run = ws.get_run(result.run_id)
    assert run["kind"] == "review" and run["status"] == "completed" and run["parent_item_id"] == item_id
    assert run["brief"]["channels"] == ["linkedin"] and run["options"]["version"] == before.item.version
    events = ws.list_events(result.run_id)
    types = _types(events)
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert {"review.started", "review.completed", "handoff"} <= set(types)
    assert events[0]["data"]["kind"] == "review" and events[0]["data"]["item_id"] == item_id
    assert events[-1]["data"]["scores"] == {"linkedin": result.review.score}
    after = ws.get_item(item_id)
    assert len(after.versions) == len(before.versions)  # a review adds no version
    assert after.versions[-1].review == result.review == result.version.review
    json.dumps(result.to_dict(), ensure_ascii=False)


def test_revise_item_with_instructions_creates_reviewed_version(settings, ws, run_items):
    item_id = run_items["naver_blog"]
    before = ws.get_item(item_id)
    backend = InstructedBackend(settings)
    result = actions.revise_item(ws, item_id, "  도입부를 더 짧게 해 주세요  ", settings=settings, backend=backend)
    assert backend.instructions == ["도입부를 더 짧게 해 주세요"]
    assert backend.context_instructions == ["도입부를 더 짧게 해 주세요"]
    after = ws.get_item(item_id)
    assert len(after.versions) == len(before.versions) + 1
    version = after.versions[-1]
    assert version.source == "agent" and version.instructions == "도입부를 더 짧게 해 주세요"
    assert version.draft.round == before.versions[-1].draft.round + 1
    assert version.review is not None and version.review == result.review
    assert version.draft.change_log[0].startswith("지시 반영")
    assert after.item.version == version.version and after.item.score == result.review.score
    events = ws.list_events(result.run_id)
    types = _types(events)
    assert types.index("revision.requested") < types.index("draft.created") < types.index("review.completed")
    requested = next(e for e in events if e["type"] == "revision.requested")
    assert requested["data"]["instructions"] == "도입부를 더 짧게 해 주세요"
    assert requested["data"]["top_issue"] == "도입부를 더 짧게 해 주세요"
    run = ws.get_run(result.run_id)
    assert run["kind"] == "revise" and run["options"]["instructions"] == "도입부를 더 짧게 해 주세요"


def test_revise_falls_back_for_backends_without_instructions(settings, ws, run_items):
    result = actions.revise_item(ws, run_items["linkedin"], "해시태그를 줄여 주세요", settings=settings,
                                 backend=LegacyBackend(settings))
    assert result.version.instructions == "해시태그를 줄여 주세요" and result.review is not None
    warns = [e for e in ws.list_events(result.run_id) if e["type"] == "log" and e["data"]["level"] == "warn"]
    assert any("수정 지시를 따로 받지 않아서" in w["data"]["message"] for w in warns)


def test_revise_without_instructions_uses_review_only(settings, ws, run_items):
    backend = InstructedBackend(settings)
    actions.revise_item(ws, run_items["instagram"], settings=settings, backend=backend)
    assert backend.instructions == [""]  # called without the keyword → default value


def test_revise_reviews_first_when_latest_version_has_no_review(settings, ws, run_items):
    item_id = run_items["linkedin"]
    latest = ws.get_item(item_id).versions[-1].draft
    actions.edit_item(ws, item_id, latest.title, latest.content + "\n\n덧붙인 문장.", latest.hashtags, settings=settings)
    result = actions.revise_item(ws, item_id, "마지막 문장을 질문으로", settings=settings)
    events = ws.list_events(result.run_id)
    assert _types(events).count("review.completed") == 2  # the human edit first, then the revision
    versions = ws.get_item(item_id).versions
    assert versions[-2].source == "human" and versions[-2].review is not None
    assert versions[-1].source == "agent" and versions[-1].review is not None


def test_edit_item_human_version_with_format_checks(settings, ws, run_items):
    item_id = run_items["linkedin"]
    ws.set_item_status(item_id, "approved", force=True)
    runs_before = len(ws.list_runs(limit=100))
    with pytest.raises(WorkspaceError, match="제목"):
        actions.edit_item(ws, item_id, "  ", "본문", settings=settings)
    with pytest.raises(WorkspaceError, match="본문"):
        actions.edit_item(ws, item_id, "제목", "\n\n", settings=settings)
    with pytest.raises(NotFoundError):
        actions.edit_item(ws, "it_missing", "제목", "본문", settings=settings)
    assert len(ws.list_runs(limit=100)) == runs_before  # invalid input never creates a run

    result = actions.edit_item(ws, item_id, "  새   제목 ", "짧은 본문이에요.\r\n두 번째 줄", ["AI 마케팅", "#창업", "창업"],
                               settings=settings)
    version = result.version
    assert version.source == "human" and version.review is None
    assert version.draft.title == "새 제목" and version.draft.content == "짧은 본문이에요.\n두 번째 줄"
    assert version.draft.hashtags == ["#AI마케팅", "#창업"] and version.draft.change_log == ["사람이 직접 수정함"]
    brief = ws.get_item(item_id).brief.model_copy(update={"channels": ["linkedin"]})
    assert result.format_checks == check_format(version.draft, brief)
    assert not next(c for c in result.format_checks if c.id == "length").passed
    item = ws.get_item(item_id).item
    assert item.status == "draft" and item.score is None and item.passed is None  # approval must be renewed
    run = ws.get_run(result.run_id)
    assert run["kind"] == "edit" and run["status"] == "completed" and run["mode"] == ""
    events = ws.list_events(result.run_id)
    assert _types(events) == ["run.started", "draft.created", "log", "log", "run.completed"]
    assert events[1]["agent"] == "system" and events[1]["data"]["source"] == "human"
    assert events[-1]["data"]["format_checks"][0]["id"] == "length"


def test_edit_keeps_published_status_and_restores_archived(settings, ws, run_items):
    published = run_items["instagram"]
    ws.set_item_status(published, "approved")
    ws.set_item_status(published, "published", published_url="https://www.instagram.com/p/abc")
    latest = ws.get_item(published).versions[-1].draft
    actions.edit_item(ws, published, latest.title, latest.content, latest.hashtags, settings=settings)
    assert ws.get_item(published).item.status == "published"

    archived = run_items["bizplan"]
    ws.set_item_status(archived, "archived")
    actions.edit_item(ws, archived, "복원한 계획서", "# 복원\n\n본문", settings=settings)
    assert ws.get_item(archived).item.status == "draft"


def test_edit_applies_profile_brand_checks(settings, ws, run_items):
    ws.save_profile(Profile(banned_words=["최고"], required_phrases=["#광고"]))
    result = actions.edit_item(ws, run_items["linkedin"], "제목", "우리가 최고예요", settings=settings)
    checks = {c.id: c.passed for c in result.format_checks}
    assert checks["banned_words"] is False and checks["required_phrases"] is False
    off = actions.edit_item(ws, run_items["linkedin"], "제목", "우리가 최고예요", settings=replace(settings, use_profile=False))
    assert "banned_words" not in {c.id for c in off.format_checks}


def test_job_with_server_supplied_bus(settings, ws, run_items):
    backend, bus, _ = prepare_run(settings, run_id=new_run_id())
    seen = []
    result = actions.review_item(ws, run_items["bizplan"], settings=settings, backend=backend, bus=bus, listener=seen.append)
    assert result.run_id == bus.run_id and seen == bus.events == ws.list_events(bus.run_id)


class ExpensiveReviewBackend(MockBackend):
    def review(self, brief, research, draft, format_checks):
        out = super().review(brief, research, draft, format_checks)
        if getattr(self, "on_usage", None):
            self.on_usage(UsageRecord(agent="reviewer", task="review", model="claude-opus-5", cost_usd=5.0))
        return out


def test_job_budget_cap(settings, ws, run_items):
    item_id = run_items["linkedin"]
    latest = ws.get_item(item_id).versions[-1].draft
    actions.edit_item(ws, item_id, latest.title, latest.content, latest.hashtags, settings=settings)
    versions_before = len(ws.get_item(item_id).versions)
    capped = replace(settings, max_cost_usd=1.0)
    with pytest.raises(BudgetExceeded, match="예산 상한 \\$1.00"):
        actions.revise_item(ws, item_id, "짧게", settings=capped, backend=ExpensiveReviewBackend(capped))
    run = ws.list_runs(kind="revise")[0]
    assert run["status"] == "failed" and "예산 상한" in run["error"] and run["cost_usd"] == pytest.approx(5.0)
    assert ws.list_events(run["run_id"])[-1]["data"]["budget_exceeded"] is True
    assert len(ws.get_item(item_id).versions) == versions_before  # the pre-review ran; the revision never started


class BrokenReviewBackend(MockBackend):
    def review(self, brief, research, draft, format_checks):
        raise BackendError("검수 테스트 실패")


def test_failed_job_is_recorded(settings, ws, run_items):
    with pytest.raises(BackendError):
        actions.review_item(ws, run_items["bizplan"], settings=settings, backend=BrokenReviewBackend(settings))
    run = ws.list_runs(kind="review")[0]
    assert run["status"] == "failed" and run["error"] == "검수 테스트 실패"
    assert ws.list_events(run["run_id"])[-1]["type"] == "run.failed"


def test_generate_slot_links_item_and_slot(settings, ws):
    ws.save_profile(Profile(company_name="인시아", target_customers="동네 카페 사장님", tone="다정한 전문가 톤"))
    slot = ws.add_slots([PlannedSlot(date="2026-10-01", channel="linkedin", topic="카페 사장님의 콘텐츠 루틴",
                                     angle="체크리스트", keywords=["카페 마케팅", "SNS 운영"], goal="문의")])[0]
    result = actions.generate_slot(ws, slot.id, settings=settings)
    assert result.kind == "slot" and result.slot.status == "drafted"
    assert result.slot.item_id == pipeline_item_id(result.run_id, "linkedin") == result.item.id
    assert result.item.scheduled_at == "2026-10-01" and result.review is not None
    run = ws.get_run(result.run_id)
    assert run["kind"] == "slot" and run["status"] == "completed" and run["options"]["slot_id"] == slot.id
    assert run["brief"]["channels"] == ["linkedin"] and run["brief"]["audience"] == "동네 카페 사장님"
    assert run["brief"]["keywords"] == ["카페 마케팅", "SNS 운영"] and "체크리스트" in run["brief"]["notes"]
    assert run["profile"]["company_name"] == "인시아"
    assert ws.get_slot(slot.id).run_id == result.run_id
    with pytest.raises(WorkspaceError, match="이미 초안"):
        actions.generate_slot(ws, slot.id, settings=settings)
    again = actions.generate_slot(ws, slot.id, settings=settings, force=True)
    assert again.run_id != result.run_id and ws.get_slot(slot.id).item_id == again.item.id
    with pytest.raises(BackendError):
        actions.generate_slot(ws, slot.id, settings=settings, backend=BrokenPlanBackend(settings), force=True)
    kept = ws.get_slot(slot.id)
    assert (kept.status, kept.item_id, kept.run_id) == ("drafted", again.item.id, again.run_id)
    with pytest.raises(NotFoundError):
        actions.generate_slot(ws, "sl_missing", settings=settings)


class BrokenPlanBackend(MockBackend):
    def plan(self, brief):
        raise BackendError("계획 실패")


def test_generate_slot_failure_puts_slot_back(settings, ws):
    slot = ws.add_slots([PlannedSlot(date="2026-10-02", channel="instagram", topic="주제", angle="", keywords=[], goal="")])[0]
    with pytest.raises(BackendError):
        actions.generate_slot(ws, slot.id, settings=settings, backend=BrokenPlanBackend(settings))
    after = ws.get_slot(slot.id)
    assert after.status == "planned" and after.run_id and after.item_id == ""
    assert ws.get_run(after.run_id)["status"] == "failed"


def test_resuming_a_slot_run_links_the_slot(settings, ws):
    from insia_agents.pipeline import resume_run

    class FailingDraftBackend(MockBackend):
        def draft(self, brief, plan, research, channel):
            raise BackendError("초안 실패")

    slot = ws.add_slots([PlannedSlot(date="2026-10-03", channel="linkedin", topic="주제", angle="", keywords=["키워드"],
                                     goal="")])[0]
    bus = EventBus(new_run_id(), clock=SimClock(0))
    with pytest.raises(BackendError):
        actions.generate_slot(ws, slot.id, settings=settings, backend=FailingDraftBackend(settings), bus=bus)
    resume_run(bus.run_id, settings, ws, backend=MockBackend(settings))
    after = ws.get_slot(slot.id)
    assert after.status == "drafted" and after.item_id == pipeline_item_id(bus.run_id, "linkedin")
