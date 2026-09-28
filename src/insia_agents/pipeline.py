"""The pipeline: plan → research → per-channel draft/review/revise loop.

``run_pipeline`` drives agent steps (generators, see ``agents.common``) with a
runner:

- ``SimRunner`` (mock): a tiny discrete-event scheduler over a ``SimClock``.
  Channels interleave in virtual time, deterministically (ties break by
  channel order), and ``t`` stays monotonic.
- ``ThreadRunner`` (live): channels run concurrently in a ThreadPoolExecutor;
  real time passes during API calls.
"""

from __future__ import annotations

import heapq
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator, Hashable, TypeVar

from .agents import orchestrator, researcher, reviewer
from .agents.common import AgentContext, Step, channel_label
from .backends import create_backend
from .backends.base import Backend, BackendError
from .config import Settings, resolve_mode
from .events import EventBus, RealClock, SimClock
from .models import Brief, ChannelId, ChannelResult, Draft, Plan, Review, RunResult
from .storage import run_dir, save_run

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


class PipelineError(RuntimeError):
    pass


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    """Runs steps in real time; parallel steps in worker threads (live mode)."""

    def __init__(self, max_workers: int = 4) -> None:
        self.max_workers = max(1, max_workers)

    def run(self, gen: Generator[float, None, T]) -> T:
        return _drive(gen, lambda _delay: None)

    def run_parallel(self, gens: dict[K, Generator[float, None, Any]]) -> dict[K, Any]:
        results: dict[K, Any] = {}
        if not gens:
            return results
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(gens)), thread_name_prefix="insia-channel") as pool:
            futures = {key: pool.submit(self.run, gen) for key, gen in gens.items()}
            for key, future in futures.items():
                try:
                    results[key] = future.result()
                except Exception as exc:
                    results[key] = exc
        return results


# ---------------------------------------------------------------------------
# Channel loop
# ---------------------------------------------------------------------------


def _best_index(reviews: list[Review]) -> int:
    return max(range(len(reviews)), key=lambda i: (reviews[i].passed, reviews[i].score, i))


def channel_flow(ctx: AgentContext, plan: Plan, store: researcher.ResearchStore, channel: ChannelId) -> Step[ChannelResult]:
    max_rounds = ctx.settings.max_rounds
    drafts: list[Draft] = []
    reviews: list[Review] = []
    current = yield from orchestrator.draft(ctx, plan, store.snapshot(), channel)
    while True:
        try:
            result = yield from reviewer.review(ctx, store.snapshot(), current)
        except BackendError as exc:
            if not reviews:
                raise
            ctx.log(f"{channel_label(channel)} R{current.round} 검수 실패: {exc} — 이전 버전을 최종본으로 써요", "warn", "reviewer")
            break
        drafts.append(current)
        reviews.append(result)
        if result.passed or current.round >= max_rounds:
            yield from reviewer.conclude(ctx, result)
            break
        yield from reviewer.request_revision(ctx, result)
        try:
            if result.needs_research:
                yield from researcher.followup(ctx, store, result.needs_research, channel)
            current = yield from orchestrator.revise(ctx, plan, store.snapshot(), current, result)
        except BackendError as exc:
            ctx.log(f"{channel_label(channel)} 수정 중 오류: {exc} — 지금까지 가장 좋은 버전을 최종본으로 써요", "warn", "orchestrator")
            break

    best = _best_index(reviews)
    outcome = ChannelResult(channel=channel, final=drafts[best], drafts=drafts, reviews=reviews,
                            passed=reviews[best].passed, rounds=len(drafts) - 1)
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


def run_pipeline(brief: Brief, backend: Backend, bus: EventBus, settings: Settings, *,
                 runner: SimRunner | ThreadRunner | None = None, out_dir: str | Path | None = None,
                 mode_note: str | None = None) -> RunResult:
    """Run the whole pipeline and return the result. Emits ``run.failed`` and
    re-raises when something goes wrong before any channel could finish."""
    simulated = bool(getattr(bus.clock, "simulated", False))
    if runner is None:
        runner = SimRunner(bus.clock) if isinstance(bus.clock, SimClock) else ThreadRunner(settings.max_workers)
    started_at = _iso_now()
    target_dir: Path | None = None
    ctx = AgentContext(bus=bus, backend=backend, settings=settings, brief=brief, simulated=simulated)
    if hasattr(backend, "on_notice"):
        backend.on_notice = lambda agent, level, message: bus.emit("log", agent, {"level": level, "message": message})  # type: ignore[attr-defined]

    try:
        channels = _channels(brief)
        brief = brief.model_copy(update={"channels": channels})
        ctx.brief = brief
        backend_note = backend.prepare(brief) if hasattr(backend, "prepare") else None  # type: ignore[attr-defined]
        if out_dir is not None:
            target_dir = run_dir(out_dir, bus.run_id)
            bus.attach_sink(target_dir / "events.jsonl")

        bus.emit("run.started", "system", {
            "brief": brief,
            "channels": channels,
            "mode": backend.name,
            "model": backend.model,
            "max_rounds": settings.max_rounds,
            "pass_score": settings.pass_score,
        })
        if mode_note:
            ctx.log(mode_note)
        if backend_note:
            ctx.log(backend_note)
        ctx.status("researcher", "waiting", "총괄의 리서치 질문을 기다려요")
        ctx.status("reviewer", "waiting", "초안이 오면 바로 검수할게요")

        plan = runner.run(orchestrator.plan(ctx))
        runner.run(orchestrator.delegate_research(ctx, plan))
        store = researcher.ResearchStore()
        runner.run(researcher.run(ctx, store, list(plan.questions)))

        outcomes = runner.run_parallel({ch: channel_flow(ctx, plan, store, ch) for ch in channels})
        results: list[ChannelResult] = []
        errors: dict[str, str] = {}
        for ch in channels:
            value = outcomes.get(ch)
            if isinstance(value, ChannelResult):
                results.append(value)
            else:
                message = str(value) if isinstance(value, BaseException) else "알 수 없는 오류"
                errors[ch] = message
                ctx.status("orchestrator", "error", f"{channel_label(ch)} 작업이 실패했어요")
                ctx.log(f"{channel_label(ch)} 실패: {message}", "error", "orchestrator")
        if not results:
            first = next(iter(outcomes.values()), None)
            if isinstance(first, BaseException):
                raise first
            raise PipelineError("모든 채널이 실패했어요")

        result = RunResult(
            run_id=bus.run_id, mode=backend.name, model=backend.model, brief=brief,  # type: ignore[arg-type]
            plan=plan, research=store.snapshot(), results=results, started_at=started_at, finished_at=_iso_now(),
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
        bus.emit("run.completed", "system", completed)
        return result
    except BaseException as exc:
        if not bus.closed:
            if isinstance(exc, KeyboardInterrupt):
                message = "사용자가 실행을 중단했어요"
            elif isinstance(exc, (BackendError, PipelineError)):
                message = str(exc)
            else:
                message = f"{type(exc).__name__}: {exc}"
            try:
                bus.emit("run.failed", "system", {"error": message})
            except RuntimeError:
                pass
        raise
    finally:
        bus.detach_sink()


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
                ) -> tuple[RunResult, EventBus]:
    backend, bus, note = prepare_run(settings, run_id=run_id, client=client, backend=backend)
    if listener is not None:
        bus.add_listener(listener)
    result = run_pipeline(brief, backend, bus, settings, out_dir=settings.out_dir, mode_note=note)
    return result, bus
