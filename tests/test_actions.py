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
from insia_agents.models import Draft, PlannedSlot, Profile, UsageRecord
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


# ---------------------------------------------------------------------------
# Human edits during a job (review finding 4, data side), cancellable job runners, stuck slots
# ---------------------------------------------------------------------------

HUMAN_CONTENT = "사람이 직접 고친 본문이에요. " * 20


class EditsWhileWorking(MockBackend):
    """Saves a human edit (like a PUT /draft from the dashboard) while the job's backend call is in flight."""

    def __init__(self, settings, workspace, item_id, during="revise"):
        super().__init__(settings)
        self.job_settings = settings
        self.workspace = workspace
        self.item_id = item_id
        self.during = during

    def _edit(self):
        actions.edit_item(self.workspace, self.item_id, "사람이 고친 제목", HUMAN_CONTENT, ["#사람"], settings=self.job_settings)

    def revise(self, brief, plan, research, draft, review, *args, **kwargs):
        out = super().revise(brief, plan, research, draft, review)
        if self.during == "revise":
            self._edit()
        return out

    def review(self, brief, research, draft, format_checks):
        out = super().review(brief, research, draft, format_checks)
        if self.during == "review" and not getattr(self, "_edited", False):
            self._edited = True
            self._edit()
        return out


def test_revise_does_not_replace_a_human_edit_saved_meanwhile(settings, ws, run_items):
    item_id = run_items["linkedin"]
    base = ws.get_item(item_id).versions[-1]
    result = actions.revise_item(ws, item_id, "짧게", settings=settings,
                                 backend=EditsWhileWorking(settings, ws, item_id, during="revise"))
    detail = ws.get_item(item_id)
    human, agent, current = detail.versions[-3], detail.versions[-2], detail.versions[-1]
    assert (human.source, human.version) == ("human", base.version + 1)
    assert (agent.source, agent.version, agent.instructions) == ("agent", base.version + 2, "짧게")
    assert agent.review is not None  # the revision was reviewed and kept in the history
    assert current.version == base.version + 3 and current.source == "human"
    assert (current.draft.title, current.draft.content) == ("사람이 고친 제목", HUMAN_CONTENT.rstrip())
    assert detail.item.version == current.version and detail.item.title == "사람이 고친 제목"
    assert result.superseded and result.superseded_by_human_edit
    assert (result.version.version, result.base_version, result.current_version) == (agent.version, base.version, current.version)
    data = result.to_dict()
    assert data["superseded_by_human_edit"] is True and data["current_version"] == current.version
    events = ws.list_events(result.run_id)
    assert events[-1]["type"] == "run.completed" and events[-1]["data"]["superseded_by_human_edit"] is True
    assert ws.get_run(result.run_id)["options"]["base_version"] == base.version
    assert any(e["type"] == "log" and "현재 버전" in e["data"]["message"] for e in events)


def test_revise_reports_a_human_edit_saved_during_its_re_review(settings, ws, run_items):
    item_id = run_items["linkedin"]
    base = ws.get_item(item_id).versions[-1]
    assert base.review is not None  # so the only review call of the job is the re-review of its revision
    result = actions.revise_item(ws, item_id, "짧게", settings=settings,
                                 backend=EditsWhileWorking(settings, ws, item_id, during="review"))
    detail = ws.get_item(item_id)
    agent, human = detail.versions[-2], detail.versions[-1]
    assert (agent.version, agent.source, human.version, human.source) == (base.version + 1, "agent", base.version + 2, "human")
    assert agent.review is not None and agent.review == result.review and human.review is None
    assert detail.item.version == human.version and detail.item.title == "사람이 고친 제목"  # the edit stays current
    assert result.superseded and result.superseded_by_human_edit
    assert (result.version.version, result.base_version, result.current_version) == (agent.version, base.version, human.version)
    events = ws.list_events(result.run_id)
    completed = events[-1]
    assert completed["type"] == "run.completed" and completed["data"]["superseded_by_human_edit"] is True
    assert completed["data"]["superseded"] is True and completed["data"]["current_version"] == human.version
    assert any(e["type"] == "log" and "재검수하는 동안 사람이 고친 버전" in e["data"]["message"] for e in events)


def test_revise_without_a_concurrent_edit_is_unchanged(settings, ws, run_items):
    item_id = run_items["instagram"]
    result = actions.revise_item(ws, item_id, settings=settings)
    assert not result.superseded and not result.superseded_by_human_edit
    assert ws.get_item(item_id).versions[-1].id == result.version.id
    assert result.current_version == result.version.version


def test_review_stays_on_the_version_it_reviewed(settings, ws, run_items):
    item_id = run_items["naver_blog"]
    base = ws.get_item(item_id).versions[-1]
    result = actions.review_item(ws, item_id, settings=settings,
                                 backend=EditsWhileWorking(settings, ws, item_id, during="review"))
    detail = ws.get_item(item_id)
    reviewed = next(v for v in detail.versions if v.version == base.version)
    assert reviewed.review == result.review and result.version.version == base.version
    assert detail.versions[-1].source == "human" and detail.versions[-1].review is None  # the edit is current, unreviewed
    assert detail.item.version == base.version + 1 and detail.item.score is None
    assert result.superseded_by_human_edit and result.current_version == base.version + 1
    assert ws.list_events(result.run_id)[-1]["data"]["superseded"] is True


def test_jobs_accept_a_cancellable_runner(settings, ws, run_items):
    from insia_agents.pipeline import RunCancelled, SimRunner

    class StoppedRunner(SimRunner):
        cancelled = True  # e.g. the server's cancel endpoint was pressed before the first backend call

    item_id = run_items["bizplan"]
    versions = len(ws.get_item(item_id).versions)
    for job in (lambda bus, backend: actions.review_item(ws, item_id, settings=settings, backend=backend, bus=bus,
                                                         runner=StoppedRunner(bus.clock)),
                lambda bus, backend: actions.revise_item(ws, item_id, settings=settings, backend=backend, bus=bus,
                                                         runner=StoppedRunner(bus.clock))):
        backend, bus, _ = prepare_run(settings, run_id=new_run_id())
        with pytest.raises(RunCancelled):
            job(bus, backend)
        assert ws.get_run(bus.run_id)["status"] == "cancelled"
    assert len(ws.get_item(item_id).versions) == versions
    slot = ws.add_slots([PlannedSlot(date="2026-10-04", channel="linkedin", topic="주제", angle="", keywords=[], goal="")])[0]
    backend, bus, _ = prepare_run(settings, run_id=new_run_id())
    with pytest.raises(RunCancelled):
        actions.generate_slot(ws, slot.id, settings=settings, backend=backend, bus=bus, runner=StoppedRunner(bus.clock))
    assert ws.get_run(bus.run_id)["status"] == "cancelled" and ws.get_slot(slot.id).status == "planned"


def test_generate_slot_recovers_a_slot_whose_run_died(settings, ws):
    import subprocess
    import sys

    from insia_agents import db as dbmod

    slot = ws.add_slots([PlannedSlot(date="2026-10-05", channel="linkedin", topic="주제", angle="", keywords=[], goal="")])[0]
    ws.create_run("killed-run", actions.slot_brief(slot), kind="slot", options={"slot_id": slot.id})
    ws.claim_slot(slot.id, "killed-run")
    dead = int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout)
    with ws._tx() as conn:
        conn.execute("UPDATE runs SET owner_pid = ?, owner_host = ?, owner_token = 'x' WHERE id = 'killed-run'",
                     (dead, dbmod.this_host()))
    result = actions.generate_slot(ws, slot.id, settings=settings)  # no force needed: its process is gone
    assert result.slot.status == "drafted" and ws.get_run("killed-run")["status"] == "interrupted"


class CliRoundDuringReReview(MockBackend):
    """A CLI run (`insia run-due` in a terminal, another Workspace) stores its next round on the item while the
    dashboard's 수정 요청 re-reviews its revision."""

    def __init__(self, settings, home, item_id, run_id):
        super().__init__(settings)
        self.home, self.item_id, self.run_id, self.done = home, item_id, run_id, False

    def review(self, brief, research, draft, format_checks):
        out = super().review(brief, research, draft, format_checks)
        if not self.done:
            self.done = True
            with Workspace(self.home) as cli:
                cli.add_run_version(self.item_id, Draft(channel="linkedin", round=9, title="CLI 라운드", content="CLI 본문"),
                                    run_id=self.run_id)
        return out


def test_revise_is_not_superseded_when_a_cli_round_keeps_its_revision_current(settings, brief, tmp_path):
    """A CLI round stored during 수정 요청's re-review is put back under the revision: the job is not 'superseded'."""
    home = tmp_path / "ws"
    ws = Workspace(home)
    try:
        result, _ = execute_run(brief, settings, workspace=ws)
        item_id = pipeline_item_id(result.run_id, "linkedin")
        base = ws.get_item(item_id).versions[-1]
        assert base.review is not None
        job = actions.revise_item(ws, item_id, "짧게", settings=settings,
                                  backend=CliRoundDuringReReview(settings, home, item_id, result.run_id))
        detail = ws.get_item(item_id)
        revision, cli_round, current = detail.versions[-3], detail.versions[-2], detail.versions[-1]
        assert (revision.version, cli_round.draft.title) == (base.version + 1, "CLI 라운드")
        assert current.draft.content == revision.draft.content and current.review == job.review  # the revision is current
        assert detail.item.score == job.review.score
        assert not job.superseded and not job.superseded_by_human_edit
        assert ws.list_events(job.run_id)[-1]["data"]["superseded"] is False
    finally:
        ws.close()
