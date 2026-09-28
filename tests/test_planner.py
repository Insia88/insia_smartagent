from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import date

import pytest

from fakes import FakeClient, json_text, message
from insia_agents.backends.anthropic_backend import AnthropicBackend
from insia_agents.backends.mock_backend import MockBackend, template_calendar
from insia_agents.db import Workspace
from insia_agents.models import CalendarSlot, ContentItem, ContentPlan, PlannedSlot, Profile, UsageRecord
from insia_agents.planner import (
    PlanningError,
    WeekPlan,
    cap_counts,
    normalize_counts,
    normalize_plan,
    plan_week,
    slot_days,
    spread,
    topic_key,
)

PROFILE = Profile(service_name="INSIA", one_liner="1인 창업자를 위한 AI 콘텐츠 비서", target_customers="1인 창업자",
                  industry="마케팅 SaaS", differentiators=["출처 기반 작성"], problem="콘텐츠 제작 시간이 부족함")
WEEK = ("2026-10-05", "2026-10-11")  # Monday .. Sunday


# ---------------------------------------------------------------------------
# Dates and counts
# ---------------------------------------------------------------------------


def test_slot_days_are_weekdays_or_every_day_of_a_weekend_range():
    assert slot_days(*WEEK) == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    assert slot_days("2026-10-10", "2026-10-11") == ["2026-10-10", "2026-10-11"]
    assert slot_days(date(2026, 10, 7), date(2026, 10, 7)) == ["2026-10-07"]


@pytest.mark.parametrize("start, end, fragment", [
    ("2026-10-05", "2026-10-01", "시작일"),
    ("10/05/2026", "2026-10-11", "YYYY-MM-DD"),
    ("2026-10-01", "2026-11-15", "최대 31일"),
])
def test_bad_ranges_have_korean_errors(start, end, fragment):
    with pytest.raises(PlanningError, match=fragment):
        slot_days(start, end)


def test_normalize_counts_aliases_and_validation():
    assert normalize_counts({"blog": 2, "linkedin": "1", "instagram": 0}) == {"naver_blog": 2, "linkedin": 1}
    assert list(normalize_counts({"instagram": 1, "naver_blog": 1})) == ["naver_blog", "instagram"]
    for bad in ({"tiktok": 1}, {"linkedin": -1}, {"linkedin": 1.5}, {"linkedin": True}, {"linkedin": 0}, {}, ["linkedin"]):
        with pytest.raises(PlanningError):
            normalize_counts(bad)  # type: ignore[arg-type]


def test_normalize_counts_handles_huge_and_non_finite_numbers_without_crashing():
    from decimal import Decimal
    from fractions import Fraction

    from insia_agents.planner import MAX_PER_CHANNEL

    # whole numbers, however large, are clamped (never converted through float(), which overflows for 10**400)
    assert normalize_counts({"linkedin": 10**400}) == {"linkedin": MAX_PER_CHANNEL}
    assert normalize_counts({"linkedin": 1e308}) == {"linkedin": MAX_PER_CHANNEL}
    assert normalize_counts({"linkedin": 3.0, "blog": Decimal("2"), "instagram": Fraction(4, 2)}) == \
        {"naver_blog": 2, "linkedin": 3, "instagram": 2}
    for bad in (float("inf"), float("-inf"), float("nan"), 2.5, -(10**400), Decimal("2.5"), Fraction(5, 2), "1e3", "2.5"):
        with pytest.raises(PlanningError, match="0 이상의 정수"):
            normalize_counts({"linkedin": bad})


def test_cap_counts_explains_every_cap():
    capped, notices = cap_counts({"naver_blog": 7, "linkedin": 2}, 5)
    assert capped == {"naver_blog": 5, "linkedin": 2}
    assert notices == ["네이버 블로그은(는) 하루 한 편까지라 7편 대신 5편만 계획해요."]


def test_spread_is_even_distinct_and_rotatable():
    days = slot_days(*WEEK)
    assert spread(2, days) == ["2026-10-06", "2026-10-08"]
    assert spread(5, days) == days and spread(9, days) == days
    assert len(set(spread(3, days, offset=1))) == 3 and spread(0, days) == []


# ---------------------------------------------------------------------------
# normalize_plan
# ---------------------------------------------------------------------------


def _slot(day, channel="naver_blog", topic="주제", **extra):
    return PlannedSlot(date=day, channel=channel, topic=topic, angle=extra.get("angle", "체크리스트"),
                       keywords=extra.get("keywords", ["키워드"]), goal=extra.get("goal", ""))


def test_normalize_plan_enforces_the_calendar_rules():
    raw = ContentPlan(summary="  전략  ", slots=[
        _slot("2026-10-06", topic="  첫   글 "),
        _slot("2026-10-06", topic="같은 날 같은 채널"),     # moved to the nearest free weekday
        _slot("2026-10-10", topic="토요일"),                 # would move, but the channel is full by then
        _slot("2026-10-20", topic="기간 밖"),
        _slot("2026-10-07", channel="instagram", topic="요청 안 한 채널"),
        _slot("2026-10-08", channel="linkedin", topic="   "),
        _slot("2026-10-09", channel="linkedin", topic="링크드인", keywords=["a", " a ", "", "b", "c", "d", "e", "f"]),
        _slot("2026-10-09", topic="금요일 글"),              # valid slots are kept before any relocation
        _slot("2026-10-11", channel="linkedin", topic="일요일 초과"),
    ])
    plan = normalize_plan(raw, *WEEK, {"naver_blog": 3, "linkedin": 1})
    assert [(s.date, s.channel, s.topic) for s in plan.slots] == [
        ("2026-10-06", "naver_blog", "첫 글"), ("2026-10-07", "naver_blog", "같은 날 같은 채널"),
        ("2026-10-09", "naver_blog", "금요일 글"), ("2026-10-09", "linkedin", "링크드인")]
    weekend_only = normalize_plan(ContentPlan(summary="s", slots=[_slot("2026-10-10", topic="토요일")]), *WEEK, {"naver_blog": 1})
    assert [(s.date, s.topic) for s in weekend_only.slots] == [("2026-10-09", "토요일")]  # Friday is nearest
    assert plan.slots[-1].keywords == ["a", "b", "c", "d", "e"]
    assert plan.slots[0].goal == "검색 유입과 문의"  # empty goal → channel default
    assert plan.summary == "전략"
    assert normalize_plan(plan, *WEEK, {"naver_blog": 3, "linkedin": 1}) == plan  # idempotent


# ---------------------------------------------------------------------------
# Mock plan_calendar
# ---------------------------------------------------------------------------


def test_mock_calendar_is_deterministic_and_exact(settings):
    counts = {"naver_blog": 2, "linkedin": 2, "instagram": 1}
    backend = MockBackend(settings)
    records: list[UsageRecord] = []
    backend.on_usage = records.append
    first = backend.plan_calendar(PROFILE, "AI 마케팅 자동화", *WEEK, counts, [])
    second = backend.plan_calendar(PROFILE, "AI 마케팅 자동화", *WEEK, counts, [])
    assert first == second
    per_channel = {c: [s for s in first.slots if s.channel == c] for c in counts}
    assert {c: len(v) for c, v in per_channel.items()} == counts
    weekdays = set(slot_days(*WEEK))
    assert all(s.date in weekdays for s in first.slots)
    assert all(len({s.date for s in v}) == len(v) for v in per_channel.values())
    assert len({topic_key(s.topic) for s in first.slots}) == len(first.slots)
    assert all(s.keywords and s.keywords[0] == "AI 마케팅 자동화" and s.goal for s in first.slots)
    assert first.summary.startswith("[데모]")
    assert [(r.task, r.model, r.cost_usd) for r in records] == [("plan_calendar", "mock", 0.0)] * 2


def test_mock_calendar_avoids_history(settings):
    counts = {"naver_blog": 3}
    fresh = template_calendar(PROFILE, "AI 마케팅 자동화", *WEEK, counts, [])
    history = [ContentItem(id=f"it_{i}", channel="naver_blog", title=f"[데모] {s.topic}", status="published")
               for i, s in enumerate(fresh.slots)]
    again = template_calendar(PROFILE, "AI 마케팅 자동화", *WEEK, counts, history)
    assert len(again.slots) == 3
    assert not ({topic_key(s.topic) for s in again.slots} & {topic_key(s.topic) for s in fresh.slots})


def test_mock_calendar_without_profile_or_theme_still_plans(settings):
    plan = MockBackend(settings).plan_calendar(Profile(), "", "2026-10-10", "2026-10-11", {"instagram": 2}, [])
    assert [s.date for s in plan.slots] == ["2026-10-10", "2026-10-11"]
    assert all(s.topic.strip() for s in plan.slots)


# ---------------------------------------------------------------------------
# plan_week
# ---------------------------------------------------------------------------


class FakeWorkspace:
    def __init__(self) -> None:
        self.saved: list[PlannedSlot] = []
        self.usage: list[UsageRecord] = []
        self.published = [ContentItem(id="it_p", channel="naver_blog", title="이미 게시한 글", status="published")]
        self.slots = [CalendarSlot(id="sl_old", date="2026-09-30", channel="linkedin", topic="지난주 계획", status="planned"),
                      CalendarSlot(id="sl_skip", date="2026-09-30", channel="linkedin", topic="건너뛴 계획", status="skipped")]
        self.calls: list[tuple] = []

    def get_profile(self) -> Profile:
        return PROFILE

    def published_history(self, channel=None, limit=50):
        return list(self.published)

    def list_items(self, *, status=None, channel=None, limit=200):
        self.calls.append(("list_items", status))
        return []

    def list_slots(self, *, date_from=None, date_to=None):
        self.calls.append(("list_slots", date_from, date_to))
        return list(self.slots)

    def add_slots(self, slots):
        self.saved.extend(slots)
        return [CalendarSlot(id=f"sl_{i}", **s.model_dump()) for i, s in enumerate(slots)]

    def record_usage(self, record):
        self.usage.append(record)


class SpyBackend(MockBackend):
    def plan_calendar(self, profile, theme, start, end, counts, history):
        self.seen = (profile, theme, start, end, counts, [h.title for h in history])
        return super().plan_calendar(profile, theme, start, end, counts, history)


def test_plan_week_stores_slots_and_explains_caps(settings):
    workspace = FakeWorkspace()
    backend = SpyBackend(settings)
    result = plan_week(workspace, backend, "  AI 마케팅 자동화 ", "2026-10-05", "2026-10-07", {"blog": 5, "linkedin": 1})
    assert isinstance(result, WeekPlan)
    profile, theme, start, end, counts, history = backend.seen
    assert profile == PROFILE and theme == "AI 마케팅 자동화" and (start, end) == ("2026-10-05", "2026-10-07")
    assert counts == {"naver_blog": 3, "linkedin": 1}
    assert history == ["이미 게시한 글", "지난주 계획"]  # skipped slots are not history
    assert ("list_slots", "2026-08-10", "2026-10-07") in workspace.calls
    assert {("list_items", "scheduled"), ("list_items", "approved")} <= set(workspace.calls)
    assert [s.id for s in result.slots] == ["sl_0", "sl_1", "sl_2", "sl_3"] and len(workspace.saved) == 4
    assert result.notices == ["네이버 블로그은(는) 하루 한 편까지라 5편 대신 3편만 계획해요."]
    assert result.summary.startswith("[데모]")
    # usage went to the workspace while planning, and the hook was removed afterwards
    assert [r.task for r in workspace.usage] == ["plan_calendar"] and backend.on_usage is None


def test_plan_week_keeps_an_existing_usage_hook(settings):
    workspace = FakeWorkspace()
    backend = MockBackend(settings)
    mine: list[UsageRecord] = []
    backend.on_usage = mine.append
    plan_week(workspace, backend, "주제", *WEEK, {"instagram": 1}, profile=Profile(), history=[])
    assert len(mine) == 1 and workspace.usage == [] and backend.on_usage == mine.append


def test_plan_week_rejects_bad_input_before_calling_the_backend(settings):
    class Exploding(MockBackend):
        def plan_calendar(self, *args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("backend called")

    for start, end, counts in (("2026-10-07", "2026-10-05", {"blog": 1}), (*WEEK, {"blog": 0}), (*WEEK, {"x": 1})):
        with pytest.raises(PlanningError):
            plan_week(FakeWorkspace(), Exploding(settings), "주제", start, end, counts)


def test_plan_week_with_a_live_backend_and_structured_output(settings, prompts_dir):
    (prompts_dir / "agents" / "planner.md").write_text("# planner", encoding="utf-8")
    raw = ContentPlan(summary="이번 주는 검색 유입 중심", slots=[
        _slot("2026-10-05", topic="블로그 A"), _slot("2026-10-06", channel="linkedin", topic="링크드인 A"),
        _slot("2026-10-11", topic="일요일 블로그"),
    ])
    client = FakeClient([message([json_text(raw)])])
    backend = AnthropicBackend(settings, client=client)
    workspace = FakeWorkspace()
    result = plan_week(workspace, backend, "AI 마케팅", *WEEK, {"naver_blog": 3, "linkedin": 1})
    assert [(s.date, s.channel, s.topic) for s in result.slots] == [
        ("2026-10-05", "naver_blog", "블로그 A"), ("2026-10-06", "linkedin", "링크드인 A"), ("2026-10-09", "naver_blog", "일요일 블로그")]
    assert result.notices == ["네이버 블로그은(는) 3편을 요청했지만 2편만 계획됐어요."]
    body = client.calls[0]["messages"][0]["content"][-1]["text"]
    payload = json.loads(body.split("```json\n", 1)[1].rsplit("```", 1)[0])
    assert payload["channel_days"] == {"naver_blog": slot_days(*WEEK), "linkedin": slot_days(*WEEK)}
    assert [r.task for r in workspace.usage] == ["plan_calendar"] and workspace.usage[0].model == "claude-opus-5"
    assert "이미 게시한 글" in client.calls[0]["messages"][0]["content"][-1]["text"]


def test_plan_week_with_the_real_workspace(settings, tmp_path):
    db = pytest.importorskip("insia_agents.db")
    workspace = db.Workspace(tmp_path / "ws")
    workspace.save_profile(PROFILE)
    result = plan_week(workspace, MockBackend(replace(settings, home=tmp_path / "ws")), "AI 마케팅 자동화", *WEEK,
                       {"naver_blog": 2, "linkedin": 1})
    stored = workspace.list_slots(date_from=WEEK[0], date_to=WEEK[1])
    assert [s.id for s in stored] == [s.id for s in result.slots] and len(stored) == 3
    assert all(s.status == "planned" and s.topic for s in stored)
    # planning the same week again does not repeat the topics already planned
    again = plan_week(workspace, MockBackend(settings), "AI 마케팅 자동화", *WEEK, {"naver_blog": 2, "linkedin": 1})
    assert not ({topic_key(s.topic) for s in again.slots} & {topic_key(s.topic) for s in result.slots})
    assert workspace.usage_summary()["by_task"]["plan_calendar"]["usd"] == 0.0


# ---------------------------------------------------------------------------
# Weekends per channel (finding 20)
# ---------------------------------------------------------------------------

from collections import Counter  # noqa: E402

from insia_agents.models import ALL_CHANNELS  # noqa: E402
from insia_agents.planner import (  # noqa: E402
    DEFAULT_WEEKEND_CHANNELS,
    calendar_days,
    channel_days,
    fit_plan,
    plan_capacity,
    repeats_history,
    weekend_channel_set,
)


def test_weekend_channel_set_parsing():
    assert weekend_channel_set(None) == set(DEFAULT_WEEKEND_CHANNELS) == set()  # weekdays only, as before
    assert weekend_channel_set("blog, ig") == {"naver_blog", "instagram"}
    assert weekend_channel_set(["linkedin"]) == {"linkedin"}
    assert weekend_channel_set("all") == weekend_channel_set(True) == set(ALL_CHANNELS)
    assert weekend_channel_set("none") == weekend_channel_set("") == weekend_channel_set(False) == set()
    with pytest.raises(PlanningError, match="알 수 없는 채널"):
        weekend_channel_set(["tiktok"])


def test_channel_days_follow_the_weekend_rule():
    days = channel_days(*WEEK, ["naver_blog", "linkedin"], weekend_channels=["naver_blog"])
    assert days["naver_blog"][-2:] == ["2026-10-10", "2026-10-11"] and len(days["naver_blog"]) == 7
    assert days["linkedin"] == slot_days(*WEEK)
    lone_weekend = channel_days("2026-10-10", "2026-10-11", ["linkedin"], weekend_channels=())
    assert lone_weekend["linkedin"] == ["2026-10-10", "2026-10-11"]  # a range with no weekday: every day


def test_a_weekday_cap_names_the_weekend_exclusion(settings):
    """The review's repro: instagram 7 over Mon..Sun used to blame the one-post-per-day rule."""
    week = plan_week(FakeWorkspace(), MockBackend(settings), "AI 마케팅", *WEEK, {"instagram": 7, "linkedin": 2},
                     profile=PROFILE, history=[])
    assert Counter(s.channel for s in week.slots) == {"instagram": 5, "linkedin": 2}
    assert all(date.fromisoformat(s.date).weekday() < 5 for s in week.slots)
    # was: '인스타그램은(는) 하루 한 편까지라 7편 대신 5편만 계획해요.'
    assert week.notices == ["인스타그램은(는) 주말을 빼고 평일에 하루 한 편까지라 이 기간(평일 5일)에는 7편 대신 5편만 계획해요."]


def test_weekends_are_allowed_per_channel(settings):
    week = plan_week(FakeWorkspace(), MockBackend(settings), "AI 마케팅", *WEEK, {"instagram": 7, "linkedin": 7},
                     profile=PROFILE, history=[], weekend_channels=["instagram", "blog"])
    assert Counter(s.channel for s in week.slots) == {"instagram": 7, "linkedin": 5}
    assert {"2026-10-10", "2026-10-11"} <= {s.date for s in week.slots if s.channel == "instagram"}
    assert not {"2026-10-10", "2026-10-11"} & {s.date for s in week.slots if s.channel == "linkedin"}
    assert week.notices == ["링크드인은(는) 주말을 빼고 평일에 하루 한 편까지라 이 기간(평일 5일)에는 7편 대신 5편만 계획해요."]
    assert "주말 포함" in week.summary
    everyone = plan_week(FakeWorkspace(), MockBackend(settings), "AI 마케팅", *WEEK, {"linkedin": 7},
                         profile=PROFILE, history=[], weekend_channels="all")
    assert len(everyone.slots) == 7 and everyone.notices == []
    over = plan_week(FakeWorkspace(), MockBackend(settings), "AI 마케팅", *WEEK, {"blog": 9}, profile=PROFILE,
                     history=[], weekend_channels="blog")
    assert over.notices == ["네이버 블로그은(는) 하루 한 편까지라 9편 대신 7편만 계획해요."]


def test_normalize_plan_with_per_channel_days_is_idempotent():
    days = {"naver_blog": ["2026-10-05", "2026-10-10"], "linkedin": ["2026-10-06"]}
    raw = ContentPlan(summary="s", slots=[_slot("2026-10-10", topic="토요일 블로그"), _slot("2026-10-10", channel="linkedin", topic="토요일 링크드인"),
                                          _slot("2026-10-11", topic="일요일 블로그")])
    plan = normalize_plan(raw, *WEEK, {"naver_blog": 2, "linkedin": 1}, days=days)
    # Saturday is a blog day here, so that slot stays; Sunday is not, so it moves to the blog's other day;
    # LinkedIn has only Tuesday
    assert [(s.date, s.channel, s.topic) for s in plan.slots] == [
        ("2026-10-05", "naver_blog", "일요일 블로그"), ("2026-10-06", "linkedin", "토요일 링크드인"),
        ("2026-10-10", "naver_blog", "토요일 블로그")]
    assert normalize_plan(plan, *WEEK, {"naver_blog": 2, "linkedin": 1}, days=days) == plan
    capped, by_channel = calendar_days(*WEEK, {"naver_blog": 5, "linkedin": 1}, days)
    assert capped == {"naver_blog": 2, "linkedin": 1} and by_channel == days


# ---------------------------------------------------------------------------
# Re-planning the same days (finding 21) and repeated topics (finding 29)
# ---------------------------------------------------------------------------


def _real_workspace(tmp_path):
    db = pytest.importorskip("insia_agents.db")
    return db.Workspace(tmp_path / "ws")


def test_replanning_a_week_never_doubles_a_channel_on_a_day(settings, tmp_path):
    workspace = _real_workspace(tmp_path)
    try:
        backend = MockBackend(settings)
        plan_week(workspace, backend, "AI 마케팅", *WEEK, {"instagram": 7, "linkedin": 2}, weekend_channels=["instagram"])
        again = plan_week(workspace, backend, "AI 마케팅", *WEEK, {"linkedin": 2})
        stored = workspace.list_slots(date_from=WEEK[0], date_to=WEEK[1])
        dup = {k: v for k, v in Counter((s.date, s.channel) for s in stored).items() if v > 1}
        assert dup == {}  # was {('2026-10-05', 'linkedin'): 3, ('2026-10-08', 'linkedin'): 2}
        assert len(again.slots) == 2
        assert "이 기간에 이미 계획된 일정 2개(링크드인 2)가 있어, 같은 채널은 그날을 비워 두고 계획했어요." in again.notices
        # a third plan asks for more LinkedIn posts than free weekdays: the notice says why
        third = plan_week(workspace, backend, "AI 마케팅", *WEEK, {"linkedin": 3})
        assert len(third.slots) == 1
        assert third.notices[0] == ("링크드인은(는) 주말을 빼고 평일에 하루 한 편까지인데, 평일 5일 중 4일에는 이미 계획이 있어 "
                                    "3편 대신 1편만 계획해요.")
        # nothing left: no backend call, a clear notice
        calls: list[UsageRecord] = []
        backend.on_usage = calls.append
        full = plan_week(workspace, backend, "AI 마케팅", *WEEK, {"linkedin": 1, "instagram": 1},
                         weekend_channels=["instagram"])
        assert full.slots == [] and calls == [] and full.notices == [
            "링크드인은(는) 이 기간에 올릴 수 있는 평일 5일에 모두 이미 계획이 있어 새로 계획하지 않았어요.",
            "인스타그램은(는) 이 기간에 올릴 수 있는 7일에 모두 이미 계획이 있어 새로 계획하지 않았어요.",
            "새로 계획할 수 있는 날이 없어요. 기간을 바꾸거나 기존 계획을 건너뛴 뒤 다시 시도해 주세요."]
        backend.on_usage = None
        # a skipped slot frees its day again
        victim = next(s for s in stored if s.channel == "linkedin")
        workspace.update_slot(victim.id, status="skipped")
        freed = plan_week(workspace, backend, "AI 마케팅", *WEEK, {"linkedin": 1})
        assert [s.date for s in freed.slots] == [victim.date]
    finally:
        workspace.close()


def test_plan_capacity_counts_drafted_and_generating_slots_as_taken(settings):
    capped, free, notices = plan_capacity({"linkedin": 5}, *WEEK, taken={"linkedin": ["2026-10-05", "2026-10-06"]})
    assert capped == {"linkedin": 3} and free["linkedin"] == ["2026-10-07", "2026-10-08", "2026-10-09"]
    assert "이미 계획이 있어" in notices[0]

    class Busy(FakeWorkspace):
        def list_slots(self, *, date_from=None, date_to=None):
            return [CalendarSlot(id=f"sl_{st}", date=day, channel="linkedin", topic=f"기존 {st}", status=st)
                    for st, day in (("drafted", "2026-10-05"), ("generating", "2026-10-06"), ("skipped", "2026-10-07"))]

    week = plan_week(Busy(), MockBackend(settings), "주제", *WEEK, {"linkedin": 5})
    assert sorted(s.date for s in week.slots) == ["2026-10-07", "2026-10-08", "2026-10-09"]


def test_repeats_history_ignores_short_topics_inside_long_ones():
    keys = {topic_key("AI 마케팅 자동화 체크리스트 10가지")}
    assert not repeats_history("A", keys) and not repeats_history("AI", keys) and not repeats_history("마케팅", keys)
    assert repeats_history("AI 마케팅 자동화 체크리스트", keys)  # the long topic contains this 8+ character one
    assert repeats_history("ai 마케팅 자동화 체크리스트 10가지!", keys)


def test_repeats_history_still_catches_short_korean_topics():
    """Follow-up: requiring 8+ characters on both sides let 5–7 syllable Korean topics through
    (HEAD caught them). Five Hangul syllables are enough to name a topic."""
    keys = {topic_key("재고 관리 엑셀 체크리스트 10가지")}
    assert repeats_history("엑셀 체크리스트", keys)  # 7 syllables (was False)
    assert repeats_history("재고 관리 엑셀", keys)  # 6 syllables (was False)
    assert not repeats_history("재고 관리", keys)  # 4 syllables: a broad subject, not the same post
    # symmetric: a short history title inside a longer new topic
    assert repeats_history("카페 사장님을 위한 재고 관리 엑셀 체크리스트", {topic_key("엑셀 체크리스트")})
    assert not repeats_history("AI 마케팅 자동화 가이드", {topic_key("AI")})


def test_fit_plan_drops_history_repeats_and_in_plan_duplicates_separately():
    plan = ContentPlan(summary="s", slots=[_slot("2026-10-05", topic="재고 관리 엑셀 체크리스트 10가지"),
                                           _slot("2026-10-06", channel="linkedin", topic="완전히 새 주제"),
                                           _slot("2026-10-07", channel="instagram", topic="완전히 새 주제!")])
    fitted = fit_plan(plan, *WEEK, {"naver_blog": 1, "linkedin": 1, "instagram": 1},
                      avoid={topic_key("재고 관리 엑셀 체크리스트 10가지")})
    assert [s.topic for s in fitted.plan.slots] == ["완전히 새 주제"]
    assert [s.date for s in fitted.repeated] == ["2026-10-05"] and [s.date for s in fitted.duplicates] == ["2026-10-07"]
    # without ``avoid`` topics are not compared (normalize_plan's behavior)
    assert len(fit_plan(plan, *WEEK, {"naver_blog": 1, "linkedin": 1, "instagram": 1}).plan.slots) == 3


def test_a_repeat_never_takes_the_place_of_a_usable_spare_slot():
    """Verifier's repro (v29_order.py): the backend returns more slots than requested and the first
    one repeats history. Deduping after the count cap used to throw the spare away and plan 0 posts."""
    plan = ContentPlan(summary="s", slots=[_slot("2026-10-05", channel="linkedin", topic="1인 창업자의 콘텐츠 루틴 만들기"),
                                           _slot("2026-10-07", channel="linkedin", topic="고객 인터뷰 질문 12개")])
    fitted = fit_plan(plan, *WEEK, {"linkedin": 1}, avoid={topic_key("1인 창업자의 콘텐츠 루틴 만들기")})
    assert [(s.date, s.topic) for s in fitted.plan.slots] == [("2026-10-07", "고객 인터뷰 질문 12개")]
    assert [s.topic for s in fitted.repeated] == ["1인 창업자의 콘텐츠 루틴 만들기"]
    # a duplicate inside the plan does not take the place either (it would have been the second post)
    twins = ContentPlan(summary="s", slots=[_slot("2026-10-05", topic="블로그 주제 하나 입니다"),
                                            _slot("2026-10-06", topic="블로그 주제 하나 입니다!"),
                                            _slot("2026-10-08", topic="전혀 다른 블로그 주제")])
    fitted = fit_plan(twins, *WEEK, {"naver_blog": 2}, avoid=set())
    assert [s.date for s in fitted.plan.slots] == ["2026-10-05", "2026-10-08"] and len(fitted.duplicates) == 1
    # idempotent with the same avoid set
    again = fit_plan(fitted.plan, *WEEK, {"naver_blog": 2}, avoid=set())
    assert again.plan == fitted.plan and not again.repeated and not again.duplicates


def test_plan_week_keeps_the_spare_slot_and_words_each_notice_by_its_reason(settings, tmp_path):
    workspace = _real_workspace(tmp_path)
    try:
        workspace.create_item("linkedin", "1인 창업자의 콘텐츠 루틴 만들기", status="published")

        class Over(MockBackend):
            def plan_calendar(self, profile, theme, start, end, counts, history, *, days=None):
                return ContentPlan(summary="s", slots=[
                    _slot("2026-10-05", channel="linkedin", topic="1인 창업자의 콘텐츠 루틴 만들기"),
                    _slot("2026-10-06", channel="linkedin", topic="고객 인터뷰 질문 12개"),
                    _slot("2026-10-07", channel="linkedin", topic="고객 인터뷰 질문 12개!"),
                    _slot("2026-10-08", channel="linkedin", topic="가격표를 한 장으로 정리하는 법")])

        week = plan_week(workspace, Over(settings), "t", *WEEK, {"linkedin": 2})
        assert [(s.date, s.topic) for s in week.slots] == [("2026-10-06", "고객 인터뷰 질문 12개"),
                                                            ("2026-10-08", "가격표를 한 장으로 정리하는 법")]
        assert week.notices == [
            "지난 게시물·기존 계획과 주제가 겹치는 슬롯 1개는 넣지 않았어요: 「1인 창업자의 콘텐츠 루틴 만들기」(링크드인 2026-10-05)",
            "새 계획 안에서 다른 슬롯과 주제가 겹치는 슬롯 1개는 넣지 않았어요: 「고객 인터뷰 질문 12개!」(링크드인 2026-10-07)"]
    finally:
        workspace.close()


def test_live_planner_never_saves_a_topic_that_is_already_planned(settings, prompts_dir, tmp_path):
    """The review's repro: 61 published + 10 approved items push the planned slot out of the
    60-row prompt, and the model returns the same topic again."""
    (prompts_dir / "agents" / "planner.md").write_text("# planner", encoding="utf-8")
    workspace = _real_workspace(tmp_path)
    try:
        for i in range(10):
            workspace.create_item("instagram", f"예약된 인스타 {i}", status="approved")
        for i in range(61):
            workspace.create_item("linkedin", f"지난 링크드인 글 {i}", status="published")
        topic = "재고 관리 엑셀 체크리스트 10가지"
        workspace.add_slots([PlannedSlot(date="2026-10-06", channel="naver_blog", topic=topic, angle="체크리스트",
                                         keywords=["재고 관리"], goal="검색")])
        reply = ContentPlan(summary="s", slots=[
            PlannedSlot(date="2026-10-07", channel="naver_blog", topic=topic, angle="체크리스트", keywords=["재고 관리"], goal="검색"),
            PlannedSlot(date="2026-10-08", channel="naver_blog", topic="재고 회전율 쉽게 계산하는 법", angle="방법",
                        keywords=["재고 회전율"], goal="검색")])
        client = FakeClient([message([json_text(reply)])])
        week = plan_week(workspace, AnthropicBackend(settings, client=client), "재고", "2026-10-05", "2026-10-09",
                         {"naver_blog": 2})
        body = client.calls[0]["messages"][0]["content"][-1]["text"]
        payload = json.loads(body.split("```json\n", 1)[1].rsplit("```", 1)[0])
        assert len(payload["history"]) == 60 and any(r["title"] == topic for r in payload["history"])  # was False
        saved = sorted((s.date, s.topic) for s in workspace.list_slots())
        assert saved == [("2026-10-06", topic), ("2026-10-08", "재고 회전율 쉽게 계산하는 법")]  # no second copy
        assert any("주제가 겹치는 슬롯 1개" in n and topic in n for n in week.notices)
        assert "네이버 블로그은(는) 2편을 요청했지만 1편만 계획됐어요." in week.notices
    finally:
        workspace.close()


def test_plan_week_works_with_a_backend_that_does_not_take_days(settings):
    class OldBackend(MockBackend):
        def plan_calendar(self, profile, theme, start, end, counts, history):  # the pre-weekend signature
            return ContentPlan(summary="s", slots=[_slot("2026-10-11", channel="linkedin", topic="일요일 링크드인"),
                                                   _slot("2026-10-11", channel="instagram", topic="일요일 인스타")])

    week = plan_week(FakeWorkspace(), OldBackend(settings), "주제", *WEEK, {"linkedin": 1, "instagram": 1},
                     profile=PROFILE, history=[], weekend_channels=["instagram"])
    assert {(s.date, s.channel) for s in week.slots} == {("2026-10-09", "linkedin"), ("2026-10-11", "instagram")}


def test_mock_calendar_filler_topics_never_loop_forever(settings, tmp_path):
    """template_calendar spun forever once it needed a 10th filler topic ("…인사이트 1" is inside "…인사이트 10"):
    a mock POST /api/calendar/plan then held a server thread and a planning place for good (4 of them → 429)."""
    ws = Workspace(tmp_path / "ws")
    backend = MockBackend(replace(settings, home=tmp_path / "ws"))
    results: list[int] = []

    def plan_all() -> None:
        results.append(len(plan_week(ws, backend, "주말 테스트", "2026-10-05", "2026-10-11",
                                     {"instagram": 7, "linkedin": 7, "naver_blog": 7}, weekend_channels="all").slots))
        for start, end in (("2026-10-12", "2026-10-16"), ("2026-10-19", "2026-10-23"), ("2026-10-26", "2026-10-30"),
                           ("2026-11-02", "2026-11-06")):  # the same theme week after week
            results.append(len(plan_week(ws, backend, "주간 테마", start, end,
                                         {"instagram": 5, "linkedin": 5, "naver_blog": 5}).slots))
        # a past post titled like the theme is inside every filler topic: still returns
        history = [ContentItem(id="it_old", run_id="r", channel="linkedin", title="주간 테마 인사이트")]
        results.append(len(plan_week(ws, backend, "주간 테마", "2026-11-09", "2026-11-13",
                                     {"instagram": 5, "linkedin": 5, "naver_blog": 5}, history=history).slots))

    thread = threading.Thread(target=plan_all, daemon=True)
    thread.start()
    thread.join(30)
    try:
        assert not thread.is_alive(), f"template_calendar hangs (plans done: {results})"
        assert results[:5] == [21, 15, 15, 15, 15], results
        assert len(results) == 6
    finally:
        ws.close()
