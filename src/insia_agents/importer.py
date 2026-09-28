"""Bring outside data into the workspace: loose profile files (``insia profile import``)
and Claude Code run folders (``insia import-run``).

Moved out of ``cli.py`` (which re-exports every name) so the server and scripts
can import profiles and runs without the command line. Errors are
``errors.UsageError`` with Korean messages.
"""

from __future__ import annotations

import hashlib
import json
import re
import typing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from .agents.common import channel_label
from .channels import finalize_review
from .config import Settings
from .documents import load_structured_file
from .errors import UsageError, error_text
from .models import ALL_CHANNELS, Brief, ChannelResult, Draft, Plan, Profile, ResearchPack, Review, UserDocument

if TYPE_CHECKING:
    from .db import Workspace


# ---------------------------------------------------------------------------
# Profile: labels and coercion of loose JSON/YAML data
# ---------------------------------------------------------------------------

# (label, hint) per Profile field — the template comments and ``profile show``.
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "company_name": ("회사 이름", "예: 인시아랩"),
    "service_name": ("서비스 이름", "예: INSIA 스마트에이전트"),
    "one_liner": ("한 줄 소개", "예: 1인 창업자를 위한 AI 콘텐츠 비서"),
    "description": ("서비스 설명", "무엇을 누구에게 어떻게 제공하는지 2~5문장"),
    "industry": ("업종", "예: 마케팅 SaaS, 동네 베이커리"),
    "stage": ("창업 단계", "예: 예비창업, 초기(3년 이내), 도약"),
    "target_customers": ("목표 고객", "예: 콘텐츠 마케팅을 혼자 하는 1인 창업자"),
    "problem": ("고객 문제", "고객이 지금 겪는 불편"),
    "solution": ("해결 방법", "우리 서비스가 그 문제를 푸는 방식"),
    "differentiators": ("차별점", "한 줄에 하나씩"),
    "business_model": ("수익 모델", "예: 월 구독(베이직/프로)"),
    "pricing": ("가격", "확정 가격이 아니면 '가정'이라고 적어 주세요"),
    "traction": ("실적·지표", "기준 시점을 붙여 한 줄에 하나씩. 예: 베타 사용자 120명 (2026-08 기준)"),
    "team": ("팀", "역할·역량만 사업계획서에 쓰고, 이름은 절대 쓰지 않아요 (블라인드 규정)"),
    "tone": ("브랜드 톤", "예: 신뢰감 있고 친근한 전문가 톤, 과장 금지"),
    "banned_words": ("금지 표현", "쓰면 안 되는 말, 한 줄에 하나씩. 예: 최고, 무조건"),
    "required_phrases": ("필수 문구", "SNS 글에 꼭 넣을 문구. 예: #광고, 면책 문구"),
    "default_hashtags": ("기본 해시태그", "예: #1인창업 (# 없이 적어도 붙여 줘요)"),
    "cta": ("기본 행동 유도 문구", "예: 무료 체험 신청은 프로필 링크에서"),
    "contact": ("문의처", "이메일, 네이버 톡톡 등"),
    "naver_blog_url": ("네이버 블로그 주소", "https://blog.naver.com/..."),
    "linkedin_url": ("링크드인 주소", "https://www.linkedin.com/in/..."),
    "instagram_handle": ("인스타그램 계정", "예: @insia.kr"),
    "brand_colors": ("브랜드 색", "카드뉴스용 #RRGGBB, 첫 번째가 주 색. 예: #0F766E"),
    "notes": ("메모", "에이전트가 알아야 할 기타 사실"),
}
TEAM_FIELDS: dict[str, tuple[str, str]] = {
    "role": ("역할", "예: 대표, CTO"),
    "name": ("실명", "사업계획서에는 절대 나오지 않아요"),
    "background": ("역량", "학위·전공, 경력, 보유 역량"),
    "hiring": ("채용 예정", "채용할 사람이면 true"),
}
_COLOR = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_TRUE_WORDS = {"true", "yes", "y", "1", "예", "네", "o", "채용", "채용예정"}
_FALSE_WORDS = {"false", "no", "n", "0", "아니오", "아니요", "x", ""}


def _list_fields() -> set[str]:
    return {name for name, info in Profile.model_fields.items()
            if typing.get_origin(info.annotation) is list and name != "team"}


def _field_label(name: str) -> str:
    return PROFILE_FIELDS.get(name, (name, ""))[0]


def _scalar_text(value: Any, label: str) -> str:
    if isinstance(value, (dict, list, tuple)):
        raise UsageError(f"'{label}'에는 목록이 아니라 글을 적어 주세요.")
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).strip()


def _text_list(value: Any, label: str) -> list[str]:
    if isinstance(value, dict):
        raise UsageError(f"'{label}'에는 한 줄에 하나씩 목록으로 적어 주세요.")
    if isinstance(value, str):
        items = [re.sub(r"^\s*[-*•]\s*", "", line) for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [_scalar_text(v, label) for v in value if v is not None]
    else:
        items = [_scalar_text(value, label)]
    return [item.strip() for item in items if item and item.strip()]


def _team(value: Any) -> list[dict[str, Any]]:
    entries = [value] if isinstance(value, dict) else value
    if not isinstance(entries, (list, tuple)):
        raise UsageError("'팀'은 role·name·background·hiring 항목을 가진 목록으로 적어 주세요.")
    team: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, 1):
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise UsageError(f"팀 {index}번째 항목은 role·name·background·hiring을 가진 묶음이어야 해요.")
        member: dict[str, Any] = {}
        for key in ("role", "name", "background"):
            if entry.get(key) is not None:
                member[key] = _scalar_text(entry[key], f"팀 {index}번째 {TEAM_FIELDS[key][0]}")
        hiring = entry.get("hiring")
        if isinstance(hiring, bool):
            member["hiring"] = hiring
        elif hiring is not None:
            word = str(hiring).strip().lower()
            if word in _TRUE_WORDS:
                member["hiring"] = True
            elif word in _FALSE_WORDS:
                member["hiring"] = False
            else:
                raise UsageError(f"팀 {index}번째 채용 예정(hiring)은 true 또는 false로 적어 주세요 (받은 값: {hiring!r})")
        if not any(member.get(k) for k in ("role", "name", "background")) and not member.get("hiring"):
            continue  # an empty template row
        if not member.get("role"):
            raise UsageError(f"팀 {index}번째에 역할(role)을 적어 주세요 (예: 대표, CTO, 채용 예정 개발자).")
        team.append(member)
    return team


def profile_from_data(data: Any, source: str = "파일") -> tuple[Profile, list[str]]:
    """A ``Profile`` from loose JSON/YAML data → ``(profile, ignored_keys)``.

    Accepts ``{"profile": {...}}``, skips ``_comment``-style keys, turns
    numbers/dates into text and a single string into a one-item list,
    normalizes hashtags and brand colors. Raises ``UsageError`` (Korean).
    """
    from .db import normalize_hashtags

    if data is None:
        data = {}
    if isinstance(data, dict) and isinstance(data.get("profile"), dict) and set(data) <= {"profile", "_안내", "_comment"}:
        data = data["profile"]
    if not isinstance(data, dict):
        raise UsageError(f"{source}의 프로필은 항목: 값 묶음(객체)이어야 해요.")
    list_fields = _list_fields()
    clean: dict[str, Any] = {}
    ignored: list[str] = []
    for raw_key, value in data.items():
        key = str(raw_key).strip()
        if key.startswith("_") or key == "updated_at":
            continue
        if key not in Profile.model_fields:
            ignored.append(key)
            continue
        if value is None:
            continue
        label = _field_label(key)
        if key == "team":
            clean[key] = _team(value)
        elif key == "default_hashtags" and isinstance(value, str):
            clean[key] = normalize_hashtags(value)  # "#a #b" or "a, b" on one line
        elif key in list_fields:
            clean[key] = _text_list(value, label)
        else:
            clean[key] = _scalar_text(value, label)
    if "default_hashtags" in clean:
        clean["default_hashtags"] = normalize_hashtags(clean["default_hashtags"])
    if "brand_colors" in clean:
        colors = []
        for color in clean["brand_colors"]:
            match = _COLOR.match(color.strip())
            if not match:
                raise UsageError(f"브랜드 색은 #RRGGBB 형식이어야 해요 (받은 값: {color!r}, 예: #0F766E)")
            colors.append("#" + match.group(1).upper())
        clean["brand_colors"] = colors
    try:
        profile = Profile.model_validate(clean)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = " · ".join(str(p) for p in first.get("loc", ()))
        raise UsageError(f"프로필 형식이 올바르지 않아요 ({where}): {first.get('msg', '')}") from None
    return profile, ignored


# ---------------------------------------------------------------------------
# import-run (Claude Code run folders)
# ---------------------------------------------------------------------------

_DRAFT_FILE = re.compile(r"^(?P<channel>[a-z_]+)\.r(?P<round>\d+)\.json$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")  # same rule as the workspace
_JSON_BLOCK = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class _ChannelImport:
    channel: str
    rounds: list[tuple[Draft, Review | None]] = field(default_factory=list)
    result: ChannelResult | None = None
    best_score: int | None = None
    item_id: str = ""
    added: int = 0
    reviews_updated: int = 0
    score_changes: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    run_id: str
    folder: str
    created: bool
    topic: str
    plan: bool
    research: tuple[int, int] | None
    channels: list[_ChannelImport]
    problems: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "folder": self.folder,
            "created": self.created,
            "topic": self.topic,
            "plan": self.plan,
            "research": {"findings": self.research[0], "sources": self.research[1]} if self.research else None,
            "items": {c.channel: {"item_id": c.item_id, "rounds": [d.round for d, _ in c.rounds],
                                  "scores": [r.score if r else None for _, r in c.rounds],
                                  "passed": c.result.passed if c.result else None, "versions_added": c.added,
                                  "reviews_updated": c.reviews_updated} for c in self.channels},
            "problems": self.problems,
            "notes": self.notes,
        }


def import_run_id(folder: Path) -> str:
    """``cc-<folder name>`` (ASCII-safe; a short hash keeps Korean folder names distinct)."""
    name = folder.name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).replace("..", ".").strip("-._")
    if base != name or not base:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
        base = f"{base[:50]}-{digest}" if base else digest
    return f"cc-{base[:70]}"


def _load_model(path: Path, model: type[BaseModel], what: str, problems: list[str]) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        problems.append(f"{what} 파일을 읽지 못했어요: {path.name} ({exc.strerror or exc})")
        return None
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "-"
        problems.append(f"{what} 형식이 올바르지 않아요: {path.name} ({exc.error_count()}개 오류, 예: {where} {first.get('msg', '')})")
        return None


def _plan_from_markdown(path: Path) -> Plan | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for block in reversed(_JSON_BLOCK.findall(text)):
        try:
            return Plan.model_validate_json(block)
        except ValidationError:
            continue
    return None


def _review_event(review: Review) -> dict[str, Any]:
    verdicts = {"supported": 0, "unsupported": 0, "needs_source": 0}
    for fact in review.fact_checks:
        verdicts[fact.verdict] = verdicts.get(fact.verdict, 0) + 1
    return {"channel": review.channel, "round": review.round, "score": review.score, "passed": review.passed,
            "rubric": review.rubric, "issues": review.issues, "format_checks": review.format_checks,
            "fact_checks": verdicts, "needs_research": review.needs_research, "summary": review.summary}


def _emit_import_events(ws: "Workspace", run_id: str, brief: Brief, plan: Plan | None, research: ResearchPack | None,
                        channels: list[_ChannelImport], settings: Settings, model: str, folder: Path) -> None:
    """A replayable event stream for the dashboard (first import only)."""
    from .agents import orchestrator, reviewer
    from .agents.common import AgentContext
    from .backends.mock_backend import sim_seconds
    from .events import EventBus, SimClock
    from .pipeline import event_sink

    bus = EventBus(run_id, clock=SimClock(0))
    sink = event_sink(ws, run_id)
    bus.add_listener(sink)
    ctx = AgentContext(bus=bus, backend=None, settings=settings, brief=brief, simulated=True)  # type: ignore[arg-type]
    advance = bus.clock.advance
    try:
        bus.emit("run.started", "system", {
            "brief": brief, "channels": [c.channel for c in channels], "mode": "import", "model": model,
            "max_rounds": max([len(c.rounds) - 1 for c in channels] + [settings.max_rounds]),
            "pass_score": settings.pass_score, "kind": "import",
        })
        ctx.log(f"Claude Code 실행 폴더를 보관함으로 가져왔어요: {folder.name}")
        if plan is not None:
            advance(sim_seconds("plan"))
            orchestrator.emit_plan(ctx, plan)
        if research is not None:
            for source in research.sources:
                advance(sim_seconds("research_source"))
                bus.emit("research.source", "researcher", {"source": source})
            for finding in research.findings:
                advance(sim_seconds("research_finding"))
                bus.emit("research.finding", "researcher", {"finding": finding})
            bus.emit("research.completed", "researcher", {"findings": len(research.findings), "sources": len(research.sources),
                                                          "gaps": list(research.gaps), "followup": False})
        for entry in channels:
            for index, (draft, review) in enumerate(entry.rounds):
                advance(sim_seconds("draft" if draft.round == 0 else "revise", entry.channel, draft.round))
                orchestrator.emit_draft(ctx, draft)
                if review is None:
                    continue
                bus.emit("review.started", "reviewer", {"channel": entry.channel, "round": draft.round})
                advance(sim_seconds("review", entry.channel, draft.round))
                bus.emit("review.completed", "reviewer", _review_event(review))
                if not review.passed and index < len(entry.rounds) - 1:
                    bus.emit("revision.requested", "reviewer", {"channel": entry.channel, "round": draft.round,
                                                                "issues": len(review.issues), "top_issue": reviewer.top_issue(review)})
            if entry.result is not None and entry.best_score is not None:
                orchestrator.complete_channel(ctx, entry.result, entry.best_score)
        finished = [c for c in channels if c.result is not None]
        bus.emit("run.completed", "system", {
            "duration_s": round(bus.clock.now(), 1),
            "scores": {c.channel: c.best_score for c in finished},
            "passed": {c.channel: c.result.passed for c in finished if c.result is not None},
            "output_dir": None, "items": {c.channel: c.item_id for c in channels}, "kind": "import",
        })
    finally:
        bus.remove_listener(sink)


def _store_channel(ws: "Workspace", run_id: str, brief: Brief, entry: _ChannelImport) -> None:
    """Versions for one channel, idempotently.

    Rounds already stored unchanged are kept (their review is updated when the
    file changed); from the first changed/new round on, the rounds are added
    again in order so the newest version is always the latest round. When the
    best-scoring round is not the last one, it is added once more as the
    newest version (same rule as the pipeline).
    """
    from .db import pipeline_item_id

    item_id = pipeline_item_id(run_id, entry.channel)
    entry.item_id = item_id
    first = entry.rounds[0][0]
    ws.ensure_item(item_id, entry.channel, first.title, run_id=run_id, brief=brief)
    stored: dict[int, Any] = {}
    for version in ws.list_run_versions(run_id, entry.channel):
        stored[version.draft.round] = version  # newest version of a round wins
    changed_from = len(entry.rounds)
    for index, (draft, _) in enumerate(entry.rounds):
        existing = stored.get(draft.round)
        if existing is None or existing.draft.model_dump() != draft.model_dump():
            changed_from = index
            break
    for draft, review in entry.rounds[:changed_from]:
        existing = stored[draft.round]
        if review is not None and (existing.review is None or existing.review.model_dump() != review.model_dump()):
            ws.attach_review(existing.id, review)
            entry.reviews_updated += 1
    for draft, review in entry.rounds[changed_from:]:
        ws.add_version(item_id, draft, source="agent", review=review, run_id=run_id)
        entry.added += 1
    reviewed = [(d, r) for d, r in entry.rounds if r is not None]
    pending = entry.rounds[-1][1] is None
    if entry.result is not None and not pending and entry.result.final.round != reviewed[-1][0].round:
        # the newest version *this import* stored (a later human edit in the library is left alone)
        ours = sorted(ws.list_run_versions(run_id, entry.channel), key=lambda v: v.version)
        latest = ours[-1] if ours else None
        if latest is None or latest.draft.model_dump() != entry.result.final.model_dump():
            best_review = next(r for d, r in reviewed if d.round == entry.result.final.round)
            ws.add_version(item_id, entry.result.final, source="agent", review=best_review, run_id=run_id)
            entry.added += 1


def import_run_folder(ws: "Workspace", folder: Path, settings: Settings, *, run_id: str | None = None) -> ImportReport:
    """Import a Claude Code run folder (layout: ``.claude/agents/orchestrator.md``) into the workspace.

    ``brief.json`` and at least one ``drafts/<channel>.r<N>.json`` are
    required (``UsageError`` otherwise). Every other file is validated; an
    invalid file is skipped and reported in ``problems``. Reviews are
    re-finalized with the current code (format checks, score, verdict) using
    the folder's ``profile.json`` when present. Re-importing the same folder
    updates the run in place (no duplicate items or versions).
    """
    folder = folder.expanduser()
    if not folder.is_dir():
        raise UsageError(f"실행 폴더를 찾을 수 없어요: {folder}")
    folder = folder.resolve()
    problems: list[str] = []
    notes: list[str] = []
    brief_path = folder / "brief.json"
    if not brief_path.is_file():
        raise UsageError(f"brief.json이 없어요: {folder}. Claude Code 실행 폴더(outputs/<날짜>-<주제>/)를 지정해 주세요.")
    brief_problems: list[str] = []
    brief = _load_model(brief_path, Brief, "브리프(brief.json)", brief_problems)
    if brief is None:
        raise UsageError(brief_problems[0])

    plan: Plan | None = None
    if (folder / "plan.json").is_file():
        plan = _load_model(folder / "plan.json", Plan, "계획(plan.json)", problems)
    if plan is None and (folder / "plan.md").is_file():
        plan = _plan_from_markdown(folder / "plan.md")
        if plan is None:
            notes.append("plan.md 끝에 Plan JSON 코드 블록이 없어 계획은 가져오지 않았어요.")
    if plan is None and not (folder / "plan.json").is_file() and not (folder / "plan.md").is_file():
        notes.append("계획 파일(plan.json/plan.md)이 없어요.")
    research: ResearchPack | None = None
    if (folder / "research.json").is_file():
        research = _load_model(folder / "research.json", ResearchPack, "리서치(research.json)", problems)
    else:
        notes.append("리서치 파일(research.json)이 없어요. 재검수·수정 요청 때 근거 없이 진행돼요.")

    profile: Profile | None = None
    if (folder / "profile.json").is_file():
        try:
            data = load_structured_file(folder / "profile.json", "profile.json")
            loaded, _ = profile_from_data(data, "profile.json")
            from .db import profile_is_empty

            profile = None if profile_is_empty(loaded) else loaded
        except UsageError as exc:
            problems.append(f"profile.json을 쓰지 못했어요: {exc}")
    doc_ids: list[str] = []
    if (folder / "documents.json").is_file():
        try:
            raw = json.loads((folder / "documents.json").read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                raw = raw.get("documents", [])
            docs = [UserDocument.model_validate(d) for d in raw or []]
        except (OSError, ValueError, ValidationError) as exc:
            problems.append(f"documents.json 형식이 올바르지 않아요 ({type(exc).__name__})")
        else:
            doc_ids = [d.id for d in docs]
            known = {d.id: d for d in ws.list_documents()}
            missing = [d.id for d in docs if d.id not in known or known[d.id].text != d.text]
            if missing:
                notes.append(f"실행에 쓴 자료 {len(docs)}개 중 {len(missing)}개({', '.join(missing)})는 이 워크스페이스에 같은 내용이 없어요. "
                             "필요하면 insia docs add로 넣어 주세요.")

    draft_dir, review_dir = folder / "drafts", folder / "reviews"
    by_channel: dict[str, dict[int, Draft]] = {}
    for path in sorted(draft_dir.glob("*.json")) if draft_dir.is_dir() else []:
        match = _DRAFT_FILE.match(path.name)
        if not match or match.group("channel") not in ALL_CHANNELS:
            problems.append(f"초안 파일 이름이 <채널>.r<N>.json 형식이 아니라 건너뛰었어요: drafts/{path.name}")
            continue
        channel, number = match.group("channel"), int(match.group("round"))
        draft = _load_model(path, Draft, "초안", problems)
        if draft is None:
            continue
        if draft.channel != channel:
            problems.append(f"drafts/{path.name}의 채널이 파일 이름과 달라요 ({draft.channel}). 건너뛰었어요.")
            continue
        if draft.round != number:
            notes.append(f"drafts/{path.name}의 round({draft.round})를 파일 이름에 맞춰 {number}로 읽었어요.")
            draft = draft.model_copy(update={"round": number})
        by_channel.setdefault(channel, {})[number] = draft
    if not by_channel:
        raise UsageError(f"가져올 초안이 없어요: {folder / 'drafts'}에 <채널>.r<N>.json 파일이 필요해요. "
                         + (problems[0] if problems else ""))

    entries: list[_ChannelImport] = []
    order = [c for c in brief.channels if c in by_channel] + [c for c in ALL_CHANNELS if c in by_channel and c not in brief.channels]
    for channel in order:
        entry = _ChannelImport(channel)
        rounds = by_channel[channel]
        for number in sorted(rounds):
            draft = rounds[number]
            review: Review | None = None
            path = review_dir / f"{channel}.r{number}.json"
            if path.is_file():
                recorded = _load_model(path, Review, "검수", problems)
                if recorded is not None:
                    review = finalize_review(recorded, draft, brief, pass_score=settings.pass_score, profile=profile)
                    if review.score != recorded.score or review.passed != recorded.passed:
                        entry.score_changes.append(f"R{number} {recorded.score}→{review.score}점")
            entry.rounds.append((draft, review))
        for path in sorted(review_dir.glob(f"{channel}.r*.json")) if review_dir.is_dir() else []:
            match = _DRAFT_FILE.match(path.name)
            if match and int(match.group("round")) not in rounds:
                problems.append(f"reviews/{path.name}에 맞는 초안이 없어 건너뛰었어요.")
        unreviewed = [d.round for d, r in entry.rounds[:-1] if r is None]
        if unreviewed:
            notes.append(f"{channel_label(channel)} R{', R'.join(map(str, unreviewed))}에는 검수 파일이 없어요.")
        reviewed = [(d, r) for d, r in entry.rounds if r is not None]
        if reviewed:
            reviews = [r for _, r in reviewed]
            best = max(range(len(reviews)), key=lambda i: (reviews[i].passed, reviews[i].score, i))  # like the pipeline
            entry.result = ChannelResult(channel=channel, final=reviewed[best][0], drafts=[d for d, _ in reviewed],  # type: ignore[arg-type]
                                         reviews=reviews, passed=reviews[best].passed, rounds=len(reviewed) - 1)
            entry.best_score = reviews[best].score
        if entry.rounds[-1][1] is None:
            notes.append(f"{channel_label(channel)} 마지막 초안 R{entry.rounds[-1][0].round}은 검수 전이에요. 보관함에서 재검수할 수 있어요.")
        entries.append(entry)

    run_id = run_id or import_run_id(folder)
    existing = ws.get_run(run_id)
    if existing is not None:
        if existing["kind"] != "import":
            raise UsageError(f"이미 같은 id의 실행이 있어요: {run_id}. --run-id로 다른 id를 정해 주세요.")
        previous = (existing.get("options") or {}).get("source_folder")
        if previous and previous != str(folder) and (existing.get("brief") or {}).get("topic") != brief.topic:
            raise UsageError(f"다른 폴더({previous})에서 가져온 실행과 id가 겹쳐요: {run_id}. --run-id로 다른 id를 정해 주세요.")
    meta: dict[str, Any] = {}
    if (folder / "meta.json").is_file():
        try:
            loaded_meta = json.loads((folder / "meta.json").read_text(encoding="utf-8-sig"))
            meta = loaded_meta if isinstance(loaded_meta, dict) else {}
        except (OSError, ValueError):
            meta = {}
    model = str(meta.get("model") or "claude-code")
    options = {"source": "claude-code", "source_folder": str(folder), "pass_score": settings.pass_score,
               "doc_ids": doc_ids, "use_profile": profile is not None,
               "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    created = existing is None
    if created:
        ws.create_run(run_id, brief, kind="import", options=options, mode="import", model=model, profile=profile)
    fields: dict[str, Any] = {"plan": plan, "research": research, "options": options, "model": model}
    if profile is not None:
        fields["profile"] = profile
    try:
        ws.update_run(run_id, **{k: v for k, v in fields.items() if v is not None or k in ("plan", "research")})
        for entry in entries:
            _store_channel(ws, run_id, brief, entry)
        if created or ws.last_event(run_id) is None:  # one event stream per imported run (dashboard replay)
            _emit_import_events(ws, run_id, brief, plan, research, entries, settings, model, folder)
    except BaseException as exc:  # never leave an import run "running"
        try:
            ws.update_run(run_id, status="failed", error=f"가져오기 실패: {error_text(exc)}")
        except Exception:  # noqa: BLE001 - keep the original error
            pass
        raise
    summary = " · ".join(problems[:3])
    ws.update_run(run_id, status="completed", error=summary, cost_usd=0.0)
    return ImportReport(run_id=run_id, folder=str(folder), created=created, topic=brief.topic, plan=plan is not None,
                        research=(len(research.findings), len(research.sources)) if research else None,
                        channels=entries, problems=problems, notes=notes)
