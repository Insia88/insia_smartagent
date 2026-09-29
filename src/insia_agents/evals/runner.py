"""Run eval cases through the real pipeline and write the results.

``run_eval`` calls the same entry point as ``insia run`` (``prepare_run`` +
``run_pipeline`` with a workspace and ``build_context``), once per case and
repetition, each in a fresh temporary workspace (``INSIA_HOME``), so the
user's workspace is never written. The user's ``prices.json`` is copied in
(read only) so live costs use their prices.

Output folder (``--out``, default ``evals/results/<시각>-<mode>``)::

    summary.json        totals, per-case/channel results, assertion outcomes (compare reads this)
    report.md           Korean report: failures first, table per case/channel, regressions vs baseline
    cases/<id>.json     the case, the settings it ran with, every rep's full grade (final draft, numbers with
                        status, checks, review); rewritten after every rep, so a finished rep survives Ctrl+C
    runs/<id>/          the pipeline's own run folder (brief, plan, research, drafts, reviews, events.jsonl and
                        result.json, which --resume re-grades when only the grader code changed)
    spend.jsonl         (live) the folder's spend ledger: one line per billed API response, written the moment
                        the usage arrives — also for a run that is interrupted or timed out afterwards

Live mode never starts without ``--max-cost-usd``, credentials and a known
price for the model (an unpriced model is metered at $0, so no cap could
trip); it prints the planned runs and an estimate first, gives each run the
remaining budget as its pipeline cap (``Settings.max_cost_usd``) and stops
the eval once the cap is reached — or as soon as a response comes from a
model it cannot price (also a server-side fallback whose declined attempt
was priced). The remaining budget is always ``--max-cost-usd`` minus the
ledger total, so a call that finishes after a timeout or an interrupted run
still counts; a new run never starts while a timed-out run's call is still
in flight (the eval waits for it up to ``--timeout-s``, then stops rather
than start with a cap that leaves that call out). Failures that produced no
gradable output (API error, refusal, timeout, budget stop, Ctrl+C) are
recorded with a failure class and never scored as a quality failure — with
``--reps``, a must that some reps could not evaluate is "not evaluated", not
failed, unless an evaluated rep failed it. Ctrl+C at any point (also while
the eval waits for a late call at the end) still writes ``summary.json`` and
``report.md``.

``--resume`` reuses a finished run only when the case file, the planned
channels, the mode and the settings (model, max rounds, pass score, effort
per role, server-side fallback, document and web-search limits, prompt
files, run code) are unchanged; anything else is run again. When only the
grader code changed, the saved runs (``runs/<id>/result.json``) are graded
again for free. A narrower ``--channels`` than the folder ran a case with is
refused (the other channels' valid results would silently leave the summary
and any comparison against it). The cap covers the whole eval folder:
everything in ``spend.jsonl`` (every case, reused runs, replaced ones,
interrupted ones) is counted first. Cases of the folder that this invocation
does not plan (``--case`` narrower than before) stay in ``summary.json`` as
they were when their settings match, and a Ctrl+C mid-resume keeps the
earlier finished reps it had not reached yet, so the summary — and a
comparison against it — still covers the whole folder.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..backends.base import BackendError, RefusalError
from ..config import KST, Settings, has_credentials
from ..errors import CommandError, UsageError
from ..models import ALL_CHANNELS, RunResult, UsageRecord
from .cases import EvalCase, find_cases_dir, load_cases
from .estimate import load_measured, measured_estimate, model_estimate
from .graders import apply_assertions, evaluate_case_level, grade_channel, not_evaluated, public
from .grounding import Evidence

SCHEMA_VERSION = 1
KIND = "insia-eval"
DEFAULT_MOCK_TODAY = "2026-09-28"  # mock runs are dated the same way every time (deterministic output)
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_MAX_SCORE_DROP = 5.0
OK_STATUSES = ("ok", "partial")
SPEND_FILE = "spend.jsonl"
# After a timeout or Ctrl+C the pipeline is cancelled at its next step, but the API call in flight still finishes (and
# is billed). Wait this long for it (never longer than --timeout-s) so its cost lands in the record.
TIMEOUT_GRACE_S = 30.0
INTERRUPT_GRACE_S = 2.0
LATE_WAIT_S = 30.0  # at the end of the eval: how long to wait for calls still running after a timeout

Printer = Callable[[str], None]


@dataclass
class EvalOptions:
    mode: str = "mock"
    cases_dir: Path | None = None
    case_ids: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    out_dir: Path | None = None
    max_cost_usd: float | None = None
    model: str | None = None
    reps: int = 1
    baseline: Path | None = None
    max_score_drop: float = DEFAULT_MAX_SCORE_DROP
    timeout_s: float = DEFAULT_TIMEOUT_S
    resume: bool = False
    dry_run: bool = False
    estimate_from: Path | None = None
    judge: bool = False
    judge_model: str = ""
    assume_yes: bool = False
    max_rounds: int | None = None
    pass_score: int | None = None


@dataclass
class PlannedCase:
    case: EvalCase
    channels: list[str]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _usd(value: float) -> str:
    if not value:
        return "$0"
    return f"${value:.4f}" if value < 0.01 else f"${value:,.2f}"


def default_out_dir(cases_dir: Path, mode: str) -> Path:
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    return cases_dir.parent / "results" / f"{stamp}-{mode}"


def plan_cases(cases: Sequence[EvalCase], channels: Sequence[str]) -> tuple[list[PlannedCase], list[str]]:
    """Cases with the ``--channels`` filter applied; returns (planned, skipped case ids)."""
    planned, skipped = [], []
    for case in cases:
        chosen = [c for c in case.channels if not channels or c in channels]
        if chosen:
            planned.append(PlannedCase(case, chosen))
        else:
            skipped.append(case.id)
    return planned, skipped


# ---------------------------------------------------------------------------
# Usage collection
# ---------------------------------------------------------------------------


class SpendLedger:
    """What the eval folder has spent: ``spend.jsonl``, one line per billed response, appended as the usage arrives.

    It is the budget's source of truth. ``cases/<id>.json`` is written only between runs, so a run interrupted with
    Ctrl+C, or a call that finishes after a timeout, would otherwise vanish from the cap and from ``--resume``.
    ``path=None`` (mock) keeps the total in memory only. Thread-safe (live channels report from worker threads)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._total = 0.0
        self._lock = threading.Lock()
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self._total += max(0.0, float(json.loads(line).get("cost_usd") or 0.0))
                except (ValueError, TypeError, AttributeError):
                    continue  # a line cut short by a crash: skip it

    @property
    def total(self) -> float:
        with self._lock:
            return round(self._total, 6)

    def add(self, cost_usd: float, **fields: Any) -> None:
        cost = max(0.0, float(cost_usd or 0.0))
        line = json.dumps({"at": _iso_now(), **fields, "cost_usd": round(cost, 8)}, ensure_ascii=False)
        with self._lock:
            self._total += cost
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError:
                    pass  # the in-memory total still guards this invocation's cap

    def seed_from_case_files(self, out_root: Path) -> float:
        """A folder written before the ledger existed: carry what its case files say was spent (once)."""
        if self.path is not None and self.path.exists():
            return 0.0
        carried = 0.0
        for path in sorted((out_root / "cases").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            spent = _case_file_spent(data)
            if spent:
                self.add(spent, kind="carried", case=path.stem, note="이전 결과 파일(cases/)에 적힌 비용")
                carried += spent
        return carried


def _case_file_spent(data: dict[str, Any]) -> float:
    reps = [r for r in data.get("reps", []) if isinstance(r, dict)]
    return float(data.get("discarded_cost_usd") or 0.0) + sum(_record_cost(r) for r in reps)


class UsageCollector:
    """``backend.on_usage`` target (the pipeline's meter forwards every priced record here).

    With ``prices`` (live), a record with tokens served by a model that has no price is noted in ``unpriced``: that
    model's tokens were metered as $0, so the budget cap cannot see them and the eval stops. With ``ledger``, every
    record is also appended to the folder's spend ledger (tagged with ``tags``) the moment it arrives."""

    def __init__(self, prices: dict[str, Any] | None = None, *, ledger: SpendLedger | None = None,
                 tags: dict[str, Any] | None = None) -> None:
        self.records: list[UsageRecord] = []
        self.unpriced: set[str] = set()
        self._prices = prices
        self._ledger = ledger
        self._tags = dict(tags or {})
        self._lock = threading.Lock()

    def __call__(self, record: UsageRecord) -> None:
        unpriced = self._prices is not None and _is_unpriced(record, self._prices)
        with self._lock:
            self.records.append(record)
            if unpriced:
                self.unpriced.add(record.model or "?")
        if self._ledger is not None:
            self._ledger.add(float(record.cost_usd or 0.0), **self._tags, model=record.model, agent=record.agent,
                             task=record.task, input_tokens=record.input_tokens, output_tokens=record.output_tokens)

    @property
    def cost(self) -> float:
        with self._lock:
            return round(sum(float(r.cost_usd or 0.0) for r in self.records), 6)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.records)
        return {
            "calls": len(records),
            "input_tokens": sum(r.input_tokens for r in records),
            "output_tokens": sum(r.output_tokens for r in records),
            "cache_read_tokens": sum(r.cache_read_tokens for r in records),
            "cache_write_tokens": sum(r.cache_write_tokens for r in records),
            "web_search_requests": sum(r.web_search_requests for r in records),
            "models": sorted({r.model for r in records if r.model}),
        }


def _is_unpriced(record: UsageRecord, prices: dict[str, Any]) -> bool:
    """Tokens served by a model without a price. ``record.model`` is the served model: after a server-side refusal
    fallback it is the fallback, and the record's cost is not 0 (the declined attempt at the requested, priced model is
    billed), so the cost cannot tell. The requested model is checked before the eval starts."""
    from ..costs import price_for

    tokens = record.input_tokens or record.output_tokens or record.cache_read_tokens or record.cache_write_tokens
    return bool(tokens) and price_for(record.model or "", prices=prices) is None


def _live_prices(base: Settings) -> dict[str, Any]:
    """The price table live usage is metered with: costs.py defaults + the user's prices.json + ``INSIA_PRICE_*``."""
    from ..costs import load_prices

    return load_prices(home=base.home)[0]


def unpriced_models(base: Settings, options: "EvalOptions") -> list[str]:
    """Models a live eval would call whose price is unknown (the eval model and, with ``--judge``, the judge model)."""
    from ..costs import price_for

    prices = _live_prices(base)
    models = [base.model]
    if options.judge:
        from .judge import DEFAULT_JUDGE_MODEL

        models.append(options.judge_model or DEFAULT_JUDGE_MODEL)
    return [m for m in dict.fromkeys(models) if price_for(m, prices=prices) is None]


# ---------------------------------------------------------------------------
# One case, one repetition
# ---------------------------------------------------------------------------


def _failure(kind: str, message: str) -> dict[str, str]:
    return {"class": kind, "message": message}


def _classify(exc: BaseException) -> dict[str, str]:
    from ..errors import error_text
    from ..pipeline import BudgetExceeded, RunCancelled

    if isinstance(exc, BudgetExceeded):
        return _failure("budget", str(exc))
    if isinstance(exc, RefusalError):
        return _failure("refusal", str(exc))
    if isinstance(exc, RunCancelled):
        return _failure("cancelled", str(exc))
    if isinstance(exc, BackendError):
        return _failure("api", str(exc))
    return _failure("harness", error_text(exc))


def _copy_prices(real_home: Path, tmp_home: Path) -> None:
    from ..costs import PRICES_FILE

    source = Path(real_home).expanduser() / PRICES_FILE
    try:
        if source.is_file():
            shutil.copyfile(source, tmp_home / PRICES_FILE)
    except OSError:
        pass


def _channel_errors(bus: Any) -> dict[str, str]:
    for event in reversed(bus.events):
        if event.get("type") == "run.completed":
            return dict((event.get("data") or {}).get("errors") or {})
    return {}


def _served_mismatch(requested: str, models: list[str], mode: str) -> list[str]:
    if mode != "live":
        return []
    return [m for m in models if m != requested and not m.startswith(requested + "-")]


def run_case(planned: PlannedCase, rep: int, base: Settings, options: EvalOptions, out_root: Path, *,
             remaining_cap: float | None, client: Any = None, backend_factory: Callable[[Settings], Any] | None = None,
             printer: Printer | None = None, reps: int = 1, ledger: SpendLedger | None = None) -> dict[str, Any]:
    """Run one case once and grade every channel. Never raises for a failed run.

    Ctrl+C (``KeyboardInterrupt``) is re-raised after the run is cancelled, with the partial record (what the run
    spent so far, failure class ``cancelled``) attached as ``exc.eval_record`` so the caller can still write it."""
    from ..db import Workspace
    from ..pipeline import BudgetExceeded, ThreadRunner, build_context, prepare_run, run_pipeline

    case, channels = planned.case, planned.channels
    run_id = case.id if reps == 1 else f"{case.id}-r{rep}"
    record: dict[str, Any] = {"rep": rep, "run_id": run_id, "status": "error", "failure": None, "model": "",
                              "served_models": [], "model_mismatch": [], "unpriced_models": [], "cost_usd": 0.0,
                              "judge_cost_usd": 0.0, "usage": {}, "duration_s": 0.0, "wall_s": 0.0, "channels": [],
                              "must": [], "should": [], "run_dir": f"runs/{run_id}", "started_at": _iso_now()}
    today = case.today or (DEFAULT_MOCK_TODAY if options.mode == "mock" else base.today)
    tmp = Path(tempfile.mkdtemp(prefix="insia-eval-"))
    _copy_prices(base.home, tmp)
    settings = replace(base, home=tmp, out_dir=out_root / "runs", today=today,
                       max_cost_usd=max(0.0, remaining_cap) if remaining_cap is not None else 0.0)
    brief = case.brief.model_copy(update={"channels": list(channels)})
    collector = UsageCollector(_live_prices(base) if options.mode == "live" else None, ledger=ledger,
                               tags={"kind": "run", "case": case.id, "rep": rep, "run_id": run_id})
    result: RunResult | None = None
    failure: dict[str, str] | None = None
    context = None
    workspace = None
    bus = None
    runner = None
    worker: _Worker | None = None
    thread_alive = False
    interrupted: KeyboardInterrupt | None = None
    started = time.monotonic()
    try:
        workspace = Workspace(tmp)
        if case.profile is not None:
            workspace.save_profile(case.profile)
        for doc in case.documents:
            workspace.add_document(doc.title, doc.text, kind=doc.kind)
        context = build_context(workspace, settings, docs="all")
        backend = backend_factory(settings) if backend_factory is not None else None
        backend, bus, note = prepare_run(settings, run_id=run_id, client=client, backend=backend)
        backend.on_usage = collector
        record["model"] = backend.model
        if printer is not None and options.mode == "live":
            bus.add_listener(_live_listener(printer))
        if options.mode == "live":
            runner = ThreadRunner(settings.max_workers)
            box: dict[str, Any] = {}

            def work() -> None:
                box["result"] = run_pipeline(brief, backend, bus, settings, runner=runner, out_dir=settings.out_dir,
                                             mode_note=note, workspace=workspace, context=context)

            worker = _Worker(work, box, name=f"insia-eval-{case.id}")
            deadline = started + options.timeout_s
            while worker.alive and time.monotonic() < deadline:
                worker.wait(min(0.5, max(0.01, deadline - time.monotonic())))
            if worker.alive:
                runner.cancel()  # stop the channels at their next step
                worker.wait(min(TIMEOUT_GRACE_S, options.timeout_s))  # the call in flight is billed: wait for it
                failure = _failure("timeout", f"{options.timeout_s:g}초 안에 끝나지 않아 멈췄어요")
            elif "error" in box:
                raise box["error"]
            else:
                result = box.get("result")
        else:
            result = run_pipeline(brief, backend, bus, settings, out_dir=settings.out_dir, mode_note=note,
                                  workspace=workspace, context=context)
        record["model"] = getattr(backend, "model", record["model"])
    except KeyboardInterrupt as exc:  # Ctrl+C: cancel the run, keep what it spent, re-raise below
        interrupted = exc
        result = None
        if runner is not None:
            runner.cancel()
        if worker is not None:
            worker.wait(min(INTERRUPT_GRACE_S, options.timeout_s))
        failure = _failure("cancelled", "평가를 도중에 멈췄어요 (Ctrl+C)")
    except BudgetExceeded as exc:
        failure = _classify(exc)
        result = exc.result
    except BaseException as exc:  # noqa: BLE001 - recorded with its failure class
        failure = _classify(exc)
    finally:
        # A worker still running (after a timeout or Ctrl+C) keeps its workspace until it ends; closing it under the
        # worker would only turn its last event and usage writes into errors.
        thread_alive = worker is not None and worker.alive
        if thread_alive:
            _reap_later(worker, workspace, tmp)  # type: ignore[arg-type]
        else:
            if workspace is not None:
                try:
                    workspace.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)

    record["wall_s"] = round(time.monotonic() - started, 2)
    record["duration_s"] = round(bus.clock.now(), 1) if bus is not None else record["wall_s"]
    _fill_usage(record, collector, base, options)
    if failure is not None and failure["class"] == "budget" and remaining_cap is not None:
        failure = _failure("budget", f"평가 예산 상한에 닿아 멈췄어요 (이 케이스 몫으로 남은 예산 {_usd(max(0.0, remaining_cap))}, "
                                     f"이 케이스에서 쓴 비용 {_usd(record['cost_usd'])})")
    if thread_alive and failure is not None:
        # the call in flight finishes later: its cost reaches the spend ledger (and the budget), not this record yet
        record["cost_incomplete"] = True
        record["_late"] = (collector, worker)
        failure = _failure(failure["class"], failure["message"] + f" · {LATE_COST_NOTE}")
    elif worker is not None and failure is not None and failure["class"] in ("timeout", "cancelled"):
        failure = _failure(failure["class"], failure["message"] + " · 진행 중이던 호출 비용까지 넣었어요")
    record["failure"] = failure

    errors = _channel_errors(bus) if bus is not None and result is not None else {}
    graded: dict[str, dict[str, Any]] = {}
    if result is not None:
        graded = _grade_result(case, channels, result, context.documents if context is not None else [], options.mode)
    for channel in channels:
        if channel in graded:
            record["channels"].append(graded[channel])
            continue
        if failure is not None:
            status, reason = FAILURE_STATUS.get(failure["class"], "error"), failure["message"]
        else:
            status, reason = "error", errors.get(channel) or "채널 결과가 없어요"
        entry = {"channel": channel, "status": status, "error": reason}
        for kind in ("must", "should"):
            entry[kind] = [not_evaluated(a, f"평가하지 못했어요: {reason}") for a in case.assertions(kind)
                           if a.applies(channel, options.mode)]
        record["channels"].append(entry)

    done = [c for c in record["channels"] if c.get("status") == "ok"]
    if failure is None and len(done) == len(channels):
        record["status"] = "ok"
    elif done:
        record["status"] = "partial"
    else:
        record["status"] = RECORD_FAILURE_STATUS.get((failure or {}).get("class", ""), "error")
    for kind in ("must", "should"):
        applicable = [a for a in case.assertions(kind) if a.applies("*", options.mode)]
        if record["status"] in OK_STATUSES:
            record[kind] = [evaluate_case_level(a, cost_usd=record["cost_usd"], duration_s=record["duration_s"])
                            for a in applicable]
        else:
            record[kind] = [not_evaluated(a, "실행이 끝나지 않아 평가하지 못했어요") for a in applicable]
    if interrupted is not None:
        interrupted.eval_record = record  # type: ignore[attr-defined]
        raise interrupted
    return record


def _grade_result(case: EvalCase, channels: Sequence[str], result: RunResult, documents: Sequence[Any],
                  mode: str) -> dict[str, dict[str, Any]]:
    """Grade every planned channel of a finished run (also used to re-grade a saved run with new grader code)."""
    evidence = Evidence.build(result.research, profile=case.profile, documents=documents, brief=result.brief)
    graded: dict[str, dict[str, Any]] = {}
    for channel_result in result.results:
        if channel_result.channel not in channels:
            continue
        grade = grade_channel(case, channel_result, result.research, evidence)
        apply_assertions(case, grade, mode)
        graded[channel_result.channel] = grade
    return graded


LATE_COST_NOTE = "진행 중이던 호출 비용은 이 기록에 빠져 있어요 (평가 중에 끝나면 spend.jsonl과 예산에는 들어가요)"
# failure class → status of a channel / a record that produced no gradable output
FAILURE_STATUS = {"budget": "budget_stopped", "timeout": "timeout", "cancelled": "cancelled"}
RECORD_FAILURE_STATUS = {"budget": "budget_stopped", "timeout": "timeout", "cancelled": "cancelled"}


class _Worker:
    """The pipeline thread of one live run. Liveness is its own ``done`` event, not ``Thread.is_alive()``: a Ctrl+C that
    interrupts ``Thread.join()`` can leave CPython reporting a still-running thread as finished, and the workspace
    would then be closed under it."""

    def __init__(self, target: Callable[[], None], box: dict[str, Any], *, name: str) -> None:
        self._done = threading.Event()

        def run() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - handed to the main thread
                box["error"] = exc
            finally:
                self._done.set()

        threading.Thread(target=run, name=name, daemon=True).start()

    @property
    def alive(self) -> bool:
        return not self._done.is_set()

    def wait(self, seconds: float | None = None) -> bool:
        """Wait up to ``seconds`` (``None``: until it ends); returns True once it has ended."""
        if seconds is None:
            return self._done.wait()
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._done.is_set() and time.monotonic() < deadline:
            self._done.wait(min(0.2, max(0.01, deadline - time.monotonic())))
        return self._done.is_set()


def _fill_usage(record: dict[str, Any], collector: UsageCollector, base: Settings, options: EvalOptions) -> None:
    record["cost_usd"] = collector.cost
    usage = collector.summary()
    record["usage"] = usage
    record["served_models"] = usage["models"]
    record["model_mismatch"] = _served_mismatch(record["model"] or base.model, usage["models"], options.mode)
    record["unpriced_models"] = sorted(collector.unpriced)


def _late_records(states: list["_CaseRun"]) -> list[tuple["_CaseRun", dict[str, Any]]]:
    """Records whose run kept a call in flight after a timeout or Ctrl+C (its cost is not in the record yet)."""
    return [(state, record) for state in states for record in state.records if record.get("_late")]


def _late_running(states: list["_CaseRun"]) -> bool:
    return any(record["_late"][1].alive for _, record in _late_records(states))


def _wait_late(states: list["_CaseRun"], wait_s: float) -> bool:
    """Wait up to ``wait_s`` in total for the calls still in flight; True once none is running."""
    deadline = time.monotonic() + max(0.0, wait_s)
    for _, record in _late_records(states):
        record["_late"][1].wait(max(0.0, deadline - time.monotonic()))
    return not _late_running(states)


def _refresh_late(states: list["_CaseRun"]) -> list["_CaseRun"]:
    """Refresh the cost of records with a late call from their collectors (a finished call's cost lands in the record
    and the note says so). Returns the cases whose records changed."""
    changed: list[_CaseRun] = []
    for state, record in _late_records(states):
        collector, worker = record["_late"]
        before = record["cost_usd"]
        record["cost_usd"] = collector.cost
        usage = collector.summary()
        record["usage"], record["served_models"] = usage, usage["models"]
        record["unpriced_models"] = sorted(collector.unpriced)
        if not worker.alive:
            record.pop("_late", None)
            record.pop("cost_incomplete", None)
            _replace_note(record, f" · {LATE_COST_NOTE}", " · 진행 중이던 호출 비용까지 넣었어요")
        if record["cost_usd"] != before or "_late" not in record:
            if state not in changed:
                changed.append(state)
    return changed


def _replace_note(record: dict[str, Any], old: str, new: str) -> None:
    """Rewrite a note in the record's failure message and everywhere it was copied (channel errors, outcome details)."""
    if record.get("failure"):
        record["failure"] = {**record["failure"], "message": record["failure"]["message"].replace(old, new)}
    for holder in [record, *record.get("channels", [])]:
        if isinstance(holder.get("error"), str):
            holder["error"] = holder["error"].replace(old, new)
        for kind in ("must", "should"):
            for outcome in holder.get(kind, []):
                if isinstance(outcome.get("detail"), str):
                    outcome["detail"] = outcome["detail"].replace(old, new)


def _reap_later(worker: _Worker, workspace: Any, tmp: Path) -> None:
    """After a timeout or Ctrl+C the pipeline thread may still finish a call: close its workspace and delete the
    temporary folder once it ends (a daemon thread, so a finished eval never waits for it)."""

    def reap() -> None:
        worker.wait()
        try:
            if workspace is not None:
                workspace.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    threading.Thread(target=reap, name="insia-eval-reap", daemon=True).start()


def _live_listener(printer: Printer) -> Callable[[dict[str, Any]], None]:
    from ..agents.common import channel_label

    def listen(event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        if event.get("type") == "channel.completed":
            printer(f"    · {channel_label(data.get('channel', ''))} {data.get('score')}점 · 수정 {data.get('rounds')}회")
        elif event.get("type") == "run.failed":
            printer(f"    · 실행 멈춤: {data.get('error')}")
        elif event.get("type") == "log" and data.get("level") in ("warn", "error"):
            # e.g. a response from a model without a price, a refusal fallback, a retry
            printer(f"    · {'주의' if data.get('level') == 'warn' else '오류'}: {data.get('message')}")

    return listen


# ---------------------------------------------------------------------------
# Aggregation over repetitions
# ---------------------------------------------------------------------------


def _merge_outcomes(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Strict per assertion key: passes only when it passed in every rep.

    - an evaluated rep failed → failed (a quality failure), with that rep's detail;
    - no evaluated rep failed but some rep could not be evaluated (API error, refusal, timeout, budget stop, Ctrl+C)
      → not passed and ``error`` (an execution problem, counted in ``must_errors`` and never a regression), with
      ``reps_unevaluated`` and a detail that says how many reps were not evaluated;
    - otherwise passed."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for outcomes in lists:
        for outcome in outcomes:
            grouped.setdefault(outcome["key"], []).append(outcome)
    merged: list[dict[str, Any]] = []
    for outcomes in grouped.values():
        unevaluated = [o for o in outcomes if o.get("error")]
        failed = [o for o in outcomes if not o.get("error") and not o["passed"]]
        base = failed[0] if failed else (unevaluated[0] if unevaluated else outcomes[0])
        entry = {k: v for k, v in base.items() if k != "error"}
        entry.update({"passed": not failed and not unevaluated, "reps": len(outcomes),
                      "reps_passed": sum(1 for o in outcomes if o["passed"])})
        if unevaluated:
            entry["reps_unevaluated"] = len(unevaluated)
        if unevaluated and not failed:
            entry["error"] = True
            if len(unevaluated) < len(outcomes):
                entry["detail"] = (f"반복 {len(outcomes)}번 중 {len(unevaluated)}번은 평가하지 못했어요 (평가한 반복은 통과) — "
                                   f"{base.get('detail', '')}")
        merged.append(entry)
    return merged


def aggregate_channel(channel: str, grades: list[dict[str, Any]]) -> dict[str, Any]:
    if not grades:  # no record has this channel (never expected: resume only reuses records with the planned channels)
        return {"channel": channel, "status": "error", "reps": 0, "reps_ok": 0, "error": "이 채널의 실행 기록이 없어요",
                "must": [], "should": []}
    ok = [g for g in grades if g.get("status") == "ok"]
    entry: dict[str, Any] = {"channel": channel, "status": "ok" if ok and len(ok) == len(grades) else
                             (grades[-1].get("status", "error") if not ok else "partial"),
                             "reps": len(grades), "reps_ok": len(ok)}
    errors = [g.get("error") for g in grades if g.get("status") != "ok" and g.get("error")]
    if errors:
        entry["error"] = errors[0]
    if ok:
        scores = [g["score"] for g in ok if g.get("score") is not None]
        entry["score"] = round(statistics.fmean(scores), 1) if scores else None
        entry["scores"] = scores
        entry["passed"] = all(g["passed"] for g in ok)
        entry["passed_reps"] = sum(1 for g in ok if g["passed"])
        entry["rounds"] = round(statistics.fmean(g["rounds"] for g in ok), 2)
        entry["critical"] = max(g["critical"] for g in ok)
        entry["title"] = ok[-1]["title"]
        checks: dict[str, dict[str, Any]] = {}
        for g in ok:
            for c in g["checks"]:
                slot = checks.setdefault(c["id"], {"id": c["id"], "label": c["label"], "passed": True, "passed_reps": 0,
                                                   "value": c["value"], "expected": c["expected"]})
                slot["passed_reps"] += int(c["passed"])
                if not c["passed"] and slot["passed"]:
                    slot["value"] = c["value"]
                slot["passed"] = slot["passed"] and c["passed"]
        entry["checks"] = list(checks.values())
        keys = ("claims", "supported", "assumed", "flagged", "unsupported", "factual", "cited", "dated")
        grounding = {k: sum(g["grounding"][k] for g in ok) for k in keys}
        grounding["citation_coverage"] = round(grounding["cited"] / grounding["factual"], 3) if grounding["factual"] else None
        entry["grounding"] = grounding
        entry["unsupported"] = [{"text": n["text"], "sentence": n["sentence"], "tentative": bool(n.get("tentative"))}
                                for g in ok for n in g["grounding"]["numbers"] if n["status"] == "unsupported"][:20]
        entry["placeholders"] = round(statistics.fmean(g["placeholders"]["count"] for g in ok), 1)
        judged = [g["judge"] for g in ok if g.get("judge")]
        if judged:
            entry["judge_win"] = round(statistics.fmean(j["win"] for j in judged), 3)
            entry["judge"] = judged[-1]
    for kind in ("must", "should"):
        entry[kind] = _merge_outcomes([g.get(kind, []) for g in grades])
    return entry


def aggregate_case(planned: PlannedCase, records: list[dict[str, Any]]) -> dict[str, Any]:
    case = planned.case
    statuses = [r["status"] for r in records]
    if all(s == "ok" for s in statuses):
        status = "ok"
    elif any(s in OK_STATUSES for s in statuses):
        status = "partial"
    else:
        status = statuses[-1] if statuses else "skipped"
    channels = [aggregate_channel(ch, [next(g for g in r["channels"] if g["channel"] == ch) for r in records
                                       if any(g["channel"] == ch for g in r["channels"])])
                for ch in planned.channels] if records else []
    entry: dict[str, Any] = {
        "id": case.id, "title": case.title, "source": case.source, "tags": list(case.tags),
        "description": case.description, "status": status, "reps": len(records),
        "failures": [r["failure"] for r in records if r.get("failure")],
        "model": next((r["model"] for r in records if r.get("model")), ""),
        "served_models": sorted({m for r in records for m in r.get("served_models", [])}),
        "model_mismatch": sorted({m for r in records for m in r.get("model_mismatch", [])}),
        "unpriced_models": sorted({m for r in records for m in r.get("unpriced_models", [])}),
        "cost_usd": round(sum(r["cost_usd"] for r in records), 6),
        "judge_cost_usd": round(sum(r.get("judge_cost_usd", 0.0) for r in records), 6),
        "discarded_cost_usd": 0.0,
        "cost_incomplete": any(r.get("cost_incomplete") for r in records),
        "duration_s": round(statistics.fmean(r["duration_s"] for r in records), 1) if records else 0.0,
        "wall_s": round(sum(r["wall_s"] for r in records), 2),
        "usage": _sum_usage([r.get("usage") or {} for r in records]),
        "channels": channels,
        "must": _merge_outcomes([r.get("must", []) for r in records]),
        "should": _merge_outcomes([r.get("should", []) for r in records]),
    }
    all_must = entry["must"] + [o for c in channels for o in c["must"]]
    entry["must_total"] = len(all_must)
    entry["must_failed"] = sum(1 for o in all_must if not o["passed"] and not o.get("error"))
    entry["must_errors"] = sum(1 for o in all_must if o.get("error"))
    all_should = entry["should"] + [o for c in channels for o in c["should"]]
    entry["should_total"] = len(all_should)
    entry["should_passed"] = sum(1 for o in all_should if o["passed"])
    return entry


def _sum_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "web_search_requests")
    return {k: sum(int(u.get(k) or 0) for u in usages) for k in keys}


def build_summary(entries: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    channels = [c for e in entries for c in e["channels"]]
    ok_channels = [c for c in channels if c["status"] in OK_STATUSES]
    outcomes = [o for e in entries for o in e["must"] + [x for c in e["channels"] for x in c["must"]]]
    should = [o for e in entries for o in e["should"] + [x for c in e["channels"] for x in c["should"]]]
    scores = [c["score"] for c in ok_channels if c.get("score") is not None]
    checks: dict[str, dict[str, Any]] = {}
    for c in ok_channels:
        for check in c.get("checks", []):
            slot = checks.setdefault(f"{c['channel']}:{check['id']}", {"channel": c["channel"], "id": check["id"],
                                                                       "label": check["label"], "passed": 0, "total": 0})
            slot["total"] += 1
            slot["passed"] += int(check["passed"])
    grounding_keys = ("claims", "supported", "assumed", "flagged", "unsupported", "factual", "cited")
    grounding = {k: sum(c.get("grounding", {}).get(k, 0) for c in ok_channels) for k in grounding_keys}
    grounding["citation_coverage"] = round(grounding["cited"] / grounding["factual"], 3) if grounding["factual"] else None
    runs = sum(e["reps"] for e in entries)
    totals = {
        "cases": len(entries),
        "runs": runs,
        "channels": len(channels),
        "ok_channels": len(ok_channels),
        "error_channels": len(channels) - len(ok_channels),
        "must_total": len(outcomes),
        "must_passed": sum(1 for o in outcomes if o["passed"]),
        "must_failed": sum(1 for o in outcomes if not o["passed"] and not o.get("error")),
        "must_errors": sum(1 for o in outcomes if o.get("error")),
        "should_total": len(should),
        "should_passed": sum(1 for o in should if o["passed"]),
        "mean_score": round(statistics.fmean(scores), 1) if scores else None,
        "reviewer_passed": sum(1 for c in ok_channels if c.get("passed")),
        "cost_usd": round(sum(e["cost_usd"] for e in entries), 6),
        "judge_cost_usd": round(sum(e.get("judge_cost_usd", 0.0) for e in entries), 6),
        "discarded_cost_usd": round(sum(e.get("discarded_cost_usd", 0.0) for e in entries), 6),
        "duration_s": round(sum(e["duration_s"] * max(1, e["reps"]) for e in entries), 1),
        "wall_s": round(sum(e["wall_s"] for e in entries), 2),
        "failed_cases": [e["id"] for e in entries if e["must_failed"] or e["status"] not in ("ok",)],
    }
    costs = [e["cost_usd"] / max(1, e["reps"]) for e in entries if e["status"] in OK_STATUSES]
    if costs:
        totals["cost_per_case"] = {"mean": round(statistics.fmean(costs), 6), "min": round(min(costs), 6),
                                   "max": round(max(costs), 6)}
    return {"schema": SCHEMA_VERSION, "kind": KIND, **meta, "totals": totals, "checks": sorted(checks.values(),
            key=lambda c: (ALL_CHANNELS.index(c["channel"]) if c["channel"] in ALL_CHANNELS else 9, c["id"])),
            "grounding": grounding, "cases": entries}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_case_file(out_root: Path, planned: PlannedCase, records: list[dict[str, Any]], entry: dict[str, Any], *,
                    mode: str = "", discarded_cost_usd: float = 0.0, settings: dict[str, Any] | None = None,
                    grader: str = "") -> Path:
    """``discarded_cost_usd``: what earlier attempts in this folder spent on runs that ``--resume`` replaced.
    ``settings``: what the runs were made with (``run_settings``); ``--resume`` reuses them only when it matches.
    ``grader``: the grader code the records were graded with (``grader_fingerprint``); when it differs, ``--resume``
    grades the saved runs again (free) instead of running them again."""
    path = out_root / "cases" / f"{planned.case.id}.json"
    _write_json(path, {"schema": SCHEMA_VERSION, "mode": mode, "settings": dict(settings or {}), "grader": grader,
                       "channels": list(planned.channels), "case": planned.case.model_dump(mode="json"),
                       "result": entry, "discarded_cost_usd": round(discarded_cost_usd, 6),
                       "reps": [{**public(r), "channels": [public(g) for g in r["channels"]]} for r in records]})
    return path


# Package code that decides what a run produces. Changing it makes --resume run the case again. The eval's own grader
# code is fingerprinted separately (GRADER_CODE): a change there re-grades the saved runs for free.
RUN_CODE = ("agents", "backends", "pipeline.py", "channels.py", "prompt_loader.py", "models.py", "schema.py")
GRADER_CODE = ("evals/grounding.py", "evals/graders.py", "evals/cases.py")


def _code_fingerprint(paths: Sequence[str]) -> str:
    root = Path(__file__).resolve().parent.parent  # the insia_agents package
    digest = hashlib.sha256()
    for rel in paths:
        target = root / rel
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for file in files:
            try:
                data = file.read_bytes()
            except OSError:
                data = b""
            digest.update(file.relative_to(root).as_posix().encode("utf-8") + b"\n" + data + b"\n")
    return digest.hexdigest()[:12]


def grader_fingerprint() -> str:
    """The grader code (grounding, graders, case model) the records of this invocation are graded with."""
    return _code_fingerprint(GRADER_CODE)


def run_settings(base: Settings, options: "EvalOptions") -> dict[str, Any]:
    """Everything besides the case file, channels and mode that changes what a run produces or how the reviewer judges
    it: the model, max rounds, pass score, effort per role, server-side fallback, the document and web-search limits,
    the prompt files (agent prompts + channel guides) and the run code (pipeline, agents, backends, channel rules), each
    as a fingerprint."""
    from .. import __version__

    return {"model": base.model if options.mode == "live" else "mock", "max_rounds": base.max_rounds,
            "pass_score": base.pass_score, "effort": dict(sorted(base.effort.items())), "fallbacks": base.fallbacks,
            "max_document_chars": base.max_document_chars, "web_search_max_uses": base.web_search_max_uses,
            "prompts": _prompts_fingerprint(), "code": _code_fingerprint(RUN_CODE), "version": __version__}


def _prompts_fingerprint() -> str:
    from ..prompt_loader import AGENT_PROMPTS, CHANNEL_GUIDES, PromptNotFoundError, load_prompt

    digest = hashlib.sha256()
    for kind, names in (("agents", AGENT_PROMPTS), ("channels", CHANNEL_GUIDES)):
        for name in names:
            try:
                text = load_prompt(kind, name)
            except PromptNotFoundError:
                text = ""
            digest.update(f"{kind}/{name}\n{text}\n".encode("utf-8"))
    return digest.hexdigest()[:12]


SETTING_LABELS = {"model": "모델", "max_rounds": "최대 수정 횟수", "pass_score": "통과 점수",
                  "effort": "추론 노력(INSIA_EFFORT_*)", "fallbacks": "거절 시 대체 모델(INSIA_FALLBACKS)",
                  "max_document_chars": "자료 글자 수 상한(INSIA_MAX_DOCUMENT_CHARS)", "web_search_max_uses": "웹 검색 횟수 상한",
                  "prompts": "프롬프트 파일", "code": "파이프라인 코드", "version": "패키지 버전"}


def settings_diff(before: dict[str, Any] | None, now: dict[str, Any]) -> list[str]:
    """Korean labels of the settings that differ (an old case file without settings differs in everything)."""
    before = before or {}
    return [SETTING_LABELS.get(k, k) for k in now if before.get(k) != now[k]]


def load_summary(folder: str | Path) -> dict[str, Any]:
    """``summary.json`` of an eval result folder (or the file itself)."""
    path = Path(folder)
    path = path / "summary.json" if path.is_dir() else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"평가 결과를 읽지 못했어요: {path} ({exc.strerror or exc})") from None
    except ValueError as exc:
        raise UsageError(f"평가 결과 형식이 올바르지 않아요: {path} ({exc})") from None
    if not isinstance(data, dict) or data.get("kind") != KIND:
        raise UsageError(f"insia eval 결과 폴더가 아니에요: {path}")
    return data


def load_case_file(folder: str | Path, case_id: str) -> dict[str, Any] | None:
    path = Path(folder) / "cases" / f"{case_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The eval run
# ---------------------------------------------------------------------------


def _base_settings(options: EvalOptions) -> Settings:
    try:
        settings = Settings.from_env(mode=options.mode, model=options.model or None, speed=0.0,
                                     max_rounds=options.max_rounds, pass_score=options.pass_score)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    return replace(settings, max_cost_usd=0.0)


def _estimates(planned: list[PlannedCase], base: Settings, options: EvalOptions) -> tuple[list[Any], str]:
    if options.estimate_from:
        try:
            measured = load_measured(options.estimate_from)
        except (OSError, ValueError) as exc:
            raise UsageError(f"--estimate-from을 읽지 못했어요: {exc}") from None
        return [measured_estimate(p.case.id, p.channels, measured, max_rounds=base.max_rounds) for p in planned], \
            f"이전 live 평가의 실측 비용 ({measured['source']})"
    estimates = []
    for p in planned:
        context_chars = len(p.case.brief.model_dump_json()) + (len(p.case.profile.model_dump_json()) if p.case.profile else 0) \
            + min(base.max_document_chars, sum(len(d.text) for d in p.case.documents))
        estimates.append(model_estimate(p.case.id, p.channels, model=base.model, max_rounds=base.max_rounds,
                                        context_chars=context_chars, web_search_max_uses=base.web_search_max_uses,
                                        home=base.home))
    return estimates, "토큰 모델 (프롬프트 파일 크기 + 녹화 샘플 실행의 산출물 크기, 캐시 미반영, ±2~3배)"


def print_plan(planned: list[PlannedCase], base: Settings, options: EvalOptions, printer: Printer) -> dict[str, Any]:
    """Planned runs and (live) the cost estimate. Returns the plan info for summary.json."""
    runs = len(planned) * options.reps
    channel_runs = sum(len(p.channels) for p in planned) * options.reps
    printer(f"평가 계획: 케이스 {len(planned)}개 × 반복 {options.reps}회 = 실행 {runs}번 (채널 {channel_runs}개) · "
            f"{options.mode} 모드 · 모델 {base.model if options.mode == 'live' else 'mock'}")
    # what this run covers: compare only these (case, channel) pairs against a baseline
    info: dict[str, Any] = {"runs": runs, "channel_runs": channel_runs,
                            "scope": [[p.case.id, channel] for p in planned for channel in p.channels]}
    if options.mode != "live":
        printer("mock 모드라 API를 부르지 않아요 (비용 $0). 샘플 브리프는 녹화된 실행을, 나머지는 [데모] 템플릿을 채점해요.")
        return info
    from ..costs import load_prices, price_for

    if price_for(base.model, prices=load_prices(home=base.home)[0]) is None:
        printer(f"주의: {base.model} 모델의 가격을 몰라 추정 비용이 0으로 나와요. 가격을 넣기 전에는 live 평가를 시작하지 않아요 "
                "(prices.json이나 INSIA_PRICE_*로 넣어 주세요).")
    estimates, method = _estimates(planned, base, options)
    typical = sum(e.typical_usd for e in estimates) * options.reps
    maximum = sum(e.max_usd for e in estimates) * options.reps
    per_case = [e.typical_usd for e in estimates]
    printer(f"예상 비용: 약 {_usd(typical)} (수정을 최대 {base.max_rounds}회까지 다 쓰면 {_usd(maximum)}) · "
            f"케이스당 {_usd(min(per_case))}~{_usd(max(per_case))}")
    printer(f"  추정 방법: {method}")
    if options.dry_run:
        for estimate in estimates:
            printer(f"  - {estimate.case_id}: 채널 {estimate.channels}개 · 약 {_usd(estimate.typical_usd)} "
                    f"(최대 {_usd(estimate.max_usd)})")
    printer(f"  계산: Σ(케이스별 추정) × 반복 {options.reps}회. 실제 비용은 첫 live 실행 뒤 summary.json의 cost_usd로 확인하고, "
            "다음부터 --estimate-from <그 결과 폴더>로 실측 기반 추정을 쓰세요.")
    if options.max_cost_usd:
        printer(f"예산 상한: {_usd(options.max_cost_usd)} — 넘으면 새 호출을 멈추고 남은 케이스는 건너뛰어요.")
        if typical > options.max_cost_usd:
            printer("주의: 예상 비용이 상한보다 커서 중간에 멈출 가능성이 높아요. --case로 케이스를 줄이거나 상한을 올려 주세요.")
    info.update({"estimate_usd": round(typical, 4), "estimate_max_usd": round(maximum, 4), "estimate_method": method,
                 "estimates": [e.as_dict() for e in estimates]})
    return info


def _check_live(options: EvalOptions, *, injected: bool) -> None:
    if options.mode != "live" or options.dry_run:
        return
    if not options.max_cost_usd or options.max_cost_usd <= 0:
        raise UsageError("live 평가는 돈이 들어서 --max-cost-usd(전체 예산 상한, 달러)가 꼭 필요해요. "
                         "먼저 --dry-run으로 예상 비용을 확인해 보세요.")
    base = _base_settings(options)
    unknown = unpriced_models(base, options)
    if unknown:
        from ..costs import ENV_PREFIX, env_key

        names = ", ".join(unknown)
        raise UsageError(f"{names} 모델의 가격을 몰라서 live 평가를 시작하지 않았어요. 가격을 모르면 비용이 $0으로 기록돼 "
                         f"--max-cost-usd 상한이 작동하지 않아요. 워크스페이스의 prices.json이나 "
                         f"{ENV_PREFIX}{env_key(unknown[0])}_INPUT/_OUTPUT(백만 토큰당 달러)으로 가격을 넣어 주세요.")
    if not injected and not has_credentials():
        raise CommandError("API 자격 증명을 찾지 못해 live 평가를 시작하지 않았어요. ANTHROPIC_API_KEY를 설정하거나 "
                           "`ant auth login`을 실행해 주세요. 연습은 --mode mock으로 할 수 있어요.")


def _validate(options: EvalOptions) -> None:
    if options.mode not in ("mock", "live"):
        raise UsageError("--mode는 mock 또는 live예요")
    if options.reps < 1 or options.reps > 10:
        raise UsageError("--reps는 1~10 사이로 적어 주세요")
    if options.max_score_drop < 0:
        raise UsageError("--max-score-drop은 0 이상이어야 해요")
    unknown = [c for c in options.channels if c not in ALL_CHANNELS]
    if unknown:
        raise UsageError(f"알 수 없는 채널이에요: {', '.join(unknown)} (가능: {', '.join(ALL_CHANNELS)})")
    if options.judge:
        if options.mode != "live":
            raise UsageError("--judge(LLM 비교 채점)는 live 모드에서만 써요. mock 결과는 판정할 게 없어요.")
        if not options.baseline:
            raise UsageError("--judge에는 비교할 기준 결과 폴더(--baseline)가 필요해요")
    if options.resume and not options.out_dir:
        raise UsageError("--resume에는 이어서 쓸 결과 폴더(--out)가 필요해요")
    if not options.timeout_s or options.timeout_s <= 0 or options.timeout_s != options.timeout_s:
        raise UsageError("--timeout-s는 0보다 큰 초로 적어 주세요")


@dataclass
class PriorCase:
    """What an earlier attempt left in ``cases/<id>.json`` (``--resume``)."""

    reusable: dict[int, dict[str, Any]]  # rep → finished record for the same case, channels, mode and settings
    records: dict[int, dict[str, Any]]  # rep → any record (its cost was spent either way)
    discarded_cost_usd: float  # spent on attempts that an earlier resume already replaced
    changed_settings: list[str] = field(default_factory=list)  # why finished records are not reused (labels)
    channels: list[str] = field(default_factory=list)  # the channels the folder ran this case with
    comparable: bool = False  # same case file, mode and settings: its results are valid now (channels aside)
    grader: str = ""  # the grader code its records were graded with
    regraded: int = 0  # reusable records graded again with the current grader code
    regrade_failed: int = 0  # reusable records whose saved run could not be read back (run again)

    @property
    def spent(self) -> float:
        return self.discarded_cost_usd + sum(_record_cost(r) for r in self.records.values())


def _record_cost(record: dict[str, Any]) -> float:
    return float(record.get("cost_usd") or 0.0) + float(record.get("judge_cost_usd") or 0.0)


def load_prior(out_root: Path, planned: PlannedCase, mode: str, settings: dict[str, Any] | None = None) -> PriorCase:
    """``settings`` (``run_settings``): a record made with another model, pass score, effort, prompt files or run code
    is never reused (an old case file without settings counts as different)."""
    data = load_case_file(out_root, planned.case.id) or {}
    records = {int(r["rep"]): r for r in data.get("reps", []) if isinstance(r, dict) and isinstance(r.get("rep"), int)}
    channels = [str(c) for c in data.get("channels") or []]
    same_case = bool(data) and data.get("mode") == mode and data.get("case") == planned.case.model_dump(mode="json")
    changed = settings_diff(data.get("settings"), settings) if settings is not None and same_case else []
    comparable = same_case and not changed
    same = comparable and channels == list(planned.channels)
    reusable = {rep: r for rep, r in records.items()
                if same and r.get("status") == "ok" and [g.get("channel") for g in r.get("channels", [])] == planned.channels}
    return PriorCase(reusable=reusable, records=records, discarded_cost_usd=float(data.get("discarded_cost_usd") or 0.0),
                     changed_settings=changed if records else [], channels=channels, comparable=comparable,
                     grader=str(data.get("grader") or ""))


def _case_documents(case: EvalCase) -> list[Any]:
    """The case's documents as the run saw them (a fresh workspace numbers them u1, u2, …)."""
    from types import SimpleNamespace

    return [SimpleNamespace(id=f"u{i}", title=doc.title, text=doc.text) for i, doc in enumerate(case.documents, 1)]


def regrade_record(case: EvalCase, channels: Sequence[str], record: dict[str, Any], mode: str,
                   out_root: Path) -> dict[str, Any] | None:
    """Grade a saved record again with the current grader code, from its saved run (``runs/<id>/result.json``): no API
    call. The reviewer's score and issues, the judge verdict and the cost stay as they were. A record that produced
    nothing gradable comes back unchanged; ``None`` when the saved run cannot be read back."""
    from ..storage import load_result

    if record.get("status") not in OK_STATUSES:
        return record
    try:
        result = load_result(out_root / str(record.get("run_dir") or "-"))
    except (OSError, ValueError):
        return None
    graded = _grade_result(case, channels, result, _case_documents(case), mode)
    fresh_channels: list[dict[str, Any]] = []
    for old in record.get("channels", []):
        if old.get("status") != "ok":
            fresh_channels.append(old)
            continue
        grade = graded.get(old.get("channel"))
        if grade is None:
            return None
        for key in ("judge", "judge_error", "judge_skipped"):
            if key in old:
                grade[key] = old[key]
        fresh_channels.append(grade)
    fresh = {**record, "channels": fresh_channels, "regraded_at": _iso_now()}
    for kind in ("must", "should"):
        fresh[kind] = [evaluate_case_level(a, cost_usd=record.get("cost_usd") or 0.0,
                                           duration_s=record.get("duration_s") or 0.0)
                       for a in case.assertions(kind) if a.applies("*", mode)]
    return fresh


def _regrade_prior(prior: PriorCase, planned: PlannedCase, mode: str, out_root: Path, grader: str) -> None:
    """Reusable records graded by other grader code are graded again (free); one that cannot be is run again."""
    if not prior.reusable or prior.grader == grader:
        return
    for rep, record in list(prior.reusable.items()):
        fresh = regrade_record(planned.case, planned.channels, record, mode, out_root)
        if fresh is None:
            del prior.reusable[rep]
            prior.regrade_failed += 1
        else:
            prior.reusable[rep] = prior.records[rep] = fresh
            prior.regraded += 1


@dataclass
class _CaseRun:
    """One planned case while it runs: its records so far and the entry last written to ``cases/<id>.json``."""

    planned: PlannedCase
    records: list[dict[str, Any]]
    prior: PriorCase | None = None
    entry: dict[str, Any] | None = None
    done: bool = False
    kept: bool = False  # not reached before a Ctrl+C: only its earlier finished runs are in the summary

    def file_records(self, reps: int) -> tuple[list[dict[str, Any]], float]:
        """(records, discarded cost) for ``cases/<id>.json``: this attempt's records, plus the earlier reusable records
        of reps this attempt has not reached yet (so a Ctrl+C or crash mid-case never throws a finished run away).
        Every other earlier record's cost moves to the discarded cost, so each cent is in the file exactly once."""
        records = list(self.records)
        if self.prior is None:
            return records, 0.0
        discarded = self.prior.discarded_cost_usd
        reached = {r.get("rep") for r in records}
        for rep, old in sorted(self.prior.records.items()):
            if any(r is old for r in records):
                continue  # reused as is
            if rep not in reached and rep <= reps and self.prior.reusable.get(rep) is old:
                records.append(old)
                continue
            discarded += _record_cost(old)
        return sorted(records, key=lambda r: r.get("rep") or 0), discarded


def _carried_results(out_root: Path, planned: list[PlannedCase], mode: str, settings: dict[str, Any],
                     grader: str) -> tuple[list[tuple[dict[str, Any], list[str]]], list[str]]:
    """``--resume`` with fewer cases than the folder has: the other cases' saved results (same mode and settings) stay in
    the summary — graded again from their saved runs when the grader code changed. Returns ([(entry, channels)], ids
    left out because their mode or settings differ or their saved runs cannot be graded again)."""
    planned_ids = {p.case.id for p in planned}
    carried: list[tuple[dict[str, Any], list[str]]] = []
    left_out: list[str] = []
    for path in sorted((out_root / "cases").glob("*.json")):
        if path.stem in planned_ids:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            left_out.append(path.stem)
            continue
        result = data.get("result") if isinstance(data, dict) else None
        if not (isinstance(result, dict) and data.get("mode") == mode and data.get("settings") == settings):
            left_out.append(path.stem)
            continue
        channels = [str(c) for c in data.get("channels") or [c.get("channel") for c in result.get("channels", [])]]
        if data.get("grader") != grader:
            try:
                other = PlannedCase(EvalCase.model_validate(data["case"]), channels)
            except Exception:  # noqa: BLE001 - an unreadable old case file is simply left out
                left_out.append(path.stem)
                continue
            records = [regrade_record(other.case, channels, r, mode, out_root) for r in data.get("reps", [])
                       if isinstance(r, dict)]
            if not records or any(r is None for r in records):
                left_out.append(path.stem)
                continue
            discarded = float(data.get("discarded_cost_usd") or 0.0)
            result = aggregate_case(other, records)  # type: ignore[arg-type]
            result["discarded_cost_usd"] = round(discarded, 6)
            write_case_file(out_root, other, records, result, mode=mode, discarded_cost_usd=discarded,  # type: ignore[arg-type]
                            settings=settings, grader=grader)
        carried.append(({**result, "carried": True}, channels))
    return carried, left_out


def _dropped_channels(planned: list[PlannedCase], priors: dict[str, PriorCase]) -> list[tuple[PlannedCase, PriorCase, list[str]]]:
    """Planned cases whose earlier run in this folder covered channels this ``--resume`` leaves out."""
    dropped = []
    for p in planned:
        prior = priors.get(p.case.id)
        if prior is None or not prior.records:
            continue
        missing = [c for c in prior.channels if c not in p.channels]
        if missing:
            dropped.append((p, prior, missing))
    return dropped


def _refuse_narrower_channels(dropped: list[tuple[PlannedCase, PriorCase, list[str]]]) -> None:
    """A narrower ``--channels`` on resume would re-run the case without those channels and silently drop their valid
    results from the summary (and from a comparison against it): refuse, before anything runs."""
    valid = [(p, prior, missing) for p, prior, missing in dropped if prior.comparable]
    if not valid:
        return
    cases = "; ".join(f"{p.case.id} (이전 {', '.join(prior.channels)} → 이번 {', '.join(p.channels)}, 빠지는 채널 "
                      f"{', '.join(missing)})" for p, prior, missing in valid)
    raise UsageError(f"--resume에서 채널을 줄이면 이 폴더에 있는 그 채널의 결과가 요약과 기준 비교에서 빠져요: {cases}. "
                     "이전과 같은 --channels로 이어서 하거나(끝난 실행은 다시 돌리지 않아요) 새 --out 폴더를 쓰세요.")


def run_eval(options: EvalOptions, *, printer: Printer = print, confirm: Callable[[str], bool] | None = None,
             client: Any = None, backend_factory: Callable[[Settings], Any] | None = None) -> dict[str, Any]:
    """Run the eval and write the result folder. Returns the summary (``summary["exit_code"]``: 0 ok, 1 failures)."""
    from .compare import compare_summaries
    from .report import render_report

    _validate(options)
    injected = client is not None or backend_factory is not None
    _check_live(options, injected=injected)
    cases_dir = find_cases_dir(options.cases_dir)
    from .cases import CaseError

    try:
        cases = load_cases(cases_dir, options.case_ids or None)
    except CaseError as exc:
        raise UsageError(str(exc)) from None
    planned, skipped = plan_cases(cases, options.channels)
    if not planned:
        raise UsageError("고른 채널로 돌릴 케이스가 없어요 (--channels를 확인해 주세요)")
    baseline = load_summary(options.baseline) if options.baseline else None
    base = _base_settings(options)
    out_root = Path(options.out_dir) if options.out_dir else default_out_dir(cases_dir, options.mode)
    if not options.dry_run and out_root.exists() and any(out_root.iterdir()) and not options.resume:
        raise UsageError(f"결과 폴더가 이미 있어요: {out_root} (다른 --out을 쓰거나, 이어서 하려면 --resume)")
    settings_now = run_settings(base, options)
    grader_now = grader_fingerprint()
    priors = {p.case.id: load_prior(out_root, p, options.mode, settings_now) for p in planned} if options.resume else {}
    dropped = _dropped_channels(planned, priors)
    _refuse_narrower_channels(dropped)  # before anything is printed, asked or spent
    for case_id in skipped:
        printer(f"건너뜀: {case_id} (고른 채널이 이 케이스에 없어요)")
    plan_info = print_plan(planned, base, options, printer)
    if options.dry_run:
        printer("--dry-run이라 여기서 멈춰요. 실제로 돌리려면 --dry-run을 빼세요" +
                (" (live는 --max-cost-usd 필요)." if options.mode == "live" else "."))
        return {"dry_run": True, "plan": plan_info, "exit_code": 0}
    if options.mode == "live" and not options.assume_yes and confirm is not None:
        if not confirm(f"live 평가를 시작할까요? 최대 {_usd(options.max_cost_usd or 0)}까지 쓸 수 있어요. [y/N] "):
            printer("시작하지 않았어요.")
            return {"cancelled": True, "plan": plan_info, "exit_code": 1}

    out_root.mkdir(parents=True, exist_ok=True)
    printer(f"결과 폴더: {out_root}")
    # the folder's spend ledger: the cap is always --max-cost-usd minus everything this folder has spent
    ledger = SpendLedger(out_root / SPEND_FILE if options.mode == "live" else None)
    if options.resume:
        ledger.seed_from_case_files(out_root)

    judge_backend = None
    judge_collector = UsageCollector(ledger=ledger, tags={"kind": "judge"})
    if options.judge:
        from ..backends.anthropic_backend import AnthropicBackend
        from .judge import DEFAULT_JUDGE_MODEL

        judge_settings = replace(base, model=options.judge_model or DEFAULT_JUDGE_MODEL, fallbacks=False)
        judge_backend = AnthropicBackend(judge_settings, client=client)
        judge_backend.on_usage = judge_collector
        printer(f"LLM 비교 채점(돈이 들어요): {judge_backend.model}가 이번 결과와 기준 결과의 최종본을 비교해요.")

    started_at = _iso_now()
    for p in planned:  # finished runs graded by other grader code: grade their saved runs again (no API call)
        if p.case.id in priors:
            _regrade_prior(priors[p.case.id], p, options.mode, out_root, grader_now)
    prior_cost = ledger.total if options.resume else 0.0
    reused_runs = sum(1 for p in planned for rep in range(1, options.reps + 1)
                      if rep in priors.get(p.case.id, PriorCase({}, {}, 0.0)).reusable)
    carried, left_out = _carried_results(out_root, planned, options.mode, settings_now, grader_now) \
        if options.resume else ([], [])
    if options.resume:
        printer(f"이어서 실행: 끝난 실행 {reused_runs}번을 다시 써요" +
                (f" · 이 폴더에서 이미 쓴 비용 {_usd(prior_cost)}을 예산에 넣어요" if prior_cost else ""))
        for p in planned:
            prior = priors[p.case.id]
            if prior.changed_settings:
                printer(f"  다시 돌려요: {p.case.id} — 이전 실행과 달라진 설정: {', '.join(prior.changed_settings)}")
            if prior.regraded:
                printer(f"  다시 채점해요: {p.case.id} — 채점 코드가 바뀌어서 끝난 실행 {prior.regraded}번을 저장된 결과로 "
                        "다시 채점해요 (API 호출 없음)")
            if prior.regrade_failed:
                printer(f"  다시 돌려요: {p.case.id} — 저장된 실행 결과(runs/)를 읽지 못해 새 채점 코드로 채점할 수 없어요")
        for p, _prior, missing in dropped:  # (with the same settings this was refused above)
            printer(f"  요약에서 빠지는 이전 채널: {p.case.id}의 {', '.join(missing)} (모드·설정·케이스가 달라 다시 돌리지 않아요)")
        if carried:
            printer(f"  이번에 고르지 않은 케이스 {len(carried)}개는 이전 결과를 요약에 그대로 넣어요: "
                    + ", ".join(e["id"] for e, _ in carried))
        if left_out:
            printer(f"  모드·설정이 다르거나 저장된 실행을 다시 채점할 수 없어 요약에 넣지 않은 이전 결과 {len(left_out)}개: "
                    f"{', '.join(left_out)} (쓴 비용은 예산에 넣었어요)")
    if carried:
        plan_info["scope"] = list(plan_info.get("scope", [])) + [[e["id"], ch] for e, channels in carried for ch in channels]
        plan_info["carried_cases"] = [e["id"] for e, _ in carried]

    def save(state: _CaseRun) -> None:
        records, discarded = state.file_records(options.reps)
        if not records:
            return  # nothing to write yet (an earlier attempt's file, if any, stays as it was)
        entry = aggregate_case(state.planned, records)
        entry["discarded_cost_usd"] = round(discarded, 6)
        if state.kept:
            entry["carried"] = True
        write_case_file(out_root, state.planned, records, entry, mode=options.mode, discarded_cost_usd=discarded,
                        settings=settings_now, grader=grader_now)
        state.entry = entry

    def await_late_calls() -> str:
        """Before a new live run: a timed-out run's call still in flight will be billed, and the next run's cap cannot
        include what it does not know. Wait for it (up to --timeout-s); if it is still running, stop the eval."""
        printer(f"  시간 초과로 멈춘 실행의 마지막 호출이 끝나기를 최대 {options.timeout_s:g}초 기다려요 — 그 비용을 모른 채 "
                "다음 실행을 시작하면 예산 상한을 넘을 수 있어요.")
        finished = _wait_late(states, options.timeout_s)
        for changed_state in _refresh_late(states):
            save(changed_state)
        if finished:
            return ""
        return ("시간 초과로 멈춘 실행의 마지막 호출이 끝나지 않아 쓴 비용을 알 수 없어서, 예산 상한을 지키려고 남은 케이스를 "
                "건너뛰었어요. 잠시 뒤 --resume으로 이어서 하세요")

    states: list[_CaseRun] = []
    state: _CaseRun | None = None
    stop_reason = ""
    interrupted = False
    total = len(planned)
    try:
        for index, p in enumerate(planned, 1):
            prior = priors.get(p.case.id)
            state = _CaseRun(p, [], prior=prior)
            states.append(state)
            for rep in range(1, options.reps + 1):
                reused = prior.reusable.get(rep) if prior else None
                if reused is not None:
                    state.records.append(reused)
                    continue
                cap = None
                if options.mode == "live" and options.max_cost_usd:
                    if not stop_reason and _late_running(states):
                        stop_reason = await_late_calls()
                    cap = options.max_cost_usd - ledger.total  # late calls of timed-out runs included
                    if cap <= 0 or stop_reason:
                        stop_reason = stop_reason or f"예산 상한 {_usd(options.max_cost_usd)}에 닿아 남은 케이스를 건너뛰었어요"
                        state.records.append(_skipped_record(p, rep, options, stop_reason))
                        continue
                label = f"[{index}/{total}] {p.case.id}" + (f" (반복 {rep}/{options.reps})" if options.reps > 1 else "")
                printer(f"{label} · {p.case.title}")
                record = run_case(p, rep, base, options, out_root, remaining_cap=cap, client=client,
                                  backend_factory=backend_factory, printer=printer, reps=options.reps, ledger=ledger)
                state.records.append(record)  # before the judge: a Ctrl+C during the judge still keeps this run
                if judge_backend is not None and record["status"] in OK_STATUSES:
                    _judge_record(judge_backend, judge_collector, p, record, options, ledger, out_root)
                printer("  " + _record_line(record))
                if record["status"] == "budget_stopped":
                    stop_reason = f"예산 상한 {_usd(options.max_cost_usd or 0)}에 닿아 멈췄어요"
                if record.get("unpriced_models") and options.mode == "live":
                    stop_reason = (f"가격을 모르는 모델({', '.join(record['unpriced_models'])})이 응답해서 비용을 셀 수 없어 "
                                   "멈췄어요. prices.json이나 INSIA_PRICE_*로 가격을 넣은 뒤 --resume으로 이어서 하세요")
                save(state)  # after every rep: a finished rep survives a Ctrl+C during the next one
            save(state)
            state.done = True
    except KeyboardInterrupt as exc:
        interrupted = True
        partial = getattr(exc, "eval_record", None)
        if state is not None and not state.done:
            if partial is not None and not any(r is partial for r in state.records):
                state.records.append(partial)
                printer("  " + _record_line(partial))
            save(state)  # also keeps the earlier finished reps this attempt had not reached
        printer("\n평가를 중단했어요. 끝난 실행과 중단한 실행까지 결과를 저장해요 (쓴 비용은 spend.jsonl에 모두 남아요).")
        started = {s.planned.case.id for s in states}
        for p in planned:  # cases not reached: their earlier finished runs stay in the summary
            prior = priors.get(p.case.id)
            if p.case.id not in started and prior is not None and prior.reusable:
                kept = _CaseRun(p, [], prior=prior, kept=True)
                save(kept)
                if kept.entry is not None:
                    states.append(kept)

    # calls still in flight after a timeout: wait a little so their cost lands in the records (a Ctrl+C during this
    # wait stops waiting — the summary and report are still written)
    wait_s = 0.0 if interrupted else min(LATE_WAIT_S, options.timeout_s)
    try:
        if wait_s > 0 and _late_running(states):
            printer(f"시간 초과로 멈춘 실행의 마지막 호출이 끝나기를 최대 {wait_s:g}초 기다려요 (그 비용까지 기록하려고요).")
            _wait_late(states, wait_s)
    except KeyboardInterrupt:
        interrupted = True
        printer("\n기다리지 않고 멈췄어요. 끝나지 않은 호출 비용은 기록에 빠져 있어요. 결과는 저장해요.")
    for changed_state in _refresh_late(states):
        save(changed_state)
    entries = [s.entry for s in states if s.entry is not None] + [entry for entry, _ in carried]
    kept_ids = [s.planned.case.id for s in states if s.kept and s.entry is not None]

    meta = {
        "created_at": started_at,
        "finished_at": _iso_now(),
        "mode": options.mode,
        "model": base.model if options.mode == "live" else "mock",
        "cases_dir": str(cases_dir),
        "out_dir": str(out_root),
        "reps": options.reps,
        "channels_filter": list(options.channels),
        "settings": {**{k: v for k, v in settings_now.items() if k != "model"}, "grader": grader_now,
                     "max_cost_usd": options.max_cost_usd or 0.0, "timeout_s": options.timeout_s},
        "plan": plan_info,
        "stopped": stop_reason,
        "interrupted": interrupted,
    }
    if options.resume:
        meta["resumed"] = {"reused_runs": reused_runs, "prior_cost_usd": prior_cost}
        regraded = sum(prior.regraded for prior in priors.values())
        if regraded:
            meta["resumed"]["regraded_runs"] = regraded
    if carried or left_out or kept_ids or dropped:
        meta["carried"] = {"cases": [e["id"] for e, _ in carried] + kept_ids, "left_out": left_out}
        if dropped:
            meta["carried"]["dropped_channels"] = [[p.case.id, c] for p, _prior, missing in dropped for c in missing]
    summary = build_summary(entries, meta)
    t = summary["totals"]
    # everything this folder has spent (spend.jsonl): also calls that finished after a timeout or Ctrl+C, and earlier
    # results this summary does not list
    t["folder_cost_usd"] = ledger.total
    t["unlisted_cost_usd"] = round(max(0.0, ledger.total - t["cost_usd"] - t["judge_cost_usd"] - t["discarded_cost_usd"]), 6)
    t["cost_incomplete_runs"] = sum(1 for s in states for r in s.records if r.get("cost_incomplete"))
    comparison = None
    if baseline is not None:
        comparison = compare_summaries(baseline, summary, max_score_drop=options.max_score_drop,
                                       baseline_dir=str(options.baseline), current_dir=str(out_root))
        summary["baseline"] = comparison
    failed = t["must_failed"] or t["must_errors"] or t["error_channels"]
    regressed = comparison is not None and comparison["exit_code"] != 0
    summary["exit_code"] = 1 if (failed or regressed or interrupted or stop_reason) else 0
    _write_json(out_root / "summary.json", summary)
    (out_root / "report.md").write_text(render_report(summary), encoding="utf-8")
    listed = t["cost_usd"] + t["judge_cost_usd"]
    folder = f" (이 폴더 전체 {_usd(t['folder_cost_usd'])})" if t["folder_cost_usd"] > listed + 0.005 else ""
    printer(f"\n필수 조건 {t['must_passed']}/{t['must_total']} 통과 · 권장 목표 {t['should_passed']}/{t['should_total']} · "
            f"오류 채널 {t['error_channels']}개 · 비용 {_usd(listed)}{folder}")
    if comparison is not None:
        printer(f"기준 대비: {comparison['headline']}")
    printer(f"보고서: {out_root / 'report.md'}")
    if interrupted:
        raise KeyboardInterrupt
    return summary


def _skipped_record(p: PlannedCase, rep: int, options: EvalOptions, reason: str) -> dict[str, Any]:
    record: dict[str, Any] = {"rep": rep, "run_id": p.case.id, "status": "skipped", "failure": _failure("budget", reason),
                              "model": "", "served_models": [], "model_mismatch": [], "cost_usd": 0.0,
                              "judge_cost_usd": 0.0, "usage": {}, "duration_s": 0.0, "wall_s": 0.0, "channels": [],
                              "must": [], "should": [], "run_dir": ""}
    for channel in p.channels:
        entry = {"channel": channel, "status": "skipped", "error": reason}
        for kind in ("must", "should"):
            entry[kind] = [not_evaluated(a, reason) for a in p.case.assertions(kind) if a.applies(channel, options.mode)]
        record["channels"].append(entry)
    for kind in ("must", "should"):
        record[kind] = [not_evaluated(a, reason) for a in p.case.assertions(kind) if a.applies("*", options.mode)]
    return record


def _judge_record(backend: Any, collector: UsageCollector, p: PlannedCase, record: dict[str, Any], options: EvalOptions,
                  ledger: SpendLedger, out_root: Path) -> None:
    """Optional paid pairwise judge against the baseline's final drafts (live only; its usage goes to ``ledger`` too)."""
    from ..models import Draft, ResearchPack
    from .judge import pairwise

    baseline = load_case_file(options.baseline, p.case.id) if options.baseline else None  # type: ignore[arg-type]
    if not baseline:
        return
    base_drafts = {}
    for rep in baseline.get("reps", []):
        for g in rep.get("channels", []):
            if g.get("status") == "ok" and g.get("draft"):
                base_drafts.setdefault(g["channel"], Draft.model_validate(g["draft"]))
    research = None
    research_path = out_root / record["run_dir"] / "research.json"
    try:
        research = ResearchPack.model_validate_json(research_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        research = None
    before = collector.cost
    for grade in record["channels"]:
        if grade.get("status") != "ok" or grade["channel"] not in base_drafts:
            continue
        if options.max_cost_usd and ledger.total >= options.max_cost_usd:
            grade["judge_skipped"] = "예산 상한에 닿아 LLM 비교 채점을 건너뛰었어요"
            continue
        try:
            grade["judge"] = pairwise(backend, case_id=p.case.id, channel=grade["channel"],
                                      brief=p.case.brief.model_dump(mode="json"), research=research,
                                      current=Draft.model_validate(grade["draft"]), baseline=base_drafts[grade["channel"]],
                                      rep=record["rep"])
        except BackendError as exc:
            grade["judge_error"] = str(exc)
    record["judge_cost_usd"] = round(collector.cost - before, 6)


def _record_line(record: dict[str, Any]) -> str:
    from ..agents.common import channel_label

    parts = []
    for g in record["channels"]:
        name = channel_label(g["channel"])
        if g.get("status") != "ok":
            parts.append(f"{name} {'중단' if g.get('status') == 'cancelled' else '오류'}")
            continue
        musts = g.get("must", [])
        ok = sum(1 for o in musts if o["passed"])
        parts.append(f"{name} {g['score']}점{' 통과' if g['passed'] else ' 미통과'} 필수 {ok}/{len(musts)}")
    head = {"ok": "완료", "partial": "일부 완료", "budget_stopped": "예산 상한으로 멈춤", "timeout": "시간 초과",
            "error": "실패", "skipped": "건너뜀", "cancelled": "중단"}.get(record["status"], record["status"])
    tail = f" · 비용 {_usd(record['cost_usd'])}" if record["cost_usd"] else ""
    reason = f" ({record['failure']['message']})" if record.get("failure") else ""
    return f"{head}{reason} · " + " · ".join(parts) + tail


def ask_yes_no(prompt: str) -> bool:
    """Interactive confirmation (only on a terminal).

    The question goes to stderr, like the progress lines under ``--json``: with ``--json > out.json`` it is still on the
    screen, and stdout stays pure JSON (``input(prompt)`` would write it to stdout)."""
    if not sys.stdin.isatty():
        return True
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return input().strip().lower() in ("y", "yes", "예", "네", "ㅇ")
    except EOFError:
        return False
