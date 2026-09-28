"""Item jobs: work on one content item after a run.

- ``review_item``   — 재검수: the reviewer scores the current version again.
- ``revise_item``   — 수정 요청: the orchestrator revises with the latest review and
  optional human instructions → new version (source=agent) → automatic re-review.
- ``edit_item``     — 직접 수정: a human edit becomes a new version (source=human)
  with fresh deterministic format checks (no LLM call); status goes back to draft.
- ``generate_slot`` — 캘린더 슬롯 초안: a single-channel pipeline run from a slot
  and the company profile; the item is linked to the slot.

Each job is a run of its own (``runs.kind`` = review / revise / edit / slot)
with the normal event stream, so the dashboard animates the same agents and
the workspace keeps the history. Pass ``bus`` (and ``backend``) from
``pipeline.prepare_run`` to know the run id before the job starts (the server
streams it); without them the job builds its own. Pass ``runner`` (a
cancellable ``SimRunner`` / ``ThreadRunner``) to be able to stop the job the
way a pipeline run is stopped. Every function checks its input first and
raises ``WorkspaceError`` / ``NotFoundError`` before any run is created.

Human edits during a job: a review/revise job records the version it started
from (``options.base_version``). If a newer version appears while it runs (a
person saved an edit), the job's result does not silently replace it: a
review stays attached to the version it reviewed, and a revision is kept in
the history while the newer version is put back on top as the current one
(``Workspace.add_job_version``). ``JobResult.superseded_by_human_edit`` /
``current_version`` and the ``run.completed`` event report it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .agents import orchestrator, researcher, reviewer
from .agents.common import AgentContext, channel_label
from .backends import create_backend
from .backends.base import Backend, BackendError, RunContext
from .channels import check_format
from .config import Settings, resolve_mode
from .db import (NotFoundError, RunTakenOverError, Workspace, WorkspaceError, normalize_hashtags, pipeline_item_id,
                 profile_is_empty)
from .events import EventBus, RealClock, SimClock
from .models import (Brief, CalendarSlot, ContentItem, ContentItemDetail, Draft, DraftVersion, FormatCheck, Plan, Profile,
                     ResearchPack, ResearchQuestion, Review, RunResult)
from .pipeline import (TAKEN_OVER_MESSAGE, BudgetExceeded, RunCancelled, SimRunner, ThreadRunner, UsageMeter, build_context,
                       continue_numbering, ensure_run, event_sink, failure_status, install_backend_hooks, link_slot_after_run,
                       make_checkpoint, make_wait, new_run_id, prepare_run, run_pipeline)

log = logging.getLogger(__name__)

MAX_INSTRUCTIONS_CHARS = 4_000
MAX_TITLE_CHARS = 300
MAX_CONTENT_CHARS = 100_000


@dataclass
class JobResult:
    """What a job produced. ``to_dict()`` is JSON-ready for the API."""

    run_id: str
    kind: str
    item: ContentItem | None
    version: DraftVersion | None = None
    review: Review | None = None
    format_checks: list[FormatCheck] = field(default_factory=list)
    slot: CalendarSlot | None = None
    result: RunResult | None = None
    base_version: int | None = None  # the version a review/revise job started from
    current_version: int | None = None  # the item's current version when the job ended
    superseded: bool = False  # a newer version appeared while the job ran; the job's result did not become current
    superseded_by_human_edit: bool = False  # ... and that newer version was a person's edit

    def to_dict(self) -> dict[str, Any]:
        def dump(value: Any) -> Any:
            return value.model_dump(mode="json") if value is not None else None

        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "item": dump(self.item),
            "version": dump(self.version),
            "review": dump(self.review),
            "format_checks": [c.model_dump(mode="json") for c in self.format_checks],
            "slot": dump(self.slot),
            "base_version": self.base_version,
            "current_version": self.current_version,
            "superseded": self.superseded,
            "superseded_by_human_edit": self.superseded_by_human_edit,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_item(workspace: Workspace, item_id: str) -> ContentItemDetail:
    detail = workspace.get_item(item_id)
    if detail is None:
        raise NotFoundError(f"콘텐츠 {item_id}를 찾을 수 없어요")
    return detail


def _latest(detail: ContentItemDetail, what: str) -> DraftVersion:
    if not detail.versions:
        raise WorkspaceError(f"{what} 버전이 없어요. 초안을 먼저 만들어 주세요.")
    return detail.versions[-1]


def _item_brief(detail: ContentItemDetail) -> Brief:
    channel = detail.item.channel
    brief = detail.brief or Brief(topic=detail.item.title or "콘텐츠", channels=[channel])
    return brief.model_copy(update={"channels": [channel]})


def _empty_research() -> ResearchPack:
    return ResearchPack(findings=[], sources=[], gaps=[])


def _empty_plan(brief: Brief) -> Plan:
    return Plan(summary="", key_messages=[], questions=[], outlines=[])


def _store(plan: Plan, research: ResearchPack, brief: Brief) -> researcher.ResearchStore:
    """A research store whose question ids never collide with ids already used by findings."""
    store = researcher.ResearchStore()
    store.pack = research.model_copy(deep=True)
    questions = list(plan.questions)
    known = {q.id for q in questions}
    for finding in research.findings:
        if finding.question_id and finding.question_id not in known:
            known.add(finding.question_id)
            questions.append(ResearchQuestion(id=finding.question_id, question="(이전 조사)", why="이전 실행의 조사",
                                              channels=list(brief.channels), priority="medium"))
    store.questions = questions
    return store


def _context(workspace: Workspace, settings: Settings, context: RunContext | None, instructions: str = "") -> RunContext:
    if context is None:
        return build_context(workspace, settings, instructions=instructions)
    return replace(context, instructions=instructions) if instructions else context


class _Job:
    """One item job as a run: run row, DB event sink, usage meter, budget, start/finish/failure events."""

    def __init__(self, workspace: Workspace, settings: Settings, kind: str, brief: Brief, *, item_id: str,
                 backend: Backend | None, bus: EventBus | None, client: object | None,
                 listener: Callable[[dict[str, Any]], None] | None, context: RunContext, options: dict[str, Any],
                 needs_backend: bool = True, runner: SimRunner | ThreadRunner | None = None) -> None:
        note: str | None = None
        if bus is None:
            if needs_backend:
                backend, bus, note = prepare_run(settings, client=client, backend=backend)
            else:
                bus = EventBus(new_run_id(), clock=RealClock())
        elif backend is None and needs_backend:
            mode, note = resolve_mode(settings.mode)
            backend = create_backend(mode, settings, client=client)
        self.workspace = workspace
        self.settings = settings
        self.kind = kind
        self.brief = brief
        self.item_id = item_id
        self.backend = backend if needs_backend else None
        self.bus = bus
        self.note = note
        self.listener = listener
        self.context = context
        self.options = options
        self.run_id = bus.run_id
        if runner is None:
            runner = SimRunner(bus.clock) if isinstance(bus.clock, SimClock) else ThreadRunner(1)
        self.runner: SimRunner | ThreadRunner = runner
        self.meter = UsageMeter(self.run_id, workspace=workspace, cap=settings.max_cost_usd,
                                initial=workspace.run_cost(self.run_id))
        self.lease: Any = None
        self.ctx = AgentContext(bus=bus, backend=self.backend, settings=settings, brief=brief,  # type: ignore[arg-type]
                                simulated=bool(getattr(bus.clock, "simulated", False)), context=context,
                                checkpoint=make_checkpoint(self.runner, self.meter,
                                                           lambda: self.lease is not None and not self.lease.verify()),
                                wait=make_wait(self.runner, bus))
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._restore: Callable[[], None] = lambda: None
        self._finished = False

    def __enter__(self) -> "_Job":
        backend = self.backend
        try:
            self.lease = ensure_run(self.workspace, self.run_id, self.brief, kind=self.kind, options=self.options,
                                    mode=backend.name if backend is not None else "",
                                    model=backend.model if backend is not None else "",
                                    profile=self.context.profile, parent_item_id=self.item_id)
            continue_numbering(self.workspace, self.bus)
            if self.listener is not None:
                self.bus.add_listener(self.listener)
            self._sink = event_sink(self.workspace, self.run_id, self.lease)
            self.bus.add_listener(self._sink)
            backend_note = None
            if backend is not None:
                if hasattr(backend, "on_notice"):
                    bus = self.bus
                    backend.on_notice = lambda agent, level, message: bus.emit("log", agent, {"level": level, "message": message})  # type: ignore[attr-defined]
                self._restore = install_backend_hooks(backend, self.context, self.meter)
                if hasattr(backend, "prepare"):
                    backend_note = backend.prepare(self.brief)  # type: ignore[attr-defined]
            self.bus.emit("run.started", "system", {
                "brief": self.brief,
                "channels": list(self.brief.channels),
                "mode": backend.name if backend is not None else "human",
                "model": backend.model if backend is not None else "",
                "max_rounds": self.settings.max_rounds,
                "pass_score": self.settings.pass_score,
                "kind": self.kind,
                "item_id": self.item_id,
            })
            if self.note:
                self.ctx.log(self.note)
            if backend_note:
                self.ctx.log(backend_note)
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def finish(self, **data: Any) -> None:
        payload: dict[str, Any] = {"duration_s": round(self.bus.clock.now(), 1), "scores": {}, "passed": {},
                                   "output_dir": None, "kind": self.kind, "item_id": self.item_id, **data}
        if self.meter.spent:
            payload["cost_usd"] = round(self.meter.spent, 6)
        if self.lease is not None and not self.lease.verify():  # taken over: the new owner records the run
            raise RunCancelled(TAKEN_OVER_MESSAGE)
        self.bus.emit("run.completed", "system", payload)
        self.workspace.update_run(self.run_id, status="completed", cost_usd=self.workspace.run_cost(self.run_id))
        self._finished = True

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        try:
            if exc is not None:
                status, message = failure_status(exc)
                if not self.bus.closed:
                    data: dict[str, Any] = {"error": message, "kind": self.kind, "item_id": self.item_id}
                    if isinstance(exc, BudgetExceeded):
                        data.update({"budget_exceeded": True, "budget_usd": exc.cap, "cost_usd": round(exc.spent, 6)})
                    try:
                        self.bus.emit("run.failed", "system", data)
                    except RuntimeError:
                        pass
                try:
                    if self.lease is not None and self.lease.lost:
                        raise RunTakenOverError(f"다른 곳에서 실행 {self.run_id}를 넘겨받았어요. 이 프로세스는 더 기록하지 않고 멈춰요.")
                    self.workspace.update_run(self.run_id, status=status, error=message,
                                              cost_usd=self.workspace.run_cost(self.run_id))
                except RunTakenOverError as taken:  # the new owner records how the run goes from here
                    log.warning("%s", taken)
                except Exception:  # noqa: BLE001 - never mask the job's own error
                    log.exception("작업 상태를 저장하지 못했어요 (run %s)", self.run_id)
            elif not self._finished:
                self.finish()
        finally:
            if self._sink is not None:
                self.bus.remove_listener(self._sink)
            self._restore()
            if self.lease is not None:
                self.lease.release()
        return False

    def done(self, *agents: str) -> None:
        messages = {"orchestrator": "요청한 작업을 마쳤어요", "researcher": "조사를 마쳤어요", "reviewer": "검수를 마쳤어요"}
        for agent in agents:
            self.ctx.status(agent, "done", messages[agent])


def _verdict(review: Review) -> str:
    return "통과" if review.passed else "미통과"


def _superseded(workspace: Workspace, item_id: str, base_version: int, own_version: int) -> tuple[DraftVersion | None, bool]:
    """When a job ends: ``(current, by_human)``.

    ``current`` is the item's current version when it is not the job's own
    result (``own_version``: the revision, or for a review the version it
    reviewed), i.e. something newer was saved while the job ran, else None.
    ``by_human`` is whether a person saved a version after the job started
    (any ``human`` version after ``base_version`` other than the job's own).
    """
    detail = workspace.get_item(item_id)
    if detail is None or not detail.versions or detail.versions[-1].version == own_version:
        return None, False
    by_human = any(v.source == "human" for v in detail.versions if v.version > base_version and v.version != own_version)
    return detail.versions[-1], by_human


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def review_item(workspace: Workspace, item_id: str, *, settings: Settings | None = None, backend: Backend | None = None,
                bus: EventBus | None = None, client: object | None = None,
                listener: Callable[[dict[str, Any]], None] | None = None, context: RunContext | None = None,
                runner: SimRunner | ThreadRunner | None = None) -> JobResult:
    """재검수: review the item's current (latest) version and attach the result to it.

    The review always stays on the version it reviewed. When a newer version
    was saved meanwhile, that one stays current (and unreviewed) and the
    result says ``superseded``.
    """
    settings = settings or Settings.from_env()
    detail = _require_item(workspace, item_id)
    latest = _latest(detail, "검수할")
    brief = _item_brief(detail)
    research = workspace.item_research(item_id) or _empty_research()
    context = _context(workspace, settings, context)
    channel = detail.item.channel
    label = channel_label(channel)
    options = {"item_id": item_id, "version": latest.version, "base_version": latest.version}
    with _Job(workspace, settings, "review", brief, item_id=item_id, backend=backend, bus=bus, client=client,
              listener=listener, context=context, options=options, runner=runner) as job:
        ctx = job.ctx
        ctx.status("orchestrator", "waiting", f"{label} v{latest.version} 재검수 결과를 기다려요")
        review = job.runner.run(reviewer.review(ctx, research, latest.draft))
        workspace.attach_review(latest.id, review, run_id=job.run_id)
        ctx.handoff(reviewer.AGENT, orchestrator.AGENT, "result",
                    f"{label} v{latest.version} 재검수 {review.score}점 · {_verdict(review)}", channel)
        newer, by_human = _superseded(workspace, item_id, latest.version, latest.version)
        if newer is not None:
            who = "사람이 고친" if by_human else "새"
            ctx.log(f"검수하는 동안 {who} 버전 v{newer.version}이 저장됐어요. 이 점수는 v{latest.version}의 점수이고, "
                    f"현재 버전 v{newer.version}은 아직 검수 전이에요.", "warn", reviewer.AGENT)
        job.done("reviewer", "orchestrator")
        item = _require_item(workspace, item_id).item
        job.finish(scores={channel: review.score}, passed={channel: review.passed}, version=latest.version, status=item.status,
                   base_version=latest.version, current_version=item.version, superseded=newer is not None,
                   superseded_by_human_edit=by_human)
    return JobResult(run_id=job.run_id, kind="review", item=item, version=workspace.get_version(latest.id), review=review,
                     format_checks=list(review.format_checks), base_version=latest.version, current_version=item.version,
                     superseded=newer is not None, superseded_by_human_edit=by_human)


def revise_item(workspace: Workspace, item_id: str, instructions: str = "", *, settings: Settings | None = None,
                backend: Backend | None = None, bus: EventBus | None = None, client: object | None = None,
                listener: Callable[[dict[str, Any]], None] | None = None, context: RunContext | None = None,
                runner: SimRunner | ThreadRunner | None = None) -> JobResult:
    """수정 요청: revise the latest version with its review (+ human ``instructions``) → new version → re-review.

    A latest version without a review is reviewed first. When the review
    asks for more research, the researcher runs a follow-up and the pack is
    stored on this job's run (later jobs on the item reuse it). When a newer
    version was saved while the job ran (a person's edit), the revision is
    kept in the history but that newer version stays current
    (``superseded_by_human_edit``; see ``Workspace.add_job_version``).
    """
    settings = settings or Settings.from_env()
    instructions = (instructions or "").strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise WorkspaceError(f"수정 지시가 너무 길어요 ({len(instructions):,}자). {MAX_INSTRUCTIONS_CHARS:,}자 이하로 줄여 주세요.")
    detail = _require_item(workspace, item_id)
    latest = _latest(detail, "수정할")
    brief = _item_brief(detail)
    plan = workspace.item_plan(item_id) or _empty_plan(brief)
    research = workspace.item_research(item_id) or _empty_research()
    context = _context(workspace, settings, context, instructions)
    channel = detail.item.channel
    label = channel_label(channel)
    options = {"item_id": item_id, "base_version": latest.version, "instructions": instructions}
    with _Job(workspace, settings, "revise", brief, item_id=item_id, backend=backend, bus=bus, client=client,
              listener=listener, context=context, options=options, runner=runner) as job:
        ctx, runner = job.ctx, job.runner
        store = _store(plan, research, brief)
        review = latest.review
        if review is None:
            ctx.log(f"{label} v{latest.version}에 검수 결과가 없어 먼저 검수해요", "info", reviewer.AGENT)
            review = runner.run(reviewer.review(ctx, store.snapshot(), latest.draft))
            workspace.attach_review(latest.id, review, run_id=job.run_id)
        runner.run(reviewer.request_revision(ctx, review, instructions))
        if review.needs_research:
            try:
                runner.run(researcher.followup(ctx, store, review.needs_research, channel))
                workspace.update_run(job.run_id, research=store.snapshot())
            except BackendError as exc:
                ctx.log(f"{label} 추가 조사 실패: {exc} — 지금 있는 근거로 수정을 이어가요", "warn", researcher.AGENT)
                ctx.status(researcher.AGENT, "idle", "추가 조사 요청이 오면 다시 찾아볼게요")
        new_draft = runner.run(orchestrator.revise(ctx, plan, store.snapshot(), latest.draft, review, instructions=instructions))
        version, restored = workspace.add_job_version(item_id, new_draft, base_version=latest.version, source="agent",
                                                      instructions=instructions, run_id=job.run_id)
        if restored is not None:
            who = "사람이 고친 버전" if restored.source == "human" else "새 버전"
            ctx.log(f"수정하는 동안 {who}이 저장돼서, 수정 결과는 v{version.version}으로 기록만 하고 그 내용을 현재 버전 "
                    f"v{restored.version}으로 유지했어요. 수정 결과를 쓰려면 보관함 기록에서 v{version.version}을 확인해 주세요.",
                    "warn", orchestrator.AGENT)
        new_review: Review | None = None
        try:
            new_review = runner.run(reviewer.review(ctx, store.snapshot(), new_draft))
        except BackendError as exc:  # the new version is saved; the user can re-run the review
            ctx.log(f"수정본 v{version.version}은 저장했지만 재검수를 마치지 못했어요: {exc}. 보관함에서 재검수를 눌러 주세요.",
                    "warn", reviewer.AGENT)
        if new_review is not None:
            workspace.attach_review(version.id, new_review, run_id=job.run_id)
            ctx.handoff(reviewer.AGENT, orchestrator.AGENT, "result",
                        f"{label} v{version.version} 검수 {new_review.score}점 · {_verdict(new_review)}", channel)
        # judged at the end: a person may also have saved a version while the revision was being re-reviewed
        newer, by_human = _superseded(workspace, item_id, latest.version, version.version)
        superseded = newer is not None
        if superseded and restored is None:
            who = "사람이 고친" if by_human else "새"
            ctx.log(f"재검수하는 동안 {who} 버전 v{newer.version}이 저장됐어요. 수정 결과 v{version.version}은 기록에 남고, "
                    f"현재 버전은 v{newer.version}이에요.", "warn", orchestrator.AGENT)
        job.done("orchestrator", "reviewer")
        item = _require_item(workspace, item_id).item
        job.finish(scores={channel: new_review.score} if new_review else {},
                   passed={channel: new_review.passed} if new_review else {},
                   version=version.version, status=item.status, base_version=latest.version, current_version=item.version,
                   superseded=superseded, superseded_by_human_edit=by_human)
    return JobResult(run_id=job.run_id, kind="revise", item=item, version=workspace.get_version(version.id),
                     review=new_review, format_checks=list(new_review.format_checks) if new_review else [],
                     base_version=latest.version, current_version=item.version, superseded=superseded,
                     superseded_by_human_edit=by_human)


def edit_item(workspace: Workspace, item_id: str, title: str, content: str, hashtags: list[str] | str | None = None, *,
              settings: Settings | None = None, bus: EventBus | None = None,
              listener: Callable[[dict[str, Any]], None] | None = None, profile: Profile | None = None) -> JobResult:
    """직접 수정: store a human edit as a new version (source=human) with fresh format checks.

    No LLM call and no review: the item goes back to ``draft`` (approval needs
    a re-review or "그래도 승인"); a ``published`` item keeps its status.
    ``profile`` defaults to the workspace profile (unless ``use_profile`` is off).
    """
    settings = settings or Settings.from_env()
    detail = _require_item(workspace, item_id)
    title = " ".join((title or "").split())
    content = (content or "").replace("\r\n", "\n").rstrip()
    if not title:
        raise WorkspaceError("제목을 입력해 주세요")
    if len(title) > MAX_TITLE_CHARS:
        raise WorkspaceError(f"제목이 너무 길어요 ({len(title)}자). {MAX_TITLE_CHARS}자 이하로 줄여 주세요.")
    if not content.strip():
        raise WorkspaceError("본문이 비어 있어요")
    if len(content) > MAX_CONTENT_CHARS:
        raise WorkspaceError(f"본문이 너무 길어요 ({len(content):,}자). {MAX_CONTENT_CHARS:,}자 이하로 줄여 주세요.")
    latest = detail.versions[-1] if detail.versions else None
    channel = detail.item.channel
    draft = Draft(channel=channel, round=latest.draft.round if latest else 0, title=title, content=content,
                  hashtags=normalize_hashtags(hashtags), used_finding_ids=list(latest.draft.used_finding_ids) if latest else [],
                  change_log=["사람이 직접 수정함"])
    brief = _item_brief(detail)
    if profile is None and settings.use_profile:
        saved = workspace.get_profile()
        profile = None if profile_is_empty(saved) else saved
    checks = check_format(draft, brief, profile)
    context = RunContext(profile=profile, documents=[], today=settings.today)
    with _Job(workspace, settings, "edit", brief, item_id=item_id, backend=None, bus=bus, client=None, listener=listener,
              context=context, options={"item_id": item_id}, needs_backend=False) as job:
        version = workspace.add_version(item_id, draft, source="human", run_id=job.run_id)
        if workspace.get_item(item_id).item.status == "archived":  # type: ignore[union-attr]
            workspace.set_item_status(item_id, "draft")
        orchestrator.emit_draft(job.ctx, draft, agent="system", source="human", version=version.version)
        failed = [c for c in checks if not c.passed]
        job.ctx.log(f"직접 고친 버전 v{version.version}을 저장했어요. 승인 전에 재검수를 돌리면 점수를 다시 매겨요.")
        if failed:
            job.ctx.log("아직 맞추지 못한 형식 기준: " + ", ".join(f"{c.label} {c.value} (기준 {c.expected})" for c in failed), "warn")
        item = _require_item(workspace, item_id).item
        job.finish(version=version.version, status=item.status, format_checks=checks)
    return JobResult(run_id=job.run_id, kind="edit", item=item, version=version, format_checks=checks)


def slot_brief(slot: CalendarSlot, profile: Profile | None = None) -> Brief:
    """A single-channel brief for a calendar slot (audience/tone from the profile)."""
    notes = [f"게시 예정일: {slot.date}"]
    if slot.angle:
        notes.append(f"관점·형식: {slot.angle}")
    if slot.goal:
        notes.append(f"이 게시물의 목표: {slot.goal}")
    return Brief(
        topic=slot.topic,
        goal=slot.goal,
        audience=profile.target_customers if profile else "",
        channels=[slot.channel],
        tone=profile.tone if profile else "",
        keywords=list(slot.keywords),
        notes="\n".join(notes),
    )


def generate_slot(workspace: Workspace, slot_id: str, *, settings: Settings | None = None, backend: Backend | None = None,
                  bus: EventBus | None = None, client: object | None = None,
                  listener: Callable[[dict[str, Any]], None] | None = None, context: RunContext | None = None,
                  force: bool = False, runner: SimRunner | ThreadRunner | None = None) -> JobResult:
    """캘린더 슬롯 초안: run the pipeline for the slot's channel and link the item to the slot.

    The slot is ``generating`` while it runs and ``drafted`` afterwards (the
    item gets the slot date as ``scheduled_at``); on failure it goes back to
    ``planned`` so it can be retried (or resumed with ``resume_run``), and a
    failed ``force=True`` regeneration keeps the draft the slot already had.
    A slot another live run is generating is refused even with ``force``; a
    slot left ``generating`` by a process that is gone is recovered and
    generated again (``Workspace.claim_slot``).
    """
    settings = settings or Settings.from_env()
    slot = workspace.get_slot(slot_id)
    if slot is None:
        raise NotFoundError(f"캘린더 슬롯 {slot_id}를 찾을 수 없어요")
    if slot.status == "drafted" and slot.item_id and not force:
        raise WorkspaceError(f"이미 초안이 있어요 (보관함 {slot.item_id}). 다시 만들려면 force로 요청해 주세요.")
    context = _context(workspace, settings, context)
    brief = slot_brief(slot, context.profile)
    note: str | None = None
    if bus is None:
        backend, bus, note = prepare_run(settings, client=client, backend=backend)
    elif backend is None:
        mode, note = resolve_mode(settings.mode)
        backend = create_backend(mode, settings, client=client)
    if listener is not None:
        bus.add_listener(listener)
    run_id = bus.run_id
    # a slot that already had a draft keeps it when this (re)generation fails
    had_draft = bool(slot.item_id) and slot.status in ("drafted", "generating") and workspace.get_item(slot.item_id) is not None
    workspace.claim_slot(slot.id, run_id, force=force)  # atomic: a double click cannot start two runs
    lease = None
    try:
        lease = ensure_run(workspace, run_id, brief, kind="slot", options={"slot_id": slot.id}, mode=backend.name,
                           model=backend.model, profile=context.profile)
        result = run_pipeline(brief, backend, bus, settings, runner=runner, out_dir=settings.out_dir, mode_note=note,
                              workspace=workspace, context=context)
    except BaseException:
        # a failed regeneration keeps the draft the slot already had; otherwise the slot can be retried. Only while
        # the slot is still this run's: when another process took the run over (recovered it, or resumed it with
        # force) or another run claimed the slot meanwhile, the slot is theirs now and stays as they left it.
        token = lease.token if lease is not None else None
        try:
            if had_draft:
                workspace.release_slot(slot.id, run_id, status="drafted", item_id=slot.item_id,
                                       set_run_id=slot.run_id or run_id, owner_token=token)
            else:
                workspace.release_slot(slot.id, run_id, status="planned", owner_token=token)
        except Exception:  # noqa: BLE001
            log.exception("슬롯 상태를 되돌리지 못했어요 (%s)", slot.id)
        raise
    finally:
        if lease is not None:
            lease.release()
    linked = link_slot_after_run(workspace, run_id, owner_token=lease.token)
    detail = _require_item(workspace, pipeline_item_id(run_id, slot.channel))
    latest = detail.versions[-1] if detail.versions else None
    return JobResult(run_id=run_id, kind="slot", item=detail.item, version=latest, review=latest.review if latest else None,
                     format_checks=list(latest.review.format_checks) if latest and latest.review else [],
                     slot=linked, result=result)
