from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from fakes import FakeClient, json_text, message
from insia_agents.backends.anthropic_backend import AnthropicBackend
from insia_agents.backends.mock_backend import MockBackend, template_calendar
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
