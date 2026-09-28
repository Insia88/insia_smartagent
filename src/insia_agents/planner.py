"""Content calendar planning: company profile + theme + history → calendar slots.

``plan_week`` asks the backend (``Backend.plan_calendar``) for a
``ContentPlan``, normalizes it (dates inside the range on each channel's
posting days, requested channels and counts only, one post per channel per
day, never on a day the channel already has a slot, no topic that repeats the
history) and stores the slots with ``workspace.add_slots``. The helpers here
are shared by both backends so live and mock plans obey the same rules.

Dates are ISO ``YYYY-MM-DD`` strings. Posting days per channel: every day for
the channels in ``weekend_channels`` (e.g. ``["instagram", "naver_blog"]``,
which people also read on weekends), weekdays (Monday–Friday) for the rest.
The default, ``DEFAULT_WEEKEND_CHANNELS``, is empty: weekdays for every
channel, as before per-channel weekends existed — the documented weekly
routine drafts due slots on weekday mornings (``run-due`` cron, Mon–Fri), so a
weekend slot would only be drafted after its date unless that routine runs on
weekends too. A range with no weekday, e.g. a lone weekend, falls back to every
day in it.
"""

from __future__ import annotations

import inspect
import math
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

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

# Channels that may be scheduled on Saturday/Sunday unless the caller says otherwise
# (empty: weekdays only, matching the Mon–Fri run-due routine; see the module docstring).
DEFAULT_WEEKEND_CHANNELS: tuple[ChannelId, ...] = ()
# Slot statuses that occupy their (date, channel): a new plan never adds a second post there.
ACTIVE_SLOT_STATUSES = frozenset({"planned", "generating", "drafted"})

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
    """Weekday posting days: the weekdays in the range, or every day when the
    range has no weekday (the days of a channel that does not post on weekends)."""
    days = date_span(start, end)
    weekdays = [d for d in days if d.weekday() < 5]
    return [d.isoformat() for d in (weekdays or days)]


def _channel_key(value: Any) -> ChannelId:
    key = str(value).strip().lower()
    channel = COUNT_ALIASES.get(key, key)
    if channel not in CHANNELS:
        raise PlanningError(f"알 수 없는 채널이에요: {value!r} (naver_blog, linkedin, instagram, bizplan 중에서 골라 주세요)")
    return channel  # type: ignore[return-value]


def weekend_channel_set(value: Iterable[str] | str | bool | None = None) -> frozenset[ChannelId]:
    """Channels that may post on weekends.

    ``None`` → ``DEFAULT_WEEKEND_CHANNELS``; ``True``/``"all"`` → every
    channel; ``False``/``""``/``"none"`` → none (weekdays only); otherwise
    channel ids or aliases, as a list or a comma-separated string
    (``"blog,instagram"``). Unknown names raise ``PlanningError``.
    """
    if value is None:
        return frozenset(DEFAULT_WEEKEND_CHANNELS)
    if isinstance(value, bool):
        return frozenset(ALL_CHANNELS) if value else frozenset()
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("all", "전체", "모두"):
            return frozenset(ALL_CHANNELS)
        if text in ("", "none", "없음"):
            return frozenset()
        value = [part for part in re.split(r"[,\s]+", text) if part]
    if not isinstance(value, Iterable):
        raise PlanningError("주말 게시 채널은 [\"naver_blog\", \"instagram\"] 같은 목록이나 \"all\", \"none\"으로 적어 주세요.")
    return frozenset(_channel_key(item) for item in value)


def channel_days(start: str | date, end: str | date, channels: Iterable[str],
                 weekend_channels: Iterable[str] | str | bool | None = None) -> dict[ChannelId, list[str]]:
    """Posting days per channel: every day of the range for a weekend channel,
    ``slot_days`` (weekdays) for the others."""
    weekend = weekend_channel_set(weekend_channels)
    every_day = [d.isoformat() for d in date_span(start, end)]
    weekdays = slot_days(start, end)
    return {c: list(every_day if c in weekend else weekdays) for c in (_channel_key(ch) for ch in channels)}


def calendar_days(start: str | date, end: str | date, counts: Mapping[str, Any],
                  days: Mapping[str, Sequence[str]] | None = None) -> tuple[dict[ChannelId, int], dict[ChannelId, list[str]]]:
    """What a backend plans: ``(counts capped to each channel's days, days per channel)``.

    ``days`` gives each channel's posting days (from ``plan_week``: weekend
    rule applied, days already taken removed); a channel it does not list —
    or ``days=None`` — gets ``slot_days`` (weekdays). Days outside the range
    are ignored. Channels left with no day (or a zero count) are dropped.
    """
    wanted = normalize_counts({k: v for k, v in counts.items() if v}) if any(counts.values()) else {}
    span = [d.isoformat() for d in date_span(start, end)]
    inside = set(span)
    given = {_channel_key(k): list(v) for k, v in (days or {}).items()}
    weekdays = slot_days(start, end)
    capped: dict[ChannelId, int] = {}
    by_channel: dict[ChannelId, list[str]] = {}
    for channel, count in wanted.items():
        chosen = {str(d).strip() for d in given[channel]} & inside if channel in given else set(weekdays)
        ordered = [d for d in span if d in chosen]
        if ordered and count > 0:
            capped[channel] = min(count, len(ordered))
            by_channel[channel] = ordered
    return capped, by_channel


def weekday_label(day: str) -> str:
    return WEEKDAYS_KO[date.fromisoformat(day).weekday()]


def normalize_counts(counts: Mapping[str, Any]) -> dict[ChannelId, int]:
    """Validate ``{channel: posts}`` (aliases such as ``blog`` allowed); zero
    counts are dropped; order follows ``ALL_CHANNELS``."""
    if not isinstance(counts, Mapping):
        raise PlanningError("채널별 개수는 {\"naver_blog\": 2, \"linkedin\": 1} 같은 형식이어야 해요.")
    merged: dict[str, int] = {}
    for raw_key, raw_value in counts.items():
        channel = _channel_key(raw_key)
        invalid = PlanningError(f"{CHANNELS[channel].label} 개수는 0 이상의 정수여야 해요.")
        if isinstance(raw_value, bool):
            raise invalid
        try:
            value = int(raw_value)
        except (TypeError, ValueError, OverflowError):  # OverflowError: int(float('inf'))
            raise invalid from None
        # exact checks only: a huge int (10**400) must never go through float(), which overflows
        if isinstance(raw_value, float) and (not math.isfinite(raw_value) or not raw_value.is_integer()):
            raise invalid
        if not isinstance(raw_value, (str, bytes, int, float)) and value != raw_value:  # Decimal("2.5"), Fraction
            raise invalid
        if value < 0:
            raise invalid
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


_HANGUL = re.compile(r"[가-힣]")


def _substantial(key: str) -> bool:
    """Whether a topic key says enough to count as "the same topic" when it
    appears inside a longer one: at least 8 characters, or at least 5 Hangul
    syllables ("엑셀체크리스트" yes; "ai", "마케팅", "재고관리" no)."""
    return len(key) >= 8 or len(_HANGUL.findall(key)) >= 5


def repeats_history(topic: str, keys: set[str]) -> bool:
    """Same topic key, or one key containing the other when the shorter one is
    ``_substantial`` (a short topic such as "AI" is never a repeat of a long
    one just because it appears inside it; "엑셀 체크리스트" is a repeat of
    "재고 관리 엑셀 체크리스트 10가지"). Symmetric."""
    key = topic_key(topic)
    if not key:
        return False
    for k in keys:
        if key == k:
            return True
        shorter, longer = (key, k) if len(key) <= len(k) else (k, key)
        if shorter and _substantial(shorter) and shorter in longer:
            return True
    return False


def history_from_slots(slots: Iterable[CalendarSlot]) -> list[ContentItem]:
    """Planned slots as pseudo items so a second plan does not repeat them."""
    return [ContentItem(id=s.id, run_id=s.run_id, channel=s.channel, title=s.topic, status="scheduled",
                        scheduled_at=s.date, note=s.angle)
            for s in slots if s.status != "skipped" and s.topic.strip()]


def gather_history(workspace: Any, start: str, end: str) -> list[ContentItem]:
    """Published items, approved/scheduled items (up to ``HISTORY_LIMIT`` each)
    and every slot planned from ``HISTORY_LOOKBACK_DAYS`` before ``start`` to
    ``end`` — never cut, so a second plan for the same days always sees them.
    (The live backend picks which rows reach the prompt, upcoming ones first.)"""
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
    return history


def existing_slots(workspace: Any, start: str, end: str) -> list[CalendarSlot]:
    """Slots already on the calendar between ``start`` and ``end`` that still
    occupy their day (planned, generating or drafted — not skipped)."""
    if not hasattr(workspace, "list_slots"):
        return []
    span = {d.isoformat() for d in date_span(start, end)}
    return [s for s in workspace.list_slots(date_from=start, date_to=end)
            if s.status in ACTIVE_SLOT_STATUSES and s.date in span]


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


class FittedPlan(BaseModel):
    """Result of ``fit_plan``: the normalized plan and the slots left out
    because their topic repeats the history (``repeated``) or another slot of
    the same plan (``duplicates``)."""

    plan: ContentPlan
    repeated: list[PlannedSlot] = Field(default_factory=list)
    duplicates: list[PlannedSlot] = Field(default_factory=list)


def fit_plan(plan: ContentPlan, start: str, end: str, counts: Mapping[str, int],
             days: Mapping[str, Sequence[str]] | None = None, *, avoid: Iterable[str] | None = None) -> FittedPlan:
    """Make any backend's plan obey the calendar rules (see ``normalize_plan``).

    With ``avoid`` (topic keys of the history and existing slots) the topic
    rule is applied *while* slots are chosen, not afterwards: a slot whose topic
    repeats ``avoid`` or a slot already chosen (``repeats_history``) never takes
    one of its channel's posts, so a usable spare slot from the backend can
    fill the place instead. ``avoid=None`` skips the topic rule.
    """
    span = {d.isoformat() for d in date_span(start, end)}
    wanted, allowed_by = calendar_days(start, end, counts, days) if counts else ({}, {})
    order = {c: i for i, c in enumerate(ALL_CHANNELS)}
    taken: dict[str, set[str]] = {c: set() for c in wanted}
    kept: list[PlannedSlot] = []
    check_topics = avoid is not None
    past = {k for k in (avoid or ()) if k}
    chosen: set[str] = set()
    repeated: list[PlannedSlot] = []
    duplicates: list[PlannedSlot] = []

    def take(slot: PlannedSlot, day: str) -> None:
        taken[slot.channel].add(day)
        kept.append(_clean_slot(slot, day))
        if key := topic_key(slot.topic):
            chosen.add(key)

    def duplicate(slot: PlannedSlot) -> bool:
        if check_topics and repeats_history(slot.topic, chosen):
            duplicates.append(slot)
            return True
        return False

    candidates = [s for s in sorted(plan.slots, key=lambda s: (s.date, order.get(s.channel, 99)))
                  if s.channel in wanted and s.topic.strip() and s.date.strip() in span]
    movers: list[PlannedSlot] = []
    for slot in candidates:  # pass 1: slots that are fine where they are
        day, used = slot.date.strip(), taken[slot.channel]
        if len(used) >= wanted[slot.channel]:
            continue  # more slots than requested: the rest are simply not used
        if check_topics and repeats_history(slot.topic, past):
            repeated.append(slot)
            continue
        if duplicate(slot):
            continue
        if day in allowed_by[slot.channel] and day not in used:
            take(slot, day)
        else:
            movers.append(slot)
    for slot in movers:  # pass 2: relocate while there is room
        used = taken[slot.channel]
        free = [d for d in allowed_by[slot.channel] if d not in used]
        if len(used) >= wanted[slot.channel] or not free or duplicate(slot):
            continue
        day = slot.date.strip()
        target = date.fromisoformat(day)
        take(slot, min(free, key=lambda d: (abs((date.fromisoformat(d) - target).days), d < day)))

    kept.sort(key=lambda s: (s.date, order[s.channel]))
    return FittedPlan(plan=ContentPlan(summary=_clip(plan.summary, 600), slots=kept), repeated=repeated,
                      duplicates=duplicates)


def normalize_plan(plan: ContentPlan, start: str, end: str, counts: Mapping[str, int],
                   days: Mapping[str, Sequence[str]] | None = None) -> ContentPlan:
    """Make any backend's plan obey the calendar rules.

    ``days`` = posting days per channel (see ``calendar_days``; default: the
    weekdays for every channel). Drops slots with an unknown/unrequested
    channel, an empty topic or a date outside the range; keeps one post per
    channel per day and at most ``counts[channel]`` posts. Slots already on one
    of their channel's free days are kept first (earliest first); then a slot on
    another day (a weekend for a weekday channel, a taken day, a same-day
    duplicate) moves to the nearest free day of its channel while the channel
    still has room. Sorted by date then channel order. Idempotent. (Topics are
    not compared here; ``fit_plan(..., avoid=...)`` adds that.)
    """
    return fit_plan(plan, start, end, counts, days).plan


def shortfall_notices(plan: ContentPlan, counts: Mapping[str, int]) -> list[str]:
    got: dict[str, int] = {}
    for slot in plan.slots:
        got[slot.channel] = got.get(slot.channel, 0) + 1
    return [f"{CHANNELS[c].label}은(는) {n}편을 요청했지만 {got.get(c, 0)}편만 계획됐어요."  # type: ignore[index]
            for c, n in counts.items() if got.get(c, 0) < n]


def plan_capacity(counts: Mapping[str, Any], start: str, end: str, *,
                  weekend_channels: Iterable[str] | str | bool | None = None,
                  taken: Mapping[str, Iterable[str]] | None = None,
                  ) -> tuple[dict[ChannelId, int], dict[ChannelId, list[str]], list[str]]:
    """``(capped counts, free posting days per channel, notices)``.

    A channel's free days are its posting days (``channel_days``: weekends
    only for ``weekend_channels``) minus the days in ``taken[channel]`` (days
    that already hold a slot of that channel). A count above its free days is
    capped, and the notice names the real reason: one post per day, weekends
    left out for that channel, or days already planned.
    """
    wanted = normalize_counts(counts)
    span_len = len(date_span(start, end))
    base = channel_days(start, end, wanted, weekend_channels)
    busy_days = {_channel_key(c): {str(d) for d in days} for c, days in (taken or {}).items()}
    capped: dict[ChannelId, int] = {}
    free: dict[ChannelId, list[str]] = {}
    notices: list[str] = []
    for channel, count in wanted.items():
        free[channel] = [d for d in base[channel] if d not in busy_days.get(channel, set())]
        room = len(free[channel])
        capped[channel] = min(count, room)
        if count <= room:
            continue
        label = CHANNELS[channel].label
        posting = len(base[channel])
        busy = posting - room
        weekday_only = posting < span_len
        where = f"평일 {posting}일" if weekday_only else f"{posting}일"
        if room == 0:
            notices.append(f"{label}은(는) 이 기간에 올릴 수 있는 {where}에 모두 이미 계획이 있어 새로 계획하지 않았어요.")
        elif weekday_only and busy:
            notices.append(f"{label}은(는) 주말을 빼고 평일에 하루 한 편까지인데, 평일 {posting}일 중 {busy}일에는 이미 계획이 있어 "
                           f"{count}편 대신 {room}편만 계획해요.")
        elif weekday_only:
            notices.append(f"{label}은(는) 주말을 빼고 평일에 하루 한 편까지라 이 기간(평일 {posting}일)에는 "
                           f"{count}편 대신 {room}편만 계획해요.")
        elif busy:
            notices.append(f"{label}은(는) 하루 한 편까지인데, 이 기간 {posting}일 중 {busy}일에는 이미 계획이 있어 "
                           f"{count}편 대신 {room}편만 계획해요.")
        else:
            notices.append(f"{label}은(는) 하루 한 편까지라 {count}편 대신 {room}편만 계획해요.")
    return capped, free, notices


def _accepts_days(fn: Any) -> bool:
    """Whether a backend's ``plan_calendar`` takes the ``days`` keyword (older
    or third-party backends may not; their plan is normalized either way)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD or (p.name == "days" and p.kind is not p.VAR_POSITIONAL) for p in params)


def _slot_list(slots: Sequence[PlannedSlot], shown: int = 3) -> str:
    text = ", ".join(f"「{s.topic}」({CHANNELS[s.channel].label} {s.date})" for s in slots[:shown])
    return text + (f" 외 {len(slots) - shown}개" if len(slots) > shown else "")


def _count_by_channel(slots: Iterable[Any]) -> str:
    counts: dict[str, int] = {}
    for slot in slots:
        counts[slot.channel] = counts.get(slot.channel, 0) + 1
    return " · ".join(f"{CHANNELS[c].label} {counts[c]}" for c in ALL_CHANNELS if c in counts)  # type: ignore[index]


# ---------------------------------------------------------------------------
# plan_week
# ---------------------------------------------------------------------------


class WeekPlan(BaseModel):
    """Result of ``plan_week``: the strategy summary, the stored slots and
    Korean notices (caps, days already planned, repeated topics, shortfalls).
    Duck-types ``ContentPlan`` (``summary``, ``slots``) with saved
    ``CalendarSlot`` rows."""

    summary: str
    slots: list[CalendarSlot]
    notices: list[str] = Field(default_factory=list)


def plan_week(workspace: Any, backend: "Backend", theme: str, start: str, end: str, counts: Mapping[str, Any], *,
              profile: Profile | None = None, history: list[ContentItem] | None = None,
              weekend_channels: Iterable[str] | str | bool | None = None) -> WeekPlan:
    """Plan ``start``..``end`` and store the slots in the workspace.

    ``profile`` defaults to ``workspace.get_profile()``; ``history`` to the
    published/approved/scheduled items and recently planned slots.
    ``weekend_channels`` = channels that may post on Saturday/Sunday (see
    ``weekend_channel_set``; default ``DEFAULT_WEEKEND_CHANNELS`` = none).

    Planning the same days again never doubles up: a channel's days that
    already hold one of its slots (planned, generating, drafted) are left out
    (with a notice), and a new slot whose topic repeats the history, an
    existing slot or another new slot is dropped in code (``fit_plan``, with a
    notice per reason) — the live model is told the same, but not trusted with
    it. A repeat is dropped while slots are chosen, so it never takes the place
    of a usable spare slot the backend returned. When the backend has
    no usage hook, API usage is recorded to the workspace
    (``task="plan_calendar"``, no run id).
    """
    theme = _clip(theme or "", 300)
    wanted = normalize_counts(counts)
    start_iso, end_iso = parse_day(start, "시작일").isoformat(), parse_day(end, "종료일").isoformat()
    date_span(start_iso, end_iso)  # validates the range before anything else
    weekend = weekend_channel_set(weekend_channels)

    current = existing_slots(workspace, start_iso, end_iso)
    taken: dict[str, set[str]] = {}
    for slot in current:
        taken.setdefault(slot.channel, set()).add(slot.date)
    capped, free_days, notices = plan_capacity(wanted, start_iso, end_iso, weekend_channels=weekend, taken=taken)
    active = {c: n for c, n in capped.items() if n > 0}
    if not active:  # every requested channel is full: no backend call (and no cost)
        notices.append("새로 계획할 수 있는 날이 없어요. 기간을 바꾸거나 기존 계획을 건너뛴 뒤 다시 시도해 주세요.")
        return WeekPlan(summary="", slots=[], notices=notices)
    overlapping = [s for s in current if s.channel in active]
    if overlapping:
        notices.append(f"이 기간에 이미 계획된 일정 {len(overlapping)}개({_count_by_channel(overlapping)})가 있어, "
                       "같은 채널은 그날을 비워 두고 계획했어요.")

    if profile is None:
        profile = workspace.get_profile() if hasattr(workspace, "get_profile") else Profile()
    if history is None:
        history = gather_history(workspace, start_iso, end_iso)
    known = {item.id for item in history}
    history = [*history, *(item for item in history_from_slots(current) if item.id not in known)]
    days = {c: free_days[c] for c in active}

    hooked = getattr(backend, "on_usage", "absent") is None and hasattr(workspace, "record_usage")
    if hooked:
        backend.on_usage = workspace.record_usage  # type: ignore[attr-defined]
    try:
        if _accepts_days(backend.plan_calendar):
            plan = backend.plan_calendar(profile, theme, start_iso, end_iso, dict(active), history, days=days)
        else:
            plan = backend.plan_calendar(profile, theme, start_iso, end_iso, dict(active), history)
    finally:
        if hooked:
            backend.on_usage = None  # type: ignore[attr-defined]
    fitted = fit_plan(plan, start_iso, end_iso, active, days=days, avoid=history_keys(history))
    plan = fitted.plan
    if fitted.repeated:
        notices.append(f"지난 게시물·기존 계획과 주제가 겹치는 슬롯 {len(fitted.repeated)}개는 넣지 않았어요: "
                       f"{_slot_list(fitted.repeated)}")
    if fitted.duplicates:
        notices.append(f"새 계획 안에서 다른 슬롯과 주제가 겹치는 슬롯 {len(fitted.duplicates)}개는 넣지 않았어요: "
                       f"{_slot_list(fitted.duplicates)}")
    notices += shortfall_notices(plan, active)
    saved = workspace.add_slots(list(plan.slots)) if plan.slots else []
    if not plan.slots:
        notices.append("계획된 일정이 없어요. 기간이나 채널별 개수를 바꿔 다시 시도해 주세요.")
    return WeekPlan(summary=plan.summary, slots=saved, notices=notices)
