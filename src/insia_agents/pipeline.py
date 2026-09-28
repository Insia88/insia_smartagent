"""The pipeline: plan → research → per-channel draft/review/revise loop.

``run_pipeline`` drives agent steps (generators, see ``agents.common``) with a
runner:

- ``SimRunner`` (mock): a tiny discrete-event scheduler over a ``SimClock``.
  Channels interleave in virtual time, deterministically (ties break by
  channel order), and ``t`` stays monotonic.
- ``ThreadRunner`` (live): channels run concurrently in daemon worker threads;
  real time passes during API calls. Ctrl+C (or ``cancel()``) stops the
  channels at their next step.

Production behaviour (all optional, the CLI/server pass them):

- ``workspace``: the run is persisted as it happens — run row and status,
  every event, the plan and research pack as soon as they exist, every draft
  round as a content-item version and every review attached to it, usage
  records. A crash loses at most the step in flight; ``resume_run`` picks up
  from there without redoing finished work.
- ``context`` (``RunContext``): company profile, user documents, reference
  date; handed to the backend (``backend.context``) and to the reviewer's
  deterministic brand checks.
- Budget cap (``Settings.max_cost_usd``): the cumulative cost of the run
  (from the backend's ``on_usage`` records) is checked before every backend
  call; once exceeded no new call starts, finished channels are kept and the
  run ends with ``run.failed`` ("예산 상한 $X를 넘어 실행을 멈췄어요").
  Calls already in flight in other threads still finish, so the final cost
  can end slightly above the cap.
- Retries: a retryable ``BackendError`` (429/5xx/timeout) is retried after
  2 s and then 6 s before the step fails (``agents.common.RETRY_DELAYS``).
"""

from __future__ import annotations

import heapq
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Generator, Hashable, Sequence, TypeVar

from .agents import orchestrator, researcher, reviewer
from .agents.common import AgentContext, Step, channel_label
from .backends import create_backend
from .backends.base import Backend, BackendError, RunContext
from .config import Settings, resolve_mode
from .events import EventBus, RealClock, SimClock
from .models import (Brief, CalendarSlot, ChannelId, ChannelResult, Draft, Plan, Profile, ResearchPack, ResearchQuestion,
                     Review, RunResult, UsageRecord)
from .storage import run_dir, save_run

if TYPE_CHECKING:
    from .db import RunLease, Workspace

try:  # priced by the intelligence layer; records normally arrive priced already
    from .costs import cost_of as _cost_of
except ImportError:  # pragma: no cover - costs.py ships with the package
    _cost_of = None

log = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)

RESUMABLE_KINDS = ("pipeline", "slot")


class PipelineError(RuntimeError):
    pass


class RunCancelled(PipelineError):
    """The run was stopped (Ctrl+C or ``ThreadRunner.cancel()``)."""

    def __init__(self, message: str = "실행을 중단했어요") -> None:
        super().__init__(message)


class BudgetExceeded(PipelineError):
    """The run's cost passed ``Settings.max_cost_usd``.

    ``result`` holds the channels that finished before the stop (``None`` if
    none did); with a workspace they are saved as content items anyway.
    """

    def __init__(self, message: str, *, cap: float = 0.0, spent: float = 0.0, result: RunResult | None = None,
                 completed: Sequence[str] = (), stopped: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.cap = cap
        self.spent = spent
        self.result = result
        self.completed = list(completed)
        self.stopped = list(stopped)


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def budget_message(cap: float, spent: float, completed: Sequence[str] | None = None) -> str:
    """Korean stop message; ``completed`` = channels that finished before the stop (None = unknown yet)."""
    text = f"예산 상한 ${cap:,.2f}를 넘어 실행을 멈췄어요 (지금까지 ${spent:,.2f} 사용)."
    if completed is not None:
        if completed:
            names = ", ".join(channel_label(ch) for ch in completed)
            text += f" 끝난 채널({names})은 저장해 뒀어요."
        else:
            text += " 끝난 채널은 없어요."
    return text + " 상한을 올린 뒤 이어서 실행하면 남은 작업만 마저 해요."


def josa(word: str, pair: str) -> str:
    """Korean particle after ``word``: ``josa("링크드인", "은/는")`` → ``"링크드인은"``."""
    with_batchim, without = pair.split("/")
    last = word.strip()[-1:] if word.strip() else ""
    has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return word + (with_batchim if has_batchim else without)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _drive(gen: Generator[float, None, T], on_yield: Callable[[float], None]) -> T:
    try:
        delay = next(gen)
        while True:
            on_yield(float(delay or 0.0))
            delay = next(gen)
    except StopIteration as stop:
        return stop.value


class SimRunner:
    """Deterministic virtual-time scheduler (mock mode)."""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock

    def run(self, gen: Generator[float, None, T]) -> T:
        return _drive(gen, self.clock.advance)

    def wait(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def run_parallel(self, gens: dict[K, Generator[float, None, Any]]) -> dict[K, Any]:
        results: dict[K, Any] = {}
        keys = list(gens)
        heap: list[tuple[float, int]] = [(self.clock.now(), i) for i in range(len(keys))]
        heapq.heapify(heap)
        while heap:
            wake, index = heapq.heappop(heap)
            self.clock.advance_to(wake)
            key = keys[index]
            try:
                delay = next(gens[key])
            except StopIteration as stop:
                results[key] = stop.value
                continue
            except Exception as exc:  # the channel failed; others continue
                results[key] = exc
                continue
            heapq.heappush(heap, (self.clock.now() + max(0.0, float(delay or 0.0)), index))
        return results


class ThreadRunner:
    """Runs steps in real time; parallel steps in worker threads (live mode).

    Agent steps yield after every backend call, and each yield checks a cancel
    flag. On Ctrl+C the flag is set and ``run_parallel`` re-raises at once
    instead of waiting for every channel to finish its draft/review/revise
    loop: a worker stops once its in-flight API call returns (or at its next
    event, since ``run.failed`` closes the bus). Workers are daemon threads,
    so they never hold the process open at exit (``insia serve`` included).
    """

    def __init__(self, max_workers: int = 4) -> None:
        self.max_workers = max(1, max_workers)
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, seconds: float) -> None:
        """Sleep that ends early when the run is cancelled."""
        self._cancel.wait(max(0.0, seconds))

    def _checkpoint(self, _delay: float) -> None:
        if self._cancel.is_set():
            raise RunCancelled()

    def run(self, gen: Generator[float, None, T]) -> T:
        return _drive(gen, self._checkpoint)

    def run_parallel(self, gens: dict[K, Generator[float, None, Any]]) -> dict[K, Any]:
        if not gens:
            return {}
        pending = list(gens)
        results: dict[K, Any] = {}
        lock = threading.Lock()

        def worker() -> None:
            while not self._cancel.is_set():
                with lock:
                    if not pending:
                        return
                    key = pending.pop(0)
                try:
                    value: Any = self.run(gens[key])
                except BaseException as exc:  # the channel failed; others continue
                    value = exc
                with lock:
                    results[key] = value

        threads = [threading.Thread(target=worker, name=f"insia-channel_{i}", daemon=True)
                   for i in range(min(self.max_workers, len(gens)))]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                while thread.is_alive():  # a timed join keeps Ctrl+C responsive on every platform
                    thread.join(0.2)
        except BaseException:  # Ctrl+C: stop the channels at their next step, do not wait for them
            self.cancel()
            raise
        if self._cancel.is_set():
            raise RunCancelled()
        return {key: results[key] for key in gens if key in results}


def _default_runner(bus: EventBus, settings: Settings) -> SimRunner | ThreadRunner:
    return SimRunner(bus.clock) if isinstance(bus.clock, SimClock) else ThreadRunner(settings.max_workers)


# ---------------------------------------------------------------------------
# Cost meter, run context, persistence helpers
# ---------------------------------------------------------------------------


def record_cost(record: UsageRecord, *, home: str | Path | None = None) -> float:
    """USD cost of a usage record: its ``cost_usd`` when the backend priced it,
    otherwise priced with ``costs.cost_of`` (0 when the model price is unknown).
    ``home`` is the workspace whose ``prices.json`` applies."""
    try:
        cost = float(record.cost_usd or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost != cost or cost in (float("inf"), float("-inf")) or cost < 0:
        cost = 0.0
    if cost > 0 or _cost_of is None:
        return cost
    if not (record.input_tokens or record.output_tokens or record.cache_read_tokens or record.cache_write_tokens
            or record.web_search_requests):
        return 0.0
    try:
        return max(0.0, float(_cost_of(record, home=home)))
    except Exception:  # noqa: BLE001 - a pricing problem must never break a run
        log.warning("사용량 비용을 계산하지 못했어요 (model=%r)", record.model, exc_info=True)
        return 0.0


class UsageMeter:
    """``backend.on_usage`` for one run: records usage in the workspace and sums the cost.

    Thread-safe (live channels report from worker threads). ``check()`` raises
    ``BudgetExceeded`` once the cumulative cost is above the cap (0 = no cap).
    Backends that never report usage simply leave the cost at 0.
    """

    def __init__(self, run_id: str, *, workspace: "Workspace | None" = None, initial: float = 0.0, cap: float = 0.0,
                 forward: Callable[[UsageRecord], None] | None = None) -> None:
        self.run_id = run_id
        self.workspace = workspace
        self.cap = max(0.0, float(cap or 0.0))
        self.forward = forward
        self._spent = max(0.0, float(initial or 0.0))
        self._calls = 0
        self._lock = threading.Lock()
        self._record_failed = False

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def __call__(self, record: UsageRecord) -> None:
        if not isinstance(record, UsageRecord):
            try:
                record = UsageRecord.model_validate(record)
            except Exception:  # noqa: BLE001
                log.warning("알 수 없는 사용량 기록을 건너뛰어요: %r", record)
                return
        cost = record_cost(record, home=getattr(self.workspace, "home", None))
        record = record.model_copy(update={"run_id": record.run_id or self.run_id, "cost_usd": cost,
                                           "created_at": record.created_at or _iso_now()})
        with self._lock:
            self._spent += cost
            self._calls += 1
        if self.workspace is not None:
            try:
                self.workspace.record_usage(record)
            except Exception:  # noqa: BLE001 - the cost still counts toward the cap
                if not self._record_failed:
                    self._record_failed = True
                    log.exception("사용량을 워크스페이스에 기록하지 못했어요 (run %s)", self.run_id)
        if self.forward is not None:
            try:
                self.forward(record)
            except Exception:  # noqa: BLE001
                log.warning("on_usage 콜백 오류", exc_info=True)

    def exceeded(self) -> bool:
        return self.cap > 0 and self.spent > self.cap

    def check(self) -> None:
        if self.exceeded():
            spent = self.spent
            raise BudgetExceeded(budget_message(self.cap, spent), cap=self.cap, spent=spent)


_MISSING = object()


def install_backend_hooks(backend: Any, context: RunContext, meter: UsageMeter) -> Callable[[], None]:
    """Set ``backend.context`` and ``backend.on_usage``; returns a function that restores ``on_usage``."""
    previous = getattr(backend, "on_usage", _MISSING)
    if callable(previous) and meter.forward is None and not isinstance(previous, UsageMeter):
        meter.forward = previous
    for name, value in (("context", context), ("on_usage", meter)):
        try:
            setattr(backend, name, value)
        except Exception:  # noqa: BLE001 - e.g. a backend with __slots__; it just won't report
            log.debug("could not set backend.%s", name, exc_info=True)

    def restore() -> None:
        try:
            backend.on_usage = None if previous is _MISSING else previous
        except Exception:  # noqa: BLE001
            pass

    return restore


def build_context(workspace: "Workspace | None", settings: Settings, *, docs: str | Sequence[str] | None = None,
                  instructions: str = "") -> RunContext:
    """The run context from the workspace: the saved profile (unless
    ``settings.use_profile`` is off or it is empty) and the chosen documents
    (``docs``: ``None``/``"none"`` = none, ``"all"``, ``"u1,u3"`` or a list of ids)."""
    from .db import WorkspaceError, profile_is_empty

    profile: Profile | None = None
    documents = []
    if workspace is not None:
        if settings.use_profile:
            saved = workspace.get_profile()
            profile = None if profile_is_empty(saved) else saved
        wanted: list[str] | str = []
        if isinstance(docs, str):
            text = docs.strip().lower()
            wanted = "all" if text == "all" else ([] if text in ("", "none") else [d.strip() for d in docs.split(",") if d.strip()])
        elif docs:
            wanted = [str(d).strip() for d in docs if str(d).strip()]
        if wanted == "all":
            documents = workspace.list_documents()
        else:
            for doc_id in wanted:
                doc = workspace.get_document(doc_id)
                if doc is None:
                    raise WorkspaceError(f"자료 {doc_id}를 찾을 수 없어요 ('insia docs list'로 id를 확인해 주세요)")
                documents.append(doc)
    return RunContext(profile=profile, documents=documents, today=settings.today, instructions=instructions)


def ensure_run(workspace: "Workspace", run_id: str, brief: Brief, *, kind: str, options: dict[str, Any] | None = None,
               mode: str = "", model: str = "", profile: Profile | None = None, parent_item_id: str = "") -> "RunLease":
    """Create the run row, or mark an existing one (server-created, resumed) as running again.

    Returns this process's lease on the run (``Workspace.acquire_run``): the
    heartbeat that tells other processes the run is alive. The caller releases
    it when the run ends (``lease.release()``; releasing twice is fine).
    """
    existing = workspace.get_run(run_id)
    if existing is None:
        workspace.create_run(run_id, brief, kind=kind, options=options or {}, mode=mode, model=model, profile=profile,
                             parent_item_id=parent_item_id)
        return workspace.acquire_run(run_id)
    fields: dict[str, Any] = {"status": "running", "options": {**(existing.get("options") or {}), **(options or {})}}
    if mode:
        fields["mode"] = mode
    if model:
        fields["model"] = model
    if profile is not None and not existing.get("profile"):
        fields["profile"] = profile
    workspace.update_run(run_id, **fields)
    return workspace.acquire_run(run_id)


def continue_numbering(workspace: "Workspace", bus: EventBus) -> None:
    """Make ``bus`` continue after the run's stored events (resume, retry of a job id)."""
    last = workspace.last_event(bus.run_id)
    if last and bus.last_seq() < int(last.get("seq", 0)):
        bus.continue_from(int(last["seq"]), float(last.get("t") or 0.0))


def event_sink(workspace: "Workspace", run_id: str, lease: "RunLease | None" = None) -> Callable[[dict[str, Any]], None]:
    """Bus listener that appends every event to the workspace (logs, never raises).

    With ``lease`` (this process's claim on the run), nothing more is stored once another process took the run over.
    """
    from .db import RunTakenOverError

    failed = [False]

    def sink(event: dict[str, Any]) -> None:
        try:
            if lease is not None and lease.lost:
                raise RunTakenOverError(f"다른 곳에서 실행 {run_id}를 넘겨받았어요. 이 프로세스는 더 기록하지 않고 멈춰요.")
            workspace.append_event(run_id, event)
        except Exception as exc:  # noqa: BLE001 - the bus ignores listener errors; make them visible in the log
            if not failed[0]:
                failed[0] = True
                if isinstance(exc, RunTakenOverError):
                    log.warning("%s (run %s)", exc, run_id)
                else:
                    log.exception("이벤트를 워크스페이스에 저장하지 못했어요 (run %s)", run_id)

    return sink


TAKEN_OVER_MESSAGE = "다른 곳에서 이 실행을 넘겨받아서 여기서는 멈췄어요"


def make_checkpoint(runner: Any, meter: UsageMeter, lost: Callable[[], bool] | None = None) -> Callable[[], None]:
    """Before every backend call: stop when the run was cancelled, taken over by another process (``lost``),
    or went over its budget."""

    def checkpoint() -> None:
        if getattr(runner, "cancelled", False):
            raise RunCancelled()
        if lost is not None and lost():
            cancel = getattr(runner, "cancel", None)
            if callable(cancel):  # stop the other channels too
                cancel()
            raise RunCancelled(TAKEN_OVER_MESSAGE)
        meter.check()

    return checkpoint


def make_wait(runner: Any, bus: EventBus) -> Callable[[float], None]:
    if hasattr(runner, "wait"):
        return runner.wait
    if getattr(bus.clock, "simulated", False):
        return bus.clock.advance
    return time.sleep


def failure_status(exc: BaseException) -> tuple[str, str]:
    """(run status, Korean message) for an exception that ended a run."""
    if isinstance(exc, KeyboardInterrupt):
        return "cancelled", "사용자가 실행을 중단했어요"
    if isinstance(exc, RunCancelled):
        return "cancelled", str(exc)
    from .db import WorkspaceError

    if isinstance(exc, (BackendError, PipelineError, WorkspaceError)):
        return "failed", str(exc)
    return "failed", f"{type(exc).__name__}: {exc}"


class _Recorder:
    """Persists pipeline progress to the workspace as it happens (no-op without one).

    Thread-safe: live channels record from worker threads.
    """

    def __init__(self, workspace: "Workspace | None", run_id: str, brief: Brief, *, progress: dict[str, Any] | None = None,
                 version_ids: dict[tuple[str, int], str] | None = None) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.brief = brief
        self.progress: dict[str, Any] = {"channels": {}, "followups": {}, "questions": [], **(progress or {})}
        self.version_ids: dict[tuple[str, int], str] = dict(version_ids or {})
        self.lease: "RunLease | None" = None  # set once the run is claimed: no more writes after a takeover
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.workspace is not None

    def _guard(self) -> None:
        if self.lease is not None and self.lease.lost:
            from .db import RunTakenOverError

            raise RunTakenOverError(f"다른 곳에서 실행 {self.run_id}를 넘겨받았어요. 이 프로세스는 더 기록하지 않고 멈춰요.")

    def item_id(self, channel: str) -> str:
        from .db import pipeline_item_id

        return pipeline_item_id(self.run_id, channel)

    def item_ids(self) -> dict[str, str]:
        with self._lock:
            channels = [ch for ch, status in self.progress["channels"].items() if status == "completed"]
        return {ch: self.item_id(ch) for ch in channels}

    def _save_progress(self) -> None:
        assert self.workspace is not None
        self._guard()
        self.workspace.update_run(self.run_id, progress=self.progress)

    def plan(self, plan: Plan) -> None:
        if self.workspace is not None:
            self._guard()
            self.workspace.update_run(self.run_id, plan=plan)

    def research(self, store: researcher.ResearchStore) -> None:
        if self.workspace is None:
            return
        with self._lock:  # snapshot inside the lock: a later writer always has the fuller pack
            with store.lock:
                pack = store.pack.model_copy(deep=True)
                questions = [q.model_dump(mode="json") for q in store.questions]
            self.progress["questions"] = questions
            self._guard()
            self.workspace.update_run(self.run_id, research=pack, progress=self.progress)

    def draft(self, draft: Draft) -> None:
        if self.workspace is None:
            return
        item_id = self.item_id(draft.channel)
        with self._lock:
            self._guard()
            self.workspace.ensure_item(item_id, draft.channel, draft.title, run_id=self.run_id, brief=self.brief)
            version = self.workspace.add_version(item_id, draft, source="agent", run_id=self.run_id)
            self.version_ids[(draft.channel, draft.round)] = version.id

    def review(self, draft: Draft, review: Review) -> None:
        if self.workspace is None:
            return
        with self._lock:
            version_id = self.version_ids.get((draft.channel, draft.round))
            if version_id is None:  # drafted before recording started (should not happen): store it now
                self.draft(draft)
                version_id = self.version_ids[(draft.channel, draft.round)]
            self._guard()
            self.workspace.attach_review(version_id, review, run_id=self.run_id)

    def followup_done(self, channel: str, round: int, store: researcher.ResearchStore) -> None:
        if self.workspace is None:
            return
        with self._lock:
            rounds = self.progress["followups"].setdefault(channel, [])
            if round not in rounds:
                rounds.append(round)
            self.research(store)

    def channel_completed(self, result: ChannelResult) -> None:
        if self.workspace is None:
            return
        with self._lock:
            self._guard()
            self.workspace.upsert_item_from_result(self.run_id, result, self.brief)
            self.progress["channels"][result.channel] = "completed"
            self._save_progress()

    def channel_ended(self, channel: str, status: str) -> None:
        """``failed`` or ``stopped`` (budget); both are retried by ``resume_run``."""
        if self.workspace is None:
            return
        with self._lock:
            self.progress["channels"][channel] = status
            try:
                self._save_progress()
            except Exception:  # noqa: BLE001 - reporting the channel's own error matters more
                log.exception("채널 상태를 저장하지 못했어요 (run %s)", self.run_id)


_NULL_RECORDER = _Recorder(None, "", Brief(topic="-"))


# ---------------------------------------------------------------------------
# Channel loop
# ---------------------------------------------------------------------------


def _best_index(reviews: list[Review]) -> int:
    return max(range(len(reviews)), key=lambda i: (reviews[i].passed, reviews[i].score, i))


@dataclass
class ChannelState:
    """Where a channel stood when its run stopped (for ``resume_run``)."""

    drafts: list[Draft] = field(default_factory=list)  # reviewed rounds, in order
    reviews: list[Review] = field(default_factory=list)
    pending: Draft | None = None  # the last draft, written but not reviewed yet
    followup_rounds: set[int] = field(default_factory=set)  # rounds whose follow-up research already ran
    completed: ChannelResult | None = None


@dataclass
class ResumeState:
    plan: Plan | None
    research: ResearchPack | None
    questions: list[ResearchQuestion]
    channels: dict[str, ChannelState]
    progress: dict[str, Any]
    version_ids: dict[tuple[str, int], str]


def channel_flow(ctx: AgentContext, plan: Plan, store: researcher.ResearchStore, channel: ChannelId, *,
                 state: ChannelState | None = None, recorder: _Recorder | None = None) -> Step[ChannelResult]:
    recorder = recorder or _NULL_RECORDER
    max_rounds = ctx.settings.max_rounds
    drafts: list[Draft] = list(state.drafts) if state else []
    reviews: list[Review] = list(state.reviews) if state else []
    followups_done: set[int] = set(state.followup_rounds) if state else set()
    current: Draft | None = state.pending if state else None
    label = channel_label(channel)
    if state and (drafts or current is not None):
        where = f"R{current.round} 검수" if current is not None else f"R{drafts[-1].round} 검수 결과"
        ctx.log(f"{label}: 저장된 {where}부터 이어서 진행해요", "info", "orchestrator")
    if current is None and not drafts:
        current = yield from orchestrator.draft(ctx, plan, store.snapshot(), channel)
        recorder.draft(current)
    while True:
        if current is not None:
            try:
                result = yield from reviewer.review(ctx, store.snapshot(), current)
            except BackendError as exc:
                if not reviews:
                    raise
                ctx.log(f"{label} R{current.round} 검수 실패: {exc} — 이전 버전을 최종본으로 써요", "warn", "reviewer")
                break
            recorder.review(current, result)
            drafts.append(current)
            reviews.append(result)
        else:  # resumed right after a review
            current, result = drafts[-1], reviews[-1]
        if result.passed or current.round >= max_rounds:
            yield from reviewer.conclude(ctx, result)
            break
        yield from reviewer.request_revision(ctx, result)
        if result.needs_research and current.round not in followups_done:
            try:
                yield from researcher.followup(ctx, store, result.needs_research, channel)
                followups_done.add(current.round)
                recorder.followup_done(channel, current.round, store)
            except BackendError as exc:  # revise with the research we already have
                ctx.log(f"{label} 추가 조사 실패: {exc} — 지금 있는 근거로 수정을 이어가요", "warn", "researcher")
                ctx.status("researcher", "error", f"{label} 추가 조사를 마치지 못했어요")
                ctx.status("researcher", "idle", "추가 조사 요청이 오면 다시 찾아볼게요")
        try:
            current = yield from orchestrator.revise(ctx, plan, store.snapshot(), current, result)
        except BackendError as exc:
            ctx.log(f"{label} 수정 중 오류: {exc} — 지금까지 가장 좋은 버전을 최종본으로 써요", "warn", "orchestrator")
            break
        recorder.draft(current)

    best = _best_index(reviews)
    outcome = ChannelResult(channel=channel, final=drafts[best], drafts=drafts, reviews=reviews,
                            passed=reviews[best].passed, rounds=len(drafts) - 1)
    recorder.channel_completed(outcome)
    orchestrator.complete_channel(ctx, outcome, reviews[best].score)
    return outcome


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _channels(brief: Brief) -> list[ChannelId]:
    channels = list(dict.fromkeys(brief.channels))
    if not channels:
        raise PipelineError("채널을 하나 이상 골라 주세요 (bizplan, naver_blog, linkedin, instagram)")
    return channels


def _replay_research(ctx: AgentContext, store: researcher.ResearchStore) -> None:
    """Re-emit a stored research pack so a resumed stream shows the full research board."""
    pack = store.snapshot()
    for source in pack.sources:
        ctx.bus.emit("research.source", researcher.AGENT, {"source": source})
    for finding in pack.findings:
        ctx.bus.emit("research.finding", researcher.AGENT, {"finding": finding})
    ctx.bus.emit("research.completed", researcher.AGENT, {"findings": len(pack.findings), "sources": len(pack.sources),
                                                          "gaps": list(pack.gaps), "followup": False, "resumed": True})
    ctx.status(researcher.AGENT, "idle", "추가 조사 요청이 오면 다시 찾아볼게요")


def run_pipeline(brief: Brief, backend: Backend, bus: EventBus, settings: Settings, *,
                 runner: SimRunner | ThreadRunner | None = None, out_dir: str | Path | None = None,
                 mode_note: str | None = None, workspace: "Workspace | None" = None,
                 context: RunContext | None = None, resume_state: ResumeState | None = None) -> RunResult:
    """Run the whole pipeline and return the result.

    Emits ``run.failed`` and re-raises when something goes wrong before any
    channel could finish (or the budget cap stops the run: ``BudgetExceeded``
    carries the channels that did finish). See the module docstring for
    ``workspace`` / ``context``; ``resume_state`` is built by ``resume_run``.
    """
    simulated = bool(getattr(bus.clock, "simulated", False))
    if runner is None:
        runner = _default_runner(bus, settings)
    started_at = _iso_now()
    run_id = bus.run_id
    target_dir: Path | None = None
    restore_hooks: Callable[[], None] = lambda: None
    sink: Callable[[dict[str, Any]], None] | None = None
    lease: "RunLease | None" = None

    def lost() -> bool:  # another process took the run over (Workspace.recover_stale / a forced resume)
        return lease is not None and not lease.verify()

    try:
        if context is None:
            context = build_context(workspace, settings) if workspace is not None else RunContext(None, [], settings.today)
        meter = UsageMeter(run_id, workspace=workspace, cap=settings.max_cost_usd,
                           initial=workspace.run_cost(run_id) if workspace is not None else 0.0)
        ctx = AgentContext(bus=bus, backend=backend, settings=settings, brief=brief, simulated=simulated, context=context,
                           checkpoint=make_checkpoint(runner, meter, lost), wait=make_wait(runner, bus))
        recorder = _Recorder(workspace, run_id, brief,
                             progress=resume_state.progress if resume_state else None,
                             version_ids=resume_state.version_ids if resume_state else None)
        if hasattr(backend, "on_notice"):
            backend.on_notice = lambda agent, level, message: bus.emit("log", agent, {"level": level, "message": message})  # type: ignore[attr-defined]
        restore_hooks = install_backend_hooks(backend, context, meter)
        channels = _channels(brief)
        brief = brief.model_copy(update={"channels": channels})
        ctx.brief = brief
        recorder.brief = brief
        backend_note = backend.prepare(brief) if hasattr(backend, "prepare") else None  # type: ignore[attr-defined]
        if workspace is not None:
            lease = ensure_run(workspace, run_id, brief, kind="pipeline", mode=backend.name, model=backend.model,
                               profile=context.profile, options={
                           "max_rounds": settings.max_rounds, "pass_score": settings.pass_score,
                           "max_cost_usd": settings.max_cost_usd, "doc_ids": [d.id for d in context.documents],
                           "use_profile": context.profile is not None,
                       })
            recorder.lease = lease
            continue_numbering(workspace, bus)
            sink = event_sink(workspace, run_id, lease)
            bus.add_listener(sink)
        if out_dir is not None:
            target_dir = run_dir(out_dir, run_id)
            bus.attach_sink(target_dir / "events.jsonl", append=resume_state is not None)

        started: dict[str, Any] = {
            "brief": brief,
            "channels": channels,
            "mode": backend.name,
            "model": backend.model,
            "max_rounds": settings.max_rounds,
            "pass_score": settings.pass_score,
        }
        if resume_state is not None:
            started["resumed"] = True
        if settings.max_cost_usd:
            started["budget_usd"] = settings.max_cost_usd
        bus.emit("run.started", "system", started)
        if mode_note:
            ctx.log(mode_note)
        if backend_note:
            ctx.log(backend_note)
        if resume_state is not None:
            ctx.log("중단된 실행을 이어서 진행해요. 끝난 단계는 다시 하지 않아요.")
        if context.profile is not None:
            ctx.log("회사 프로필을 참고해 작성·검수해요")
        if context.documents:
            ctx.log(f"사용자 자료 {len(context.documents)}개를 리서치에 넣어요")
        ctx.status("researcher", "waiting", "총괄의 리서치 질문을 기다려요")
        ctx.status("reviewer", "waiting", "초안이 오면 바로 검수할게요")

        # 1. plan
        if resume_state is not None and resume_state.plan is not None:
            plan = resume_state.plan
            orchestrator.emit_plan(ctx, plan)
        else:
            plan = runner.run(orchestrator.plan(ctx))
            recorder.plan(plan)

        # 2. research
        store = researcher.ResearchStore()
        if resume_state is not None and resume_state.research is not None:
            store.pack = resume_state.research.model_copy(deep=True)
            store.questions = list(resume_state.questions) or list(plan.questions)
            _replay_research(ctx, store)
        else:
            runner.run(orchestrator.delegate_research(ctx, plan))
            runner.run(researcher.run(ctx, store, list(plan.questions)))
            recorder.research(store)

        # 3. channels (finished ones are kept on resume)
        done: dict[str, ChannelResult] = {}
        todo: list[ChannelId] = []
        for ch in channels:
            state = resume_state.channels.get(ch) if resume_state is not None else None
            if state is not None and state.completed is not None:
                done[ch] = state.completed
            else:
                todo.append(ch)
        for ch, finished in done.items():
            ctx.log(f"{josa(channel_label(ch), '은/는')} 이미 끝나서 건너뛰어요", "info", "orchestrator")
            orchestrator.complete_channel(ctx, finished, finished.reviews[_best_index(finished.reviews)].score)
        outcomes: dict[str, Any] = dict(done)
        outcomes.update(runner.run_parallel({
            ch: channel_flow(ctx, plan, store, ch, recorder=recorder,
                             state=resume_state.channels.get(ch) if resume_state is not None else None)
            for ch in todo
        }))

        results: list[ChannelResult] = []
        errors: dict[str, str] = {}
        stopped: list[str] = []
        budget_stop: BudgetExceeded | None = None
        for ch in channels:
            value = outcomes.get(ch)
            if isinstance(value, ChannelResult):
                results.append(value)
            elif isinstance(value, BudgetExceeded):
                budget_stop = budget_stop or value
                stopped.append(ch)
                recorder.channel_ended(ch, "stopped")
            else:
                message = failure_status(value)[1] if isinstance(value, BaseException) else "알 수 없는 오류"
                errors[ch] = message
                recorder.channel_ended(ch, "failed")
                ctx.status("orchestrator", "error", f"{channel_label(ch)} 작업이 실패했어요")
                ctx.log(f"{channel_label(ch)} 실패: {message}", "error", "orchestrator")

        research = store.snapshot()
        if budget_stop is not None:
            partial = None
            if results:
                partial = RunResult(run_id=run_id, mode=backend.name, model=backend.model, brief=brief,  # type: ignore[arg-type]
                                    plan=plan, research=research, results=results, started_at=started_at,
                                    finished_at=_iso_now())
                if target_dir is not None:
                    save_run(partial, target_dir)
            finished = [r.channel for r in results]
            raise BudgetExceeded(budget_message(meter.cap, meter.spent, finished), cap=meter.cap, spent=meter.spent,
                                 result=partial, completed=finished, stopped=stopped)
        if not results:
            first = next((v for v in outcomes.values() if isinstance(v, BaseException)), None)
            if first is not None:
                raise first
            raise PipelineError("모든 채널이 실패했어요")

        result = RunResult(
            run_id=run_id, mode=backend.name, model=backend.model, brief=brief,  # type: ignore[arg-type]
            plan=plan, research=research, results=results, started_at=started_at, finished_at=_iso_now(),
        )
        if target_dir is not None:
            save_run(result, target_dir)
        for agent, message in (("orchestrator", "모든 채널 작업을 마쳤어요"), ("researcher", "조사를 마쳤어요"),
                               ("reviewer", "검수를 마쳤어요")):
            ctx.status(agent, "done", message)
        completed: dict[str, Any] = {
            "duration_s": round(bus.clock.now(), 1),
            "scores": {r.channel: r.reviews[_best_index(r.reviews)].score for r in results},
            "passed": {r.channel: r.passed for r in results},
            "output_dir": str(target_dir) if target_dir is not None else None,
        }
        if errors:
            completed["errors"] = errors
        if workspace is not None:
            completed["items"] = recorder.item_ids()
        if meter.spent:
            completed["cost_usd"] = round(meter.spent, 6)
        if lost():  # taken over at the very end: the new owner finishes and records the run
            raise RunCancelled(TAKEN_OVER_MESSAGE)
        bus.emit("run.completed", "system", completed)
        if workspace is not None:
            summary = " · ".join(f"{channel_label(ch)} 실패: {msg}" for ch, msg in errors.items())
            workspace.update_run(run_id, status="completed", error=summary, cost_usd=workspace.run_cost(run_id))
        return result
    except BaseException as exc:
        status, message = failure_status(exc)
        if not bus.closed:  # before run.started (bad input) this is the stream's only event
            data: dict[str, Any] = {"error": message}
            if isinstance(exc, BudgetExceeded):
                data.update({"budget_exceeded": True, "budget_usd": exc.cap, "cost_usd": round(exc.spent, 6),
                             "completed_channels": exc.completed, "stopped_channels": exc.stopped})
            try:
                bus.emit("run.failed", "system", data)
            except RuntimeError:
                pass
        if workspace is not None and lease is not None and lease.lost:
            log.warning("다른 곳에서 실행 %s를 넘겨받아서, 이 프로세스는 실행 상태를 기록하지 않아요", run_id)
        elif workspace is not None:
            from .db import RunTakenOverError

            try:
                workspace.update_run(run_id, status=status, error=message, cost_usd=workspace.run_cost(run_id))
            except RunTakenOverError as taken:  # the new owner records how the run goes from here
                log.warning("%s", taken)
            except Exception:  # noqa: BLE001 - never mask the original error
                log.exception("실행 상태를 저장하지 못했어요 (run %s)", run_id)
        raise
    finally:
        bus.detach_sink()
        if sink is not None:
            bus.remove_listener(sink)
        restore_hooks()
        if lease is not None:
            lease.release()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

_QID = re.compile(r"^q(\d+)$", re.IGNORECASE)


def load_resume_state(workspace: "Workspace", run_id: str) -> ResumeState:
    """Rebuild where a run stopped from what the workspace stored."""
    from .db import NotFoundError

    run = workspace.get_run(run_id)
    if run is None:
        raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요")
    brief = Brief.model_validate(run["brief"])
    plan = Plan.model_validate(run["plan"]) if run.get("plan") else None
    research = ResearchPack.model_validate(run["research"]) if run.get("research") else None
    progress = dict(run.get("progress") or {})
    progress.setdefault("channels", {})
    progress.setdefault("followups", {})
    progress.setdefault("questions", [])

    questions: list[ResearchQuestion] = []
    for raw in progress.get("questions") or []:
        try:
            questions.append(ResearchQuestion.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
    if not questions and plan is not None:
        questions = list(plan.questions)
    known = {q.id for q in questions}
    for finding in (research.findings if research else []):  # ids used by findings must never be handed out again
        if finding.question_id not in known and _QID.match(finding.question_id or ""):
            known.add(finding.question_id)
            questions.append(ResearchQuestion(id=finding.question_id, question="(이전 실행의 추가 조사)",
                                              why="이전 실행에서 추가로 조사한 질문", channels=list(brief.channels),
                                              priority="medium"))

    channels: dict[str, ChannelState] = {}
    version_ids: dict[tuple[str, int], str] = {}
    for ch in brief.channels:
        by_round = {}
        for version in workspace.list_run_versions(run_id, ch):
            by_round[version.draft.round] = version  # the newest version of a round wins
        ordered = [by_round[r] for r in sorted(by_round)]
        state = ChannelState(followup_rounds=set(int(r) for r in progress["followups"].get(ch, [])))
        for index, version in enumerate(ordered):
            version_ids[(ch, version.draft.round)] = version.id
            if version.review is not None:
                state.drafts.append(version.draft)
                state.reviews.append(version.review)
            elif index == len(ordered) - 1:
                state.pending = version.draft
        if progress["channels"].get(ch) == "completed" and state.reviews:
            best = _best_index(state.reviews)
            state.completed = ChannelResult(channel=ch, final=state.drafts[best], drafts=state.drafts, reviews=state.reviews,
                                            passed=state.reviews[best].passed, rounds=len(state.drafts) - 1)
        channels[ch] = state
    return ResumeState(plan=plan, research=research, questions=questions, channels=channels, progress=progress,
                       version_ids=version_ids)


def link_slot_after_run(workspace: "Workspace", run_id: str, *, owner_token: str | None = None):
    """For a calendar-slot run: link the slot to its item and mark it drafted.

    Only while the slot is still this run's claim (and, with ``owner_token``,
    the run still this process's): a slot another run or process took
    meanwhile is left alone (``Workspace.link_slot``). Returns the slot
    (``None`` when the run has no slot).
    """
    from .db import pipeline_item_id

    run = workspace.get_run(run_id)
    slot_id = (run or {}).get("options", {}).get("slot_id") if run else None
    if not slot_id:
        return None
    slot = workspace.get_slot(slot_id)
    if slot is None:
        return None
    item_id = pipeline_item_id(run_id, slot.channel)
    if workspace.get_item(item_id) is None:
        return workspace.release_slot(slot_id, run_id, status="planned", owner_token=owner_token) or workspace.get_slot(slot_id)
    return workspace.link_slot(slot_id, run_id, item_id, owner_token=owner_token)


_DEFAULT = object()


def _stored_cap(value: Any) -> float:
    try:
        cap = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return cap if cap == cap and 0 < cap < float("inf") else 0.0


def still_running_message(workspace: "Workspace", run_id: str) -> str:
    """Why a ``running`` run cannot be resumed yet, and what to do (CLI wording)."""
    from .db import STALE_AFTER_SECONDS, _parse_ts

    owner = workspace.run_owner(run_id) or {}
    if owner.get("this_host") and owner.get("pid"):
        where = f"이 컴퓨터의 프로세스 {owner['pid']}"
    elif owner.get("host"):
        where = f"다른 컴퓨터·컨테이너({owner['host']})"
    else:
        where = "다른 프로그램"
    beat = _parse_ts(owner.get("heartbeat_at") or "")
    if beat is not None:
        minutes = int(max(0.0, (datetime.now(timezone.utc) - beat).total_seconds()) // 60)
        where += f", 마지막 신호 {minutes}분 전" if minutes else ", 방금 신호가 있었어요"
    stale = int(STALE_AFTER_SECONDS // 60)
    return (f"다른 곳에서 아직 실행 중인 작업이에요 ({where}). 끝날 때까지 기다려 주세요. 신호가 {stale}분 넘게 끊기면 "
            f"이어서 실행할 때 자동으로 정리돼요. 멈춘 게 확실하면 'insia resume {run_id} --force'로 지금 이어서 할 수 있어요.")


def _restore_slot(workspace: "Workspace", slot_id: str, before: "CalendarSlot", run_id: str,
                  owner_token: str | None) -> None:
    """Put a slot back the way it was before a resume took it (the resume failed or stopped) — unless another
    process took the run over or another run claimed the slot meanwhile (``Workspace.release_slot``)."""
    try:
        if before.item_id and workspace.get_item(before.item_id) is not None:
            workspace.release_slot(slot_id, run_id, status="drafted", item_id=before.item_id,
                                   set_run_id=before.run_id or run_id, owner_token=owner_token)
        elif before.status == "skipped":
            workspace.release_slot(slot_id, run_id, status="skipped", set_run_id=before.run_id or run_id,
                                   owner_token=owner_token)
        else:
            workspace.release_slot(slot_id, run_id, status="planned", owner_token=owner_token)
    except Exception:  # noqa: BLE001 - never mask the run's own error
        log.exception("슬롯 상태를 되돌리지 못했어요 (%s)", slot_id)


def resume_run(run_id: str, settings: Settings, workspace: "Workspace", *, backend: Backend | None = None,
               bus: EventBus | None = None, client: object | None = None,
               listener: Callable[[dict[str, Any]], None] | None = None, runner: SimRunner | ThreadRunner | None = None,
               out_dir: Any = _DEFAULT, context: RunContext | None = None, force: bool = False,
               max_cost_usd: float | None = None) -> RunResult:
    """Continue an interrupted / failed / budget-stopped pipeline run.

    Reuses the stored plan and research, skips channels that already
    finished and continues each other channel from its last stored draft or
    review (same run id: its items and event stream continue). The original
    run's round limit, pass score and budget cap are kept; the mode too when
    ``settings.mode`` is ``auto``. ``max_cost_usd`` sets a new cap for this
    run (raise it after a budget stop, or lower it); without it the run keeps
    the cap it was started with. ``settings.max_cost_usd`` applies only when
    the run had no cap, or when the run already spent its cap and the
    settings' cap is higher (resuming a budget-stopped run with a raised cap).

    A run still marked ``running`` is resumed only when its owner process is
    gone (``Workspace.recover_stale``: dead pid, restarted machine, or no
    heartbeat for 10 minutes); ``force=True`` takes it over anyway (only when
    you are sure no process is working on it). A calendar-slot run takes its
    slot back (``generating``) while it runs, so it cannot be generated twice,
    and puts it back the way it was when the resume fails — unless another
    process took the run over meanwhile (then this process stops at its next
    checkpoint and leaves the run and the slot to the new owner). A run this
    same process is running right now is refused.
    """
    run = workspace.get_run(run_id)
    if run is None:
        from .db import NotFoundError

        raise NotFoundError(f"실행 {run_id}를 찾을 수 없어요")
    if run["kind"] not in RESUMABLE_KINDS:
        raise PipelineError(f"'{run['kind']}' 작업은 이어서 실행할 수 없어요. 같은 작업을 다시 시작해 주세요.")
    if workspace.holds_run(run_id):  # this very process is running it (another thread, or a nested call)
        raise PipelineError("이 실행은 이 프로그램에서 아직 진행 중이에요. 끝날 때까지 기다리거나 먼저 중단한 뒤 이어서 실행해 주세요.")
    if run["status"] == "running" and not force:
        if not workspace.recover_stale(run_ids=[run_id], trust_own_pid=True):
            raise PipelineError(still_running_message(workspace, run_id))
        run = workspace.get_run(run_id) or run  # its owner was gone: now 'interrupted'
    state = load_resume_state(workspace, run_id)
    brief = Brief.model_validate(run["brief"])
    if run["status"] == "completed" and all(state.channels.get(ch) and state.channels[ch].completed for ch in brief.channels):
        raise PipelineError("이미 모든 채널을 마친 실행이에요. 결과는 보관함에서 볼 수 있어요.")

    options = run.get("options") or {}
    overrides: dict[str, Any] = {"max_rounds": options.get("max_rounds"), "pass_score": options.get("pass_score")}
    stored_cap = _stored_cap(options.get("max_cost_usd"))
    if max_cost_usd is not None:
        overrides["max_cost_usd"] = float(max_cost_usd)
    elif stored_cap > 0:
        # the run keeps its own cap; a run the cap already stopped can only go on with a higher one, so a higher
        # cap from the caller's settings counts as raising it (e.g. the server's per-request option)
        budget_stopped = workspace.run_cost(run_id) >= stored_cap
        higher = settings.max_cost_usd > stored_cap
        overrides["max_cost_usd"] = settings.max_cost_usd if budget_stopped and higher else stored_cap
    if settings.mode == "auto" and run.get("mode") in ("live", "mock"):
        overrides["mode"] = run["mode"]
    settings = settings.with_options(**overrides)
    if context is None:
        profile = Profile.model_validate(run["profile"]) if run.get("profile") else None
        documents = [doc for doc in (workspace.get_document(d) for d in options.get("doc_ids") or []) if doc is not None]
        context = RunContext(profile=profile, documents=documents, today=settings.today)

    note: str | None = None
    if bus is None:
        backend, bus, note = prepare_run(settings, run_id=run_id, client=client, backend=backend)
    else:
        if bus.run_id != run_id:
            raise PipelineError(f"이벤트 버스의 실행 id({bus.run_id})가 이어서 실행할 id({run_id})와 달라요")
        if backend is None:
            mode, note = resolve_mode(settings.mode)
            backend = create_backend(mode, settings, client=client)
    if listener is not None:
        bus.add_listener(listener)
    target = settings.out_dir if out_dir is _DEFAULT else out_dir
    if not workspace.claim_run(run_id, run["status"]):  # everything is ready: claim the run atomically
        raise PipelineError("이 실행은 이미 다른 곳에서 이어서 진행 중이에요")
    slot_id = str(options.get("slot_id") or "") if run["kind"] == "slot" else ""
    slot_before = None
    if slot_id:
        try:
            slot_before = workspace.reclaim_slot(slot_id, run_id)
        except BaseException:  # another live run is generating the slot: give the run back as it was
            try:
                workspace.update_run(run_id, status=run["status"], error=run.get("error") or "")
            except Exception:  # noqa: BLE001
                log.exception("실행 상태를 되돌리지 못했어요 (run %s)", run_id)
            raise
    # this process owns the run from here (run_pipeline keeps using this lease); its token decides whether the
    # slot may still be handed back or linked when the run ends (not when another process took the run over)
    lease = workspace.acquire_run(run_id)
    try:
        try:
            result = run_pipeline(brief, backend, bus, settings, runner=runner, out_dir=target, mode_note=note,
                                  workspace=workspace, context=context, resume_state=state)
        except BaseException:
            if slot_before is not None:
                _restore_slot(workspace, slot_id, slot_before, run_id, lease.token)
            raise
        if slot_before is not None:
            link_slot_after_run(workspace, run_id, owner_token=lease.token)
    finally:
        lease.release()
    return result


# ---------------------------------------------------------------------------
# Convenience: build everything from settings
# ---------------------------------------------------------------------------


def prepare_run(settings: Settings, *, run_id: str | None = None, client: object | None = None,
                backend: Backend | None = None) -> tuple[Backend, EventBus, str]:
    """Resolve the mode and build ``(backend, bus, mode_note)``."""
    if backend is None:
        mode, note = resolve_mode(settings.mode)
        backend = create_backend(mode, settings, client=client)
    else:
        note = f"{backend.name} 백엔드로 실행해요"
    clock = SimClock(settings.speed) if backend.name == "mock" else RealClock()
    bus = EventBus(run_id or new_run_id(), clock=clock)
    return backend, bus, note


def execute_run(brief: Brief, settings: Settings, *, run_id: str | None = None, client: object | None = None,
                backend: Backend | None = None, listener: Callable[[dict[str, Any]], None] | None = None,
                workspace: "Workspace | None" = None, context: RunContext | None = None,
                ) -> tuple[RunResult, EventBus]:
    backend, bus, note = prepare_run(settings, run_id=run_id, client=client, backend=backend)
    if listener is not None:
        bus.add_listener(listener)
    result = run_pipeline(brief, backend, bus, settings, out_dir=settings.out_dir, mode_note=note,
                          workspace=workspace, context=context)
    return result, bus
