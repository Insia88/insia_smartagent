"""Eval cases: one JSON file per case in ``evals/cases/``.

A case is the input the pipeline gets in production (a ``Brief`` plus an
optional company ``Profile`` and user documents) and what the output must and
should satisfy:

- ``must``: hard assertions. A failing ``must`` fails the case and makes
  ``insia eval run`` exit 1; ``insia eval compare`` exits 1 when one that
  passed in the baseline fails now.
- ``should``: soft targets (reviewer score, rounds, citation coverage …).
  They are reported and compared but never fail the run.

Every assertion is deterministic and cheap (see ``graders``); an assertion
may be limited to some channels (``channels``) or modes (``modes``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..channels import check_format
from ..config import REPO_ROOT_GUESS
from ..models import Brief, ChannelId, Draft, Profile

CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
CASE_SUFFIX = ".json"
DEFAULT_CASES_DIR = Path("evals") / "cases"

AssertionType = Literal[
    "checks_pass",            # these format/brand check ids exist and pass (channels.check_format)
    "all_checks_pass",        # every format/brand check passes
    "not_contains",           # none of these strings appear (title, body, hashtags; spacing/case-insensitive)
    "contains",               # every one of these strings appears
    "max_unsupported_numbers",  # at most N number claims without research/profile/document support or a marker
    "min_placeholders",       # at least N placeholders (○○, [확인 필요: …], [대표자 성명] …)
    "assumption_marked",      # every figure in these units that research/documents do not support says 가정
    "min_citation_coverage",  # share of number claims with a source next to them (0~1)
    "min_score",              # reviewer's final score
    "reviewer_passed",        # reviewer passed the final draft
    "max_rounds",             # revisions used
    "max_critical_issues",    # critical issues left in the final review
    "max_cost_usd",           # case cost (all channels)
    "max_duration_s",         # case duration (virtual seconds in mock, real seconds in live)
]

CASE_LEVEL_TYPES = frozenset({"max_cost_usd", "max_duration_s"})
_NEEDS_CHECKS = frozenset({"checks_pass"})
_NEEDS_VALUES = frozenset({"not_contains", "contains"})
_NEEDS_VALUE = frozenset({"max_unsupported_numbers", "min_placeholders", "min_citation_coverage", "min_score",
                          "max_rounds", "max_critical_issues", "max_cost_usd", "max_duration_s"})

TYPE_LABELS: dict[str, str] = {
    "checks_pass": "형식·브랜드 검사 통과",
    "all_checks_pass": "모든 형식 검사 통과",
    "not_contains": "들어가면 안 되는 말 없음",
    "contains": "꼭 들어갈 말 있음",
    "max_unsupported_numbers": "근거 없는 수치",
    "min_placeholders": "자리표시 사용",
    "assumption_marked": "가정 표시",
    "min_citation_coverage": "수치 인용률",
    "min_score": "검수 점수",
    "reviewer_passed": "검수 통과",
    "max_rounds": "수정 횟수",
    "max_critical_issues": "critical 이슈",
    "max_cost_usd": "비용",
    "max_duration_s": "걸린 시간",
}


IMPLIED_NOTE = "facts_available: false라서 붙은 조건이에요 (공식 수치가 없는 주제는 모르는 값을 자리표시로 남기고 수치를 지어내지 않아요)"


class CaseError(ValueError):
    """A case file is missing or invalid. ``str(exc)`` is Korean and names every problem."""


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AssertionType
    channels: list[ChannelId] = Field(default_factory=list, description="비우면 케이스의 모든 채널")
    checks: list[str] = Field(default_factory=list, description="checks_pass: 검사 id (channels.check_format)")
    values: list[str] = Field(default_factory=list, description="not_contains / contains: 찾을 말")
    value: float | None = Field(default=None, description="숫자 기준 (개수, 점수, 비율, 달러, 초)")
    units: list[str] = Field(default_factory=list, description="assumption_marked: 단위 (기본 원)")
    markers: list[str] = Field(default_factory=list, description="assumption_marked: 표시 말 (기본 가정)")
    modes: list[Literal["mock", "live"]] = Field(default_factory=list, description="비우면 mock·live 모두")
    note: str = Field(default="", description="왜 보는지 (보고서에 보여요)")

    @model_validator(mode="after")
    def _params(self) -> "Assertion":
        if self.type in _NEEDS_CHECKS and not [c for c in self.checks if c.strip()]:
            raise ValueError(f"{self.type}에는 checks(검사 id 목록)가 필요해요")
        if self.type in _NEEDS_VALUES and not [v for v in self.values if v.strip()]:
            raise ValueError(f"{self.type}에는 values(찾을 말 목록)가 필요해요")
        if self.type in _NEEDS_VALUE and self.value is None:
            raise ValueError(f"{self.type}에는 value(숫자 기준)가 필요해요")
        if self.value is not None and (self.value != self.value or self.value < 0):
            raise ValueError(f"{self.type}의 value는 0 이상인 숫자여야 해요")
        if self.type == "min_citation_coverage" and self.value is not None and self.value > 1:
            raise ValueError("min_citation_coverage의 value는 0~1 사이 비율이에요 (예: 0.8)")
        if self.type in CASE_LEVEL_TYPES and self.channels:
            raise ValueError(f"{self.type}는 케이스 전체 기준이라 channels를 쓰지 않아요")
        return self

    @property
    def key(self) -> str:
        """Stable id within a case (used to match results across runs)."""
        parts = self.checks or self.values or self.units
        key = self.type + (":" + "+".join(p.strip() for p in parts) if parts else "")
        if self.value is not None:
            key += "=" + format_number(self.value)
        return key

    @property
    def case_level(self) -> bool:
        return self.type in CASE_LEVEL_TYPES

    def applies(self, channel: str, mode: str) -> bool:
        if self.modes and mode not in self.modes:
            return False
        if self.case_level:
            return channel == "*"
        return channel != "*" and (not self.channels or channel in self.channels)

    def label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)


class CaseDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    text: str
    kind: Literal["text", "markdown"] = "text"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = Field(default="", description="무엇을 보는 케이스인지 (한국어)")
    source: Literal["sample-run", "synthetic", "real-bad-output"] = "synthetic"
    tags: list[str] = Field(default_factory=list)
    today: str = Field(default="", description="기준일 YYYY-MM-DD (mock은 비우면 고정 날짜, live는 비우면 오늘)")
    facts_available: bool = Field(default=True, description=(
        "false면 공식 수치를 구할 수 없는 주제예요. 모든 채널에 '자리표시 1개 이상'(min_placeholders ≥ 1)과 "
        "'근거 없는 수치 0개'(max_unsupported_numbers = 0)가 필수 조건으로 자동으로 붙어요 (must에 직접 적으면 그 값을 써요)"))
    brief: Brief
    profile: Profile | None = None
    documents: list[CaseDocument] = Field(default_factory=list)
    must: list[Assertion] = Field(default_factory=list)
    should: list[Assertion] = Field(default_factory=list)

    @property
    def channels(self) -> list[str]:
        return list(dict.fromkeys(self.brief.channels))

    def implied(self) -> list[Assertion]:
        """``must`` assertions that ``facts_available: false`` adds.

        A topic without official figures must keep unknown values as placeholders and invent none: every channel
        not already covered by an explicit ``must`` of the same type (for every mode) gets ``min_placeholders ≥ 1``
        and ``max_unsupported_numbers = 0``."""
        if self.facts_available:
            return []
        out: list[Assertion] = []
        for kind, value in (("min_placeholders", 1.0), ("max_unsupported_numbers", 0.0)):
            covered = {c for a in self.must if a.type == kind and not a.modes for c in (a.channels or self.channels)}
            missing = [c for c in self.channels if c not in covered]
            if missing:
                out.append(Assertion(type=kind, value=value, channels=[] if len(missing) == len(self.channels) else missing,
                                     note=IMPLIED_NOTE))
        return out

    def assertions(self, kind: str) -> list[Assertion]:
        return list(self.must) + self.implied() if kind == "must" else list(self.should)


def format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# ---------------------------------------------------------------------------
# Check ids a draft of this channel gets (for validating ``checks_pass``)
# ---------------------------------------------------------------------------


def expected_check_ids(channel: ChannelId, brief: Brief, profile: Profile | None) -> list[str]:
    """The ids ``channels.check_format`` produces for this channel, brief and profile."""
    dummy = Draft(channel=channel, round=0, title="-", content="-")
    return [check.id for check in check_format(dummy, brief, profile)]


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def find_cases_dir(explicit: str | Path | None = None) -> Path:
    """``--cases`` when given, else ``evals/cases`` under the current folder or the repository."""
    if explicit:
        return Path(explicit).expanduser()
    for base in (Path.cwd(), REPO_ROOT_GUESS):
        candidate = base / DEFAULT_CASES_DIR
        if candidate.is_dir():
            return candidate
    return Path.cwd() / DEFAULT_CASES_DIR


def case_problems(case: EvalCase, *, stem: str | None = None) -> list[str]:
    """Korean problems of a parsed case (empty = valid)."""
    problems: list[str] = []
    if not CASE_ID.match(case.id):
        problems.append(f"id {case.id!r}는 영문 소문자·숫자·하이픈 2~61자로 적어 주세요")
    if stem is not None and stem != case.id:
        problems.append(f"파일 이름({stem}{CASE_SUFFIX})과 id({case.id})가 달라요")
    if not case.title.strip():
        problems.append("title이 비어 있어요")
    if not case.brief.topic.strip():
        problems.append("brief.topic이 비어 있어요")
    if not case.brief.channels:
        problems.append("brief.channels에 채널을 하나 이상 적어 주세요")
    if case.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", case.today):
        problems.append(f"today {case.today!r}는 YYYY-MM-DD로 적어 주세요")
    if not case.must:
        problems.append("must(꼭 지켜야 할 조건)를 하나 이상 적어 주세요")
    for i, doc in enumerate(case.documents, 1):
        if not doc.text.strip():
            problems.append(f"documents[{i}]의 text가 비어 있어요")
    channels = case.channels
    seen: dict[tuple[str, str, str], int] = {}
    for kind in ("must", "should"):
        for index, assertion in enumerate(case.assertions(kind), 1):
            where = f"{kind}[{index}] {assertion.type}"
            scope = assertion.channels or channels
            outside = [c for c in assertion.channels if c not in channels]
            if outside:
                problems.append(f"{where}: 브리프에 없는 채널이에요 ({', '.join(outside)})")
            for channel in (["*"] if assertion.case_level else scope):
                slot = (kind, channel, assertion.key)
                if slot in seen:
                    problems.append(f"{where}: 같은 조건({assertion.key})이 {channel} 채널에 두 번 있어요")
                seen[slot] = index
            if assertion.type == "checks_pass":
                for channel in scope:
                    if channel not in channels:
                        continue
                    known = expected_check_ids(channel, case.brief, case.profile)  # type: ignore[arg-type]
                    unknown = [c for c in assertion.checks if c not in known]
                    if unknown:
                        problems.append(f"{where}: {channel}에는 없는 검사 id예요 ({', '.join(unknown)}; "
                                        f"가능: {', '.join(known)})")
    return problems


def load_case(path: str | Path) -> EvalCase:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise CaseError(f"{path.name}: 파일을 읽지 못했어요 ({exc.strerror or exc})") from None
    except ValueError as exc:
        raise CaseError(f"{path.name}: JSON 형식이 올바르지 않아요 ({exc})") from None
    try:
        case = EvalCase.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or '-'}: {err['msg']}" for err in exc.errors()[:5])
        raise CaseError(f"{path.name}: 케이스 형식이 올바르지 않아요 ({details})") from None
    problems = case_problems(case, stem=path.stem)
    if problems:
        raise CaseError(f"{path.name}: " + " · ".join(problems))
    return case


def load_cases(cases_dir: str | Path, ids: list[str] | None = None) -> list[EvalCase]:
    """Every case in ``cases_dir`` (sorted by id), or only ``ids`` (in the given order).

    Raises ``CaseError`` listing every broken file, duplicate id or unknown id.
    """
    root = Path(cases_dir)
    if not root.is_dir():
        raise CaseError(f"케이스 폴더가 없어요: {root} (--cases로 폴더를 알려 주세요)")
    cases: dict[str, EvalCase] = {}
    problems: list[str] = []
    for path in sorted(root.glob(f"*{CASE_SUFFIX}")):
        try:
            case = load_case(path)
        except CaseError as exc:
            problems.append(str(exc))
            continue
        if case.id in cases:
            problems.append(f"{path.name}: id {case.id}가 다른 파일과 겹쳐요")
            continue
        cases[case.id] = case
    if problems:
        raise CaseError("케이스 파일에 문제가 있어요:\n- " + "\n- ".join(problems))
    if not cases:
        raise CaseError(f"케이스가 하나도 없어요: {root}")
    if ids:
        wanted = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
        unknown = [i for i in wanted if i not in cases]
        if unknown:
            raise CaseError(f"없는 케이스예요: {', '.join(unknown)} ('insia eval list'로 id를 확인해 주세요)")
        return [cases[i] for i in wanted]
    return [cases[i] for i in sorted(cases)]


def case_summary(case: EvalCase) -> dict[str, Any]:
    """One line of ``insia eval list`` (also JSON)."""
    return {
        "id": case.id,
        "title": case.title,
        "channels": case.channels,
        "source": case.source,
        "profile": case.profile is not None,
        "documents": len(case.documents),
        "must": len(case.assertions("must")),
        "should": len(case.should),
        "facts_available": case.facts_available,
        "tags": list(case.tags),
        "description": case.description,
    }
