"""Content calendar planning: company profile + theme + history → calendar slots.

``plan_week`` asks the backend (``Backend.plan_calendar``) for a
``ContentPlan``, normalizes it (dates inside the range on weekdays, requested
channels and counts only, one post per channel per day) and stores the slots
with ``workspace.add_slots``. The helpers here are shared by both backends so
live and mock plans obey the same rules.

Dates are ISO ``YYYY-MM-DD`` strings; "weekdays" are Monday–Friday (a range
with no weekday, e.g. a lone weekend, falls back to every day in it).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from pydantic import BaseModel, Field

from .channels import CHANNELS
from .models import ALL_CHANNELS, CalendarSlot, ChannelId, ContentItem, ContentPlan, PlannedSlot, Profile

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import Backend

WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
MAX_DAYS = 31
MAX_PER_CHANNEL = 31
HISTORY_LIMIT = 50
HISTORY_LOOKBACK_DAYS = 56  # planned slots this far back count as "already covered"

COUNT_ALIASES: dict[str, ChannelId] = {
    "blog": "naver_blog", "naver": "naver_blog", "naverblog": "naver_blog", "naver-blog": "naver_blog",
    "li": "linkedin", "ig": "instagram", "insta": "instagram", "plan": "bizplan",
}

DEFAULT_GOALS: dict[str, str] = {
    "bizplan": "지원사업 제출용 문서 준비",
    "naver_blog": "검색 유입과 문의",
    "linkedin": "전문성 인지와 대화",
    "instagram": "저장·공유로 도달 확대",
}


class PlanningError(ValueError):
    """Invalid planning input. ``str(exc)`` is a Korean message."""


# ---------------------------------------------------------------------------
# Dates and counts
# ---------------------------------------------------------------------------


def parse_day(value: str | date, label: str = "날짜") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise PlanningError(f"{label} '{value}'을(를) 읽을 수 없어요. YYYY-MM-DD 형식으로 적어 주세요.") from None


def date_span(start: str | date, end: str | date) -> list[date]:
    """Every day from ``start`` to ``end`` inclusive (validated)."""
    first, last = parse_day(start, "시작일"), parse_day(end, "종료일")
    if last < first:
        raise PlanningError(f"종료일({last.isoformat()})이 시작일({first.isoformat()})보다 빨라요.")
    days = (last - first).days + 1
    if days > MAX_DAYS:
        raise PlanningError(f"계획 기간은 최대 {MAX_DAYS}일까지예요 (지금 {days}일).")
    return [first + timedelta(days=i) for i in range(days)]


def slot_days(start: str | date, end: str | date) -> list[str]:
    """Days that can hold posts: the weekdays in the range, or every day when
    the range has no weekday."""
    days = date_span(start, end)
    weekdays = [d for d in days if d.weekday() < 5]
    return [d.isoformat() for d in (weekdays or days)]


def weekday_label(day: str) -> str:
    return WEEKDAYS_KO[date.fromisoformat(day).weekday()]


def normalize_counts(counts: Mapping[str, Any]) -> dict[ChannelId, int]:
    """Validate ``{channel: posts}`` (aliases such as ``blog`` allowed); zero
    counts are dropped; order follows ``ALL_CHANNELS``."""
    if not isinstance(counts, Mapping):
        raise PlanningError("채널별 개수는 {\"naver_blog\": 2, \"linkedin\": 1} 같은 형식이어야 해요.")
    merged: dict[str, int] = {}
    for raw_key, raw_value in counts.items():
        key = str(raw_key).strip().lower()
        channel = COUNT_ALIASES.get(key, key)
        if channel not in CHANNELS:
            raise PlanningError(f"알 수 없는 채널이에요: {raw_key!r} (naver_blog, linkedin, instagram, bizplan 중에서 골라 주세요)")
        if isinstance(raw_value, bool):
            raise PlanningError(f"{CHANNELS[channel].label} 개수는 0 이상의 정수여야 해요.")
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise PlanningError(f"{CHANNELS[channel].label} 개수는 0 이상의 정수여야 해요.") from None
        if value < 0 or value != float(raw_value):
            raise PlanningError(f"{CHANNELS[channel].label} 개수는 0 이상의 정수여야 해요.")
        merged[channel] = merged.get(channel, 0) + value
    result = {c: min(merged[c], MAX_PER_CHANNEL) for c in ALL_CHANNELS if merged.get(c, 0) > 0}
    if not result:
        raise PlanningError("채널별 게시물 개수를 하나 이상 정해 주세요.")
    return result  # type: ignore[return-value]


def cap_counts(counts: Mapping[ChannelId, int], available_days: int) -> tuple[dict[ChannelId, int], list[str]]:
    """At most one post per channel per day: cap each count at the number of
    plannable days and explain every cap in Korean."""
    capped: dict[ChannelId, int] = {}
    notices: list[str] = []
    for channel, count in counts.items():
        if count > available_days:
            notices.append(f"{CHANNELS[channel].label}은(는) 하루 한 편까지라 {count}편 대신 {available_days}편만 계획해요.")
        capped[channel] = min(count, available_days)
    return capped, notices


def spread(count: int, days: list[str], offset: int = 0) -> list[str]:
    """``count`` distinct days spread evenly over ``days`` (deterministic);
    ``offset`` rotates the pattern so channels do not all land on one day."""
    if count <= 0 or not days:
        return []
    n = len(days)
    count = min(count, n)
    picks = {(int((i + 0.5) * n / count) + offset) % n for i in range(count)}
    return [days[i] for i in sorted(picks)]


# ---------------------------------------------------------------------------
# Topics and history
# ---------------------------------------------------------------------------


def topic_key(text: str) -> str:
    """Comparison key for "same topic": no spaces, punctuation or [데모] tag, lower-case."""
    text = re.sub(r"\[데모\]", "", text or "")
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def history_keys(history: Iterable[ContentItem]) -> set[str]:
    return {key for item in history if (key := topic_key(item.title))}


def repeats_history(topic: str, keys: set[str]) -> bool:
    key = topic_key(topic)
    if not key:
        return False
    return any(key == k or (len(k) >= 8 and (k in key or key in k)) for k in keys)


def history_from_slots(slots: Iterable[CalendarSlot]) -> list[ContentItem]:
    """Planned slots as pseudo items so a second plan does not repeat them."""
    return [ContentItem(id=s.id, run_id=s.run_id, channel=s.channel, title=s.topic, status="scheduled",
                        scheduled_at=s.date, note=s.angle)
            for s in slots if s.status != "skipped" and s.topic.strip()]


def gather_history(workspace: Any, start: str, end: str) -> list[ContentItem]:
    """Published items, approved/scheduled items and recently planned slots."""
    history: list[ContentItem] = []
    seen: set[str] = set()

    def add(items: Iterable[ContentItem]) -> None:
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                history.append(item)

    if hasattr(workspace, "published_history"):
        add(workspace.published_history(limit=HISTORY_LIMIT))
    if hasattr(workspace, "list_items"):
        for status in ("scheduled", "approved"):
            add(workspace.list_items(status=status, limit=HISTORY_LIMIT))
    if hasattr(workspace, "list_slots"):
        since = (parse_day(start) - timedelta(days=HISTORY_LOOKBACK_DAYS)).isoformat()
        add(history_from_slots(workspace.list_slots(date_from=since, date_to=end)))
    return history[: HISTORY_LIMIT * 3]


# ---------------------------------------------------------------------------
# Normalizing a plan
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_slot(slot: PlannedSlot, day: str) -> PlannedSlot:
    keywords: list[str] = []
    for word in slot.keywords:
        word = _clip(word, 40)
        if word and word not in keywords:
            keywords.append(word)
    return PlannedSlot(date=day, channel=slot.channel, topic=_clip(slot.topic, 120), angle=_clip(slot.angle, 80),
                       keywords=keywords[:5], goal=_clip(slot.goal, 80) or DEFAULT_GOALS.get(slot.channel, ""))


def normalize_plan(plan: ContentPlan, start: str, end: str, counts: Mapping[str, int]) -> ContentPlan:
    """Make any backend's plan obey the calendar rules.

    Drops slots with an unknown/unrequested channel, an empty topic or a date
    outside the range; moves a weekend slot to the nearest free weekday;
    keeps one post per channel per day and at most ``counts[channel]`` posts
    (earliest first); sorts by date then channel order. Idempotent.
    """
    days = slot_days(start, end)
    allowed = set(days)
    span = {d.isoformat() for d in date_span(start, end)}
    wanted = normalize_counts(counts) if counts else {}
    order = {c: i for i, c in enumerate(ALL_CHANNELS)}
    taken: dict[str, set[str]] = {}
    kept: list[PlannedSlot] = []

    candidates = sorted(plan.slots, key=lambda s: (s.date, order.get(s.channel, 99)))
    for slot in candidates:
        if slot.channel not in wanted or not slot.topic.strip():
            continue
        day = slot.date.strip()
        if day not in span:
            continue
        used = taken.setdefault(slot.channel, set())
        if len(used) >= wanted[slot.channel]:
            continue
        if day not in allowed or day in used:
            free = [d for d in days if d not in used]
            if not free:
                continue
            target = date.fromisoformat(day)
            day = min(free, key=lambda d: (abs((date.fromisoformat(d) - target).days), d < day))
        used.add(day)
        kept.append(_clean_slot(slot, day))

    kept.sort(key=lambda s: (s.date, order[s.channel]))
    return ContentPlan(summary=_clip(plan.summary, 600), slots=kept)


def shortfall_notices(plan: ContentPlan, counts: Mapping[str, int]) -> list[str]:
    got: dict[str, int] = {}
    for slot in plan.slots:
        got[slot.channel] = got.get(slot.channel, 0) + 1
    return [f"{CHANNELS[c].label}은(는) {n}편을 요청했지만 {got.get(c, 0)}편만 계획됐어요."  # type: ignore[index]
            for c, n in counts.items() if got.get(c, 0) < n]


# ---------------------------------------------------------------------------
# plan_week
# ---------------------------------------------------------------------------


class WeekPlan(BaseModel):
    """Result of ``plan_week``: the strategy summary, the stored slots and
    Korean notices (caps, shortfalls). Duck-types ``ContentPlan`` (``summary``,
    ``slots``) with saved ``CalendarSlot`` rows."""

    summary: str
    slots: list[CalendarSlot]
    notices: list[str] = Field(default_factory=list)


def plan_week(workspace: Any, backend: "Backend", theme: str, start: str, end: str, counts: Mapping[str, Any], *,
              profile: Profile | None = None, history: list[ContentItem] | None = None) -> WeekPlan:
    """Plan ``start``..``end`` and store the slots in the workspace.

    ``profile`` defaults to ``workspace.get_profile()``; ``history`` to the
    published/approved/scheduled items and recently planned slots. When the
    backend has no usage hook, API usage is recorded to the workspace
    (``task="plan_calendar"``, no run id).
    """
    theme = _clip(theme or "", 300)
    days = slot_days(start, end)
    start_iso, end_iso = parse_day(start).isoformat(), parse_day(end).isoformat()
    capped, notices = cap_counts(normalize_counts(counts), len(days))
    if profile is None:
        profile = workspace.get_profile() if hasattr(workspace, "get_profile") else Profile()
    if history is None:
        history = gather_history(workspace, start_iso, end_iso)

    hooked = getattr(backend, "on_usage", "absent") is None and hasattr(workspace, "record_usage")
    if hooked:
        backend.on_usage = workspace.record_usage  # type: ignore[attr-defined]
    try:
        plan = backend.plan_calendar(profile, theme, start_iso, end_iso, dict(capped), history)
    finally:
        if hooked:
            backend.on_usage = None  # type: ignore[attr-defined]
    plan = normalize_plan(plan, start_iso, end_iso, capped)
    notices += shortfall_notices(plan, capped)
    saved = workspace.add_slots(list(plan.slots)) if plan.slots else []
    if not plan.slots:
        notices.append("계획된 일정이 없어요. 기간이나 채널별 개수를 바꿔 다시 시도해 주세요.")
    return WeekPlan(summary=plan.summary, slots=saved, notices=notices)
