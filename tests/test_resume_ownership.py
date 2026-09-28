"""Run ownership across processes, resume safety (slot, budget cap) and the CLI recovery paths.

Regression tests for the P3 review findings 0 (a restart took over live runs), 6 (a resumed slot
could be generated twice), 7 (resume dropped the run's own budget cap), 8 (CLI-only users could not
recover a killed run-due) and 18 (a failed review on a later round left an unreviewed latest version),
and for the verification round after them: a taken-over owner that is still alive must stop before its
next paid call and must not hand the slot back over the new owner's work.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from insia_agents import actions
from insia_agents import db as dbmod
from insia_agents.backends.base import BackendError
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.cli import main
from insia_agents.db import Workspace, WorkspaceError, pipeline_item_id
from insia_agents.events import EventBus, SimClock
from insia_agents.models import Brief, Draft, PlannedSlot, Review, ReviewIssue, RubricScore, UsageRecord
from insia_agents.cli import build_parser
from insia_agents.pipeline import (TAKEN_OVER_MESSAGE, BudgetExceeded, PipelineError, RunCancelled, prepare_run,
                                   resume_run, run_pipeline)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    yield workspace
    workspace.close()


def _slot(ws, day="2026-09-28", channel="linkedin", topic="슬롯 주제"):
    return ws.add_slots([PlannedSlot(date=day, channel=channel, topic=topic, angle="사례", keywords=["AI"], goal="인지")])[0]


def _dead_pid() -> int:
    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                              check=True).stdout)


def _owned_by(ws, run_id, pid, heartbeat_at=None):
    """Make ``run_id`` look like another process on this machine is (or was) running it."""
    beat = heartbeat_at or dbmod.utc_now()
    with ws._tx() as conn:
        conn.execute("UPDATE runs SET owner_pid = ?, owner_host = ?, owner_boot = ?, owner_token = 'elsewhere', heartbeat_at = ?, "
                     "updated_at = ? WHERE id = ?", (pid, dbmod.this_host(), dbmod.boot_marker(), beat, beat, run_id))


@pytest.fixture
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    yield proc.pid
    proc.stdin.close()
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Finding 0: a server start next to a live CLI run
# ---------------------------------------------------------------------------

CHILD = r'''
import sys
sys.path.insert(0, {src!r})
from insia_agents.db import Workspace
from insia_agents.models import Brief
ws = Workspace({home!r})
ws.create_run("run-cli", Brief(topic="크론 실행", channels=["linkedin"]), kind="slot", options={{"slot_id": {slot!r}}})
ws.claim_slot({slot!r}, "run-cli")
lease = ws.acquire_run("run-cli")
ws.append_event("run-cli", {{"type": "run.started", "data": {{}}}})
print("ready", flush=True)
if sys.stdin.readline().strip() == "finish":
    ws.append_event("run-cli", {{"type": "log", "data": {{"message": "still working"}}}})
    ws.update_run("run-cli", status="completed")
    lease.release()
    print("done", flush=True)
sys.stdin.read()
'''


def _child(home: Path, slot_id: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", CHILD.format(src=str(SRC), home=str(home), slot=slot_id)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def test_server_start_leaves_a_live_cli_run_alone(tmp_path):
    home = tmp_path / "ws"
    with Workspace(home) as ws:
        slot = _slot(ws)
    child = _child(home, slot.id)  # e.g. the 07:50 cron `insia run-due`, still working
    try:
        with Workspace(home) as server_ws:  # `insia serve` starting on the same workspace
            assert server_ws.mark_interrupted() == 0
            assert server_ws.get_run("run-cli")["status"] == "running"
            assert server_ws.get_slot(slot.id).status == "generating" and server_ws.due_slots("2026-12-31") == []
            child.stdin.write("finish\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "done"
            events = server_ws.list_events("run-cli")
            assert [e["type"] for e in events] == ["run.started", "log"]  # no run.failed, no seq collision
            assert server_ws.get_run("run-cli")["status"] == "completed"
    finally:
        child.stdin.close()
        child.wait(timeout=10)


def test_a_killed_cli_run_is_taken_over(tmp_path):
    home = tmp_path / "ws"
    with Workspace(home) as ws:
        slot = _slot(ws)
    child = _child(home, slot.id)
    child.kill()  # power loss / closed terminal
    child.wait(timeout=10)
    with Workspace(home) as ws:
        assert ws.mark_interrupted() == 1
        run = ws.get_run("run-cli")
        assert run["status"] == "interrupted" and ws.last_event("run-cli")["data"]["interrupted"] is True
        assert ws.get_slot(slot.id).status == "planned"


# ---------------------------------------------------------------------------
# Finding 8: resume and run-due recover runs whose process is gone
# ---------------------------------------------------------------------------


def test_resume_recovers_a_running_run_whose_process_died(settings, ws):
    brief = Brief(topic="죽은 실행", channels=["linkedin"])
    ws.create_run("dead-run", brief, options={"max_rounds": 1, "pass_score": 80}, mode="mock", model="mock")
    ws.append_event("dead-run", {"seq": 1, "t": 0.0, "type": "run.started", "agent": "system", "data": {}})
    _owned_by(ws, "dead-run", _dead_pid())
    result = resume_run("dead-run", settings, ws, backend=MockBackend(settings), out_dir=None)
    assert [r.channel for r in result.results] == ["linkedin"] and ws.get_run("dead-run")["status"] == "completed"
    types = [e["type"] for e in ws.list_events("dead-run")]
    assert types[:3] == ["run.started", "run.failed", "run.started"] and types[-1] == "run.completed"


def test_resume_refuses_a_live_run_with_accurate_advice(settings, ws, live_process):
    ws.create_run("live-run", Brief(topic="살아 있는 실행", channels=["linkedin"]), mode="mock")
    _owned_by(ws, "live-run", live_process)
    with pytest.raises(PipelineError) as refused:
        resume_run("live-run", settings, ws, backend=MockBackend(settings), out_dir=None)
    message = str(refused.value)
    assert "실행 중" in message and f"이 컴퓨터의 프로세스 {live_process}" in message
    assert "insia resume live-run --force" in message and "다시 시작한 뒤" not in message
    assert ws.get_run("live-run")["status"] == "running"
    result = resume_run("live-run", settings, ws, backend=MockBackend(settings), out_dir=None, force=True)
    assert result.results and ws.get_run("live-run")["status"] == "completed"


def _cli(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "cli-ws"
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.setenv("INSIA_TODAY", "2026-09-28")
    monkeypatch.delenv("INSIA_MAX_COST_USD", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_cli_run_due_recovers_a_killed_run_due(capsys, cli_home, live_process):
    with Workspace(cli_home) as ws:
        stuck = _slot(ws, topic="멈춘 슬롯")
        busy = _slot(ws, channel="instagram", topic="다른 곳에서 만드는 슬롯")
        for run_id, slot, pid in (("killed-run", stuck, _dead_pid()), ("busy-run", busy, live_process)):
            ws.create_run(run_id, Brief(topic=slot.topic, channels=[slot.channel]), kind="slot", options={"slot_id": slot.id})
            ws.claim_slot(slot.id, run_id)
            _owned_by(ws, run_id, pid)
    code, out, _ = _cli(capsys, "run-due", "--dry-run", "--mode", "mock")
    assert code == 0 and "멈춘 슬롯: 2026-09-28 링크드인 · 멈춘 슬롯" in out and "--dry-run 없이" in out
    assert "건너뜀: 2026-09-28 인스타그램 · 다른 곳에서 만드는 슬롯" in out
    with Workspace(cli_home) as ws:
        assert ws.get_run("killed-run")["status"] == "running"  # a dry run changes nothing
    code, out, err = _cli(capsys, "run-due", "--mode", "mock", "--quiet")
    assert code == 0, err
    assert "지난번에 끝나지 못한 실행 1개를 '중단됨'으로 정리했어요 (killed-run)" in out and "초안 1개를 만들었어요" in out
    with Workspace(cli_home) as ws:
        assert ws.get_run("killed-run")["status"] == "interrupted"
        assert ws.get_slot(stuck.id).status == "drafted" and ws.get_slot(busy.id).status == "generating"
        assert ws.get_run("busy-run")["status"] == "running"


def test_cli_resume_and_runs_list_explain_a_killed_run(capsys, cli_home):
    run_id = "20260928-070000-dead"
    with Workspace(cli_home) as ws:
        ws.create_run(run_id, Brief(topic="밤새 멈춘 실행", channels=["linkedin"]), mode="mock",
                      options={"max_rounds": 1, "pass_score": 80})
        _owned_by(ws, run_id, _dead_pid())
    code, out, _ = _cli(capsys, "runs", "list")
    assert code == 0 and f"실행하던 프로그램이 멈춘 실행 1개: insia resume {run_id}" in out
    code, out, err = _cli(capsys, "resume", run_id, "--mode", "mock", "--no-save", "--quiet")
    assert code == 0, err
    assert f"'중단됨'으로 정리했어요: {run_id}" in err and "누적 비용" in out
    with Workspace(cli_home) as ws:
        assert ws.get_run(run_id)["status"] == "completed"


def test_cli_forced_approval_says_it_is_recorded(capsys, cli_home):
    with Workspace(cli_home) as ws:
        item = ws.create_item("linkedin", "검수 미통과 글")
        ws.add_version(item.id, Draft(channel="linkedin", round=0, title="검수 미통과 글", content="본문"), source="agent",
                       review=Review(channel="linkedin", round=0, score=68, passed=False,
                                     rubric=[RubricScore(id="hook", label="훅", score=10, max=25, comment="")], issues=[],
                                     summary="요약"))
    code, _, err = _cli(capsys, "items", "approve", item.id)
    assert code == 1 and "--force" in err
    code, out, _ = _cli(capsys, "items", "approve", item.id, "--force")
    assert code == 0 and "강제 승인으로 기록했어요" in out and "v1(68점)" in out
    with Workspace(cli_home) as ws:
        stored = ws.get_item(item.id).item
        assert (stored.approval_forced, stored.approved_version, stored.approved_score) == (True, 1, 68)


# ---------------------------------------------------------------------------
# Finding 6: a resumed slot run holds its slot
# ---------------------------------------------------------------------------


class CrashOnReview(MockBackend):
    def review(self, *args, **kwargs):
        raise KeyboardInterrupt  # stands in for the process dying mid-run


def _interrupted_slot_run(settings, ws, slot, run_id="run-slot"):
    backend, bus, _ = prepare_run(settings, run_id=run_id, backend=CrashOnReview(settings))
    with pytest.raises(KeyboardInterrupt):
        actions.generate_slot(ws, slot.id, settings=settings, backend=backend, bus=bus)
    # what a killed process leaves behind, then the next start recovers it
    ws.update_run(run_id, status="running")
    ws.update_slot(slot.id, status="generating", run_id=run_id)
    _owned_by(ws, run_id, _dead_pid())
    assert ws.mark_interrupted() == 1 and ws.get_slot(slot.id).status == "planned"


def test_resumed_slot_run_holds_the_slot_while_it_runs(settings, ws):
    slot = _slot(ws)
    _interrupted_slot_run(settings, ws, slot)
    seen: dict = {}

    def listener(event):
        if event["type"] == "run.started" and "status" not in seen:
            seen["status"] = ws.get_slot(slot.id).status
            seen["due"] = ws.due_slots("2026-12-31")
            try:  # cron run-due / 초안 만들기 for the same slot meanwhile
                actions.generate_slot(ws, slot.id, settings=settings)
            except WorkspaceError as exc:
                seen["second"] = str(exc)

    resume_run("run-slot", settings, ws, listener=listener, out_dir=None)
    assert seen["status"] == "generating" and seen["due"] == []
    assert "다른 실행(run-slot)이 초안을 만드는 중" in seen["second"]
    final = ws.get_slot(slot.id)
    assert (final.status, final.item_id, final.run_id) == ("drafted", pipeline_item_id("run-slot", "linkedin"), "run-slot")
    assert [i.id for i in ws.list_items() if i.scheduled_at == slot.date] == [final.item_id]  # one draft for the slot


def test_failed_resume_of_a_slot_run_puts_the_slot_back(settings, ws):
    slot = _slot(ws)
    _interrupted_slot_run(settings, ws, slot)

    class BrokenDraft(MockBackend):
        def draft(self, *args, **kwargs):
            raise BackendError("초안 실패")

        def review(self, *args, **kwargs):
            raise BackendError("검수 실패")

    with pytest.raises(BackendError):
        resume_run("run-slot", settings, ws, backend=BrokenDraft(settings), out_dir=None)
    after = ws.get_slot(slot.id)
    assert after.status == "planned" and after.run_id == "run-slot"
    assert ws.get_run("run-slot")["status"] == "failed"


def test_resume_does_not_take_a_slot_a_newer_run_drafted(settings, ws):
    slot = _slot(ws)
    _interrupted_slot_run(settings, ws, slot)
    newer = actions.generate_slot(ws, slot.id, settings=settings)  # the user made a fresh draft meanwhile
    resume_run("run-slot", settings, ws, backend=MockBackend(settings), out_dir=None)
    final = ws.get_slot(slot.id)
    assert (final.status, final.item_id) == ("drafted", newer.item.id)  # the newer draft keeps the slot
    assert ws.get_item(pipeline_item_id("run-slot", "linkedin")) is not None  # the resumed run still finished its item


def test_resume_refuses_while_another_live_run_generates_the_slot(settings, ws, live_process):
    slot = _slot(ws)
    _interrupted_slot_run(settings, ws, slot)
    ws.create_run("other-run", Brief(topic="x", channels=["linkedin"]), kind="slot", options={"slot_id": slot.id})
    ws.claim_slot(slot.id, "other-run")
    _owned_by(ws, "other-run", live_process)
    with pytest.raises(WorkspaceError, match="다른 실행\\(other-run\\)"):
        resume_run("run-slot", settings, ws, backend=MockBackend(settings), out_dir=None)
    assert ws.get_run("run-slot")["status"] == "interrupted"  # given back as it was
    assert ws.get_slot(slot.id).run_id == "other-run"


# ---------------------------------------------------------------------------
# Finding 7: a resumed run keeps its own budget cap
# ---------------------------------------------------------------------------


class Priced(MockBackend):
    """Every backend call costs $0.10; ``crash_after`` stops the process after that many calls."""

    def __init__(self, settings, crash_after=None):
        super().__init__(settings)
        self.crash_after = crash_after
        self.calls = 0

    def _usage(self, agent, task, prompt, output):
        self.calls += 1
        if self.on_usage is not None:
            self.on_usage(UsageRecord(agent=agent, task=task, model="m", input_tokens=10, output_tokens=10, cost_usd=0.10))
        if self.crash_after and self.calls >= self.crash_after:
            raise KeyboardInterrupt


def test_resume_keeps_the_runs_own_budget_cap(settings, ws):
    brief = Brief(topic="예산 테스트", channels=["linkedin", "instagram", "naver_blog"])
    capped = replace(settings, max_cost_usd=0.60)  # e.g. options {"max_cost_usd": 0.6} or --max-cost-usd 0.6
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(brief, Priced(capped, crash_after=3), EventBus("run-c", clock=SimClock(0)), capped, workspace=ws)
    assert ws.get_run("run-c")["options"]["max_cost_usd"] == 0.6 and ws.run_cost("run-c") == pytest.approx(0.3)

    # stopped by Ctrl+C, not by the budget: a resume with a bigger (or no) default cap still keeps the run's $0.60
    with pytest.raises(BudgetExceeded) as stop:
        resume_run("run-c", replace(settings, max_cost_usd=10.0), ws, backend=Priced(settings), out_dir=None)
    assert stop.value.cap == pytest.approx(0.6) and ws.run_cost("run-c") <= 0.6 + 0.35  # in-flight calls may finish
    started = [e for e in ws.list_events("run-c") if e["type"] == "run.started"]
    assert started[-1]["data"]["budget_usd"] == pytest.approx(0.6)

    # now it is budget-stopped: without a higher cap it stops again before any paid call
    idle = Priced(settings)
    with pytest.raises(BudgetExceeded):
        resume_run("run-c", settings, ws, backend=idle, out_dir=None)  # default settings: no cap at all
    assert idle.calls == 0

    result = resume_run("run-c", settings, ws, backend=Priced(settings), out_dir=None, max_cost_usd=5.0)  # raised explicitly
    assert len(result.results) == 3 and ws.get_run("run-c")["status"] == "completed"
    assert ws.get_run("run-c")["options"]["max_cost_usd"] == 5.0  # the new cap is the run's cap from now on


def test_budget_stopped_run_resumes_with_a_higher_cap_from_settings(settings, ws):
    brief = Brief(topic="예산 초과 후 상한 올리기", channels=["linkedin"])
    capped = replace(settings, max_cost_usd=0.25)
    with pytest.raises(BudgetExceeded):
        run_pipeline(brief, Priced(capped), EventBus("run-b", clock=SimClock(0)), capped, workspace=ws)
    lower = Priced(settings)
    with pytest.raises(BudgetExceeded):  # a lower cap than what was spent: nothing new starts
        resume_run("run-b", replace(settings, max_cost_usd=0.2), ws, backend=lower, out_dir=None)
    assert lower.calls == 0
    result = resume_run("run-b", replace(settings, max_cost_usd=3.0), ws, backend=Priced(settings), out_dir=None)
    assert result.results and ws.get_run("run-b")["options"]["max_cost_usd"] == 3.0


def test_resume_of_an_uncapped_run_uses_the_current_default(settings, ws):
    brief = Brief(topic="상한 없는 실행", channels=["linkedin"])
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(brief, Priced(settings, crash_after=2), EventBus("run-u", clock=SimClock(0)), settings, workspace=ws)
    with pytest.raises(BudgetExceeded) as stop:
        resume_run("run-u", replace(settings, max_cost_usd=0.25), ws, backend=Priced(settings), out_dir=None)
    assert stop.value.cap == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Finding 18: the item's latest version is the run's final draft even when a later review failed
# ---------------------------------------------------------------------------


class FailingSecondReview(MockBackend):
    def review(self, brief, research, draft, format_checks):
        if draft.round == 1:
            raise BackendError("검수 API 실패 (다시 시도해도 안 됨)")
        review = super().review(brief, research, draft, format_checks)
        issue = ReviewIssue(severity="critical", location="본문", problem="강제 실패", fix="고치기")
        return review.model_copy(update={"issues": [*review.issues, issue]})


def test_failed_later_review_keeps_the_final_draft_as_latest(settings, ws):
    brief = Brief(topic="검수 실패 테스트", channels=["linkedin"])
    result = run_pipeline(brief, FailingSecondReview(settings), EventBus("run-t1", clock=SimClock(0)), settings, workspace=ws)
    final = result.results[0].final
    detail = ws.get_item(pipeline_item_id("run-t1", "linkedin"))
    assert [(v.draft.round, v.review is not None) for v in detail.versions] == [(0, True), (1, False), (0, True)]
    assert detail.versions[-1].draft.content == final.content and final.round == 0
    assert detail.item.version == 3 and detail.item.status == "needs_changes" and detail.item.score == detail.versions[0].review.score
    completed = [e for e in ws.list_events("run-t1") if e["type"] == "channel.completed"][0]
    assert completed["data"]["final_round"] == 0



# ---------------------------------------------------------------------------
# Verification round: a taken-over owner that is still alive stops and leaves the slot to the new owner
# ---------------------------------------------------------------------------

TAKEOVER_CHILD = r"""
import sys
sys.path.insert(0, {src!r})
from insia_agents.db import Workspace
from insia_agents.models import Brief
ws = Workspace({home!r})
# e.g. a cron `insia run-due` after the owner's heartbeat was quiet for 10+ minutes (a laptop that slept)
taken = ws.recover_stale(stale_after=0)
ws.create_run("run-new", Brief(topic="새 실행", channels=["linkedin"]), kind="slot", options={{"slot_id": {slot!r}}})
ws.claim_slot({slot!r}, "run-new")
lease = ws.acquire_run("run-new")
print("taken", ",".join(taken), flush=True)
sys.stdin.read()
"""

FORCED_RESUME_CHILD = r"""
import sys
sys.path.insert(0, {src!r})
from dataclasses import replace
from pathlib import Path
from insia_agents.config import Settings
from insia_agents.db import Workspace
from insia_agents.pipeline import resume_run
settings = replace(Settings.from_env(env={{}}, mode="mock", speed=0.0, today="2026-09-28"), out_dir=None, home=Path({home!r}))
ws = Workspace({home!r})
resume_run({run_id!r}, settings, ws, force=True, out_dir=None)  # `insia resume <id> --force` while the owner still runs
print("resumed", ws.get_slot({slot!r}).status, flush=True)
"""


def test_owner_taken_over_after_a_stale_heartbeat_stops_and_leaves_the_slot_alone(settings, tmp_path):
    home = tmp_path / "ws"
    ws = Workspace(home)
    slot = _slot(ws)
    backend = Priced(settings)
    _, bus, _ = prepare_run(settings, run_id="run-old", backend=backend)
    children: list[subprocess.Popen] = []

    def take_over(event):
        if event["type"] == "run.started" and not children:
            child = subprocess.Popen([sys.executable, "-c", TAKEOVER_CHILD.format(src=str(SRC), home=str(home), slot=slot.id)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            children.append(child)
            assert child.stdout.readline().strip() == "taken run-old"

    try:
        with pytest.raises(RunCancelled, match=TAKEN_OVER_MESSAGE):
            actions.generate_slot(ws, slot.id, settings=settings, backend=backend, bus=bus, listener=take_over)
        assert backend.calls == 0  # stopped at the checkpoint before its next paid call
        after = ws.get_slot(slot.id)
        assert (after.status, after.run_id) == ("generating", "run-new")  # the new owner's claim is not undone
        assert ws.due_slots("2026-12-31") == []
        with pytest.raises(WorkspaceError, match="다른 실행\\(run-new\\)"):  # no third generation while run-new works
            ws.claim_slot(slot.id, "run-third")
        run = ws.get_run("run-old")
        assert run["status"] == "interrupted" and run["error"] == dbmod.INTERRUPTED_MESSAGE  # as the recovery left it
        assert [e["type"] for e in ws.list_events("run-old")] == ["run.failed"]  # the old owner stored nothing more
        assert ws.get_item(pipeline_item_id("run-old", "linkedin")) is None
    finally:
        for child in children:
            child.stdin.close()
            child.wait(timeout=10)
        ws.close()


def test_forced_resume_of_a_live_slot_run_keeps_the_finished_slot(settings, tmp_path):
    home = tmp_path / "ws"
    ws = Workspace(home)
    slot = _slot(ws)
    backend = Priced(settings)
    _, bus, _ = prepare_run(settings, run_id="run-live", backend=backend)
    outputs: list[str] = []

    def forced_resume(event):
        if event["type"] == "run.started" and not outputs:
            done = subprocess.run([sys.executable, "-c", FORCED_RESUME_CHILD.format(src=str(SRC), home=str(home),
                                                                                    run_id="run-live", slot=slot.id)],
                                  capture_output=True, text=True, timeout=120)
            outputs.append(done.stdout.strip() or done.stderr)

    try:
        with pytest.raises(RunCancelled, match=TAKEN_OVER_MESSAGE):
            actions.generate_slot(ws, slot.id, settings=settings, backend=backend, bus=bus, listener=forced_resume)
        assert outputs == ["resumed drafted"]
        assert backend.calls == 0
        item_id = pipeline_item_id("run-live", "linkedin")
        after = ws.get_slot(slot.id)
        assert (after.status, after.item_id, after.run_id) == ("drafted", item_id, "run-live")  # not put back to 'planned'
        assert ws.due_slots("2026-12-31") == []  # no duplicate paid draft on the next run-due
        assert ws.get_run("run-live")["status"] == "completed"
        assert ws.get_item(item_id).item.scheduled_at == slot.date
    finally:
        ws.close()


def test_checkpoint_stops_a_taken_over_run_and_its_other_channels():
    from insia_agents.pipeline import UsageMeter, make_checkpoint

    class Runner:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    runner, taken = Runner(), [False]
    checkpoint = make_checkpoint(runner, UsageMeter("run-x"), lambda: taken[0])
    checkpoint()  # still ours: goes on
    taken[0] = True
    with pytest.raises(RunCancelled, match=TAKEN_OVER_MESSAGE):
        checkpoint()
    assert runner.cancelled  # the parallel channels stop at their next checkpoint too


def test_resume_help_and_parser_describe_the_runs_own_cap(capsys):
    parser = build_parser()
    assert parser.parse_args(["resume", "run-1", "--max-cost-usd", "2.5"]).max_cost_usd == 2.5
    assert parser.parse_args(["resume", "run-1"]).max_cost_usd is None
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "-h"])
    text = " ".join(capsys.readouterr().out.split())
    assert "이 실행의 새 예산 상한" in text and "처음 실행할 때 정한 상한을 그대로" in text
    assert "기본 INSIA_MAX_COST_USD, 0 = 없음" not in text
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "-h"])
    assert "기본 INSIA_MAX_COST_USD, 0 = 없음" in " ".join(capsys.readouterr().out.split())  # other commands keep theirs


def test_failed_resume_leaves_the_slot_to_a_forced_resume_that_took_the_run_over(settings, ws, live_process):
    slot = _slot(ws)
    _interrupted_slot_run(settings, ws, slot)

    def forced_elsewhere(event):  # another process runs `insia resume run-slot --force` and is generating the slot now
        if event["type"] == "run.started":
            _owned_by(ws, "run-slot", live_process)

    with pytest.raises(RunCancelled, match=TAKEN_OVER_MESSAGE):
        resume_run("run-slot", settings, ws, listener=forced_elsewhere, out_dir=None)
    after = ws.get_slot(slot.id)
    assert (after.status, after.run_id) == ("generating", "run-slot")  # not put back to 'planned' under the new owner
    assert ws.due_slots("2026-12-31") == [] and ws.get_run("run-slot")["status"] == "running"
    with pytest.raises(WorkspaceError, match="다른 실행\\(run-slot\\)"):
        ws.claim_slot(slot.id, "run-third")


def test_forced_resume_is_refused_while_this_same_process_runs_the_run(settings, ws):
    slot = _slot(ws)
    _, bus, _ = prepare_run(settings, run_id="run-same")
    refused: list[str] = []

    def forced_resume_here(event):  # e.g. a second caller in the same process forces a resume of the live run
        if event["type"] == "run.started" and not refused:
            with pytest.raises(PipelineError) as error:
                resume_run("run-same", settings, ws, force=True, out_dir=None)
            refused.append(str(error.value))
            assert not ws.claim_run("run-same", "running")  # the atomic claim refuses it too

    result = actions.generate_slot(ws, slot.id, settings=settings, bus=bus, listener=forced_resume_here)
    assert "이 프로그램에서 아직 진행 중" in refused[0]
    assert result.slot.status == "drafted" and ws.get_run("run-same")["status"] == "completed"  # the running thread is unaffected
    types = [e["type"] for e in ws.list_events("run-same")]
    assert types[0] == "run.started" and types[-1] == "run.completed" and types.count("run.started") == 1


def test_writers_bound_to_a_lost_lease_stay_quiet_after_this_process_owns_the_run_again(ws, live_process):
    from insia_agents.models import Plan
    from insia_agents.pipeline import _Recorder, event_sink

    brief = Brief(topic="다시 넘겨받은 실행", channels=["linkedin"])
    ws.create_run("run-w", brief)
    old = ws.acquire_run("run-w")
    _owned_by(ws, "run-w", live_process)  # another process took it over ...
    assert old.verify() is False
    new = ws.acquire_run("run-w")  # ... and later this process owns it again (a new lease)
    recorder = _Recorder(ws, "run-w", brief)
    recorder.lease = old  # the first owner's recorder and event sink are still around
    with pytest.raises(dbmod.RunTakenOverError):
        recorder.plan(Plan(summary="요약", key_messages=["핵심"], questions=[], outlines=[]))
    event_sink(ws, "run-w", old)({"type": "log", "data": {"message": "stale owner"}})
    assert ws.list_events("run-w") == [] and ws.get_run("run-w")["plan"] is None
    event_sink(ws, "run-w", new)({"type": "log", "data": {"message": "current owner"}})
    assert [e["data"]["message"] for e in ws.list_events("run-w")] == ["current owner"]
    new.release()


def test_cli_run_due_skips_a_slot_whose_run_another_process_took_over(capsys, cli_home, monkeypatch):
    with Workspace(cli_home) as ws:
        _slot(ws, topic="넘겨준 슬롯")

    def taken_over(*args, **kwargs):  # the run was recovered elsewhere while this run-due slept (laptop lid closed)
        raise RunCancelled(TAKEN_OVER_MESSAGE)

    monkeypatch.setattr(actions, "generate_slot", taken_over)
    code, out, err = _cli(capsys, "run-due", "--mode", "mock", "--quiet")
    assert code == 0, err  # not a failure of this cron run: the run goes on where it was taken over
    assert f"→ 건너뛰었어요: {TAKEN_OVER_MESSAGE}" in out and "건너뜀 1개" in out
