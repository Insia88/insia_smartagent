"""Command line: ``insia run | serve | sample-brief | check``."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from . import __version__
from .agents.common import STATUS_LABELS, channel_label
from .backends.base import BackendError
from .channels import check_format
from .config import Settings, load_sample_brief
from .models import ALL_CHANNELS, Brief, Draft

AGENT_NAMES = {"orchestrator": "총괄", "researcher": "리서치", "reviewer": "검수", "system": "시스템"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------------------
# Progress printer
# ---------------------------------------------------------------------------


def _mmss(t: float) -> str:
    total = int(t)
    return f"{total // 60:02d}:{total % 60:02d}"


def describe_event(event: dict[str, Any]) -> str | None:
    """One Korean progress line for an event (``None`` = not worth printing)."""
    kind, data = event["type"], event["data"]
    if kind == "run.started":
        names = ", ".join(channel_label(c) for c in data["channels"])
        return f"실행 시작 · {data['mode']} 모드 · 모델 {data['model']} · 채널: {names}"
    if kind == "agent.status":
        if data["status"] in ("waiting", "idle"):
            return None
        return f"[{STATUS_LABELS.get(data['status'], data['status'])}] {data['message']}"
    if kind == "handoff":
        return f"→ {AGENT_NAMES[data['to']]}: {data['label']}"
    if kind == "plan.created":
        return f"계획 완료 · 핵심 메시지 {len(data['key_messages'])}개 · 리서치 질문 {len(data['questions'])}개"
    if kind == "research.query":
        return f"검색 ({data.get('question_id') or '-'}): {data['query']}"
    if kind == "research.source":
        s = data["source"]
        return f"출처 [{s['id']}] Tier {s['tier']} · {s.get('publisher') or '-'} · {s['title']}"
    if kind == "research.finding":
        f = data["finding"]
        claim = f["claim"] if len(f["claim"]) <= 70 else f["claim"][:69] + "…"
        return f"근거 [{f['id']}] {claim}"
    if kind == "research.completed":
        prefix = "추가 조사 완료" if data.get("followup") else "리서치 완료"
        return f"{prefix} · 근거 {data['findings']}건 · 출처 {data['sources']}곳 · 빈틈 {len(data['gaps'])}건"
    if kind == "draft.created":
        return (f"{channel_label(data['channel'])} R{data['round']} 초안 · {data['chars']:,}자 "
                f"(공백 제외 {data['chars_no_space']:,}자)")
    if kind == "review.completed":
        failed = [c["label"] for c in data["format_checks"] if not c["passed"]]
        verdict = "통과" if data["passed"] else "미통과"
        extra = f" · 형식 미충족: {', '.join(failed)}" if failed else ""
        return f"{channel_label(data['channel'])} R{data['round']} 검수 {data['score']}점 · {verdict}{extra}"
    if kind == "revision.requested":
        return f"{channel_label(data['channel'])} 수정 요청 {data['issues']}건 · {data['top_issue']}"
    if kind == "channel.completed":
        verdict = "통과" if data["passed"] else "미통과"
        return f"{channel_label(data['channel'])} 완료 · {data['score']}점 · {verdict} · 수정 {data['rounds']}회"
    if kind == "run.completed":
        where = f" · 결과: {data['output_dir']}" if data.get("output_dir") else ""
        return f"실행 완료 · {data['duration_s']}초{where}"
    if kind == "run.failed":
        return f"실행 실패 · {data['error']}"
    if kind == "log":
        prefix = {"warn": "주의: ", "error": "오류: "}.get(data.get("level", "info"), "")
        return prefix + data["message"]
    return None


class ProgressPrinter:
    def __init__(self, stream=None, quiet: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        if self.quiet and event["type"] not in ("run.completed", "run.failed", "channel.completed"):
            return
        line = describe_event(event)
        if line is None:
            return
        agent = AGENT_NAMES.get(event["agent"], event["agent"])
        with self._lock:
            print(f"[{_mmss(event['t'])}] {agent:<3} {line}", file=self.stream, flush=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_brief(args: argparse.Namespace) -> Brief:
    if args.brief:
        path = Path(args.brief)
        try:
            brief = Brief.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise UsageError(f"브리프 파일을 읽을 수 없어요: {path} ({exc.strerror})") from None
        except ValidationError as exc:
            raise UsageError(f"브리프 파일 형식이 올바르지 않아요: {path} ({exc.error_count()}개 오류)") from None
        updates: dict[str, Any] = {}
    else:
        if not args.topic:
            raise UsageError("--brief 파일이나 --topic을 지정해 주세요")
        brief = Brief(topic=args.topic)
        updates = {}
    for field in ("topic", "goal", "audience", "tone", "notes"):
        value = getattr(args, field, None)
        if value:
            updates[field] = value
    if args.channels:
        channels = _split(args.channels)
        unknown = [c for c in channels if c not in ALL_CHANNELS]
        if unknown:
            raise UsageError(f"알 수 없는 채널: {', '.join(unknown)} (가능: {', '.join(ALL_CHANNELS)})")
        updates["channels"] = channels
    if args.keywords:
        updates["keywords"] = _split(args.keywords)
    brief = brief.model_copy(update=updates) if updates else brief
    brief = Brief.model_validate(brief.model_dump())
    if not brief.topic.strip():
        raise UsageError("주제(topic)가 비어 있어요")
    if not brief.channels:
        raise UsageError("채널을 하나 이상 지정해 주세요")
    return brief


def _settings_from_args(args: argparse.Namespace, **extra: Any) -> Settings:
    overrides: dict[str, Any] = {
        "mode": getattr(args, "mode", None),
        "model": getattr(args, "model", None),
        "speed": getattr(args, "speed", None),
        "max_rounds": getattr(args, "max_rounds", None),
        "pass_score": getattr(args, "pass_score", None),
        **extra,
    }
    if getattr(args, "out", None):
        overrides["out_dir"] = Path(args.out)
    if getattr(args, "no_fallbacks", False):
        overrides["fallbacks"] = False
    try:
        settings = Settings.from_env(**overrides)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if getattr(args, "no_save", False):
        from dataclasses import replace

        settings = replace(settings, out_dir=None)
    return settings


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import execute_run

    brief = build_brief(args)
    settings = _settings_from_args(args)
    printer = ProgressPrinter(quiet=args.quiet)
    try:
        result, bus = execute_run(brief, settings, listener=printer)
    except BackendError as exc:
        print(f"실행하지 못했어요: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n실행을 중단했어요.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - report instead of a traceback
        print(f"실행하지 못했어요: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.record:
        record_path = Path(args.record)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "title": f"INSIA 실행 기록 — {brief.topic[:40]}",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "mode": result.mode,
            "model": result.model,
            "brief": brief.model_dump(mode="json"),
        }
        record_path.write_text(json.dumps(bus.to_trace(meta), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"트레이스 저장: {record_path} (이벤트 {len(bus)}개)")

    print("\n채널별 결과")
    out_dir = settings.out_dir / result.run_id if settings.out_dir is not None else None
    for item in result.results:
        score = next((r.score for r in item.reviews if r.round == item.final.round), 0)
        verdict = "통과" if item.passed else "미통과"
        where = f"  {out_dir / (item.channel + '.md')}" if out_dir is not None else ""
        print(f"- {channel_label(item.channel)}: {score}점 · {verdict} · 수정 {item.rounds}회{where}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    settings = _settings_from_args(args)
    try:
        serve(settings, host=args.host, port=args.port, web_dir=args.web_dir, quiet=not args.verbose)
    except OSError as exc:
        print(f"서버를 시작하지 못했어요 ({args.host}:{args.port}): {exc.strerror or exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sample_brief(args: argparse.Namespace) -> int:
    brief = load_sample_brief(Settings.from_env())
    print(json.dumps(brief.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def _find_brief_near(path: Path) -> Path | None:
    for folder in (path.parent, path.parent.parent, path.parent.parent.parent):
        candidate = folder / "brief.json"
        if candidate.is_file():
            return candidate
    return None


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.draft)
    try:
        draft = Draft.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"초안 파일을 읽을 수 없어요: {path} ({exc.strerror})") from None
    except ValidationError as exc:
        raise UsageError(f"Draft JSON 형식이 아니에요: {path} ({exc.error_count()}개 오류)") from None
    brief_path = Path(args.brief) if args.brief else _find_brief_near(path)
    brief = None
    if brief_path is not None:
        try:
            brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            raise UsageError(f"브리프 파일을 읽을 수 없어요: {brief_path}") from None
    checks = check_format(draft, brief)
    if args.json:
        print(json.dumps([c.model_dump() for c in checks], ensure_ascii=False, indent=1))
    else:
        print(f"{channel_label(draft.channel)} R{draft.round} · {draft.title}")
        if brief_path is not None:
            print(f"브리프: {brief_path}")
        for c in checks:
            mark = "통과" if c.passed else "미충족"
            print(f"- [{mark}] {c.label}: {c.value} (기준 {c.expected})")
        passed = sum(1 for c in checks if c.passed)
        print(f"{passed}/{len(checks)}개 통과")
    return 0 if all(c.passed for c in checks) else 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insia", description="INSIA 스마트에이전트 — 총괄·리서치·검수 에이전트 콘텐츠 파이프라인")
    parser.add_argument("--version", action="version", version=f"insia {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="명령")

    run = sub.add_parser("run", help="브리프로 파이프라인을 실행해요")
    run.add_argument("--brief", help="브리프 JSON 파일")
    run.add_argument("--topic", help="주제 또는 사업 아이템")
    run.add_argument("--goal", help="목적")
    run.add_argument("--audience", help="대상 독자")
    run.add_argument("--channels", help=f"쉼표로 구분 ({','.join(ALL_CHANNELS)})")
    run.add_argument("--keywords", help="쉼표로 구분, 첫 번째가 메인 키워드")
    run.add_argument("--tone", help="톤앤매너")
    run.add_argument("--notes", help="추가 요구사항")
    run.add_argument("--mode", choices=["auto", "live", "mock"], help="기본 auto: API 키가 있으면 live, 없으면 mock")
    run.add_argument("--model", help="모델 ID (기본 INSIA_MODEL 또는 claude-opus-5)")
    run.add_argument("--out", help="결과 폴더 (기본 outputs)")
    run.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않아요")
    run.add_argument("--record", help="대시보드용 트레이스 JSON 저장 경로 (예: web/demo/demo-run.json)")
    run.add_argument("--speed", type=float, help="mock 재생 속도 (1.0 = 실제 시간, 0 = 기다리지 않음)")
    run.add_argument("--max-rounds", type=int, dest="max_rounds", help="최대 수정 횟수 (기본 2)")
    run.add_argument("--pass-score", type=int, dest="pass_score", help="통과 점수 (기본 80)")
    run.add_argument("--no-fallbacks", action="store_true", help="서버 측 거절 대체 모델(fallbacks)을 끕니다")
    run.add_argument("--quiet", action="store_true", help="채널 완료와 최종 결과만 출력해요")
    run.set_defaults(func=cmd_run)

    srv = sub.add_parser("serve", help="대시보드 서버를 띄워요")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--web-dir", dest="web_dir", help="대시보드 폴더 (기본: 저장소의 web/)")
    srv.add_argument("--mode", choices=["auto", "live", "mock"], help="대시보드에서 시작하는 실행의 기본 모드")
    srv.add_argument("--model", help="모델 ID")
    srv.add_argument("--out", help="결과 폴더 (기본 outputs)")
    srv.add_argument("--verbose", action="store_true", help="요청 로그를 출력해요")
    srv.set_defaults(func=cmd_serve)

    sample = sub.add_parser("sample-brief", help="샘플 브리프 JSON을 출력해요")
    sample.set_defaults(func=cmd_sample_brief)

    check = sub.add_parser("check", help="Draft JSON의 채널 형식을 검사해요")
    check.add_argument("draft", help="Draft JSON 파일")
    check.add_argument("--brief", help="브리프 JSON (네이버 블로그 제목 키워드 검사용; 생략하면 근처 brief.json)")
    check.add_argument("--json", action="store_true", help="JSON으로 출력")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except UsageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
