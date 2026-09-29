"""Command line: ``insia <명령>`` (Korean help and messages).

Commands (grouped the way the weekly routine uses them)::

    first setup   doctor · profile show|edit-template|import|export · docs add|list|show|rm
    weekly        plan-week · run-due · calendar list|generate|skip|move
    library       items list|show|approve|schedule|publish|archive|restore|export · review · revise
    runs          run · resume · runs list|show|export · usage
    Claude Code   import-run <folder> · check <draft.json>
    quality eval  eval list|run|compare (evals/cases; live needs --max-cost-usd)
    server        serve · healthcheck · sample-brief · backup
    API 게시      publish status|setup|connect|disconnect|preview|send|attempts|resolve|refresh

Every workspace command uses ``Settings.home`` (env ``INSIA_HOME`` or
``--home``). ``--json`` prints machine-readable JSON only (for scripts and the
Claude Code agents: ``profile show --json``, ``docs list --json``, …).

Exit codes: 0 = ok (``run-due`` with nothing due included), 1 = the operation
failed (not found, approval blocked, run error, failed format check),
2 = usage error (bad option or input file), 130 = interrupted (Ctrl+C).

Nothing publishes by itself: ``items publish`` only records that a human
posted the item (and where). ``publish send`` posts one approved LinkedIn /
Instagram item through the API only on a terminal, after the person typed the
random confirm code shown with the exact preview (no ``--yes``, no schedule;
``run-due``/``plan-week``/``calendar`` never publish). Only the ``cmd_publish_*``
/ ``_publish_*`` functions import ``insia_agents.publishers``.
"""

from __future__ import annotations

import argparse
import inspect
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import traceback
import typing
import unicodedata
import urllib.parse
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from pydantic import ValidationError

from . import __version__
from .agents.common import STATUS_LABELS, channel_label
from .backends.base import BackendError
from .channels import check_format
from .config import KST, Settings, has_credentials, load_sample_brief, resolve_mode
from .models import ALL_CHANNELS, Brief, ContentItem, ContentItemDetail, Draft, Profile, Review
# Moved to library modules (the server can use them too); re-exported here for existing callers.
from .documents import (DOC_EXTENSIONS, DOCS_EXTRA_HINT, IMAGE_EXTENSIONS, UNSUPPORTED_DOCS,  # noqa: F401
                        _docx_text, _parse_json_text, _parse_yaml_text, _pdf_text, _yaml_module, clean_text,
                        extract_document, load_structured_file, read_text_file)
from .errors import CommandError, UsageError
from .errors import error_text as _error_text  # the CLI's old name (same function)
from .importer import (PROFILE_FIELDS, TEAM_FIELDS, ImportReport, _field_label, _list_fields,  # noqa: F401
                       _SAFE_RUN_ID, _scalar_text, _team, _text_list, import_run_folder, import_run_id,
                       profile_from_data)

if TYPE_CHECKING:
    from .db import Workspace

AGENT_NAMES = {"orchestrator": "총괄", "researcher": "리서치", "reviewer": "검수", "system": "시스템"}

ITEM_STATUS_LABELS = {"draft": "초안", "needs_changes": "수정 필요", "approved": "승인", "scheduled": "게시 예정",
                      "published": "게시 완료", "archived": "보관"}
SLOT_STATUS_LABELS = {"planned": "계획", "generating": "만드는 중", "drafted": "초안 있음", "skipped": "건너뜀"}
RUN_STATUS_LABELS = {"running": "실행 중", "completed": "완료", "failed": "실패", "cancelled": "중단",
                     "interrupted": "중단됨"}
JOB_LABELS = {"review": "재검수", "revise": "수정 요청", "edit": "직접 수정", "slot": "캘린더 초안", "import": "가져오기"}
RUN_KIND_LABELS = {"pipeline": "실행", **JOB_LABELS}
DOC_KIND_LABELS = {"text": "텍스트", "markdown": "마크다운", "pdf": "PDF", "docx": "Word"}
SOURCE_LABELS = {"agent": "에이전트", "human": "사람"}

CHANNEL_ALIASES = {
    "blog": "naver_blog", "naver": "naver_blog", "naverblog": "naver_blog", "naver-blog": "naver_blog",
    "li": "linkedin", "ig": "instagram", "insta": "instagram", "plan": "bizplan",
    "사업계획서": "bizplan", "블로그": "naver_blog", "네이버": "naver_blog", "네이버블로그": "naver_blog",
    "링크드인": "linkedin", "인스타그램": "instagram", "인스타": "instagram",
}
DEFAULT_PLAN_COUNTS = {"naver_blog": 2, "linkedin": 2, "instagram": 2}  # same as the dashboard's calendar form
EXPORT_FORMATS = ("md", "txt", "html", "docx", "zip")
TOKEN_HINT = "python -c \"import secrets; print(secrets.token_urlsafe(32))\""


# ---------------------------------------------------------------------------
# Korean argparse
# ---------------------------------------------------------------------------

_ARGPARSE_MESSAGES: tuple[tuple[str, str], ...] = (
    (r"^the following arguments are required: (.+)$", r"꼭 필요한 값이 빠졌어요: \1"),
    (r"^unrecognized arguments: (.+)$", r"알 수 없는 옵션이에요: \1"),
    (r"^argument (.+?): invalid choice: (.+?) \(choose from (.+)\)$", r"\1: \2은(는) 고를 수 없어요 (가능: \3)"),
    (r"^argument (.+?): expected one argument$", r"\1 뒤에 값을 하나 적어 주세요"),
    (r"^argument (.+?): expected at least one argument$", r"\1 뒤에 값을 하나 이상 적어 주세요"),
    (r"^argument (.+?): invalid (?:int|float) value: (.+)$", r"\1: 숫자를 적어 주세요 (받은 값: \2)"),
    (r"^argument (.+?): not allowed with argument (.+)$", r"\1와(과) \2는 함께 쓸 수 없어요"),
    (r"^argument (.+?): ignored explicit argument (.+)$", r"\1에는 값을 붙이지 않아요 (받은 값: \2)"),
    (r"^one of the arguments (.+) is required$", r"다음 중 하나는 꼭 적어 주세요: \1"),
    (r"^argument (.+?): (.+)$", r"\1: \2"),  # our own ArgumentTypeError messages are Korean already
)


def _translate_argparse(message: str) -> str:
    for pattern, replacement in _ARGPARSE_MESSAGES:
        if re.match(pattern, message):
            return re.sub(pattern, replacement, message)
    return message


class KoreanHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str, indent_increment: int = 2, max_help_position: int = 30, width: int | None = None) -> None:
        super().__init__(prog, indent_increment, max_help_position, width)

    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[override]
        return super().add_usage(usage, actions, groups, "사용법: " if prefix is None else prefix)


class KoreanArgumentParser(argparse.ArgumentParser):
    """``ArgumentParser`` with Korean section titles, help flag and error messages."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", KoreanHelpFormatter)
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "위치 인수"
        self._optionals.title = "옵션"
        self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="이 도움말을 보여 줘요")

    def error(self, message: str) -> typing.NoReturn:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 오류: {_translate_argparse(message)}\n")


def _parent() -> argparse.ArgumentParser:
    """Shared options (``parents=``): keep the default titles so argparse merges them."""
    return argparse.ArgumentParser(add_help=False)


def _type_channel(value: str) -> str:
    key = value.strip().lower().replace(" ", "")
    channel = CHANNEL_ALIASES.get(key, key)
    if channel not in ALL_CHANNELS:
        raise argparse.ArgumentTypeError(f"알 수 없는 채널이에요: {value!r} (가능: {', '.join(ALL_CHANNELS)}, blog, ig)")
    return channel


def _type_day(value: str) -> str:
    text = value.strip()
    if text.lower() in ("today", "tomorrow", "yesterday") or text in ("오늘", "내일", "어제"):
        return text.lower()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식으로 적어 주세요 (받은 값: {value!r})") from None


def _type_positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"1 이상의 정수를 적어 주세요 (받은 값: {value!r})") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"1 이상의 정수를 적어 주세요 (받은 값: {value!r})")
    return number


def _type_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"0 이상의 정수를 적어 주세요 (받은 값: {value!r})") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"0 이상의 정수를 적어 주세요 (받은 값: {value!r})")
    return number


def _type_usd(value: str) -> float:
    try:
        number = float(value.strip().lstrip("$"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"달러 금액을 숫자로 적어 주세요 (예: 3 또는 0.5, 받은 값: {value!r})") from None
    if not (number >= 0 and number != float("inf")):
        raise argparse.ArgumentTypeError("예산은 0 이상이어야 해요 (0 = 상한 없음)")
    return number


def _resolve_day(value: str | None, settings: Settings) -> str | None:
    if value is None:
        return None
    today = date.fromisoformat(settings.today)
    shifts = {"today": 0, "오늘": 0, "tomorrow": 1, "내일": 1, "yesterday": -1, "어제": -1}
    if value in shifts:
        return (today + timedelta(days=shifts[value])).isoformat()
    return value


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _ensure_utf8_streams() -> None:
    """Korean text survives ``> file`` redirects on Windows (cp949 consoles)."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in ("utf8", "") or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError, AttributeError):
            pass


def _cell_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _fit(text: Any, width: int) -> str:
    """One line, cut to ``width`` terminal cells (Korean = 2 cells) with an ellipsis."""
    flat = " ".join(str(text if text is not None else "").split())
    if width <= 0 or _cell_width(flat) <= width:
        return flat
    out, used = [], 0
    for ch in flat:
        w = _cell_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _cell_width(text))


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], *, max_widths: Sequence[int] | None = None,
                indent: str = "") -> None:
    cells = [[_fit(value, max_widths[i] if max_widths else 0) for i, value in enumerate(row)] for row in rows]
    widths = [max([_cell_width(h)] + [_cell_width(r[i]) for r in cells]) for i, h in enumerate(headers)]
    last = len(headers) - 1

    def line(values: Sequence[str]) -> str:
        return indent + "  ".join(v if i == last else _pad(v, widths[i]) for i, v in enumerate(values)).rstrip()

    print(line(list(headers)))
    print(line(["-" * w for w in widths]))
    for row in cells:
        print(line(row))


def _print_json(value: Any) -> None:
    from .events import to_jsonable

    print(json.dumps(to_jsonable(value), ensure_ascii=False, indent=2))


def _kst(ts: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not ts:
        return "-"
    try:
        moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime(fmt)


def _usd(value: float | int | None) -> str:
    amount = float(value or 0.0)
    if amount == 0:
        return "$0"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _weekday(day: str) -> str:
    try:
        return "월화수목금토일"[date.fromisoformat(day).weekday()]
    except ValueError:
        return ""


def _verdict(passed: bool | None) -> str:
    return "-" if passed is None else ("통과" if passed else "미통과")


def _cost_line(cost: float, mode: str) -> str:
    if mode == "mock":
        return "비용: $0 (mock 모드는 API를 부르지 않아 무료예요)"
    return f"API 비용: 약 {_usd(cost)}"


def _warn(message: str) -> None:
    print(f"주의: {message}", file=sys.stderr)


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
        job = JOB_LABELS.get(str(data.get("kind") or ""), "")
        head = "이어서 실행 시작" if data.get("resumed") else (f"{job} 시작" if job else "실행 시작")
        parts = [head, f"{data.get('mode')} 모드"]
        if data.get("model"):
            parts.append(f"모델 {data['model']}")
        names = ", ".join(channel_label(c) for c in data.get("channels") or [])
        if names:
            parts.append(f"채널: {names}")
        if data.get("budget_usd"):
            parts.append(f"예산 상한 {_usd(data['budget_usd'])}")
        return " · ".join(parts)
    if kind == "agent.status":
        if data["status"] in ("waiting", "idle"):
            return None
        return f"[{STATUS_LABELS.get(data['status'], data['status'])}] {data['message']}"
    if kind == "handoff":
        return f"→ {AGENT_NAMES.get(data['to'], data['to'])}: {data['label']}"
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
        prefix = "추가 조사 완료" if data.get("followup") else ("저장된 리서치 불러옴" if data.get("resumed") else "리서치 완료")
        return f"{prefix} · 근거 {data['findings']}건 · 출처 {data['sources']}곳 · 빈틈 {len(data['gaps'])}건"
    if kind == "draft.created":
        who = "직접 수정" if data.get("source") == "human" else f"R{data['round']} 초안"
        version = f" v{data['version']}" if data.get("version") else ""
        return (f"{channel_label(data['channel'])} {who}{version} · {data['chars']:,}자 "
                f"(공백 제외 {data['chars_no_space']:,}자)")
    if kind == "review.completed":
        failed = [c["label"] for c in data["format_checks"] if not c["passed"]]
        extra = f" · 형식 미충족: {', '.join(failed)}" if failed else ""
        return f"{channel_label(data['channel'])} R{data['round']} 검수 {data['score']}점 · {_verdict(data['passed'])}{extra}"
    if kind == "revision.requested":
        return f"{channel_label(data['channel'])} 수정 요청 {data['issues']}건 · {data['top_issue']}"
    if kind == "channel.completed":
        return (f"{channel_label(data['channel'])} 완료 · {data['score']}점 · {_verdict(data['passed'])} · "
                f"수정 {data['rounds']}회")
    if kind == "run.completed":
        job = JOB_LABELS.get(str(data.get("kind") or ""), "")
        parts = [f"{job} 완료" if job else "실행 완료", f"{data['duration_s']}초"]
        if data.get("version"):
            parts.append(f"v{data['version']}")
        if data.get("cost_usd"):
            parts.append(f"비용 {_usd(data['cost_usd'])}")
        if data.get("output_dir"):
            parts.append(f"결과: {data['output_dir']}")
        return " · ".join(parts)
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
# Settings, workspace, ids
# ---------------------------------------------------------------------------


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@contextmanager
def _env_override(name: str, value: str | None) -> Iterator[None]:
    """Temporarily set an env var (``--home`` → ``INSIA_HOME`` for every module, prices.json included)."""
    if not value:
        yield
        return
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def _settings_from_args(args: argparse.Namespace, **extra: Any) -> Settings:
    overrides: dict[str, Any] = {
        "mode": getattr(args, "mode", None),
        "model": getattr(args, "model", None),
        "speed": getattr(args, "speed", None),
        "max_rounds": getattr(args, "max_rounds", None),
        "pass_score": getattr(args, "pass_score", None),
        "max_cost_usd": getattr(args, "max_cost_usd", None),
        **extra,
    }
    if getattr(args, "out", None):
        overrides["out_dir"] = Path(args.out)
    if getattr(args, "no_fallbacks", False):
        overrides["fallbacks"] = False
    if getattr(args, "no_profile", False):
        overrides["use_profile"] = False
    try:
        settings = Settings.from_env(**overrides)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    if getattr(args, "no_save", False):
        from dataclasses import replace

        settings = replace(settings, out_dir=None)
    return settings


def _home(settings: Settings) -> Path:
    return Path(settings.home).expanduser().resolve()


@contextmanager
def open_workspace(settings: Settings) -> Iterator["Workspace"]:
    from .db import DB_NAME, Workspace

    home = Path(settings.home).expanduser()
    if not (home / DB_NAME).exists():
        print(f"새 워크스페이스를 만들어요: {home.resolve()} (다른 곳을 쓰려면 INSIA_HOME 또는 --home)", file=sys.stderr)
    workspace = Workspace(home)
    try:
        yield workspace
    finally:
        workspace.close()


def _josa(word: str, pair: str) -> str:
    """``_josa("콘텐츠", "이/가")`` → ``"콘텐츠가"`` (particle after the last syllable)."""
    with_batchim, without = pair.split("/")
    last = word.strip()[-1:] if word.strip() else ""
    has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return word + (with_batchim if has_batchim else without)


def _match_id(text: str, ids: Sequence[str], what: str, list_command: str) -> str:
    """Exact id, or the one id that contains ``text`` (ids are long; a unique part is enough)."""
    text = (text or "").strip()
    if text in ids:
        return text
    hits = [i for i in ids if text and text in i]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise CommandError(f"{what} '{text}'을(를) 찾을 수 없어요. '{list_command}'로 id를 확인해 주세요.")
    shown = ", ".join(hits[:5]) + (" …" if len(hits) > 5 else "")
    raise UsageError(f"'{text}'에 해당하는 {_josa(what, '이/가')} {len(hits)}개예요 ({shown}). id를 더 길게 적어 주세요.")


def _item_id(ws: "Workspace", text: str) -> str:
    return _match_id(text, [i.id for i in ws.list_items(limit=5000)], "콘텐츠", "insia items list")


def _run_id(ws: "Workspace", text: str) -> str:
    return _match_id(text, [r["run_id"] for r in ws.list_runs(limit=1000)], "실행", "insia runs list")


def _slot_id(ws: "Workspace", text: str) -> str:
    return _match_id(text, [s.id for s in ws.list_slots()], "캘린더 슬롯", "insia calendar list")


def _require_item(ws: "Workspace", item_id: str) -> ContentItemDetail:
    detail = ws.get_item(item_id)
    if detail is None:
        raise CommandError(f"콘텐츠 {item_id}을(를) 찾을 수 없어요. 'insia items list'로 id를 확인해 주세요.")
    return detail


def _saved_profile(ws: "Workspace") -> Profile | None:
    from .db import profile_is_empty

    profile = ws.get_profile()
    return None if profile_is_empty(profile) else profile


def _run_cost_from_bus(bus: Any) -> float:
    for event in reversed(getattr(bus, "events", []) or []):
        if event.get("type") in ("run.completed", "run.failed"):
            return float((event.get("data") or {}).get("cost_usd") or 0.0)
    return 0.0


# ---------------------------------------------------------------------------
# Profile: labels, coercion, YAML/JSON template
# ---------------------------------------------------------------------------

# PROFILE_FIELDS, TEAM_FIELDS and profile_from_data (loose JSON/YAML → Profile) live in importer.py.
PROFILE_KEY_FIELDS = ("service_name", "one_liner", "target_customers", "problem", "solution", "differentiators", "tone",
                      "contact")


def _profile_fill(profile: Profile) -> tuple[int, int, list[str]]:
    data = profile.model_dump(exclude={"updated_at"})
    filled = sum(1 for value in data.values() if value)
    missing = [_field_label(k) for k in PROFILE_KEY_FIELDS if not data.get(k)]
    if not data.get("service_name") and data.get("company_name"):
        missing = [m for m in missing if m != _field_label("service_name")]
    return filled, len(data), missing


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def profile_yaml(profile: Profile, *, header: bool = True) -> str:
    """Commented YAML for a profile (written by hand: PyYAML is only needed to read it back)."""
    lines: list[str] = []
    if header:
        lines += [
            "# INSIA 회사·브랜드 프로필",
            "# - 모든 에이전트가 이 내용을 '사용자 제공 사실'로 참고해요. 사실만, 적힌 만큼만 써요.",
            "# - 모르는 칸은 비워 두세요(\"\"). 목록은 '- \"내용\"' 줄을 늘리거나 줄이면 돼요.",
            "# - 글은 \"큰따옴표\" 안에 적으면 콜론(:)이나 #이 있어도 안전해요.",
            "# - 다 채웠으면: insia profile import 이 파일 (대시보드 '브랜드·자료'에서도 고칠 수 있어요)",
            "",
        ]
    data = profile.model_dump(exclude={"updated_at"})
    list_fields = _list_fields()
    for name in Profile.model_fields:
        if name == "updated_at":
            continue
        label, hint = PROFILE_FIELDS.get(name, (name, ""))
        lines.append(f"# {label}" + (f" — {hint}" if hint else ""))
        value = data.get(name)
        if name == "team":
            if value:
                lines.append("team:")
                for member in value:
                    lines.append(f"  - role: {_yaml_scalar(member.get('role', ''))}")
                    for key in ("name", "background", "hiring"):
                        lines.append(f"    {key}: {_yaml_scalar(member.get(key, False if key == 'hiring' else ''))}")
            else:
                lines += ["team:",
                          '  # - role: "대표"            # 역할',
                          '  #   name: ""               # 실명 (사업계획서에는 절대 나오지 않아요)',
                          '  #   background: ""         # 학위·전공, 경력, 보유 역량',
                          "  #   hiring: false          # 채용 예정 인력이면 true"]
        elif name in list_fields:
            lines.append(f"{name}:")
            if value:
                lines += [f"  - {_yaml_scalar(v)}" for v in value]
            else:
                lines.append('  # - "여기에 적어요"')
        else:
            lines.append(f"{name}: {_yaml_scalar(value or '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def profile_json_template(profile: Profile) -> str:
    """JSON with ``_<field>`` help keys (ignored on import) before every field."""
    data: dict[str, Any] = {
        "_안내": ("빈칸(\"\")을 채우고 저장한 뒤 'insia profile import 이 파일'로 넣어 주세요. "
                "'_'로 시작하는 항목은 설명이라 무시해요. 목록은 [\"첫째\", \"둘째\"]처럼 적어요."),
    }
    values = profile.model_dump(exclude={"updated_at"})
    for name in Profile.model_fields:
        if name == "updated_at":
            continue
        label, hint = PROFILE_FIELDS.get(name, (name, ""))
        data[f"_{name}"] = f"{label}" + (f" — {hint}" if hint else "")
        value = values.get(name)
        if name == "team" and not value:
            value = [{"role": "", "name": "", "background": "", "hiring": False}]
        data[name] = value
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_output(text: str, out: str | None, *, force: bool, what: str) -> Path | None:
    """Write ``text`` to ``out`` (``None``/``-`` = stdout). Refuses to overwrite without ``force``."""
    if out in (None, "-"):
        sys.stdout.write(text)
        return None
    path = Path(out)
    if path.exists() and not force:
        raise UsageError(f"이미 있는 파일이에요: {path}. 덮어쓰려면 --force를 붙여 주세요.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"{what}을(를) 저장하지 못했어요: {path} ({exc.strerror or exc})") from None
    return path


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:120] or "file"


# ---------------------------------------------------------------------------
# run / resume / review / revise
# ---------------------------------------------------------------------------


def build_brief(args: argparse.Namespace) -> Brief:
    if args.brief:
        path = Path(args.brief)
        try:
            brief = Brief.model_validate_json(path.read_text(encoding="utf-8-sig"))  # -sig: tolerate a UTF-8 BOM
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
    for name in ("topic", "goal", "audience", "tone", "notes"):
        value = getattr(args, name, None)
        if value:
            updates[name] = value
    if args.channels:
        channels = [CHANNEL_ALIASES.get(c.lower(), c) for c in _split(args.channels)]
        unknown = [c for c in channels if c not in ALL_CHANNELS]
        if unknown:
            raise UsageError(f"알 수 없는 채널: {', '.join(unknown)} (가능: {', '.join(ALL_CHANNELS)})")
        updates["channels"] = list(dict.fromkeys(channels))
    if args.keywords:
        updates["keywords"] = _split(args.keywords)
    brief = brief.model_copy(update=updates) if updates else brief
    brief = Brief.model_validate(brief.model_dump())
    if not brief.topic.strip():
        raise UsageError("주제(topic)가 비어 있어요")
    if not brief.channels:
        raise UsageError("채널을 하나 이상 지정해 주세요")
    return brief


def _budget_hint(run_id: str, cap: float) -> str:
    suggestion = max(cap * 2, cap + 1.0)
    return f"상한을 올려 남은 작업만 이어서 하려면: insia resume {run_id} --max-cost-usd {suggestion:g}"


def _print_run_summary(result: Any, settings: Settings, ws: "Workspace | None") -> None:
    from .db import pipeline_item_id

    print("\n채널별 결과")
    out_dir = settings.out_dir / result.run_id if settings.out_dir is not None else None
    for item in result.results:
        score = next((r.score for r in item.reviews if r.round == item.final.round), 0)
        where = f"  {out_dir / (item.channel + '.md')}" if out_dir is not None else ""
        library = f"  보관함 {pipeline_item_id(result.run_id, item.channel)}" if ws is not None else ""
        print(f"- {channel_label(item.channel)}: {score}점 · {_verdict(item.passed)} · 수정 {item.rounds}회{library}{where}")


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import BudgetExceeded, build_context, execute_run, new_run_id

    brief = build_brief(args)
    settings = _settings_from_args(args)
    if args.record and Path(args.record).is_dir():
        raise UsageError(f"--record에는 폴더가 아닌 파일 경로를 지정해 주세요 (예: {Path(args.record) / 'demo-run.json'})")
    if args.no_workspace and args.docs not in (None, "", "none", "all"):
        raise UsageError("--docs로 자료를 고르려면 워크스페이스가 필요해요 (--no-workspace와 함께 쓸 수 없어요)")
    printer = ProgressPrinter(quiet=args.quiet)
    run_id = new_run_id()
    manager = nullcontext(None) if args.no_workspace else open_workspace(settings)
    with manager as ws:
        context = None
        if ws is not None:
            from .db import WorkspaceError

            try:
                context = build_context(ws, settings, docs=args.docs if args.docs is not None else "all")
            except WorkspaceError as exc:
                raise UsageError(str(exc)) from None
        try:
            result, bus = execute_run(brief, settings, run_id=run_id, listener=printer, workspace=ws, context=context)
        except BudgetExceeded as exc:
            print(f"\n{exc}", file=sys.stderr)
            if exc.result is not None:
                _print_run_summary(exc.result, settings, ws)
            if ws is not None:
                print(_budget_hint(run_id, exc.cap), file=sys.stderr)
            return 1
        except BackendError as exc:
            print(f"실행하지 못했어요: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n실행을 중단했어요." + (f" 이어서 하려면: insia resume {run_id}" if ws is not None else ""),
                  file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - report instead of a traceback
            print(f"실행하지 못했어요: {_error_text(exc)}", file=sys.stderr)
            return 1

        exit_code = 0
        if args.record:
            record_path = Path(args.record)
            meta = {
                "title": f"INSIA 실행 기록 — {brief.topic[:40]}",
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "mode": result.mode,
                "model": result.model,
                "brief": brief.model_dump(mode="json"),
            }
            try:
                record_path.parent.mkdir(parents=True, exist_ok=True)
                record_path.write_text(json.dumps(bus.to_trace(meta), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"트레이스를 저장하지 못했어요: {record_path} ({exc.strerror or exc})", file=sys.stderr)
                exit_code = 1
            else:
                print(f"트레이스 저장: {record_path} (이벤트 {len(bus)}개)")

        _print_run_summary(result, settings, ws)
        cost = ws.run_cost(result.run_id) if ws is not None else _run_cost_from_bus(bus)
        print(_cost_line(cost, result.mode))
        if ws is not None:
            print("다음 단계: 'insia items list'로 보관함을 보고, 검토한 뒤 'insia items approve <id>'로 승인해요. "
                  "게시는 사람이 직접 해요.")
    return exit_code


def cmd_resume(args: argparse.Namespace) -> int:
    from .pipeline import BudgetExceeded, resume_run

    settings = _settings_from_args(args, **_fast(args))
    printer = ProgressPrinter(quiet=args.quiet)
    with open_workspace(settings) as ws:
        run_id = _run_id(ws, args.run_id)
        # runs left 'running' by a process that is gone (a killed run-due, a crash) are closed first
        recovered = ws.recover_stale()
        if recovered:
            print(f"실행하던 프로그램이 멈춘 실행 {len(recovered)}개를 '중단됨'으로 정리했어요: {', '.join(recovered)}",
                  file=sys.stderr)
        try:
            # --max-cost-usd sets a new cap for this run; without it the run keeps the cap it was started with
            result = resume_run(run_id, settings, ws, listener=printer, force=args.force,
                                max_cost_usd=getattr(args, "max_cost_usd", None))
        except BudgetExceeded as exc:
            print(f"\n{exc}", file=sys.stderr)
            print(_budget_hint(run_id, exc.cap), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print(f"\n실행을 중단했어요. 다시 이어서 하려면: insia resume {run_id}", file=sys.stderr)
            return 130
        _print_run_summary(result, settings, ws)
        print(_cost_line(ws.run_cost(run_id), result.mode) + " (이 실행의 누적 비용)")
    return 0


def _print_review_brief(review: Review | None, limit: int = 3) -> None:
    if review is None:
        return
    if review.summary:
        print(f"  요약: {review.summary}")
    order = {"critical": 0, "major": 1, "minor": 2}
    for issue in sorted(review.issues, key=lambda i: order.get(i.severity, 3))[:limit]:
        print(f"  - [{issue.severity}] {issue.problem}" + (f" → {issue.fix}" if issue.fix else ""))
    failed = [c for c in review.format_checks if not c.passed]
    if failed:
        print("  형식 미충족: " + ", ".join(f"{c.label} {c.value} (기준 {c.expected})" for c in failed))


def _fast(args: argparse.Namespace) -> dict[str, Any]:
    """Item jobs and cron runs never wait in mock mode (``run`` keeps real-time pacing for demos)."""
    return {"speed": args.speed if getattr(args, "speed", None) is not None else 0.0}


def _job_mode(settings: Settings) -> str:
    return resolve_mode(settings.mode)[0]


def cmd_review(args: argparse.Namespace) -> int:
    from .actions import review_item

    settings = _settings_from_args(args, **_fast(args))
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        job = review_item(ws, item_id, settings=settings, listener=ProgressPrinter(quiet=args.quiet))
        cost = ws.run_cost(job.run_id)
    review = job.review
    label = channel_label(job.item.channel) if job.item else ""
    version = job.version.version if job.version else "?"
    print(f"\n{label} v{version} 재검수: {review.score}점 · {_verdict(review.passed)} · 상태 "
          f"{ITEM_STATUS_LABELS.get(job.item.status, job.item.status) if job.item else '-'}")
    _print_review_brief(review)
    print(_cost_line(cost, _job_mode(settings)))
    if review.passed:
        print(f"승인하려면: insia items approve {item_id}")
    else:
        print(f"고치려면: insia revise {item_id} --instructions \"…\"  (지시 없이도 검수 의견대로 고쳐요)")
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    from .actions import revise_item

    settings = _settings_from_args(args, **_fast(args))
    instructions = args.instructions or ""
    if args.instructions_file:
        instructions = read_text_file(Path(args.instructions_file), "수정 지시 파일").strip()
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        job = revise_item(ws, item_id, instructions, settings=settings, listener=ProgressPrinter(quiet=args.quiet))
        cost = ws.run_cost(job.run_id)
    label = channel_label(job.item.channel) if job.item else ""
    version = job.version.version if job.version else "?"
    if job.review is not None:
        print(f"\n{label} 수정본 v{version}: {job.review.score}점 · {_verdict(job.review.passed)}")
        _print_review_brief(job.review)
    else:
        print(f"\n{label} 수정본 v{version}을 저장했어요 (재검수는 마치지 못했어요: insia review {item_id})")
    print(_cost_line(cost, _job_mode(settings)))
    print(f"내용 보기: insia items show {item_id}")
    return 0


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


def _item_row(item: ContentItem) -> list[Any]:
    score = f"{item.score}" if item.score is not None else "-"
    return [item.id, channel_label(item.channel), ITEM_STATUS_LABELS.get(item.status, item.status), score,
            f"v{item.version}", _kst(item.updated_at), item.title]


def cmd_items_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        items = ws.list_items(status=args.status, channel=args.channel, limit=args.limit)
        if not args.status and not args.all:
            items = [i for i in items if i.status != "archived"]
        home = _home(settings)
    if args.json:
        _print_json([i.model_dump(mode="json") for i in items])
        return 0
    if not items:
        where = f" ({ITEM_STATUS_LABELS.get(args.status, args.status)})" if args.status else ""
        print(f"보관함이 비어 있어요{where}. 워크스페이스: {home}")
        print("초안 만들기: insia run --topic \"…\"  ·  insia plan-week → insia run-due  ·  Claude Code 결과: insia import-run <폴더>")
        return 0
    print_table(["ID", "채널", "상태", "점수", "버전", "수정", "제목"], [_item_row(i) for i in items],
                max_widths=[0, 0, 0, 0, 0, 0, 44])
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    print("\n" + " · ".join(f"{ITEM_STATUS_LABELS.get(s, s)} {n}" for s, n in counts.items())
          + "  |  자세히: insia items show <id>  (id는 앞뒤를 줄여 겹치지 않는 부분만 적어도 돼요)")
    return 0


def _draft_text(draft: Draft) -> str:
    lines = [f"# {draft.title}", "", draft.content.rstrip()]
    if draft.hashtags:
        last = draft.content.strip().splitlines()[-1] if draft.content.strip() else ""
        if not all(tag in last for tag in draft.hashtags):
            lines += ["", " ".join(draft.hashtags)]
    return "\n".join(lines)


def cmd_items_show(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        detail = _require_item(ws, item_id)
    if args.json:
        _print_json(detail.model_dump(mode="json"))
        return 0
    item = detail.item
    print(f"{channel_label(item.channel)} · {item.title}")
    facts = [f"ID {item.id}", f"상태 {ITEM_STATUS_LABELS.get(item.status, item.status)}", f"v{item.version}"]
    if item.score is not None:
        facts.append(f"{item.score}점 {_verdict(item.passed)}")
    if item.run_id:
        facts.append(f"실행 {item.run_id}")
    print("  " + " · ".join(facts))
    extra = []
    if item.scheduled_at:
        extra.append(f"게시 예정 {item.scheduled_at}")
    if item.published_at:
        extra.append(f"게시 {_kst(item.published_at, '%Y-%m-%d %H:%M')}")
    if item.published_url:
        extra.append(item.published_url)
    if item.note:
        extra.append(f"메모: {item.note}")
    if extra:
        print("  " + " · ".join(extra))
    if not detail.versions:
        print("\n아직 버전이 없어요.")
        return 0
    print("\n버전")
    for v in detail.versions:
        review = f"{v.review.score}점 {_verdict(v.review.passed)}" if v.review else "검수 전"
        instr = f" · 지시: {_fit(v.instructions, 40)}" if v.instructions else ""
        print(f"  v{v.version}  {SOURCE_LABELS.get(v.source, v.source)}  R{v.draft.round}  {review}  {_kst(v.created_at)}{instr}")
    chosen = detail.versions[-1]
    if args.version is not None:
        chosen = next((v for v in detail.versions if v.version == args.version), None)  # type: ignore[assignment]
        if chosen is None:
            raise CommandError(f"v{args.version} 버전이 없어요 (v1~v{detail.versions[-1].version})")
    if chosen.review is not None:
        print(f"\n검수 (v{chosen.version}, {chosen.review.score}점 · {_verdict(chosen.review.passed)})")
        _print_review_brief(chosen.review, limit=5)
    if not args.no_content:
        print(f"\n---------- 본문 v{chosen.version} ----------")
        print(_draft_text(chosen.draft))
        print("---------- 끝 ----------")
    return 0


def cmd_items_approve(args: argparse.Namespace) -> int:
    from .db import ApprovalBlockedError

    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        try:
            item = ws.set_item_status(item_id, "approved", force=args.force)
        except ApprovalBlockedError as exc:
            message = str(exc).replace("'그래도 승인'을 눌러 주세요", "--force를 붙여 주세요")
            raise CommandError(f"{message}\n  그래도 승인: insia items approve {item_id} --force") from None
    print(f"승인했어요: {channel_label(item.channel)} v{item.version} · {item.title}")
    if item.approval_forced:
        score = f"{item.approved_score}점" if item.approved_score is not None else "검수 전"
        print(f"  강제 승인으로 기록했어요: 검수를 통과하지 않은 v{item.approved_version}({score})을 사람이 직접 확인하고 승인했어요.")
    print(f"다음: insia items export {item_id}  →  직접 게시  →  insia items publish {item_id} --url <게시 주소>")
    if item.channel in ("linkedin", "instagram"):
        print("  또는 대시보드의 ‘API로 게시’ (터미널에서는 insia publish send "
              f"{item_id}{' --ai-label yes|no' if item.channel == 'instagram' else ''})")
    return 0


def cmd_items_schedule(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    day = _resolve_day(args.date, settings)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        item = ws.set_item_status(item_id, "scheduled", scheduled_at=day)
    print(f"게시 예정으로 표시했어요: {item.scheduled_at}({_weekday(item.scheduled_at[:10])}) · "
          f"{channel_label(item.channel)} · {item.title}")
    print("INSIA는 예정일에 자동으로 게시하지 않아요. 그날 직접 올리거나 대시보드의 ‘API로 게시’를 눌러 주세요. "
          "직접 올렸다면 'insia items publish'로 표시해 주세요.")
    return 0


def cmd_items_publish(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        item = ws.set_item_status(item_id, "published", published_url=args.url)
    print(f"게시 완료로 표시했어요: {channel_label(item.channel)} · {item.title}")
    if item.published_url:
        print(f"  주소: {item.published_url}")
    else:
        print(f"  게시 주소는 나중에 추가할 수 있어요: insia items publish {item_id} --url <주소>")
    print("  (기록만 남겨요. 다음 주 계획을 세울 때 같은 주제를 피하는 데 써요)")
    return 0


def cmd_items_archive(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        item = ws.set_item_status(item_id, "archived", note=args.note)
    print(f"보관했어요: {channel_label(item.channel)} · {item.title} (되돌리기: insia items restore {item_id})")
    return 0


def cmd_items_restore(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        item = ws.set_item_status(item_id, "draft")
    print(f"초안으로 되돌렸어요: {channel_label(item.channel)} · {item.title}")
    return 0


def cmd_items_export(args: argparse.Namespace) -> int:
    from .exporters import export_item, format_label, formats_for

    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        item_id = _item_id(ws, args.item_id)
        detail = _require_item(ws, item_id)
        profile = _saved_profile(ws)
        exports_dir = ws.exports_dir
    channel = detail.item.channel
    fmt = args.format or formats_for(channel)[0]
    exported = export_item(detail, fmt, profile, version=args.version)
    target = Path(args.out) if args.out else exports_dir
    try:
        path = exported.save(target)
    except OSError as exc:
        raise CommandError(f"파일을 저장하지 못했어요: {target} ({exc.strerror or exc})") from None
    print(f"내보냈어요 ({format_label(fmt, channel)}): {path.resolve()} · {exported.size:,}바이트")
    for note in exported.notes:
        print(f"참고: {note}")
    others = [f for f in formats_for(channel) if f != fmt]
    if others:
        print(f"다른 형식: {', '.join(others)} (--format)")
    if detail.item.status in ("draft", "needs_changes"):
        print(f"아직 승인 전이에요. 게시하기 전에 내용을 확인하고 승인해 주세요: insia items approve {item_id}")
    return 0


# ---------------------------------------------------------------------------
# runs / usage
# ---------------------------------------------------------------------------


def cmd_runs_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    item_id = (args.item or "").strip() or None
    with open_workspace(settings) as ws:
        runs = ws.list_runs(limit=args.limit, kind=args.kind, status=args.status, parent_item_id=item_id)
        home = _home(settings)
        # 'running' rows whose process is gone (killed, crashed): resume/run-due/serve close them
        orphaned = [r for r in runs if r["status"] == "running" and not (ws.run_owner(r["run_id"]) or {}).get("live")]
    if args.json:
        _print_json(runs)
        return 0
    if not runs:
        if item_id:
            print(f"콘텐츠 {item_id}에 돌린 작업(재검수·수정 요청·직접 수정)이 없어요. 워크스페이스: {home}")
        else:
            print(f"실행 기록이 없어요. 워크스페이스: {home}")
        return 0
    rows = []
    for run in runs:
        channels = ", ".join(channel_label(c) for c in run.get("channels") or [])
        rows.append([run["run_id"], RUN_KIND_LABELS.get(run["kind"], run["kind"]),
                     RUN_STATUS_LABELS.get(run["status"], run["status"]), _usd(run.get("cost_usd")),
                     _kst(run.get("created_at")), channels, run.get("topic") or ""])
    print_table(["실행 ID", "종류", "상태", "비용", "시작", "채널", "주제"], rows, max_widths=[0, 0, 0, 0, 0, 24, 36])
    stuck = [r for r in runs if r["status"] in ("interrupted", "failed", "cancelled") and r["kind"] in ("pipeline", "slot")]
    if stuck:
        print(f"\n멈춘 실행 {len(stuck)}개는 이어서 할 수 있어요: insia resume {stuck[0]['run_id']}")
    gone = [r for r in orphaned if r["kind"] in ("pipeline", "slot")]
    if gone:
        print(f"'실행 중'으로 남았지만 실행하던 프로그램이 멈춘 실행 {len(gone)}개: insia resume {gone[0]['run_id']} "
              "('중단됨'으로 정리한 뒤 남은 작업만 이어서 해요)")
    jobs = [r for r in orphaned if r["kind"] not in ("pipeline", "slot")]
    if jobs:
        print(f"'실행 중'으로 남았지만 멈춘 작업(재검수·수정 요청 등) {len(jobs)}개는 이어서 할 수 없어요. "
              "보관함에서 같은 작업을 다시 시작해 주세요.")
    return 0


def cmd_runs_show(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        run_id = _run_id(ws, args.run_id)
        run = ws.get_run(run_id)
    if run is None:
        raise CommandError(f"실행 {run_id}을(를) 찾을 수 없어요")
    if args.json:
        _print_json(run)
        return 0
    print(f"{RUN_KIND_LABELS.get(run['kind'], run['kind'])} {run['run_id']} · {RUN_STATUS_LABELS.get(run['status'], run['status'])}")
    print(f"  주제: {run.get('topic') or '-'}")
    print(f"  모드: {run.get('mode') or '-'} · 모델: {run.get('model') or '-'} · 비용: {_usd(run.get('cost_usd'))}")
    print(f"  시작: {_kst(run.get('created_at'), '%Y-%m-%d %H:%M')} · 끝: {_kst(run.get('finished_at'), '%Y-%m-%d %H:%M')}")
    if run.get("error"):
        print(f"  메시지: {run['error']}")
    items = run.get("items") or {}
    scores = run.get("scores") or {}
    for channel, item_id in items.items():
        score = scores.get(channel)
        print(f"  - {channel_label(channel)}: {item_id}" + (f" · {score}점" if score is not None else ""))
    if run["status"] in ("interrupted", "failed", "cancelled") and run["kind"] in ("pipeline", "slot"):
        print(f"이어서 하려면: insia resume {run['run_id']}")
    return 0


def cmd_runs_export(args: argparse.Namespace) -> int:
    from .exporters import export_run_zip

    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        run_id = _run_id(ws, args.run_id)
        exported = export_run_zip(ws, run_id)
        target = Path(args.out) if args.out else ws.exports_dir
    path = exported.save(target)
    print(f"실행 결과 묶음을 저장했어요: {path.resolve()} · {exported.size:,}바이트")
    for note in exported.notes:
        print(f"참고: {note}")
    return 0


def _month_start(today: str) -> str:
    return date.fromisoformat(today).replace(day=1).isoformat()


def cmd_usage(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    since = None if args.all else (_resolve_day(args.since, settings) or _month_start(settings.today))
    until = _resolve_day(args.until, settings)
    with open_workspace(settings) as ws:
        summary = ws.usage_summary(since=since, until=until)
    if args.json:
        _print_json(summary)
        return 0
    period = f"{since or '처음'} ~ {until or '오늘'}"
    print(f"사용량 · {period}")
    print(f"  총 비용 {_usd(summary['total_usd'])} · API 호출 {summary['calls']:,}회 · 입력 토큰 {summary['input_tokens']:,} · "
          f"출력 토큰 {summary['output_tokens']:,} · 캐시 읽기 {summary['cache_read_tokens']:,} · "
          f"웹 검색 {summary['web_search_requests']:,}회")
    if summary["by_day"]:
        print("\n날짜별")
        peak = max((d["usd"] for d in summary["by_day"]), default=0.0) or 1.0
        for day in summary["by_day"][-31:]:
            bar = "#" * max(1 if day["usd"] > 0 else 0, round(20 * day["usd"] / peak))
            print(f"  {day['date']}({_weekday(day['date'])})  {_pad(_usd(day['usd']), 9)}  호출 {day['calls']:>4}회  {bar}")
    if summary["runs"]:
        print("\n실행별 (최근 10개)")
        rows = [[r["run_id"] or "(계획 등)", RUN_KIND_LABELS.get(r.get("kind") or "", "기타"), _usd(r["usd"]),
                 f"{r['calls']}회", r.get("topic") or ""] for r in summary["runs"][:10]]
        print_table(["실행 ID", "종류", "비용", "호출", "주제"], rows, max_widths=[0, 0, 0, 0, 40], indent="  ")
    if summary["by_task"]:
        print("\n작업별")
        for task, value in sorted(summary["by_task"].items(), key=lambda kv: -kv[1]["usd"]):
            print(f"  {_pad(task, 14)} {_pad(_usd(value['usd']), 9)} 호출 {value['calls']}회")
    cap = settings.max_cost_usd
    print(f"\n실행 1번 예산 상한: {_usd(cap) if cap else '없음'} (INSIA_MAX_COST_USD 또는 --max-cost-usd)")
    print("가격표는 바뀔 수 있어요: <워크스페이스>/prices.json 이나 INSIA_PRICE_* 환경 변수로 고쳐요 (docs/operations.md '비용 관리').")
    return 0


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def cmd_profile_show(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        profile = ws.get_profile()
    if args.json:
        _print_json(profile.model_dump(mode="json"))
        return 0
    filled, total, missing = _profile_fill(profile)
    if filled == 0:
        print("아직 회사 프로필이 없어요.")
        print("  1) insia profile edit-template   → 양식 파일(profile.yaml 또는 .json)이 생겨요")
        print("  2) 메모장 등으로 열어 채운 뒤 저장")
        print("  3) insia profile import profile.yaml")
        print("대시보드 '브랜드·자료' 화면에서도 채울 수 있어요.")
        return 0
    print(f"회사 프로필 (마지막 저장 {_kst(profile.updated_at, '%Y-%m-%d %H:%M')})")
    data = profile.model_dump(exclude={"updated_at"})
    width = max(_cell_width(label) for label, _ in PROFILE_FIELDS.values()) + 2
    for name in Profile.model_fields:
        value = data.get(name)
        if name == "updated_at" or not value:
            continue
        label = _pad(_field_label(name), width)
        if name == "team":
            for index, member in enumerate(value):
                text = member["role"] + (f" · {member['name']}" if member.get("name") else "") + \
                    (f" · {member['background']}" if member.get("background") else "") + (" · 채용 예정" if member.get("hiring") else "")
                print(f"  {label if index == 0 else ' ' * width}{text}")
        elif isinstance(value, list):
            for index, entry in enumerate(value):
                print(f"  {label if index == 0 else ' ' * width}- {entry}")
        else:
            text = str(value).replace("\n", " ")
            print(f"  {label}{text}")
    print(f"\n채운 항목 {filled}/{total}" + (f" · 비어 있는 핵심 항목: {', '.join(missing)}" if missing else " · 핵심 항목을 모두 채웠어요"))
    print("고치려면: insia profile edit-template → 파일 수정 → insia profile import <파일>")
    return 0


def _template_format(args: argparse.Namespace, out: str | None) -> str:
    if args.format:
        return args.format
    if out and out != "-":
        suffix = Path(out).suffix.lower()
        if suffix in (".yaml", ".yml"):
            return "yaml"
        if suffix == ".json":
            return "json"
    return "yaml" if _yaml_module() is not None else "json"


def cmd_profile_edit_template(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        profile = Profile() if args.blank else ws.get_profile()
    fmt = _template_format(args, args.out)
    out = args.out or f"profile.{fmt}"
    if not args.out and not os.access(Path.cwd(), os.W_OK):  # e.g. inside the Docker image: use the workspace volume
        out = str(_home(settings) / f"profile.{fmt}")
    text = profile_yaml(profile) if fmt == "yaml" else profile_json_template(profile)
    path = _write_output(text, out, force=args.force, what="프로필 양식")
    if path is not None:
        print(f"프로필 양식을 만들었어요: {path.resolve()}" + ("" if args.blank else " (지금 저장된 내용을 채워 뒀어요)"))
        print(f"메모장 등으로 열어 채우고 저장한 뒤: insia profile import {path}")
        if fmt == "yaml" and _yaml_module() is None:
            _warn(f"YAML을 다시 읽으려면 PyYAML이 필요해요: {DOCS_EXTRA_HINT}. 설치가 어려우면 --format json을 쓰세요.")
    return 0


def cmd_profile_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    data = load_structured_file(path, "프로필 파일")
    profile, ignored = profile_from_data(data, str(path))
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        current = ws.get_profile()
        if args.merge:
            merged = current.model_dump(exclude={"updated_at"})
            merged.update({k: v for k, v in profile.model_dump(exclude={"updated_at"}).items() if v not in ("", [], None)})
            profile = Profile.model_validate(merged)
        before = current.model_dump(exclude={"updated_at"})
        after = profile.model_dump(exclude={"updated_at"})
        changed = [_field_label(k) for k in after if after[k] != before.get(k)]
        saved = ws.save_profile(profile)
    filled, total, missing = _profile_fill(saved)
    print(f"프로필을 저장했어요 ({'합치기' if args.merge else '전체 바꾸기'}) · 채운 항목 {filled}/{total}")
    print("  바뀐 항목: " + (", ".join(changed) if changed else "없음"))
    if missing:
        print(f"  비어 있는 핵심 항목: {', '.join(missing)}")
    if ignored:
        _warn(f"알 수 없는 항목은 무시했어요: {', '.join(ignored)} (항목 이름은 insia profile edit-template 양식을 보세요)")
    return 0


def cmd_profile_export(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        profile = ws.get_profile()
    fmt = args.format or ("yaml" if args.out and Path(args.out).suffix.lower() in (".yaml", ".yml") else "json")
    if fmt == "yaml":
        text = profile_yaml(profile)
    else:
        text = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    path = _write_output(text, args.out, force=args.force, what="프로필")
    if path is not None:
        print(f"프로필을 저장했어요: {path.resolve()}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def cmd_docs_add(args: argparse.Namespace) -> int:
    path = Path(args.file)
    text, kind, notes = extract_document(path)
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        duplicate = next((d for d in ws.list_documents() if d.text == text), None)
        if duplicate is not None and not args.force:
            if args.json:
                _print_json({"added": False, "duplicate_of": duplicate.id, "document": duplicate.model_dump(mode="json")})
            else:
                print(f"같은 내용의 자료가 이미 있어요: {duplicate.id} · {duplicate.title}. 그래도 추가하려면 --force를 붙여 주세요.")
            return 0
        doc = ws.add_document(args.title or path.stem, text, kind=kind, filename=path.name)
        try:  # keep the original next to the extracted text (backup = the workspace folder)
            shutil.copy2(path, ws.uploads_dir / f"{doc.id}_{_safe_filename(path.name)}")
        except OSError as exc:
            notes.append(f"원본 파일을 워크스페이스에 복사하지 못했어요 ({exc.strerror or exc}). 글자는 저장했어요.")
    if args.json:
        _print_json({"added": True, "document": doc.model_dump(mode="json"), "notes": notes})
        return 0
    print(f"자료를 추가했어요: {doc.id} · {doc.title} · {doc.chars:,}자 ({DOC_KIND_LABELS.get(doc.kind, doc.kind)})")
    for note in notes:
        print(f"참고: {note}")
    if doc.chars > settings.max_document_chars:
        print(f"참고: 실행 1번에 모델로 보내는 자료는 모두 합쳐 {settings.max_document_chars:,}자까지예요. 넘는 부분은 잘라서 "
              "보내고 실행 기록에 알려 줘요 (INSIA_MAX_DOCUMENT_CHARS).")
    print("실행하면 리서치에 '사용자 제공 자료'로 들어가요 (기본 --docs all, 골라 쓰려면 --docs u1,u3).")
    return 0


def cmd_docs_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        docs = ws.list_documents()
    if args.json:
        _print_json([d.model_dump(mode="json") for d in docs])
        return 0
    if not docs:
        print("아직 자료가 없어요. 회사 소개서·IR 자료·보도자료 등을 넣어 주세요: insia docs add <파일> (txt, md, pdf, docx)")
        return 0
    rows = [[d.id, DOC_KIND_LABELS.get(d.kind, d.kind), f"{d.chars:,}자", _kst(d.created_at, "%Y-%m-%d"), d.filename or "-", d.title]
            for d in docs]
    print_table(["ID", "종류", "글자 수", "추가일", "파일", "제목"], rows, max_widths=[0, 0, 0, 0, 24, 40])
    total = sum(d.chars for d in docs)
    print(f"\n자료 {len(docs)}개 · 모두 {total:,}자 · 실행 1번에 최대 {settings.max_document_chars:,}자를 모델로 보내요")
    return 0


def cmd_docs_show(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        doc = ws.get_document(args.doc_id)
    if doc is None:
        raise CommandError(f"자료 {args.doc_id}을(를) 찾을 수 없어요. 'insia docs list'로 id를 확인해 주세요.")
    if args.json:
        _print_json(doc.model_dump(mode="json"))
        return 0
    print(f"{doc.id} · {doc.title} · {doc.chars:,}자 ({DOC_KIND_LABELS.get(doc.kind, doc.kind)}) · {doc.filename or '-'}")
    print("-" * 40)
    print(doc.text)
    return 0


def cmd_docs_rm(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        doc = ws.get_document(args.doc_id)
        if doc is None or not ws.delete_document(doc.id):
            raise CommandError(f"자료 {args.doc_id}을(를) 찾을 수 없어요. 'insia docs list'로 id를 확인해 주세요.")
    print(f"자료 {doc.id}({doc.title})를 지웠어요. 이미 만든 초안과 실행 기록의 출처 표시는 그대로 남아요.")
    return 0


# ---------------------------------------------------------------------------
# calendar: plan-week, run-due, calendar list|generate|skip|move
# ---------------------------------------------------------------------------


def _next_monday(today: str) -> str:
    day = date.fromisoformat(today)
    return (day + timedelta(days=(7 - day.weekday()) % 7)).isoformat()


class _UsageSink:
    """``backend.on_usage`` for planning calls: records to the workspace and sums the cost."""

    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.cost = 0.0

    def __call__(self, record: Any) -> None:
        from .pipeline import record_cost

        cost = record_cost(record, home=self.ws.home)
        self.cost += cost
        try:
            self.ws.record_usage(record.model_copy(update={"cost_usd": cost}))
        except Exception:  # noqa: BLE001 - a usage row must never break planning
            pass


def _slot_rows(slots: Sequence[Any]) -> list[list[Any]]:
    return [[f"{s.date}({_weekday(s.date)})", channel_label(s.channel), SLOT_STATUS_LABELS.get(s.status, s.status), s.topic,
             s.angle or "-", ", ".join(s.keywords[:3]) or "-", s.id] for s in slots]


def _weekend_arg(value: str | None) -> str | list[str] | None:
    """``--weekend``: ``None`` (weekdays only), ``"all"`` / ``"none"``, or channel ids (CLI aliases such as
    블로그·인스타 resolved; the planner checks the names)."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("all", "none", "", "전체", "모두", "없음"):
        return text or "none"
    return [CHANNEL_ALIASES.get(part, part) for part in re.split(r"[,\s]+", text) if part]


def cmd_plan_week(args: argparse.Namespace) -> int:
    from .backends import create_backend
    from .planner import PlanningError, plan_week, weekend_channel_set
    from .server import replacing_planned_slots, sigterm_as_interrupt

    settings = _settings_from_args(args)
    start = _resolve_day(args.start, settings) or _next_monday(settings.today)
    if not 1 <= args.days <= 31:
        raise UsageError("--days는 1~31 사이로 적어 주세요")
    end = (date.fromisoformat(start) + timedelta(days=args.days - 1)).isoformat()
    counts = {ch: getattr(args, attr) for ch, attr in (("naver_blog", "blog"), ("linkedin", "linkedin"),
                                                       ("instagram", "instagram"), ("bizplan", "bizplan"))
              if getattr(args, attr) is not None}
    if not counts:
        counts = dict(DEFAULT_PLAN_COUNTS)
    weekend = _weekend_arg(args.weekend)
    try:
        weekend_channel_set(weekend)  # a wrong --weekend stops here, before --replace touches any slot
    except PlanningError as exc:
        raise UsageError(f"--weekend: {exc}") from None
    mode, note = resolve_mode(settings.mode)
    backend = create_backend(mode, settings)
    quiet = args.json
    with open_workspace(settings) as ws:
        existing = [s for s in ws.list_slots(date_from=start, date_to=end) if s.status == "planned"]
        sink = _UsageSink(ws)
        backend.on_usage = sink  # type: ignore[attr-defined]
        if not quiet:
            print(note)
            wanted = " · ".join(f"{channel_label(c)} {n}편" for c, n in counts.items() if n)
            print(f"{start}({_weekday(start)}) ~ {end}({_weekday(end)}) 계획을 세우는 중이에요 · {wanted}"
                  + (" (live 모드는 1분쯤 걸려요)" if mode == "live" else ""), flush=True)
        try:
            # --replace: the range's planned slots are skipped first, and put back if planning fails or is
            # stopped (Ctrl+C, or SIGTERM from a service manager / `kill`, which would otherwise end the process
            # before they are put back)
            with sigterm_as_interrupt(), replacing_planned_slots(ws, start, end, enabled=args.replace) as replaced:
                week = plan_week(ws, backend, args.theme or "", start, end, counts, weekend_channels=weekend)
        except PlanningError as exc:  # wrong dates or counts: a usage error (exit 2)
            raise UsageError(str(exc)) from None
        except KeyboardInterrupt:
            print("\n계획을 멈췄어요. 새 계획은 저장하지 않았고, 기존 계획은 그대로예요.", file=sys.stderr)
            return 130
    if quiet:
        _print_json({**week.model_dump(mode="json"), "replaced": [slot.id for slot in replaced]})
        return 0
    if week.summary:
        print(f"\n전략: {week.summary}")
    if week.slots:
        print()
        print_table(["날짜", "채널", "상태", "주제", "관점", "키워드", "슬롯 ID"], _slot_rows(week.slots),
                    max_widths=[0, 0, 0, 40, 18, 24, 0])
    for notice in week.notices:
        print(f"참고: {notice}")
    if replaced:
        print(f"참고: 이 기간에 있던 계획 {len(replaced)}개는 건너뜀으로 바꿨어요.")
    elif existing:
        print(f"참고: 이 기간의 기존 계획 {len(existing)}개는 그대로 두었어요. 지우고 새로 짜려면 --replace를 붙여 다시 실행하세요.")
    print(_cost_line(sink.cost, mode))
    if week.slots:
        print(f"\n초안 만들기: insia run-due (오늘까지 예정분) · insia run-due --until {end} (이번 계획 전부)")
    return 0


def cmd_run_due(args: argparse.Namespace) -> int:
    from .actions import generate_slot
    from .db import RunTakenOverError, WorkspaceError
    from .pipeline import BudgetExceeded, PipelineError, RunCancelled, prepare_run

    settings = _settings_from_args(args, **_fast(args))
    if not args.out:
        from dataclasses import replace

        settings = replace(settings, out_dir=None)  # the workspace is the store; --out adds files
    until = _resolve_day(args.until, settings) or settings.today
    printer = ProgressPrinter(quiet=args.quiet)
    with open_workspace(settings) as ws:
        if not args.dry_run:
            # a killed/crashed run-due (or server) left runs 'running' and slots 'generating': close them so the
            # slots come back as 'planned' (runs another live process is working on are left alone)
            recovered = ws.recover_stale()
            if recovered:
                print(f"지난번에 끝나지 못한 실행 {len(recovered)}개를 '중단됨'으로 정리했어요 ({', '.join(recovered)}). "
                      "만들다 멈춘 슬롯은 이번에 다시 만들어요.")
        for stuck in (s for s in ws.list_slots(date_to=until) if s.status == "generating"):
            where = f"{stuck.date} {channel_label(stuck.channel)} · {stuck.topic}"
            if stuck.run_id and (ws.run_owner(stuck.run_id) or {}).get("live"):
                print(f"건너뜀: {where} — 지금 다른 곳에서 초안을 만드는 중이에요 (실행 {stuck.run_id})")
            elif args.dry_run:
                print(f"멈춘 슬롯: {where} — 초안을 만들다 멈췄어요. --dry-run 없이 실행하면 정리하고 다시 만들어요.")
            else:
                print(f"멈춘 슬롯: {where} — 방금 멈춘 것 같아요. 1분쯤 뒤 insia run-due를 다시 실행하면 만들어요.")
        due = ws.due_slots(until)
        if not due:
            print(f"{until}까지 만들 초안이 없어요.")
            return 0
        total = len(due)
        if args.limit:
            due = due[: args.limit]
        if args.dry_run:
            print(f"{until}까지 초안을 만들 슬롯 {total}개" + (f" (이번에 {len(due)}개)" if len(due) < total else ""))
            print_table(["날짜", "채널", "상태", "주제", "관점", "키워드", "슬롯 ID"], _slot_rows(due), max_widths=[0, 0, 0, 40, 18, 24, 0])
            return 0
        mode, note = resolve_mode(settings.mode)
        if mode == "mock" and settings.mode == "auto":
            raise CommandError("API 키가 없어서 데모(mock) 초안만 만들 수 있어요. ANTHROPIC_API_KEY를 설정하거나, "
                               "연습용이면 --mode mock을 붙여 주세요.")
        print(f"초안을 만들 슬롯 {len(due)}개 ({until}까지{f', 전체 {total}개 중' if len(due) < total else ''}) · {note}")
        created: list[Any] = []
        failed: list[tuple[Any, str]] = []
        skipped: list[tuple[Any, str]] = []
        cost = 0.0
        for index, slot in enumerate(due, 1):
            print(f"\n[{index}/{len(due)}] {slot.date}({_weekday(slot.date)}) {channel_label(slot.channel)} · {slot.topic}",
                  flush=True)
            current = ws.get_slot(slot.id)
            if current is None or current.status != "planned":
                # changed while earlier slots were being drafted (skipped by hand, set aside by a re-plan with
                # --replace, deleted, drafted elsewhere): never spend a paid draft on it
                reason = "그사이 캘린더에서 지워졌어요" if current is None else \
                    f"그사이 '{SLOT_STATUS_LABELS.get(current.status, current.status)}' 상태로 바뀌었어요"
                skipped.append((slot, reason))
                print(f"→ 건너뛰었어요: {reason}")
                continue
            backend, bus, _ = prepare_run(settings)
            try:
                job = generate_slot(ws, slot.id, settings=settings, backend=backend, bus=bus, listener=printer)
            except KeyboardInterrupt:
                print(f"\n중단했어요. 남은 슬롯은 다음 run-due 때 만들어요 (중단한 실행 이어서 하기: insia resume {bus.run_id})",
                      file=sys.stderr)
                return 130
            except BudgetExceeded as exc:
                failed.append((slot, str(exc)))
                print(f"→ 예산 상한에 걸렸어요: {exc}", file=sys.stderr)
            except (RunCancelled, RunTakenOverError) as exc:  # another process took this run over (it goes on there)
                skipped.append((slot, str(exc)))
                print(f"→ 건너뛰었어요: {exc}")
            except WorkspaceError as exc:  # claimed by another process meanwhile, or already drafted
                skipped.append((slot, str(exc)))
                print(f"→ 건너뛰었어요: {exc}")
            except Exception as exc:  # noqa: BLE001 - one broken slot must not stop the cron run
                message = str(exc) if isinstance(exc, (BackendError, PipelineError)) else _error_text(exc)
                failed.append((slot, message))
                print(f"→ 실패했어요: {message}", file=sys.stderr)
            else:
                created.append(job)
                review = job.review
                verdict = f"{review.score}점 · {_verdict(review.passed)}" if review else "검수 전"
                print(f"→ 보관함 {job.item.id if job.item else '-'} · {verdict}")
            finally:
                try:
                    cost += ws.run_cost(bus.run_id)
                except Exception:  # noqa: BLE001
                    pass
    print(f"\n초안 {len(created)}개를 만들었어요" + (f" · 실패 {len(failed)}개" if failed else "")
          + (f" · 건너뜀 {len(skipped)}개" if skipped else "") + f" · {_cost_line(cost, mode)}")
    if len(due) < total:
        print(f"남은 슬롯 {total - len(due)}개는 다음에 만들어요 (--limit {args.limit}).")
    if created:
        print("보관함에서 검토해 주세요: insia items list --status draft (수정 필요는 --status needs_changes)")
    for slot, message in failed:
        print(f"실패: {slot.date} {channel_label(slot.channel)} · {slot.topic} — {message}", file=sys.stderr)
    return 1 if failed else 0


def cmd_calendar_list(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    start = _resolve_day(args.date_from, settings) or (date.fromisoformat(settings.today)
                                                       - timedelta(days=date.fromisoformat(settings.today).weekday())).isoformat()
    end = _resolve_day(args.date_to, settings) or (date.fromisoformat(start) + timedelta(days=13)).isoformat()
    with open_workspace(settings) as ws:
        slots = ws.list_slots(date_from=start, date_to=end)
    if not args.all:
        slots = [s for s in slots if s.status != "skipped"]
    if args.json:
        _print_json([s.model_dump(mode="json") for s in slots])
        return 0
    if not slots:
        print(f"{start} ~ {end}에 계획된 게시물이 없어요. 계획 세우기: insia plan-week --theme \"…\"")
        return 0
    print(f"{start} ~ {end} 콘텐츠 캘린더")
    print_table(["날짜", "채널", "상태", "주제", "관점", "키워드", "슬롯 ID"], _slot_rows(slots), max_widths=[0, 0, 0, 40, 18, 24, 0])
    drafted = [s for s in slots if s.item_id]
    if drafted:
        print(f"\n초안이 있는 슬롯은 보관함에서 봐요: insia items show {drafted[0].item_id}")
    return 0


def cmd_calendar_generate(args: argparse.Namespace) -> int:
    from .actions import generate_slot

    settings = _settings_from_args(args, **_fast(args))
    if not args.out:
        from dataclasses import replace

        settings = replace(settings, out_dir=None)
    with open_workspace(settings) as ws:
        slot_id = _slot_id(ws, args.slot_id)
        job = generate_slot(ws, slot_id, settings=settings, listener=ProgressPrinter(quiet=args.quiet), force=args.force)
        cost = ws.run_cost(job.run_id)
    review = job.review
    print(f"\n보관함 {job.item.id if job.item else '-'} · " + (f"{review.score}점 · {_verdict(review.passed)}" if review else "검수 전"))
    print(_cost_line(cost, _job_mode(settings)))
    return 0


def cmd_calendar_skip(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        slot = ws.update_slot(_slot_id(ws, args.slot_id), status="skipped")
    print(f"건너뛰기로 했어요: {slot.date} {channel_label(slot.channel)} · {slot.topic}")
    return 0


def cmd_calendar_move(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    day = _resolve_day(args.date, settings)
    with open_workspace(settings) as ws:
        slot_id = _slot_id(ws, args.slot_id)
        slot = ws.update_slot(slot_id, date=day)
        if slot.item_id:
            detail = ws.get_item(slot.item_id)
            if detail is not None and detail.item.status in ("draft", "needs_changes", "approved"):
                ws.update_item(slot.item_id, scheduled_at=day)
    print(f"날짜를 바꿨어요: {slot.date}({_weekday(slot.date)}) {channel_label(slot.channel)} · {slot.topic}")
    return 0


# ---------------------------------------------------------------------------
# import-run (Claude Code run folders): the importer itself is importer.import_run_folder
# ---------------------------------------------------------------------------


def cmd_import_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        if args.run_id is not None:
            if not _SAFE_RUN_ID.match(args.run_id) or ".." in args.run_id:
                raise UsageError(f"--run-id는 영문·숫자·'.-_'로 적어 주세요 (받은 값: {args.run_id!r})")
        report = import_run_folder(ws, Path(args.folder), settings, run_id=args.run_id)
    if args.json:
        _print_json(report.to_dict())
        return 1 if report.problems else 0
    verb = "가져왔어요" if report.created else "다시 가져왔어요 (같은 실행을 갱신)"
    print(f"Claude Code 실행 폴더를 보관함으로 {verb}: {report.folder}")
    print(f"  실행 {report.run_id} · 주제: {report.topic}")
    parts = ["계획 있음" if report.plan else "계획 없음"]
    if report.research:
        parts.append(f"리서치 근거 {report.research[0]}개 · 출처 {report.research[1]}개")
    print("  " + " · ".join(parts))
    for entry in report.channels:
        scores = " → ".join(f"R{d.round} {r.score}점" if r else f"R{d.round} 검수 전" for d, r in entry.rounds)
        verdict = f" · {_verdict(entry.result.passed)}" if entry.result else ""
        change = f"버전 {entry.added}개 추가" if entry.added else "변경 없음"
        if entry.reviews_updated:
            change += f", 검수 {entry.reviews_updated}개 갱신"
        print(f"  - {channel_label(entry.channel)}: {scores}{verdict} · {change} · {entry.item_id}")
        if entry.score_changes:
            print(f"    (현재 코드 기준으로 점수를 다시 계산: {', '.join(entry.score_changes)})")
    for note in report.notes:
        print(f"참고: {note}")
    for problem in report.problems:
        print(f"문제: {problem}", file=sys.stderr)
    print("보관함에서 검토·승인·내보내기를 해요: insia items list  (대시보드 '보관함'에서도 보여요). 게시는 사람이 직접 해요.")
    return 1 if report.problems else 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _find_near(path: Path, name: str) -> Path | None:
    for folder in (path.parent, path.parent.parent, path.parent.parent.parent):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _find_brief_near(path: Path) -> Path | None:  # kept for callers of the old name
    return _find_near(path, "brief.json")


def _load_profile_file(path: Path) -> Profile | None:
    from .db import profile_is_empty

    profile, _ = profile_from_data(load_structured_file(path, "프로필 파일"), str(path))
    return None if profile_is_empty(profile) else profile


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.draft)
    try:
        draft = Draft.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise UsageError(f"초안 파일을 읽을 수 없어요: {path} ({exc.strerror})") from None
    except ValidationError as exc:
        raise UsageError(f"Draft JSON 형식이 아니에요: {path} ({exc.error_count()}개 오류)") from None
    brief_path = Path(args.brief) if args.brief else _find_near(path, "brief.json")
    brief = None
    if brief_path is not None:
        try:
            brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValidationError):
            raise UsageError(f"브리프 파일을 읽을 수 없어요: {brief_path}") from None
    profile: Profile | None = None
    profile_source = ""
    if args.profile:
        profile = _load_profile_file(Path(args.profile))
        profile_source = args.profile
    elif args.workspace_profile:
        settings = _settings_from_args(args)
        with open_workspace(settings) as ws:
            profile = _saved_profile(ws)
        profile_source = f"워크스페이스 ({_home(settings)})"
    elif not args.no_profile:
        near = _find_near(path, "profile.json")
        if near is not None:
            try:
                profile = _load_profile_file(near)
            except UsageError as exc:
                _warn(f"{near}을(를) 읽지 못해 프로필 검사는 건너뛰어요: {exc}")
            profile_source = str(near)
    checks = check_format(draft, brief, profile)
    if args.json:
        print(json.dumps([c.model_dump() for c in checks], ensure_ascii=False, indent=1))
    else:
        print(f"{channel_label(draft.channel)} R{draft.round} · {draft.title}")
        if brief_path is not None:
            print(f"브리프: {brief_path}")
        if profile is not None:
            print(f"프로필: {profile_source} (금지 표현·필수 문구·블라인드 검사 포함)")
        elif profile_source:
            print(f"프로필: {profile_source} (비어 있어 프로필 검사는 건너뜀)")
        for c in checks:
            mark = "통과" if c.passed else "미충족"
            print(f"- [{mark}] {c.label}: {c.value} (기준 {c.expected})")
        passed = sum(1 for c in checks if c.passed)
        print(f"{passed}/{len(checks)}개 통과")
    return 0 if all(c.passed for c in checks) else 1


# ---------------------------------------------------------------------------
# eval (quality evaluation set: evals/cases, see evals/README.md)
# ---------------------------------------------------------------------------


def cmd_eval_list(args: argparse.Namespace) -> int:
    from .evals.cases import CaseError, case_summary, find_cases_dir, load_cases

    cases_dir = find_cases_dir(args.cases)
    try:
        cases = load_cases(cases_dir)
    except CaseError as exc:
        raise UsageError(str(exc)) from None
    rows = [case_summary(case) for case in cases]
    if args.json:
        _print_json(rows)
        return 0
    print(f"평가 케이스 {len(rows)}개 ({cases_dir})")
    print_table(["ID", "채널", "프로필", "자료", "필수", "권장", "제목"],
                [[r["id"], ", ".join(channel_label(c) for c in r["channels"]), "있음" if r["profile"] else "-",
                  r["documents"] or "-", r["must"], r["should"], r["title"]] for r in rows],
                max_widths=[0, 40, 0, 0, 0, 0, 48])
    print("\n돌리기: insia eval run --mode mock (무료) · 비교: insia eval compare <기준 폴더> <이번 폴더>")
    return 0


def cmd_eval_run(args: argparse.Namespace) -> int:
    from .evals.runner import DEFAULT_TIMEOUT_S, EvalOptions, ask_yes_no, run_eval

    case_ids = [c for value in (args.case or []) for c in _split(value)]
    channels = [CHANNEL_ALIASES.get(c.lower(), c.lower()) for c in _split(args.channels)]
    options = EvalOptions(
        mode=args.mode, cases_dir=Path(args.cases) if args.cases else None, case_ids=case_ids, channels=channels,
        out_dir=Path(args.out) if args.out else None, max_cost_usd=args.max_cost_usd, model=args.model,
        reps=args.reps, baseline=Path(args.baseline) if args.baseline else None, max_score_drop=args.max_score_drop,
        timeout_s=args.timeout_s or DEFAULT_TIMEOUT_S, resume=args.resume, dry_run=args.dry_run,
        estimate_from=Path(args.estimate_from) if args.estimate_from else None, judge=args.judge,
        judge_model=args.judge_model or "", assume_yes=args.yes, max_rounds=args.max_rounds, pass_score=args.pass_score,
    )
    stream = sys.stderr if args.json else sys.stdout
    summary = run_eval(options, printer=lambda line: print(line, file=stream, flush=True), confirm=ask_yes_no)
    if args.json:
        _print_json(summary)
    return int(summary.get("exit_code", 1))


def cmd_eval_compare(args: argparse.Namespace) -> int:
    from .evals.compare import compare_summaries, render_compare
    from .evals.runner import load_summary

    baseline, current = load_summary(args.baseline), load_summary(args.current)
    result = compare_summaries(baseline, current, max_score_drop=args.max_score_drop, baseline_dir=args.baseline,
                               current_dir=args.current)
    if args.json:
        _print_json(result)
    else:
        print(render_compare(result).rstrip())
    return int(result["exit_code"])


# ---------------------------------------------------------------------------
# publish (LinkedIn / Instagram API publishing — only after a person confirms, one post at a time)
# ---------------------------------------------------------------------------
# Only the cmd_publish_* / _publish_* functions below import insia_agents.publishers (AST guard:
# tests/test_no_autopublish_surface.py). ``send`` runs only on a terminal and asks for a random code that
# exists nowhere but on that terminal; there is no --yes and no scheduling option anywhere.

PUBLISH_NOT_TTY_MESSAGE = ("insia publish send는 사람이 터미널에서 직접 확인할 때만 돌아요. 예약·스크립트·cron에서는 쓸 수 없어요 "
                           "(LinkedIn API 약관이 자동 게시를 금지해요).")
PUBLISH_RESOLVE_NOT_TTY_MESSAGE = ("insia publish resolve --published/--not-published는 사람이 터미널에서 직접 확인할 때만 돌아요. "
                                   "(--check는 스크립트에서도 돼요)")
PUBLISH_STATE_LABELS = {"disabled": "꺼짐", "not_configured": "설정 안 함", "not_connected": "연결 안 됨",
                        "connected": "연결됨", "expiring": "곧 만료", "needs_reconnect": "다시 연결 필요",
                        "unavailable": "지금 쓸 수 없음"}
PUBLISH_ATTEMPT_LABELS = {"sending": "게시 중", "published": "게시됨", "failed": "실패", "unknown": "확인 필요",
                          "abandoned": "안 올라감(정리)"}
PUBLISH_STEP_LABELS = {"check": "연결 확인", "polling": "인스타그램이 이미지를 처리하는 중", "carousel": "캐러셀 만들기",
                       "write": "게시 요청 보내는 중", "permalink": "게시물 주소 받는 중"}
PUBLISH_CALLBACK_TIMEOUT = 600.0  # the one-time LinkedIn callback listener waits 10 minutes (the state's lifetime)


def _publish_redact(text: str) -> str:
    """Text for the terminal with registered secret values and secret-looking parameters masked."""
    from .publishers.redact import redact

    return redact(str(text))


def _publish_errors(func: Any) -> Any:
    """Publishing errors → a Korean line (secrets masked) and the error's exit code (1 failed, 2 wrong use)."""
    import functools

    @functools.wraps(func)
    def wrapper(args: argparse.Namespace) -> int:
        from .db import WorkspaceError
        from .publishers import PublishError

        try:
            return func(args)
        except PublishError as exc:
            print(f"오류: {_publish_redact(str(exc))}", file=sys.stderr)
            return int(exc.exit_code)
        except (UsageError, CommandError):
            raise
        except WorkspaceError as exc:
            print(f"오류: {_publish_redact(str(exc))}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - no traceback (it could carry a token), a masked message instead
            print(f"오류: 예상하지 못한 문제가 생겼어요 — {type(exc).__name__}: {_publish_redact(str(exc))}", file=sys.stderr)
            if os.environ.get("INSIA_DEBUG"):
                print(_publish_redact(traceback.format_exc()), file=sys.stderr)
            return 1

    return wrapper


def _env_port() -> int | None:
    """``INSIA_PORT`` when it is a usable port number (the port the container's server and healthcheck use)."""
    raw = (os.environ.get("INSIA_PORT") or "").strip()
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else None


def _publish_server_values() -> dict[str, Any]:
    """The server's own publishing values, read from the environment exactly as ``server.make_server`` reads it
    (``server.env_public_hosts``, ``server.env_flag``) plus ``INSIA_PORT``: the public hosts and the proxy flag
    decide the default LinkedIn redirect URI and the Instagram media mode, so a CLI command must see the same ones
    as a server started from that environment. An invalid ``INSIA_PUBLIC_HOSTS`` stops the command, as it stops
    the server."""
    from . import server as server_module

    try:
        hosts = server_module.env_public_hosts()
    except ValueError as exc:  # ServerConfigError: the server would not start with this value either
        raise CommandError(f"INSIA_PUBLIC_HOSTS 환경 변수를 확인해 주세요. {exc}") from None
    values: dict[str, Any] = {"public_hosts": hosts, "trust_proxy": server_module.env_flag("INSIA_TRUST_PROXY")}
    port = _env_port()
    if port is not None:
        values["server_port"] = port
    return values


def _publish_service(settings: Settings, ws: "Workspace", **kwargs: Any) -> Any:
    """The command's ``PublishService`` (environment + workspace). Tests replace this function.

    The server's own values (``_publish_server_values``) come from the same environment the server reads, so the
    CLI derives the same default LinkedIn redirect URI and media mode as a server started from that environment
    (e.g. ``docker compose exec insia insia publish connect linkedin --paste``)."""
    from .publishers import PublishService

    for key, value in _publish_server_values().items():
        kwargs.setdefault(key, value)
    return PublishService.from_env(ws, **kwargs)


def _publish_requester() -> str:
    """``cli:<user>@<host>`` for the attempt record."""
    import getpass
    import socket

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (containers)
        user = "user"
    host = socket.gethostname() or "localhost"
    return re.sub(r"\s+", "_", f"cli:{user}@{host}")[:200]


def _publish_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


@contextmanager
def _publish_session(args: argparse.Namespace, *, recover: bool = True) -> Iterator[tuple[Settings, "Workspace", Any]]:
    """Workspace + service for one ``publish`` command: refuses when publishing is off (exit 1) and first closes
    attempts a dead process left ``sending`` (the server does this every minute; the CLI at each command)."""
    settings = _settings_from_args(args)
    with open_workspace(settings) as ws:
        service = _publish_service(settings, ws)
        try:
            if not service.settings.enabled:
                raise CommandError(service.settings.disabled_reason or "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0).")
            if recover:
                recovered = service.recover()
                if recovered:
                    _warn(f"주인이 사라진 게시 시도 {len(recovered)}개를 정리했어요 (insia publish attempts로 확인).")
            yield settings, ws, service
        finally:
            try:
                service.shutdown(timeout=5.0)
            except Exception:  # noqa: BLE001 - closing must not hide the command's own result
                pass


def _publish_label(platform: str) -> str:
    from .publishers import platform_label

    return platform_label(platform)


def _publish_options(args: argparse.Namespace, detail: ContentItemDetail) -> tuple[str, dict[str, Any]]:
    """``(platform, options)`` for ``preview`` / ``send``. Instagram needs ``--ai-label yes|no`` (no default,
    DESIGN.md 14.2); ``--visibility`` is LinkedIn only. Wrong combinations are usage errors (exit 2)."""
    from .publishers import channel_platform, parse_preview_options

    platform = args.platform or channel_platform(detail.item.channel)
    if platform is None:
        raise CommandError(f"{channel_label(detail.item.channel)}은(는) API로 게시하지 않아요. 'insia items export'로 "
                           "파일을 받아 직접 올린 뒤 'insia items publish'로 표시해 주세요.")
    if platform == "instagram":
        if args.visibility:
            raise UsageError("--visibility는 LinkedIn에서만 써요.")
        if args.ai_label is None:
            raise UsageError("인스타그램은 'AI 정보' 라벨을 붙일지 게시마다 꼭 골라야 해요: --ai-label yes (붙여요) 또는 "
                             "--ai-label no (붙이지 않아요)")
        raw: dict[str, Any] = {"is_ai_generated": args.ai_label == "yes"}
    else:
        if args.ai_label is not None:
            raise UsageError("--ai-label은 인스타그램에서만 써요.")
        raw = {"visibility": args.visibility} if args.visibility else {}
    return platform, parse_preview_options(platform, raw).to_json()


def _publish_print_preview(result: Any) -> None:
    """The preview as the person must see it before confirming (DESIGN.md 8-3)."""
    from .publishers.base import VISIBILITY_LABELS

    label = _publish_label(result.platform)
    account, item, content = result.account or {}, result.item or {}, result.content or {}
    options = content.get("options") or {}
    print(f"{label}에 게시하기 전에 확인해 주세요")
    who = account.get("name") or account.get("id_hint") or "(이름을 받지 못했어요)"
    line = f"  계정: {who}" + (f" ({account['kind']})" if account.get("kind") else "")
    if result.platform == "linkedin":
        line += f"   공개 범위: {VISIBILITY_LABELS.get(options.get('visibility', 'PUBLIC'), options.get('visibility', ''))}"
    elif "is_ai_generated" in options:
        line += "   AI 정보 라벨: " + ("붙여요" if options["is_ai_generated"] else "붙이지 않아요")
    print(line)
    score = item.get("approved_score")
    verdict = "강제 승인(검수 미통과)" if item.get("approval_forced") else "승인"
    print(f"  콘텐츠: v{item.get('version', '?')} · {item.get('title', '')} · {verdict}"
          + (f" · 검수 {score}점" if score is not None else ""))
    chars, limit = content.get("chars"), content.get("limit")
    size = f" ({chars:,} / {limit:,}자)" if isinstance(chars, int) and isinstance(limit, int) else ""
    print(f"---------- {'캡션' if result.platform == 'instagram' else '게시될 글'}{size} ----------")
    print(content.get("text", ""))
    print("---------- 끝 ----------")
    if result.slides:
        print(f"슬라이드 {len(result.slides)}장 (보낼 JPEG 그대로)")
        for slide in result.slides:
            alt = str(slide.get("alt") or "(대체텍스트 없음)")
            kb = f"{int(slide.get('bytes') or 0) / 1024:,.0f}KB"
            print(f"  {slide.get('n')}. {slide.get('width')}×{slide.get('height')} · {kb} · {_fit(alt, 60)}")
    for title, issues in (("고칠 부분", result.errors), ("확인할 부분", result.warnings)):
        if issues:
            print(title)
            for issue in issues:
                print(f"  - {issue.message}")
    if result.notices:
        print("알아 두세요")
        for notice in result.notices:
            print(f"  - {notice.get('message', '')}")
    if result.first_comment_link:
        print(f"첫 댓글로 달 링크(게시한 뒤 직접): {result.first_comment_link}")


def _publish_step_printer(platform: str) -> Any:
    def on_step(step: str) -> None:
        text = str(step or "")
        match = re.match(r"^children (\d+)/(\d+)$", text)
        shown = f"이미지 등록 {match.group(1)}/{match.group(2)}" if match else PUBLISH_STEP_LABELS.get(text, text)
        print(f"  · {shown}", flush=True)

    return on_step


def _publish_print_outcome(attempt: Any, label: str, *, first_comment_link: str = "") -> int:
    """Print a finished attempt; the exit code (0 published, 1 failed or unknown)."""
    if attempt.status == "published":
        if attempt.permalink:
            print(f"게시했어요: {attempt.permalink}")
            if attempt.platform == "linkedin":
                print("  (링크가 열리지 않으면 LinkedIn 내 활동에서 확인해 주세요)")
        else:
            print("게시했어요. 게시물 주소를 받지 못했어요. 게시물 주소를 알면 대시보드의 게시 기록에서 넣어 주세요(선택).")
        problem = (attempt.state or {}).get("item_update_error")
        if problem:
            print(f"  {problem}")
        if first_comment_link:
            print(f"  이제 첫 댓글로 링크를 달아 주세요: {first_comment_link}")
        return 0
    if attempt.status == "unknown":
        print(f"게시됐는지 확인하지 못했어요: {attempt.error}", file=sys.stderr)
        extra = f" · 인스타그램에서 다시 확인: insia publish resolve {attempt.id} --check" if attempt.platform == "instagram" else ""
        print(f"  {label}에서 확인한 뒤 정리해 주세요: insia publish resolve {attempt.id} --published [--url 주소] 또는 "
              f"--not-published{extra}", file=sys.stderr)
        return 1
    if attempt.status == "failed":
        reason = attempt.error or "이유를 받지 못했어요."
        tail = "" if "올라가지 않았" in reason or "올리지 않았" in reason else " 아무것도 올라가지 않았어요."
        print(f"게시하지 못했어요: {reason}{tail}", file=sys.stderr)
        return 1
    print(f"게시 기록 {attempt.id}: {PUBLISH_ATTEMPT_LABELS.get(attempt.status, attempt.status)} "
          "(insia publish attempts로 확인해 주세요)", file=sys.stderr)
    return 1


@_publish_errors
def cmd_publish_status(args: argparse.Namespace) -> int:
    with _publish_session(args) as (_settings, _ws, service):
        data = service.status(check=args.check)
    if args.json:
        _print_json(data)
        return 0
    print("API 게시: " + ("켜짐" if data.get("enabled") else "꺼짐")
          + (" · 가짜 게시 모드(테스트용): 실제로 올라가지 않아요" if data.get("fake") else ""))
    if not data.get("configured"):
        print("  아직 설정하지 않았어요. 시작하려면: insia publish setup linkedin (자세한 안내: docs/operations.md)")
    for platform, block in (data.get("platforms") or {}).items():
        state = PUBLISH_STATE_LABELS.get(block.get("state", ""), block.get("state", ""))
        print(f"- {block.get('label') or _publish_label(platform)}: {state}"
              + (f" · {block['reason']}" if block.get("reason") else ""))
        account = block.get("account") or {}
        who = account.get("name") or account.get("username") or account.get("id_hint")
        if who:
            print(f"    계정: {who}" + (f" ({account['kind']})" if account.get("kind") else ""))
        token = block.get("token") or {}
        if token.get("expires_at"):
            days = token.get("days_left")
            print(f"    만료: {_kst(token['expires_at'], '%Y-%m-%d')}"
                  + (f" ({days}일 남음)" if isinstance(days, int) else "")
                  + (" · 추정" if token.get("estimated") else ""))
    media = data.get("media") or {}
    instagram = (data.get("platforms") or {}).get("instagram") or {}
    if instagram and instagram.get("state") != "disabled" and (media.get("url") or media.get("reason")):
        mode = {"listener": "이미지 전용 포트", "main": "대시보드 포트"}.get(str(media.get("mode")), str(media.get("mode")))
        print("- 이미지 공개 주소: " + (f"{media.get('url')} ({mode})" if media.get("valid")
                                     else (media.get("reason") or "없음")))
    return 0


@_publish_errors
def cmd_publish_setup(args: argparse.Namespace) -> int:
    with _publish_session(args) as (_settings, _ws, service):
        block = service.platform_status("linkedin")
        app = block.get("app") or {}
        if app.get("source") == "env":
            print("LinkedIn 앱 정보는 환경 변수에서 설정됨이에요 (INSIA_LINKEDIN_CLIENT_ID 등). 여기서는 바꿀 수 없어요.")
            return 0
        print("LinkedIn 개발자 앱(https://www.linkedin.com/developers/apps)의 Auth 탭에서 값을 복사해 넣어 주세요.")
        print("비워 두면 저장된 값을 그대로 둬요.")
        try:
            client_id = input("Client ID" + (" [저장됨]" if app.get("client_id_set") else "") + ": ").strip()
            import getpass

            secret = getpass.getpass("Primary Client Secret (화면에 보이지 않아요)"
                                     + (" [저장됨]" if app.get("client_secret_set") else "") + ": ").strip()
            suggested = app.get("redirect_uri") or ""
            # empty keeps what applies now (the stored value, or the default derived from the server's address)
            redirect = input(f"Redirect URI [{suggested}]: ").strip()
        except EOFError:
            raise CommandError("입력이 끝나 저장하지 않았어요.") from None
        block = service.save_linkedin_app(client_id=client_id or None, client_secret=secret or None,
                                          redirect_uri=redirect or None)
    app = block.get("app") or {}
    print("저장했어요. LinkedIn 개발자 앱 Auth 탭의 Authorized redirect URLs에 이 주소를 그대로 넣어 주세요:")
    print(f"  {app.get('redirect_uri', '')}")
    print("다음: insia publish connect linkedin (대시보드가 켜져 있으면 브랜드·자료 → API 게시 연결에서도 돼요)")
    return 0


def _publish_connect_linkedin(args: argparse.Namespace, service: Any) -> dict[str, Any]:
    """``connect linkedin``: ``--paste`` (print the authorize URL, read the pasted address-bar URL) or the one-time
    callback listener (``publishers.oauth.OneShotCallbackListener``: the redirect URI's port on 127.0.0.1 and ::1,
    loopback ``Host`` only, only the state this command just got). Either way ``linkedin_complete`` checks the
    state (issued here, once, 10 minutes) and exchanges the code."""
    import hmac
    import webbrowser

    from .publishers import LINKEDIN_CALLBACK_PATH
    from .publishers.oauth import OneShotCallbackListener

    start = service.linkedin_connect(request_origin="")
    if args.paste:
        print("1) 아래 주소를 브라우저에서 열어 LinkedIn에 로그인하고 동의해 주세요.")
        print(f"   {start.authorize_url}")
        print("2) 동의하면 브라우저가 다른 주소로 이동해요. '연결할 수 없음'이 떠도 괜찮아요.")
        print("   그 탭의 주소창 주소 전체를 복사해 아래에 붙여 넣어 주세요 (10분 안에).")
        try:
            pasted = input("주소: ").strip()
        except EOFError:
            raise CommandError("입력이 끝나 연결하지 않았어요.") from None
        if not pasted:
            raise CommandError("붙여 넣은 주소가 없어 연결하지 않았어요.")
        return service.linkedin_complete(pasted)
    redirect = urllib.parse.urlsplit(start.redirect_uri)
    if (redirect.hostname or "").lower() not in ("localhost", "127.0.0.1", "::1"):
        raise CommandError(f"Redirect URI({start.redirect_uri})가 이 컴퓨터(localhost)가 아니라서 여기서 바로 받을 수 없어요. "
                           "--paste로 연결하거나 대시보드의 브랜드·자료 → API 게시 연결에서 연결해 주세요.")
    port = args.port or redirect.port or 80
    # only the browser coming back from *this* authorization ends the wait (another local request cannot)
    expected = (urllib.parse.parse_qs(urllib.parse.urlsplit(start.authorize_url).query).get("state") or [""])[0]

    def state_ok(state: str) -> bool:
        return bool(expected) and hmac.compare_digest(state.encode("utf-8"), expected.encode("utf-8"))

    listener = OneShotCallbackListener(port, path=redirect.path or LINKEDIN_CALLBACK_PATH, state_ok=state_ok)
    try:
        listener.start()
    except OSError:
        listener.close()
        raise CommandError(f"포트 {port}를 이미 쓰고 있어요. 대시보드 서버가 켜져 있다면 브랜드·자료 → API 게시 연결에서 "
                           "연결해 주세요. 서버 없이 하려면 --paste를 쓰세요.") from None
    try:
        print("브라우저에서 LinkedIn에 로그인하고 동의해 주세요 (10분 안에, 그만두려면 Ctrl+C):")
        print(f"  {start.authorize_url}")
        if not args.no_browser:
            try:
                webbrowser.open(start.authorize_url)
            except Exception:  # noqa: BLE001 - the address is printed above
                pass
        result = listener.wait(PUBLISH_CALLBACK_TIMEOUT)
    finally:
        listener.close()
    if not result:
        raise CommandError("10분이 지나 연결 요청이 끝났어요. 다시 실행해 주세요.")
    return service.linkedin_complete(urllib.parse.urlencode({key: value for key, value in result.items() if value}))


@_publish_errors
def cmd_publish_connect(args: argparse.Namespace) -> int:
    linkedin_only = [flag for flag, on in (("--paste", args.paste), ("--no-browser", args.no_browser),
                                           ("--port", args.port is not None)) if on]
    if args.platform == "instagram" and linkedin_only:
        raise UsageError(f"{', '.join(linkedin_only)}은(는) LinkedIn 연결에서만 써요.")
    if args.platform == "linkedin" and args.token_stdin:
        raise UsageError("--token-stdin은 인스타그램 연결에서만 써요.")
    with _publish_session(args) as (_settings, _ws, service):
        if args.platform == "linkedin":
            block = _publish_connect_linkedin(args, service)
        else:
            if args.token_stdin:
                token = sys.stdin.read().strip()
            else:
                import getpass

                print("Meta 개발자 앱의 Instagram → API setup with Instagram login → Generate token으로 만든 토큰을 붙여 넣어 주세요.")
                try:
                    token = getpass.getpass("인스타그램 토큰 (화면에 보이지 않아요): ").strip()
                except EOFError:
                    token = ""
            if not token:
                raise UsageError("토큰이 비어 있어요. 아무것도 저장하지 않았어요.")
            block = service.save_instagram_token(token)
            token = ""
    account = block.get("account") or {}
    who = account.get("name") or account.get("username") or account.get("id_hint") or ""
    print(f"{_publish_label(args.platform)} 계정을 연결했어요" + (f": {who}" if who else "."))
    expires = (block.get("token") or {}).get("expires_at")
    if expires:
        print(f"  연결 만료: {_kst(expires, '%Y-%m-%d')}" + (" (추정)" if (block.get("token") or {}).get("estimated") else ""))
    if block.get("reason"):
        print(f"  {block['reason']}")
    return 0


@_publish_errors
def cmd_publish_disconnect(args: argparse.Namespace) -> int:
    with _publish_session(args) as (_settings, _ws, service):
        block = service.disconnect(args.platform, forget_app=args.forget_app)
    print(f"INSIA에서 {_publish_label(args.platform)} 토큰을 지웠어요." + (" 앱 정보도 지웠어요." if args.forget_app else ""))
    if block.get("revoke_hint"):
        print(f"  {block['revoke_hint']}")
    return 0


@_publish_errors
def cmd_publish_preview(args: argparse.Namespace) -> int:
    """Show exactly what would be sent. Never issues a confirm code (only ``send`` does, on its own terminal)."""
    with _publish_session(args) as (_settings, ws, service):
        item_id = _item_id(ws, args.item_id)
        platform, options = _publish_options(args, _require_item(ws, item_id))
        result = service.preview(item_id, platform=platform, options=options, via="cli",
                                 requested_by=_publish_requester())
    if args.json:
        _print_json(result.to_json())
    else:
        _publish_print_preview(result)
        print("게시하려면 터미널에서: insia publish send " + item_id
              + (f" --ai-label {args.ai_label}" if args.ai_label else "")
              + (f" --visibility {args.visibility}" if args.visibility else ""))
    return 0 if result.can_publish else 1


class _PublishSendProgress:
    """How far one ``publish send`` got, so that a Ctrl+C at any moment is reported truthfully (DESIGN.md 1-7)."""

    def __init__(self) -> None:
        self.label = ""
        self.started = False          # service.send was called: from here on something may have been sent
        self.looked_up = False        # ``attempt`` is what the service recorded (None: it never created one)
        self.attempt: Any = None
        self.exit_code: int | None = None  # the outcome is on the terminal already


@_publish_errors
def cmd_publish_send(args: argparse.Namespace) -> int:
    """Preview → the person types the random code shown only here → exactly that preview is sent (DESIGN.md 8-2).

    Only on a terminal (exit 2 otherwise, before anything is opened); no ``--yes``; a wrong code is exit 2 and
    nothing is sent. Ctrl+C is exit 1 (0 when the post is already up), and what we say follows how far the command
    got (``_PublishSendProgress``): before ``service.send`` nothing was sent; during it the service closes the
    attempt (``failed`` before the write step, ``unknown`` after) and we read which; after it — a second Ctrl+C
    while the attempt is read back, one while the outcome prints or while the service and workspace close — the
    known outcome stands and "nothing was sent" is never said."""
    from .publishers import HumanConfirmation
    from .publishers.base import normalize_confirm_code

    if not _publish_is_tty():
        print(f"오류: {PUBLISH_NOT_TTY_MESSAGE}", file=sys.stderr)
        return 2
    progress = _PublishSendProgress()
    try:
        with _publish_session(args) as (_settings, ws, service):
            item_id = _item_id(ws, args.item_id)
            platform, options = _publish_options(args, _require_item(ws, item_id))
            label = _publish_label(platform)
            requester = _publish_requester()
            preview = service.preview(item_id, platform=platform, options=options, via="cli", requested_by=requester,
                                      issue_confirm_code=True)
            _publish_print_preview(preview)
            if not preview.can_publish:
                print("고칠 부분이 있어 게시하지 않았어요. 편집해서 다시 승인한 뒤 실행해 주세요.", file=sys.stderr)
                return 1
            code = preview.confirm_code or ""
            if not code:
                raise CommandError("확인 코드를 만들지 못했어요. 아무것도 올리지 않았어요.")
            try:
                typed = input(f"게시하려면 확인 코드 {code}를 입력하세요 (그만두려면 Enter): ")
            except EOFError:
                typed = ""
            if not typed.strip():
                print("그만뒀어요. 아무것도 올리지 않았어요.", file=sys.stderr)
                return 1
            if normalize_confirm_code(typed) != normalize_confirm_code(code):
                print("오류: 확인 코드가 맞지 않아요. 아무것도 올리지 않았어요. 다시 하려면 명령을 새로 실행해 주세요.",
                      file=sys.stderr)
                return 2
            confirmation = HumanConfirmation(via="cli", requested_by=requester, preview_id=preview.preview_id,
                                             preview_hash=preview.preview_hash, confirm_code=typed.strip())
            before = {a.id for a in service.list_attempts(item_id=item_id, limit=200)}
            print(f"{label}에 올리는 중이에요…", flush=True)
            progress.label, progress.started = label, True
            try:
                attempt = service.send(confirmation, item_id=item_id, platform=platform, background=False,
                                       on_step=_publish_step_printer(platform))
            except KeyboardInterrupt:  # the service closed its attempt: read it while the workspace is open
                progress.attempt, progress.looked_up = _publish_new_attempt(service, item_id, platform, before)
                progress.exit_code = _publish_report_interrupted(progress.attempt, label, looked_up=progress.looked_up)
                return progress.exit_code
            progress.attempt, progress.looked_up = attempt, True
            progress.exit_code = _publish_print_outcome(attempt, label, first_comment_link=preview.first_comment_link)
            return progress.exit_code
    except KeyboardInterrupt:
        return _publish_send_interrupted(progress)


def _publish_send_interrupted(progress: _PublishSendProgress) -> int:
    """A Ctrl+C that reached the outside of ``cmd_publish_send``: before the send, during the preview, rendering or
    the code prompt (nothing was sent), or after the send was started (never "nothing was sent" then)."""
    if not progress.started:
        print("\n중단했어요. 게시 요청을 보내기 전이라 아무것도 올리지 않았어요.", file=sys.stderr)
        return 1
    if progress.exit_code is not None:  # while the service or the workspace closed: the printed outcome stands
        print("\n중단했어요. 게시 결과는 위에 적은 그대로예요.", file=sys.stderr)
        return progress.exit_code
    return _publish_report_interrupted(progress.attempt, progress.label, looked_up=progress.looked_up)


def _publish_new_attempt(service: Any, item_id: str, platform: str, before: set[str]) -> tuple[Any, bool]:
    """``(attempt, looked_up)``: the attempt this ``send`` created (``None`` when the service never created one),
    and whether the lookup worked (the workspace may be closing: then we do not guess)."""
    try:
        new = [a for a in service.list_attempts(item_id=item_id, limit=200)
               if a.platform == platform and a.id not in before]
    except Exception:  # noqa: BLE001 - reported as "could not check", never as "nothing was sent"
        return None, False
    return (new[0] if new else None), True


def _publish_report_interrupted(attempt: Any, label: str, *, looked_up: bool) -> int:
    """Ctrl+C after ``service.send`` was called: say what the service recorded. Exit 0 only when the post is up."""
    if not looked_up:
        print("\n중단했어요. 게시하는 도중에 멈춰서 올라갔는지 확인하지 못했어요. insia publish attempts로 결과를 확인해 주세요.",
              file=sys.stderr)
        return 1
    if attempt is None or (attempt.status == "failed" and attempt.error_code == "interrupted"):
        print("\n중단했어요. 게시 요청을 보내기 전이라 아무것도 올리지 않았어요.", file=sys.stderr)
    elif attempt.status == "published":  # the platform had already confirmed: the post is up
        print(f"\n중단했지만 이미 게시됐어요: {attempt.permalink or '(게시물 주소를 받지 못했어요)'}")
        return 0
    elif attempt.status == "unknown":
        print(f"\n중단했어요. 게시 요청을 보낸 뒤라 올라갔을 수도 있어요. {label}에서 확인한 뒤 정리해 주세요: "
              f"insia publish resolve {attempt.id} --published [--url 주소] 또는 --not-published", file=sys.stderr)
    elif attempt.status == "failed":
        reason = attempt.error or "이유를 받지 못했어요."
        tail = "" if "올라가지 않았" in reason or "올리지 않았" in reason else " 아무것도 올라가지 않았어요."
        print(f"\n중단했어요. 게시하지 못했어요: {reason}{tail}", file=sys.stderr)
    elif attempt.status == "sending":
        print(f"\n중단했어요. 게시 기록 {attempt.id}이(가) 아직 '게시 중'이라 올라갔는지 확인하지 못했어요. "
              "insia publish attempts로 확인해 주세요 (다음 publish 명령이 주인 없는 기록을 정리해요).", file=sys.stderr)
    else:
        print(f"\n중단했어요. 게시 기록 {attempt.id}의 상태: {PUBLISH_ATTEMPT_LABELS.get(attempt.status, attempt.status)} "
              "(insia publish attempts로 확인해 주세요)", file=sys.stderr)
    return 1


def _publish_attempt_id(service: Any, text: str) -> str:
    return _match_id(text, [a.id for a in service.list_attempts(limit=1000)], "게시 기록", "insia publish attempts")


@_publish_errors
def cmd_publish_attempts(args: argparse.Namespace) -> int:
    with _publish_session(args) as (_settings, ws, service):
        item_id = _item_id(ws, args.item_id) if args.item_id else None
        rows = [service.attempt_json(a) for a in service.list_attempts(item_id=item_id, limit=args.limit)]
    if args.json:
        _print_json({"attempts": rows})
        return 0
    if not rows:
        print("게시 기록이 없어요.")
        return 0
    table = [[r.get("id", ""), _kst(r.get("created_at")), _publish_label(r.get("platform", "")),
              PUBLISH_ATTEMPT_LABELS.get(r.get("status", ""), r.get("status", "")), r.get("item_id", ""),
              r.get("permalink") or r.get("error") or r.get("step") or ""] for r in rows]
    print_table(["id", "시각", "플랫폼", "상태", "콘텐츠", "주소·오류"], table, max_widths=[30, 11, 10, 14, 40, 60])
    return 0


@_publish_errors
def cmd_publish_resolve(args: argparse.Namespace) -> int:
    """Close an ``unknown`` attempt after checking the platform by hand (TTY + y/N), or re-check Instagram."""
    from .publishers import HumanConfirmation

    if args.url and not args.published:
        raise UsageError("--url은 --published와 함께만 써요.")
    if not args.check and not _publish_is_tty():
        print(f"오류: {PUBLISH_RESOLVE_NOT_TTY_MESSAGE}", file=sys.stderr)
        return 2
    with _publish_session(args) as (_settings, _ws, service):
        attempt_id = _publish_attempt_id(service, args.attempt_id)
        if args.check:
            attempt = service.check_attempt(attempt_id)
            print(f"다시 확인했어요: {PUBLISH_ATTEMPT_LABELS.get(attempt.status, attempt.status)}"
                  + (f" · {attempt.permalink}" if attempt.permalink else "")
                  + (f" · {attempt.error}" if attempt.error and attempt.status != "published" else ""))
            return 0
        attempt = service.get_attempt(attempt_id)
        label = _publish_label(attempt.platform)
        outcome = "published" if args.published else "not_published"
        print(f"게시 기록 {attempt.id}: {label} · 콘텐츠 {attempt.item_id} v{attempt.version} · "
              f"{PUBLISH_ATTEMPT_LABELS.get(attempt.status, attempt.status)}")
        if outcome == "not_published":
            print(f"{label} 피드에서 먼저 확인했나요? 올라갔는데 '안 올라갔어요'로 정리하면 같은 글이 두 번 올라갈 수 있어요.")
        question = "올라갔어요" if outcome == "published" else "안 올라갔어요"
        try:
            answer = input(f"'{question}'로 정리할까요? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes", "예", "네", "ㅇ"):
            print("정리하지 않았어요.")
            return 1
        confirmation = HumanConfirmation(via="cli", requested_by=_publish_requester(), preview_id=attempt.id,
                                         preview_hash="")
        attempt, item = service.resolve(confirmation, outcome, url=(args.url or "").strip())
    if attempt.status == "published":
        print("'올라갔어요'로 정리했어요. 콘텐츠를 게시 완료로 표시했어요" + (f": {attempt.permalink}" if attempt.permalink else "."))
    else:
        print("'안 올라갔어요'로 정리했어요. 다시 게시하려면 새로 미리보기부터 해 주세요.")
    return 0


@_publish_errors
def cmd_publish_refresh(args: argparse.Namespace) -> int:
    """Refresh due Instagram tokens only — never publishes (cron: ``0 9 * * 1 insia publish refresh``)."""
    with _publish_session(args) as (_settings, _ws, service):
        result = service.refresh_tokens()
    if args.json:
        _print_json(result)
        return 0
    if not result:
        print("갱신할 토큰이 없어요.")
    for platform, info in result.items():
        state = "갱신했어요" if info.get("refreshed") else "갱신하지 않았어요"
        print(f"- {_publish_label(platform)}: {state}" + (f" · {info['message']}" if info.get("message") else "")
              + (f" · 만료 {_kst(info['expires_at'], '%Y-%m-%d')}" if info.get("expires_at") else ""))
    return 0


PUBLISH_DOCTOR_LABELS = (  # doctor_report ids → the doctor's labels (most specific first)
    (".media", "API 게시 · 이미지 공개 주소"), (".render", "API 게시 · 카드 렌더링"), ("credentials.", "API 게시 · 토큰 폴더"),
    ("linkedin.", "API 게시 · LinkedIn"), ("instagram.", "API 게시 · 인스타그램"))


def _publish_doctor_checks(settings: Settings, ws: "Workspace") -> list[dict[str, str]]:
    """``insia doctor`` lines for API publishing (DESIGN.md 8-4) — set / not set only, never a value."""
    try:
        service = _publish_service(settings, ws)
        try:
            report = service.doctor_report()
        finally:
            service.shutdown(timeout=1.0)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        return [{"level": "warn", "label": "API 게시", "message": f"점검하지 못했어요: {_publish_redact(_error_text(exc))}"}]
    out = []
    for entry in report:
        check_id = str(entry.get("id", ""))
        label = next((text for key, text in PUBLISH_DOCTOR_LABELS
                      if (check_id.endswith(key) if key.startswith(".") else check_id.startswith(key))), "API 게시")
        level = entry.get("level") if entry.get("level") in ("ok", "info", "warn", "error") else "info"
        out.append({"level": str(level), "label": label, "message": _publish_redact(str(entry.get("message", "")))})
    return out


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

BACKUP_SKIPPED = ("credentials", "publish", "logs", "exports")  # tokens, public/staged images, logs, regenerable files


def cmd_backup(args: argparse.Namespace) -> int:
    """A safe copy of the workspace: an online SQLite copy of insia.db (the server may keep running) + uploads/ +
    prices.json. ``credentials/`` (API publishing tokens), ``publish/``, ``logs/`` and ``exports/`` stay out."""
    import sqlite3

    from .db import DB_NAME

    settings = _settings_from_args(args)
    home = _home(settings)
    source = home / DB_NAME
    if not source.is_file():
        raise CommandError(f"백업할 워크스페이스 DB가 없어요: {source}")
    out = Path(args.out).expanduser().resolve()
    credentials_env = (os.environ.get("INSIA_CREDENTIALS_DIR") or "").strip()
    no_go = [home / name for name in ("credentials", "publish", "uploads", "logs")]
    if credentials_env:
        no_go.append(Path(credentials_env).expanduser().resolve())
    if out == home or any(out == p or out.is_relative_to(p) for p in no_go):
        raise UsageError(f"백업 폴더로 {out}은(는) 쓸 수 없어요. 워크스페이스 밖이나 새 폴더(예: backups/날짜)를 정해 주세요.")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CommandError(f"백업 폴더가 비어 있지 않아요: {out}. 새 폴더 이름을 정해 주세요.")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = out / DB_NAME
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    copied = [DB_NAME]
    uploads = home / "uploads"
    if uploads.is_dir():
        shutil.copytree(uploads, out / "uploads", symlinks=True)
        copied.append("uploads/")
    prices = home / "prices.json"
    if prices.is_file():
        shutil.copy2(prices, out / "prices.json")
        copied.append("prices.json")
    skipped = [f"{name}/" for name in BACKUP_SKIPPED if (home / name).exists()]
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and not p.is_symlink())
    ok = check == "ok"
    if args.json:
        _print_json({"out": str(out), "copied": copied, "skipped": skipped, "bytes": size, "db_check": check, "ok": ok})
        return 0 if ok else 1
    print(f"백업했어요: {out} ({size / 1024:,.0f}KB)")
    print(f"  넣은 것: {', '.join(copied)} (DB 점검: {'정상' if ok else check})")
    if skipped:
        print(f"  뺀 것: {', '.join(skipped)} — API 게시 토큰(credentials/)은 백업하지 않아요.")
    print("  복원: 워크스페이스 폴더에 이 파일들을 넣은 뒤, API 게시를 쓴다면 브랜드·자료 → API 게시 연결에서 다시 연결해 주세요.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# serve / healthcheck / doctor / sample-brief
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    name = (host or "").strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server as server_module

    settings = _settings_from_args(args)
    token = (args.token or os.environ.get("INSIA_ACCESS_TOKEN") or "").strip() or None
    public_hosts = tuple(dict.fromkeys(h.strip().lower() for h in (args.public_host or []) if h.strip()))
    loopback = _is_loopback(args.host)
    if not loopback and not token:
        raise UsageError(
            f"다른 기기에서 접속할 수 있는 주소(--host {args.host})로 열려면 접근 토큰이 필요해요. "
            f"INSIA_ACCESS_TOKEN 환경 변수나 --token으로 정해 주세요. 토큰 만들기: {TOKEN_HINT}")
    env_hosts = [h for h in (os.environ.get("INSIA_PUBLIC_HOSTS") or "").split(",") if h.strip()]
    if (public_hosts or env_hosts) and not token:  # a reverse proxy makes a loopback bind reachable from outside
        raise UsageError(
            "도메인(--public-host / INSIA_PUBLIC_HOSTS)으로 열면 리버스 프록시를 거쳐 바깥에서 접속할 수 있어서 접근 토큰이 "
            f"꼭 필요해요. INSIA_ACCESS_TOKEN 환경 변수나 --token으로 정해 주세요. 토큰 만들기: {TOKEN_HINT}")
    behind_proxy = bool(args.trust_proxy) or server_module.env_flag("INSIA_TRUST_PROXY")
    if behind_proxy and not token:  # --trust-proxy says a proxy is in front: same exposure as a domain
        raise UsageError(
            "리버스 프록시(--trust-proxy / INSIA_TRUST_PROXY) 뒤에서 열면 127.0.0.1로 열어도 바깥에서 접속할 수 있어서 "
            f"접근 토큰이 꼭 필요해요. INSIA_ACCESS_TOKEN 환경 변수나 --token으로 정해 주세요. 토큰 만들기: {TOKEN_HINT}")
    make_server = server_module.make_server
    try:
        parameters = inspect.signature(make_server).parameters
    except (TypeError, ValueError):
        parameters = {}  # type: ignore[assignment]
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        parameters = {**parameters, "token": None, "public_hosts": None, "trust_proxy": None, "quiet": None}  # type: ignore[dict-item]
    wanted = {"token": token, "public_hosts": public_hosts, "trust_proxy": bool(args.trust_proxy),
              "media_port": args.media_port, "media_base_url": args.media_base_url}
    unsupported = [name for name, value in wanted.items() if value not in (None, False, "", ()) and name not in parameters]
    if unsupported:
        # TODO(server group): make_server(settings, host, port, web_dir, *, token=None, public_hosts=(),
        # trust_proxy=False, quiet=False) is not available in this server.py yet.
        raise UsageError(f"이 버전의 서버는 {', '.join(unsupported)} 옵션을 아직 지원하지 않아요. INSIA를 업데이트해 주세요.")
    kwargs: dict[str, Any] = {name: value for name, value in wanted.items() if name in parameters}
    if "quiet" in parameters:
        kwargs["quiet"] = not args.verbose
    try:
        server = make_server(settings, args.host, args.port, args.web_dir, **kwargs)
    except OSError as exc:
        print(f"서버를 시작하지 못했어요 ({args.host}:{args.port}): {exc.strerror or exc}", file=sys.stderr)
        print("이미 켜 둔 insia serve가 있으면 먼저 끄거나, --port 8766처럼 다른 포트를 써 보세요.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"서버를 시작하지 못했어요: {exc}", file=sys.stderr)
        return 1
    mode, note = resolve_mode(settings.mode)
    url = getattr(server, "url", f"http://{args.host}:{args.port}")
    token_on = bool(getattr(server, "token_required", token))
    print(f"INSIA 에이전트 스튜디오: {url}")
    if not loopback:
        print(f"  다른 기기에서는 http://<이 컴퓨터 주소>:{args.port} 로 접속해요. 인터넷에 열 때는 HTTPS 리버스 프록시 뒤에 두세요.")
    print(f"  기본 모드: {mode} ({note}) · 모델: {settings.model}")
    print(f"  워크스페이스: {_home(settings)} · 대시보드 폴더: {getattr(server, 'web_root', None) or '(없음)'}")
    print("  접근 토큰: " + ("켜짐 (대시보드에서 토큰으로 로그인해요)" if token_on else "꺼짐 — 이 컴퓨터에서만 열려요"))
    hosts = getattr(server, "public_hosts", None) or public_hosts
    if hosts:
        print(f"  허용한 도메인: {', '.join(sorted(hosts))}")
    print(f"  실행 1번 예산 상한: {_usd(settings.max_cost_usd) if settings.max_cost_usd else '없음'}")
    publish_line = getattr(server_module, "publish_summary", lambda srv: "")(server)
    if publish_line:  # nothing at all when API publishing was never set up
        print(f"  {publish_line}")
    interrupted = getattr(getattr(server, "manager", None), "interrupted_on_start", 0) or 0
    if interrupted:
        print(f"  지난번에 끝나지 못한 실행 {interrupted}개를 '중단됨'으로 정리했어요. 'insia runs list'로 보고 "
              "'insia resume <실행 id>'로 이어서 할 수 있어요.")
    print("종료하려면 Ctrl+C를 누르세요.", flush=True)
    # SIGTERM (docker stop, systemd) takes the Ctrl+C path: live runs are cancelled and saved, not left 'running'
    sigterm = getattr(server_module, "sigterm_as_interrupt", nullcontext)
    stop = getattr(server_module, "stop_serving", lambda srv: srv.server_close())
    with sigterm():
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print("\n서버를 종료해요.", flush=True)
        finally:
            stop(server)
    return 0


def _health_url(host: str | None, port: int) -> str:
    """``/api/health`` on ``host``: an IPv6 literal gets brackets; a wildcard bind (0.0.0.0, ::) is asked on loopback."""
    name = (host or "127.0.0.1").strip().strip("[]") or "127.0.0.1"
    name = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(name, name)
    if ":" in name:
        name = f"[{name}]"
    return f"http://{name}:{port}/api/health"


def cmd_healthcheck(args: argparse.Namespace) -> int:
    import urllib.error
    import urllib.request

    port = args.port
    if port is None:
        port = _env_port() or 8765
    url = args.url or _health_url(args.host, port)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = (os.environ.get("INSIA_ACCESS_TOKEN") or "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never go through HTTP(S)_PROXY
    try:
        with opener.open(request, timeout=args.timeout) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"서버에 연결하지 못했어요: {url} ({reason})", file=sys.stderr)
        return 1
    if code in (200, 401):  # 401 = alive, the token just did not match
        if not args.quiet:
            print(f"정상: {url} (HTTP {code})")
        return 0
    print(f"서버 응답이 이상해요: {url} (HTTP {code})", file=sys.stderr)
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.util

    settings = _settings_from_args(args)
    checks: list[dict[str, str]] = []

    def add(level: str, label: str, message: str) -> None:
        checks.append({"level": level, "label": label, "message": message})

    add("ok" if sys.version_info >= (3, 10) else "error", "Python", f"{sys.version.split()[0]} (3.10 이상 필요)")
    if has_credentials():
        add("ok", "API 키", "Anthropic 자격 증명을 찾았어요 → 기본 live 모드 (실제 리서치·작성, 비용 발생)")
    else:
        add("warn", "API 키", "없어요 → 기본 mock(데모) 모드. 실제 초안은 ANTHROPIC_API_KEY를 설정한 뒤 만들어요")
    failed = False
    try:
        with open_workspace(settings) as ws:
            items = ws.list_items(limit=5000)
            docs = ws.list_documents()
            filled, total, missing = _profile_fill(ws.get_profile())
            version = ws.schema_version
            due = ws.due_slots(settings.today)
            publish_checks = _publish_doctor_checks(settings, ws)
        add("ok", "워크스페이스", f"{_home(settings)} (DB 버전 {version}, 콘텐츠 {len(items)}개, 자료 {len(docs)}개)")
        if filled == 0:
            add("warn", "회사 프로필", "비어 있어요 → insia profile edit-template 으로 채우면 글이 회사에 맞춰져요")
        elif missing:
            add("info", "회사 프로필", f"채운 항목 {filled}/{total} · 비어 있는 핵심 항목: {', '.join(missing)}")
        else:
            add("ok", "회사 프로필", f"채운 항목 {filled}/{total}")
        add("info", "오늘 만들 초안", f"{len(due)}개 (insia run-due)" if due else "없어요")
        checks.extend(publish_checks)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        failed = True
        add("error", "워크스페이스", f"{_home(settings)}를 열 수 없어요: {_error_text(exc)}")
    extras = (("docx", "Word 내보내기·읽기 (python-docx)", '[export]'), ("pypdf", "PDF 자료 읽기 (pypdf)", "[docs]"),
              ("yaml", "YAML 프로필 (PyYAML)", "[docs]"), ("playwright", "카드뉴스 PNG (playwright)", "[render]"))
    for module, label, extra in extras:
        if importlib.util.find_spec(module) is not None:
            add("ok", label, "설치됨")
        else:
            add("info", label, f"선택 사항, 없어요 → pip install \"insia-smartagent{extra}\"")
    web = settings.web_dir
    add("ok" if web else "warn", "대시보드 파일", str(web) if web else "web/ 폴더를 못 찾았어요 (INSIA_WEB_DIR)")
    cap = settings.max_cost_usd
    add("ok" if cap else "info", "예산 상한", f"실행 1번 {_usd(cap)}" if cap else "없어요 → INSIA_MAX_COST_USD=5 처럼 정해 두길 권해요")
    try:
        from .costs import price_for

        price = price_for(settings.model, home=settings.home)
        if price:
            add("info", "가격표", f"{settings.model}: 입력 ${price['input']:g}/MTok · 출력 ${price['output']:g}/MTok "
                "(prices.json·INSIA_PRICE_*로 바꿔요)")
        else:
            add("warn", "가격표", f"{settings.model}의 가격을 몰라 비용이 0으로 잡혀요 → prices.json에 추가해 주세요")
    except Exception:  # noqa: BLE001
        pass
    add("info", "접근 토큰", "INSIA_ACCESS_TOKEN 설정됨" if os.environ.get("INSIA_ACCESS_TOKEN") else
        "없어요 (이 컴퓨터에서만 쓸 때는 괜찮아요)")
    if args.json:
        _print_json({"version": __version__, "checks": checks, "ok": not failed})
        return 1 if failed else 0
    marks = {"ok": "정상", "warn": "주의", "info": "정보", "error": "문제"}
    print(f"INSIA 점검 · insia {__version__} · Python {sys.version.split()[0]}")
    for check in checks:
        print(f"[{marks[check['level']]}] {check['label']}: {check['message']}")
    return 1 if failed else 0


def cmd_sample_brief(args: argparse.Namespace) -> int:
    brief = load_sample_brief(Settings.from_env())
    print(json.dumps(brief.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

MAIN_EPILOG = """\
처음이라면
  insia doctor                           설치·설정 점검
  insia profile edit-template            회사 프로필 양식 → 채운 뒤 insia profile import profile.yaml
  insia docs add 회사소개서.pdf           참고 자료 넣기 (txt, md, pdf, docx)
  insia serve                            대시보드 http://127.0.0.1:8765

매주
  insia plan-week --theme "…"            이번 주 콘텐츠 계획 (월요일)
  insia run-due                          오늘까지 예정된 초안 만들기 (cron·작업 스케줄러용)
  insia items list                       보관함 → insia review / revise / items approve
  insia items export <id>                붙여넣기용 파일 → 직접 게시 → insia items publish <id> --url …

API 게시 (선택: LinkedIn·인스타그램, 내 개발자 앱 필요)
  insia publish status                   연결·준비 상태
  insia publish send <id>                미리보기 확인 → 확인 코드 입력 → 한 건 게시 (터미널에서만)
  insia backup --out <폴더>              토큰을 뺀 안전한 백업

자세한 안내: docs/operations.md · 종료 코드: 0 성공, 1 작업 실패, 2 잘못된 사용법
INSIA는 어떤 채널에도 자동으로 게시하지 않아요. API 게시도 사람이 확인하고 누를 때 한 건씩만 해요."""


def build_parser() -> argparse.ArgumentParser:
    parser = KoreanArgumentParser(prog="insia", description="INSIA 스마트에이전트 — 총괄·리서치·검수 에이전트가 사업계획서와 "
                                  "네이버 블로그·링크드인·인스타그램 초안을 만들어요", epilog=MAIN_EPILOG)
    parser.add_argument("--version", action="version", version=f"insia {__version__}", help="버전을 보여 줘요")
    parser.add_argument("--home", help="워크스페이스 폴더 (기본: INSIA_HOME 또는 ./workspace)")
    sub = parser.add_subparsers(dest="command", metavar="<명령>", title="명령", help="자세한 도움말: insia <명령> -h")

    ws = _parent()
    ws.add_argument("--home", default=argparse.SUPPRESS, help="워크스페이스 폴더 (기본: INSIA_HOME 또는 ./workspace)")
    js = _parent()
    js.add_argument("--json", action="store_true", help="JSON으로만 출력해요 (스크립트·에이전트용)")
    engine = _parent()
    engine.add_argument("--mode", choices=["auto", "live", "mock"], help="기본 auto: API 키가 있으면 live, 없으면 mock")
    engine.add_argument("--model", help="모델 ID (기본 INSIA_MODEL 또는 claude-opus-5)")
    engine.add_argument("--speed", type=float, help="mock 재생 배속 (1 = 실제 시간, 0 = 기다리지 않음)")
    engine.add_argument("--max-cost-usd", dest="max_cost_usd", type=_type_usd, metavar="USD",
                        help="실행 1번의 예산 상한 (기본 INSIA_MAX_COST_USD, 0 = 없음)")
    engine.add_argument("--quiet", action="store_true", help="채널 완료와 최종 결과만 출력해요")

    # -- run ------------------------------------------------------------------
    run = sub.add_parser("run", parents=[ws, engine], help="브리프로 파이프라인을 실행해요 (결과는 보관함에 저장)",
                         description="브리프로 계획 → 리서치 → 작성 → 검수 → 수정 루프를 돌려요. 결과는 워크스페이스 보관함과 "
                                     "outputs/<실행 id>/ 파일로 저장돼요.")
    run.add_argument("--brief", help="브리프 JSON 파일")
    run.add_argument("--topic", help="주제 또는 사업 아이템")
    run.add_argument("--goal", help="목적")
    run.add_argument("--audience", help="대상 독자")
    run.add_argument("--channels", help=f"쉼표로 구분 ({','.join(ALL_CHANNELS)})")
    run.add_argument("--keywords", help="쉼표로 구분, 첫 번째가 메인 키워드")
    run.add_argument("--tone", help="톤앤매너")
    run.add_argument("--notes", help="추가 요구사항")
    run.add_argument("--docs", help="리서치에 넣을 자료: all(기본) | none | u1,u3")
    run.add_argument("--no-profile", action="store_true", help="회사 프로필을 쓰지 않아요")
    run.add_argument("--no-workspace", action="store_true", help="보관함에 저장하지 않아요 (데모·녹화용; 프로필·자료도 안 씀)")
    run.add_argument("--out", help="결과 파일 폴더 (기본 outputs)")
    run.add_argument("--no-save", action="store_true", help="결과 파일(outputs/)을 저장하지 않아요")
    run.add_argument("--record", help="대시보드용 트레이스 JSON 저장 경로 (예: web/demo/demo-run.json)")
    run.add_argument("--max-rounds", type=int, dest="max_rounds", help="최대 수정 횟수 (기본 2)")
    run.add_argument("--pass-score", type=int, dest="pass_score", help="통과 점수 (기본 80)")
    run.add_argument("--no-fallbacks", action="store_true", help="서버 측 거절 대체 모델(fallbacks)을 꺼요")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", parents=[ws, engine], help="멈춘 실행을 이어서 해요 (끝난 단계는 건너뜀)",
                            description="중단·실패·예산 초과로 멈춘 실행을 같은 id로 이어서 해요. 끝난 채널과 단계는 다시 하지 않아요.")
    resume.add_argument("run_id", help="실행 id (insia runs list; 겹치지 않는 일부만 적어도 돼요)")
    resume.add_argument("--force", action="store_true", help="'실행 중'으로 남은 실행도 이어서 해요 (다른 곳에서 돌고 있지 않을 때만)")
    resume.add_argument("--out", help="결과 파일 폴더 (기본 outputs)")
    resume.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않아요")
    resume.set_defaults(func=cmd_resume)
    # --max-cost-usd means something else for resume (without it the run keeps the cap it was started with). The engine
    # parent's action object is shared with the other commands, so resume gets its own copy with its own help.
    import copy

    shared_cap = resume._option_string_actions["--max-cost-usd"]
    resume_cap = copy.copy(shared_cap)
    resume_cap.help = ("이 실행의 새 예산 상한 (안 주면 처음 실행할 때 정한 상한을 그대로 써요. 그때 상한이 없었으면 "
                       "INSIA_MAX_COST_USD, 0 = 상한 없음)")
    resume._actions[resume._actions.index(shared_cap)] = resume_cap
    for group in resume._action_groups:
        group._group_actions[:] = [resume_cap if action is shared_cap else action for action in group._group_actions]
    for option in resume_cap.option_strings:
        resume._option_string_actions[option] = resume_cap

    review = sub.add_parser("review", parents=[ws, engine], help="보관함 콘텐츠를 다시 검수해요",
                            description="콘텐츠의 최신 버전을 검수 에이전트가 다시 채점해요.")
    review.add_argument("item_id", help="콘텐츠 id (insia items list)")
    review.set_defaults(func=cmd_review)

    revise = sub.add_parser("revise", parents=[ws, engine], help="수정 지시를 주고 고친 버전을 만들어요 (자동 재검수)",
                            description="최신 검수 의견과 사람의 지시로 총괄 에이전트가 새 버전을 쓰고, 검수 에이전트가 다시 채점해요.")
    revise.add_argument("item_id", help="콘텐츠 id (insia items list)")
    revise.add_argument("--instructions", "-i", default="", help="수정 지시 (비우면 검수 의견만 반영)")
    revise.add_argument("--instructions-file", help="수정 지시를 적은 텍스트 파일")
    revise.set_defaults(func=cmd_revise)

    # -- items ------------------------------------------------------------------
    items = sub.add_parser("items", help="보관함: 목록·보기·승인·게시 표시·내보내기",
                           description="만든 초안을 검토하고 승인·게시 예정·게시 완료·보관으로 관리해요. 게시는 사람이 직접 해요.")
    items_sub = items.add_subparsers(dest="items_command", metavar="<동작>", title="동작", required=True, help="자세한 도움말: <동작> -h")
    p = items_sub.add_parser("list", parents=[ws, js], help="보관함 목록")
    p.add_argument("--status", choices=list(ITEM_STATUS_LABELS), help="상태: " + ", ".join(f"{k}={v}" for k, v in ITEM_STATUS_LABELS.items()))
    p.add_argument("--channel", type=_type_channel, help="채널 (bizplan, naver_blog, linkedin, instagram, blog, ig)")
    p.add_argument("--all", action="store_true", help="보관한 콘텐츠도 보여 줘요")
    p.add_argument("--limit", type=_type_positive, default=200, help="최대 개수 (기본 200)")
    p.set_defaults(func=cmd_items_list)
    p = items_sub.add_parser("show", parents=[ws, js], help="콘텐츠 하나 보기 (버전·검수·본문)")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--version", type=_type_positive, help="볼 버전 번호 (기본 최신)")
    p.add_argument("--no-content", action="store_true", help="본문은 빼고 보여 줘요")
    p.set_defaults(func=cmd_items_show)
    p = items_sub.add_parser("approve", parents=[ws], help="승인 (최신 버전이 검수를 통과해야 해요)")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--force", action="store_true", help="검수를 통과하지 않았어도 직접 확인했으면 승인해요")
    p.set_defaults(func=cmd_items_approve)
    p = items_sub.add_parser("schedule", parents=[ws], help="게시 예정일 표시 (승인한 콘텐츠)")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--date", required=True, type=_type_day, help="게시 예정일 YYYY-MM-DD (today, tomorrow도 돼요)")
    p.set_defaults(func=cmd_items_schedule)
    p = items_sub.add_parser("publish", parents=[ws], help="직접 게시한 뒤 '게시 완료'로 표시해요")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--url", help="게시한 글 주소 (https://…)")
    p.set_defaults(func=cmd_items_publish)
    p = items_sub.add_parser("archive", parents=[ws], help="보관 (목록에서 숨김)")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--note", help="메모 (왜 보관했는지)")
    p.set_defaults(func=cmd_items_archive)
    p = items_sub.add_parser("restore", parents=[ws], help="보관한 콘텐츠를 초안으로 되돌려요")
    p.add_argument("item_id", help="콘텐츠 id")
    p.set_defaults(func=cmd_items_restore)
    p = items_sub.add_parser("export", parents=[ws], help="붙여넣기·업로드용 파일로 내보내요",
                             description="채널별 추천 형식: 사업계획서 docx · 네이버 블로그 html · 링크드인 txt · 인스타그램 zip(카드 이미지)")
    p.add_argument("item_id", help="콘텐츠 id")
    p.add_argument("--format", "-f", choices=EXPORT_FORMATS, help="형식 (기본: 채널 추천 형식)")
    p.add_argument("--out", help="저장할 폴더 (기본: 워크스페이스 exports/)")
    p.add_argument("--version", type=_type_positive, help="내보낼 버전 번호 (기본 최신)")
    p.set_defaults(func=cmd_items_export)

    # -- runs / usage ---------------------------------------------------------------
    runs = sub.add_parser("runs", help="실행 기록: 목록·보기·묶음 내보내기")
    runs_sub = runs.add_subparsers(dest="runs_command", metavar="<동작>", title="동작", required=True, help="자세한 도움말: <동작> -h")
    p = runs_sub.add_parser("list", parents=[ws, js], help="실행 기록 목록 (최근 순)")
    p.add_argument("--limit", type=_type_positive, default=30, help="최대 개수 (기본 30)")
    p.add_argument("--kind", choices=list(RUN_KIND_LABELS), help="종류: " + ", ".join(f"{k}={v}" for k, v in RUN_KIND_LABELS.items()))
    p.add_argument("--status", choices=list(RUN_STATUS_LABELS), help="상태")
    p.add_argument("--item", metavar="콘텐츠ID", help="이 콘텐츠에 돌린 작업만 (재검수·수정 요청·직접 수정, 예: it_…)")
    p.set_defaults(func=cmd_runs_list)
    p = runs_sub.add_parser("show", parents=[ws, js], help="실행 하나 보기")
    p.add_argument("run_id", help="실행 id")
    p.set_defaults(func=cmd_runs_show)
    p = runs_sub.add_parser("export", parents=[ws], help="실행 결과 전체를 zip으로 (채널 파일·출처 목록·리서치)")
    p.add_argument("run_id", help="실행 id")
    p.add_argument("--out", help="저장할 폴더 (기본: 워크스페이스 exports/)")
    p.set_defaults(func=cmd_runs_export)

    usage = sub.add_parser("usage", parents=[ws, js], help="API 사용량과 비용 (기본: 이번 달)")
    usage.add_argument("--since", type=_type_day, help="시작일 YYYY-MM-DD (기본: 이번 달 1일)")
    usage.add_argument("--until", type=_type_day, help="종료일 YYYY-MM-DD (기본: 오늘)")
    usage.add_argument("--all", action="store_true", help="처음부터 전부")
    usage.set_defaults(func=cmd_usage)

    # -- calendar -------------------------------------------------------------------
    plan = sub.add_parser("plan-week", parents=[ws, js], help="한 주 콘텐츠 계획을 세워 캘린더에 넣어요",
                          description="회사 프로필·테마·지난 게시물을 바탕으로 날짜별 주제·관점·키워드를 정해요 "
                                      "(기본은 평일에 배치, --weekend로 채널별 주말 허용). 채널마다 이미 계획이 있는 날은 "
                                      "비워 두고, 지난 게시물·기존 계획과 겹치는 주제는 넣지 않아요. "
                                      f"채널 개수를 하나도 안 적으면 블로그 {DEFAULT_PLAN_COUNTS['naver_blog']} · 링크드인 "
                                      f"{DEFAULT_PLAN_COUNTS['linkedin']} · 인스타그램 {DEFAULT_PLAN_COUNTS['instagram']}편이에요.")
    plan.add_argument("--theme", "-t", default="", help="이번 주 테마 (비우면 회사 프로필로 정해요)")
    plan.add_argument("--start", type=_type_day, help="시작일 YYYY-MM-DD (기본: 오늘이 월요일이면 오늘, 아니면 다음 월요일)")
    plan.add_argument("--days", type=int, default=7, help="기간 일수 (기본 7, 최대 31)")
    plan.add_argument("--blog", type=_type_count, help="네이버 블로그 편수")
    plan.add_argument("--linkedin", type=_type_count, help="링크드인 편수")
    plan.add_argument("--instagram", type=_type_count, help="인스타그램 편수")
    plan.add_argument("--bizplan", type=_type_count, help="사업계획서 편수 (보통 0)")
    plan.add_argument("--replace", action="store_true",
                      help="이 기간에 이미 있는 계획(초안 전)은 건너뜀으로 바꾸고 새로 짜요 (계획에 실패하거나 도중에 멈추면 되돌려요)")
    plan.add_argument("--weekend", metavar="채널",
                      help="주말(토·일)에도 올릴 채널 (예: blog,instagram · all · none, 기본: 평일만). "
                           "주말 슬롯이 있으면 run-due도 주말에 돌도록 cron을 매일로 바꿔 주세요")
    plan.add_argument("--mode", choices=["auto", "live", "mock"], help="기본 auto")
    plan.add_argument("--model", help="모델 ID")
    plan.set_defaults(func=cmd_plan_week)

    due = sub.add_parser("run-due", parents=[ws, engine], help="예정일이 된 캘린더 슬롯의 초안을 만들어요 (cron용)",
                         description="예정일이 --until(기본 오늘)까지인 '계획' 상태 슬롯마다 초안을 만들어 보관함에 넣어요. "
                                     "만들 것이 없으면 종료 코드 0, 하나라도 실패하면 1. API 키가 없으면 --mode mock일 때만 데모로 돌아요.")
    due.add_argument("--until", type=_type_day, help="기준일 YYYY-MM-DD (기본 오늘; tomorrow도 돼요)")
    due.add_argument("--limit", type=_type_positive, help="이번에 만들 최대 개수 (비용 조절)")
    due.add_argument("--dry-run", action="store_true", help="만들지 않고 대상만 보여 줘요")
    due.add_argument("--out", help="결과 파일도 이 폴더에 저장해요 (기본: 보관함에만)")
    due.set_defaults(func=cmd_run_due)

    calendar = sub.add_parser("calendar", help="콘텐츠 캘린더: 목록·초안 만들기·건너뛰기·날짜 바꾸기")
    cal_sub = calendar.add_subparsers(dest="calendar_command", metavar="<동작>", title="동작", required=True, help="자세한 도움말: <동작> -h")
    p = cal_sub.add_parser("list", parents=[ws, js], help="캘린더 보기 (기본: 이번 주부터 2주)")
    p.add_argument("--from", dest="date_from", type=_type_day, help="시작일")
    p.add_argument("--to", dest="date_to", type=_type_day, help="종료일")
    p.add_argument("--all", action="store_true", help="건너뛴 슬롯도 보여 줘요")
    p.set_defaults(func=cmd_calendar_list)
    p = cal_sub.add_parser("generate", parents=[ws, engine], help="슬롯 하나의 초안을 지금 만들어요")
    p.add_argument("slot_id", help="슬롯 id (insia calendar list)")
    p.add_argument("--force", action="store_true", help="이미 초안이 있어도 다시 만들어요")
    p.add_argument("--out", help="결과 파일도 이 폴더에 저장해요")
    p.set_defaults(func=cmd_calendar_generate)
    p = cal_sub.add_parser("skip", parents=[ws], help="슬롯을 건너뛰어요")
    p.add_argument("slot_id", help="슬롯 id")
    p.set_defaults(func=cmd_calendar_skip)
    p = cal_sub.add_parser("move", parents=[ws], help="슬롯 날짜를 바꿔요")
    p.add_argument("slot_id", help="슬롯 id")
    p.add_argument("--date", required=True, type=_type_day, help="새 날짜 YYYY-MM-DD")
    p.set_defaults(func=cmd_calendar_move)

    # -- profile / docs ---------------------------------------------------------------
    profile = sub.add_parser("profile", help="회사·브랜드 프로필: 보기·양식·가져오기·내보내기",
                             description="모든 에이전트가 참고하는 회사 사실과 브랜드 규칙(금지 표현, 필수 문구, 톤…)이에요.")
    prof_sub = profile.add_subparsers(dest="profile_command", metavar="<동작>", title="동작", required=True, help="자세한 도움말: <동작> -h")
    p = prof_sub.add_parser("show", parents=[ws, js], help="프로필 보기 (--json은 Profile JSON 그대로)")
    p.set_defaults(func=cmd_profile_show)
    p = prof_sub.add_parser("edit-template", parents=[ws], help="채워 넣을 양식 파일을 만들어요 (지금 값이 채워진 채로)")
    p.add_argument("--out", help="파일 경로 (기본 profile.yaml, PyYAML이 없으면 profile.json; - = 화면)")
    p.add_argument("--format", choices=["yaml", "json"], help="양식 형식")
    p.add_argument("--blank", action="store_true", help="지금 값 없이 빈 양식")
    p.add_argument("--force", action="store_true", help="같은 이름의 파일이 있으면 덮어써요")
    p.set_defaults(func=cmd_profile_edit_template)
    p = prof_sub.add_parser("import", parents=[ws], help="양식 파일(YAML·JSON)로 프로필을 저장해요")
    p.add_argument("file", help="profile.yaml 또는 profile.json")
    p.add_argument("--merge", action="store_true", help="파일에 채운 항목만 바꾸고 나머지는 그대로 둬요")
    p.set_defaults(func=cmd_profile_import)
    p = prof_sub.add_parser("export", parents=[ws], help="프로필을 JSON(또는 YAML)으로 내보내요 (백업·이전용)")
    p.add_argument("--out", help="파일 경로 (기본: 화면에 출력)")
    p.add_argument("--format", choices=["json", "yaml"], help="형식 (기본 json, .yaml 경로면 yaml)")
    p.add_argument("--force", action="store_true", help="같은 이름의 파일이 있으면 덮어써요")
    p.set_defaults(func=cmd_profile_export)

    docs = sub.add_parser("docs", help="참고 자료: 추가·목록·보기·삭제",
                          description="회사 소개서·IR 자료·보도자료 같은 자료를 넣으면 리서치에 '사용자 제공 자료'로 들어가요.")
    docs_sub = docs.add_subparsers(dest="docs_command", metavar="<동작>", title="동작", required=True, help="자세한 도움말: <동작> -h")
    p = docs_sub.add_parser("add", parents=[ws, js], help="자료 파일 추가 (txt, md, pdf, docx)")
    p.add_argument("file", help="자료 파일 (pdf는 pypdf, docx는 python-docx 필요: " + DOCS_EXTRA_HINT + ")")
    p.add_argument("--title", help="자료 제목 (기본: 파일 이름)")
    p.add_argument("--force", action="store_true", help="같은 내용의 자료가 있어도 추가해요")
    p.set_defaults(func=cmd_docs_add)
    p = docs_sub.add_parser("list", parents=[ws, js], help="자료 목록 (--json은 본문 포함)")
    p.set_defaults(func=cmd_docs_list)
    p = docs_sub.add_parser("show", parents=[ws, js], help="자료 본문 보기")
    p.add_argument("doc_id", help="자료 id (u1, u2 …)")
    p.set_defaults(func=cmd_docs_show)
    p = docs_sub.add_parser("rm", parents=[ws], help="자료 삭제")
    p.add_argument("doc_id", help="자료 id (u1, u2 …)")
    p.set_defaults(func=cmd_docs_rm)

    # -- Claude Code -------------------------------------------------------------------
    imp = sub.add_parser("import-run", parents=[ws, js], help="Claude Code 실행 폴더를 보관함으로 가져와요",
                         description="outputs/<날짜>-<주제>/ 폴더(brief.json, plan.json|plan.md, research.json, "
                                     "drafts/<채널>.r<N>.json, reviews/<채널>.r<N>.json, 선택: profile.json, documents.json)를 "
                                     "실행·콘텐츠·버전·검수로 저장해요. 같은 폴더를 다시 가져오면 중복 없이 갱신해요. final/은 사람이 읽는 본이라 "
                                     "쓰지 않아요 (초안 JSON이 기준).")
    imp.add_argument("folder", help="Claude Code 실행 폴더")
    imp.add_argument("--run-id", help="실행 id (기본: cc-<폴더 이름>)")
    imp.add_argument("--pass-score", type=int, dest="pass_score", help="통과 점수 (기본 80)")
    imp.set_defaults(func=cmd_import_run)

    check = sub.add_parser("check", help="Draft JSON의 채널 형식을 검사해요 (프로필 규칙 포함)",
                           description="글자 수·소제목·해시태그 같은 형식과, 프로필이 있으면 금지 표현·필수 문구·사업계획서 블라인드까지 검사해요. "
                                       "초안 근처의 brief.json과 profile.json을 자동으로 찾아요.")
    check.add_argument("draft", help="Draft JSON 파일")
    check.add_argument("--brief", help="브리프 JSON (네이버 블로그 제목 키워드 검사용; 생략하면 근처 brief.json)")
    who = check.add_mutually_exclusive_group()
    who.add_argument("--profile", help="프로필 JSON/YAML 파일 (생략하면 근처 profile.json)")
    who.add_argument("--workspace-profile", action="store_true", help="워크스페이스에 저장된 프로필로 검사해요")
    who.add_argument("--no-profile", action="store_true", help="프로필 검사를 하지 않아요")
    check.add_argument("--home", default=argparse.SUPPRESS, help="워크스페이스 폴더 (--workspace-profile용)")
    check.add_argument("--json", action="store_true", help="JSON으로 출력")
    check.set_defaults(func=cmd_check)

    # -- quality eval ------------------------------------------------------------------------
    evaluation = sub.add_parser(
        "eval", help="품질 평가: 케이스 목록·실행·결과 비교 (프롬프트·가이드·모델을 바꾸기 전에)",
        description="evals/cases/의 케이스(브리프 + 프로필·자료 + 꼭 지킬 조건)를 실제 파이프라인으로 돌려 결정적 검사"
                    "(형식·블라인드·금지 표현·근거 없는 수치·가정 표시)로 채점하고, 지난 결과와 비교해요. mock은 무료, "
                    "live는 돈이 들어서 --max-cost-usd가 꼭 필요해요. 안내: evals/README.md")
    eval_sub = evaluation.add_subparsers(dest="eval_command", metavar="<동작>", title="동작", required=True,
                                         help="자세한 도움말: <동작> -h")
    cases_help = "케이스 폴더 (기본 evals/cases)"
    p = eval_sub.add_parser("list", parents=[js], help="평가 케이스 목록")
    p.add_argument("--cases", help=cases_help)
    p.set_defaults(func=cmd_eval_list)
    p = eval_sub.add_parser(
        "run", parents=[js], help="케이스를 돌려 채점해요 (결과: summary.json · report.md · cases/ · runs/)",
        description="케이스마다 임시 워크스페이스에서 파이프라인을 돌려요(내 워크스페이스는 건드리지 않아요). 필수 조건이 하나라도 "
                    "실패하거나, 평가 못 한 채널이 있거나, --baseline보다 나빠지면 종료 코드 1이에요.")
    p.add_argument("--mode", choices=["mock", "live"], default="mock",
                   help="mock(기본, 무료·오프라인) 또는 live(Anthropic API, 돈이 들어요)")
    p.add_argument("--cases", help=cases_help)
    p.add_argument("--case", action="append", metavar="ID", help="이 케이스만 (여러 번 쓰거나 쉼표로 구분)")
    p.add_argument("--channels", help=f"이 채널만 (쉼표로 구분: {','.join(ALL_CHANNELS)})")
    p.add_argument("--model", help="live 모델 ID (기본 INSIA_MODEL 또는 claude-opus-5)")
    p.add_argument("--max-cost-usd", dest="max_cost_usd", type=_type_usd, metavar="USD",
                   help="live 평가 전체의 예산 상한 (live에서는 꼭 필요해요, 넘으면 남은 케이스를 건너뛰어요, "
                        "가격을 모르는 모델이면 시작하지 않아요)")
    p.add_argument("--out", help="결과 폴더 (기본 evals/results/<시각>-<모드>)")
    p.add_argument("--baseline", metavar="폴더",
                   help="비교할 지난 결과 폴더 (보고서에 회귀를 먼저 보여 줘요, --case·--channels로 일부만 돌리면 그 부분만 비교해요)")
    p.add_argument("--max-score-drop", dest="max_score_drop", type=float, default=5.0, metavar="점",
                   help="기준보다 평균 검수 점수가 이만큼 넘게 떨어지면 실패 (기본 5)")
    p.add_argument("--reps", type=_type_positive, default=1, help="케이스마다 반복 횟수 (기본 1, live는 비용이 그만큼 늘어요)")
    p.add_argument("--timeout-s", dest="timeout_s", type=float, metavar="초",
                   help="live 실행 1번의 최대 시간 (기본 1800초, 넘으면 멈추고 '시간 초과'로 기록)")
    p.add_argument("--resume", action="store_true",
                   help="--out 폴더에서 이어서 해요 (케이스·채널·모드가 그대로인 끝난 실행은 다시 쓰고, 이미 쓴 비용도 예산 상한에 넣어요)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="돌리지 않고 계획과 (live) 예상 비용만 보여 줘요")
    p.add_argument("--estimate-from", dest="estimate_from", metavar="폴더",
                   help="예상 비용을 지난 live 평가의 실측 비용으로 계산해요")
    p.add_argument("--judge", action="store_true",
                   help="(돈이 들어요) LLM이 이번 최종본과 --baseline 최종본을 짝지어 비교 채점해요. live 전용, 기본 꺼짐")
    p.add_argument("--judge-model", dest="judge_model", help="비교 채점 모델 (기본 claude-sonnet-5, 평가 대상 모델과 다르게)")
    p.add_argument("--yes", action="store_true", help="live 시작 확인을 묻지 않아요 (예산 상한은 그대로 지켜요)")
    p.add_argument("--max-rounds", type=int, dest="max_rounds", help="최대 수정 횟수 (기본 2)")
    p.add_argument("--pass-score", type=int, dest="pass_score", help="통과 점수 (기본 80)")
    p.set_defaults(func=cmd_eval_run)
    p = eval_sub.add_parser("compare", parents=[js], help="두 결과를 비교해요 (회귀가 있으면 종료 코드 1)",
                            description="기준(A)에서 통과하던 필수 조건이 이번(B)에 실패하거나, 평가 못 한 채널이 생기거나, "
                                        "평균 검수 점수가 --max-score-drop보다 많이 떨어지면 종료 코드 1이에요.")
    p.add_argument("baseline", help="기준 결과 폴더 (A)")
    p.add_argument("current", help="이번 결과 폴더 (B)")
    p.add_argument("--max-score-drop", dest="max_score_drop", type=float, default=5.0, metavar="점",
                   help="허용하는 평균 검수 점수 하락 (기본 5)")
    p.set_defaults(func=cmd_eval_compare)

    # -- server ---------------------------------------------------------------------------
    srv = sub.add_parser("serve", parents=[ws], help="대시보드 서버를 띄워요",
                         description="대시보드(스튜디오·보관함·캘린더·브랜드·자료·사용량)를 띄워요. 기본은 이 컴퓨터에서만 열려요 "
                                     "(127.0.0.1). 다른 기기에서 쓰려면 --host 0.0.0.0과 접근 토큰(INSIA_ACCESS_TOKEN 또는 --token)이 "
                                     "꼭 필요하고, 인터넷에 열 때는 HTTPS 리버스 프록시 뒤에 두세요.")
    srv.add_argument("--host", default="127.0.0.1", help="바인드 주소 (기본 127.0.0.1 = 이 컴퓨터만)")
    srv.add_argument("--port", type=int, default=8765, help="포트 (기본 8765)")
    srv.add_argument("--web-dir", dest="web_dir", help="대시보드 폴더 (기본: 저장소의 web/)")
    srv.add_argument("--token", help="접근 토큰 (기본 INSIA_ACCESS_TOKEN). 명령 기록에 남으니 환경 변수를 권해요")
    srv.add_argument("--public-host", action="append", metavar="HOST",
                     help="허용할 외부 호스트 이름 (예: insia.example.com, 여러 번 쓸 수 있어요)")
    srv.add_argument("--trust-proxy", action="store_true",
                     help="리버스 프록시의 X-Forwarded-Proto를 믿어요 (HTTPS면 쿠키에 Secure)")
    srv.add_argument("--mode", choices=["auto", "live", "mock"], help="대시보드에서 시작하는 실행의 기본 모드")
    srv.add_argument("--model", help="모델 ID")
    srv.add_argument("--max-cost-usd", dest="max_cost_usd", type=_type_usd, metavar="USD", help="실행 1번의 예산 상한")
    srv.add_argument("--out", help="결과 파일 폴더 (기본 outputs)")
    srv.add_argument("--verbose", action="store_true", help="요청 로그를 출력해요")
    srv.add_argument("--media-port", dest="media_port", type=int, metavar="PORT",
                     help="인스타그램 API 게시용 이미지 전용 포트 (기본 INSIA_MEDIA_PORT). /pub/m/ 이미지만 보여 주고 "
                          "대시보드는 그대로 이 컴퓨터에만 둬요")
    srv.add_argument("--media-base-url", dest="media_base_url", metavar="URL",
                     help="그 포트를 바깥에서 여는 https 주소 (예: https://media.example.com, 기본 INSIA_MEDIA_BASE_URL)")
    srv.set_defaults(func=cmd_serve)

    # -- API publishing ---------------------------------------------------------------------
    publish = sub.add_parser(
        "publish", help="LinkedIn·인스타그램 API 게시 (사람이 확인하고 누를 때만, 한 건씩)",
        description="승인한 LinkedIn·인스타그램 콘텐츠를 내 개발자 앱으로 한 건씩 올려요. 예약·자동 게시는 없어요: send는 "
                    "터미널에서 미리보기를 보고 그때 나온 확인 코드를 직접 입력할 때만 돌아요. 설정 안내: docs/operations.md")
    pub_sub = publish.add_subparsers(dest="publish_command", metavar="<동작>", title="동작", required=True,
                                     help="자세한 도움말: <동작> -h")
    platform_help = "플랫폼 (보통 생략: 콘텐츠 채널로 정해져요)"
    p = pub_sub.add_parser("status", parents=[ws, js], help="연결·준비 상태 (토큰 값은 보여 주지 않아요)")
    p.add_argument("--check", action="store_true", help="토큰이 살아 있는지 플랫폼에 실제로 확인해요")
    p.set_defaults(func=cmd_publish_status)
    p = pub_sub.add_parser("setup", parents=[ws], help="LinkedIn 앱 정보(Client ID·Secret·Redirect URI)를 저장해요")
    p.add_argument("platform", choices=["linkedin"], help="linkedin")
    p.set_defaults(func=cmd_publish_setup)
    p = pub_sub.add_parser("connect", parents=[ws], help="계정 연결 (LinkedIn 로그인·동의, 인스타그램 토큰 붙여넣기)")
    p.add_argument("platform", choices=["linkedin", "instagram"], help="linkedin 또는 instagram")
    p.add_argument("--paste", action="store_true", help="LinkedIn: 동의한 뒤 이동한 주소를 붙여 넣어 연결해요 (서버·Docker·원격용)")
    p.add_argument("--no-browser", action="store_true", help="LinkedIn: 브라우저를 자동으로 열지 않아요")
    p.add_argument("--port", type=int, help="LinkedIn: 콜백을 받을 포트 (기본: Redirect URI의 포트)")
    p.add_argument("--token-stdin", action="store_true", help="인스타그램: 토큰을 표준 입력에서 읽어요 (Docker·스크립트용)")
    p.set_defaults(func=cmd_publish_connect)
    p = pub_sub.add_parser("disconnect", parents=[ws], help="연결 해제 (INSIA에 저장한 토큰을 지워요)")
    p.add_argument("platform", choices=["linkedin", "instagram"], help="linkedin 또는 instagram")
    p.add_argument("--forget-app", action="store_true", help="앱 정보(Client ID·Secret)도 지워요")
    p.set_defaults(func=cmd_publish_disconnect)
    for name, parents, help_text in (
            ("preview", [ws, js], "올라갈 내용을 미리 봐요 (게시하지 않아요)"),
            ("send", [ws], "미리보기를 확인하고 확인 코드를 입력하면 한 건 게시해요 (터미널에서만)")):
        p = pub_sub.add_parser(name, parents=parents, help=help_text)
        p.add_argument("item_id", help="콘텐츠 id (insia items list; 겹치지 않는 일부만 적어도 돼요)")
        p.add_argument("--platform", choices=["linkedin", "instagram"], help=platform_help)
        p.add_argument("--visibility", choices=["PUBLIC", "CONNECTIONS"], help="LinkedIn 공개 범위 (기본 PUBLIC = 전체 공개)")
        p.add_argument("--ai-label", dest="ai_label", choices=["yes", "no"],
                       help="인스타그램에서 꼭 골라요: 'AI 정보' 라벨을 붙일지 (yes = 붙여요, no = 붙이지 않아요)")
        p.set_defaults(func=cmd_publish_preview if name == "preview" else cmd_publish_send)
    p = pub_sub.add_parser("attempts", parents=[ws, js], help="게시 기록 (최근 순)")
    p.add_argument("item_id", nargs="?", help="이 콘텐츠의 기록만")
    p.add_argument("--limit", type=_type_positive, default=50, help="최대 개수 (기본 50)")
    p.set_defaults(func=cmd_publish_attempts)
    p = pub_sub.add_parser("resolve", parents=[ws], help="게시됐는지 모르는 기록을 정리해요 (플랫폼에서 직접 확인한 뒤)")
    p.add_argument("attempt_id", help="게시 기록 id (insia publish attempts)")
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--published", action="store_true", help="올라갔어요")
    how.add_argument("--not-published", dest="not_published", action="store_true", help="안 올라갔어요")
    how.add_argument("--check", action="store_true", help="인스타그램에서 다시 확인해요 (읽기만, 게시하지 않아요)")
    p.add_argument("--url", help="--published일 때 게시물 주소 (선택)")
    p.set_defaults(func=cmd_publish_resolve)
    p = pub_sub.add_parser("refresh", parents=[ws, js], help="인스타그램 토큰만 갱신해요 (게시하지 않아요, cron용)")
    p.set_defaults(func=cmd_publish_refresh)

    backup = sub.add_parser("backup", parents=[ws, js], help="안전한 백업 (API 게시 토큰·로그는 빼요)",
                            description="insia.db를 서버를 끄지 않고 안전하게 복사하고(uploads/, prices.json 포함) credentials/·"
                                        "publish/·logs/·exports/는 빼요. 복원한 뒤 API 게시는 다시 연결하면 돼요.")
    backup.add_argument("--out", required=True, help="백업을 넣을 새 폴더 (예: backups/2026-09-28)")
    backup.set_defaults(func=cmd_backup)

    health = sub.add_parser("healthcheck", help="서버가 살아 있는지 확인해요 (Docker HEALTHCHECK용)")
    health.add_argument("--host", default="127.0.0.1",
                        help="서버 주소 (기본 127.0.0.1, IPv6는 ::1처럼; insia serve --host와 같게)")
    health.add_argument("--port", type=int, help="포트 (기본 INSIA_PORT 또는 8765)")
    health.add_argument("--url", help="확인할 주소 (기본 http://<host>:<port>/api/health, 주면 --host·--port는 무시)")
    health.add_argument("--timeout", type=float, default=5.0, help="기다릴 초 (기본 5)")
    health.add_argument("--quiet", action="store_true", help="정상일 때는 아무것도 출력하지 않아요")
    health.set_defaults(func=cmd_healthcheck)

    doctor = sub.add_parser("doctor", parents=[ws, js], help="설치·설정을 점검해요 (API 키, 워크스페이스, 선택 기능)")
    doctor.set_defaults(func=cmd_doctor)

    sample = sub.add_parser("sample-brief", help="샘플 브리프 JSON을 출력해요")
    sample.set_defaults(func=cmd_sample_brief)
    return parser


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _report_error(exc: BaseException) -> int:
    from .db import WorkspaceError

    if isinstance(exc, CommandError):
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if isinstance(exc, (WorkspaceError, BackendError)):
        prefix = "실행하지 못했어요: " if isinstance(exc, BackendError) else "오류: "
        print(prefix + str(exc), file=sys.stderr)
        return 1
    from .exporters import ExportError
    from .pipeline import PipelineError
    from .planner import PlanningError

    if isinstance(exc, (ExportError, PipelineError, PlanningError)):
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if isinstance(exc, OSError):
        where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
        print(f"오류: 파일을 읽거나 쓰지 못했어요{where}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print(f"오류: 예상하지 못한 문제가 생겼어요 — {type(exc).__name__}: {exc}", file=sys.stderr)
    if os.environ.get("INSIA_DEBUG"):
        traceback.print_exc()
    else:
        print("자세한 내용은 INSIA_DEBUG=1을 설정하고 다시 실행하면 볼 수 있어요.", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, argparse errors
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    home = getattr(args, "home", None)
    try:
        with _env_override("INSIA_HOME", home):
            return int(args.func(args) or 0)
    except UsageError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n중단했어요.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - Korean message instead of a traceback
        return _report_error(exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
