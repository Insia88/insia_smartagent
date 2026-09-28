from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from fakes import FakeClient, FakeStream, RefusedMessage, json_text, message, text
from insia_agents.backends.anthropic_backend import (
    FALLBACK_BETA,
    AnthropicBackend,
    collect_research_notes,
    echo_content,
    match_question,
    message_text,
)
from insia_agents.backends.base import (
    APIConnectionFailed,
    AuthError,
    InvalidOutputError,
    OutputTruncatedError,
    RateLimitedError,
    RefusalError,
)
from insia_agents.channels import check_format
from insia_agents.models import (
    ChannelOutline,
    Draft,
    Finding,
    Plan,
    ResearchPack,
    ResearchQuestion,
    Review,
    RubricScore,
    Source,
)
from insia_agents.schema import output_schema


@pytest.fixture
def plan() -> Plan:
    return Plan(
        summary="요약",
        key_messages=["메시지 1", "메시지 2", "메시지 3"],
        questions=[
            ResearchQuestion(id="q1", question="국내 소상공인 사업체 수 추이", why="문제인식", channels=["bizplan"]),
            ResearchQuestion(id="q2", question="생성형 AI 이용률 조사", why="블로그", channels=["naver_blog"]),
        ],
        outlines=[ChannelOutline(channel=c, sections=["도입"]) for c in ("bizplan", "naver_blog", "linkedin", "instagram")],
    )


@pytest.fixture
def pack() -> ResearchPack:
    return ResearchPack(
        findings=[Finding(id="f1", question_id="q1", claim="2024년 기준 ○○만 개", source_ids=["s1"], confidence="high")],
        sources=[Source(id="s1", title="실태조사", url="https://www.mss.go.kr/a", publisher="중소벤처기업부", tier=1)],
        gaps=[],
    )


def _assert_common(call: dict, *, effort: str, schema_model=None) -> None:
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == effort
    if schema_model is None:
        assert "format" not in call["output_config"]
    else:
        assert call["output_config"]["format"] == {"type": "json_schema", "schema": output_schema(schema_model)}
    assert call["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["max_tokens"] >= 16000
    # no prefill: the conversation we start always ends with the user turn
    assert call["messages"][-1]["role"] == "user"
    assert all(k not in call for k in ("temperature", "top_p", "top_k", "budget_tokens"))
    # prompt caching on the stable system prompt
    assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "today" in call["messages"][0]["content"]


def test_plan_request_shape(settings, prompts_dir, brief, plan):
    client = FakeClient([message([json_text(plan)])])
    backend = AnthropicBackend(settings, client=client)
    narrowed = brief.model_copy(update={"channels": ["linkedin", "instagram"]})
    result = backend.plan(narrowed)
    assert client.endpoints == ["beta.messages.stream"]
    call = client.calls[0]
    _assert_common(call, effort="high", schema_model=Plan)
    assert "tools" not in call
    assert len(call["system"]) == 1 and "orchestrator" in call["system"][0]["text"]
    assert [o.channel for o in result.outlines] == ["linkedin", "instagram"]


def test_fallbacks_can_be_disabled(settings, prompts_dir, brief, plan):
    client = FakeClient([message([json_text(plan)])])
    backend = AnthropicBackend(replace(settings, fallbacks=False), client=client)
    backend.plan(brief)
    assert client.endpoints == ["messages.stream"]
    assert "betas" not in client.calls[0] and "fallbacks" not in client.calls[0]


def test_effort_and_model_follow_settings(settings, prompts_dir, brief, plan):
    client = FakeClient([message([json_text(plan)])])
    custom = replace(settings, model="claude-sonnet-5", effort={"orchestrator": "low", "researcher": "low", "reviewer": "low"})
    AnthropicBackend(custom, client=client).plan(brief)
    assert client.calls[0]["model"] == "claude-sonnet-5"
    assert client.calls[0]["output_config"]["effort"] == "low"


def test_research_uses_server_tools_then_structured_call(settings, prompts_dir, brief, plan, pack):
    search_use = SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "소상공인 사업체 수 2024"})
    fetch_use = SimpleNamespace(type="server_tool_use", id="srv_2", name="web_fetch", input={"url": "https://www.mss.go.kr/a"})
    search_result = SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1", content=[
        SimpleNamespace(type="web_search_result", url="https://www.mss.go.kr/a", title="실태조사", page_age="2025-03-01"),
    ])
    fetch_result = SimpleNamespace(type="web_fetch_tool_result", tool_use_id="srv_2", content=SimpleNamespace(
        type="web_fetch_result", url="https://www.mss.go.kr/a", retrieved_at="2026-09-28",
        content=SimpleNamespace(type="document", title="실태조사 원문")))
    paused = message([text("조사를 시작해요. "), search_use], stop_reason="pause_turn")
    stop_event = SimpleNamespace(type="content_block_stop", index=1, content_block=search_use)
    memo = text("q1: 2024년 기준 ○○만 개 (중소벤처기업부)", citations=[
        SimpleNamespace(type="web_search_result_location", url="https://www.mss.go.kr/a", title="실태조사", cited_text="…"),
    ])
    finished = message([search_result, fetch_use, fetch_result, memo])
    structured = message([json_text(pack)])
    client = FakeClient([FakeStream(paused, events=[stop_event]), finished, structured])
    emitted: list[tuple[str, dict]] = []
    backend = AnthropicBackend(settings, client=client)
    result = backend.research(brief, plan.questions, lambda t, d: emitted.append((t, d)))

    assert result == pack
    assert len(client.calls) == 3
    first, resumed, convert = client.calls
    _assert_common(first, effort="medium")
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert not any("code_execution" in t["type"] for t in first["tools"])
    # pause_turn: re-send the paused assistant turn as-is, no extra "continue" user message
    assert resumed["messages"][0] == first["messages"][0]
    assert resumed["messages"][1] == {"role": "assistant", "content": paused.content}
    assert len(resumed["messages"]) == 2
    assert resumed["tools"] == first["tools"]
    # conversion: tool-less structured output built from the collected notes
    _assert_common(convert, effort="medium", schema_model=ResearchPack)
    assert "tools" not in convert
    assert "https://www.mss.go.kr/a" in convert["messages"][0]["content"]
    assert "실태조사 원문" in convert["messages"][0]["content"]
    # the search was surfaced once (stream event + final-message scan are de-duplicated)
    queries = [d for t, d in emitted if t == "research.query"]
    assert queries == [{"question_id": "q1", "query": "소상공인 사업체 수 2024"}]
    assert any(t == "agent.status" and d["status"] == "reading" for t, d in emitted)


def test_followup_research_passes_existing_ids(settings, prompts_dir, brief, plan, pack):
    client = FakeClient([message([text("메모")]), message([json_text(ResearchPack(findings=[], sources=[], gaps=["없음"]))])])
    backend = AnthropicBackend(settings, client=client)
    backend.research(brief, plan.questions, lambda t, d: None, existing=pack)
    content = client.calls[1]["messages"][0]["content"]
    assert '"source": "s2"' in content and '"finding": "f2"' in content
    assert "https://www.mss.go.kr/a" in client.calls[0]["messages"][0]["content"]


def test_draft_review_revise_shapes(settings, prompts_dir, brief, plan, pack):
    draft = Draft(channel="bizplan", round=0, title="t", content="# t\n본문")
    review = Review(channel="bizplan", round=0, score=50, passed=False,
                    rubric=[RubricScore(id="problem", label="문제인식", score=10, max=20, comment="")], issues=[], summary="요약")
    revised = draft.model_copy(update={"round": 5, "channel": "linkedin"})  # backend must correct these
    client = FakeClient([message([json_text(draft)]), message([json_text(review)]), message([json_text(revised)])])
    backend = AnthropicBackend(settings, client=client)

    out = backend.draft(brief, plan, pack, "bizplan")
    assert out.round == 0 and out.channel == "bizplan"
    call = client.calls[0]
    _assert_common(call, effort="high", schema_model=Draft)
    assert call["max_tokens"] == 64000  # bizplan is long
    assert len(call["system"]) == 2 and "bizplan" in call["system"][1]["text"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    checks = check_format(draft, brief)
    got = backend.review(brief, pack, draft, checks)
    _assert_common(client.calls[1], effort="high", schema_model=Review)
    assert got.format_checks == checks

    again = backend.revise(brief, plan, pack, draft, got)
    assert again.round == 1 and again.channel == "bizplan"
    _assert_common(client.calls[2], effort="high", schema_model=Draft)


def test_refusal_is_checked_before_content(settings, prompts_dir, brief):
    client = FakeClient([RefusedMessage("cyber")])
    with pytest.raises(RefusalError) as info:
        AnthropicBackend(settings, client=client).plan(brief)
    assert info.value.category == "cyber"
    assert "거절" in str(info.value)


def test_max_tokens_and_invalid_json(settings, prompts_dir, brief):
    backend = AnthropicBackend(settings, client=FakeClient([message([text("{")], stop_reason="max_tokens")]))
    with pytest.raises(OutputTruncatedError):
        backend.plan(brief)
    backend = AnthropicBackend(settings, client=FakeClient([message([text("계획을 세울 수 없어요")])]))
    with pytest.raises(InvalidOutputError):
        backend.plan(brief)
    backend = AnthropicBackend(settings, client=FakeClient([message([text('{"summary": 1}')])]))
    with pytest.raises(InvalidOutputError):
        backend.plan(brief)


def test_pause_turn_limit(settings, prompts_dir, brief, plan):
    paused = message([SimpleNamespace(type="server_tool_use", id="x", name="web_search", input={"query": "q"})], stop_reason="pause_turn")
    backend = AnthropicBackend(replace(settings, max_continuations=1), client=FakeClient([paused, paused]))
    with pytest.raises(Exception, match="pause_turn"):
        backend.research(brief, plan.questions, lambda t, d: None)


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"request-id": "req_x"})
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize("exc, expected", [
    (lambda: _status_error(anthropic.RateLimitError, 429), RateLimitedError),
    (lambda: _status_error(anthropic.AuthenticationError, 401), AuthError),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), APIConnectionFailed),
])
def test_api_errors_are_typed(settings, prompts_dir, brief, exc, expected):
    error = exc()
    backend = AnthropicBackend(settings, client=FakeClient([error]))
    with pytest.raises(expected) as info:
        backend.plan(brief)
    assert info.value.__cause__ is error


def test_server_side_fallback_is_reported(settings, prompts_dir, brief, plan):
    served = message([SimpleNamespace(type="fallback", **{"from": None}), json_text(plan)], model="claude-opus-4-8",
                     iterations=[SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")])
    notices = []
    backend = AnthropicBackend(settings, client=FakeClient([served]))
    backend.on_notice = lambda agent, level, msg: notices.append((agent, level, msg))
    backend.plan(brief)
    assert notices and notices[0][0] == "orchestrator" and notices[0][1] == "warn" and "claude-opus-4-8" in notices[0][2]


def test_echo_content_after_fallback():
    blocks = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="server_tool_use", id="a", name="web_search", input={}),
        SimpleNamespace(type="server_tool_use", id="b", name="web_search", input={}),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="b", content=[]),
        text("부분 "),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="server_tool_use", id="c", name="web_search", input={}),
        text("이어서  "),
    ]
    out = echo_content(blocks)
    kinds = [getattr(b, "type", None) or b["type"] for b in out]
    assert kinds == ["server_tool_use", "web_search_tool_result", "text", "server_tool_use", "text"]
    assert out[0].id == "b"
    assert out[-1] == {"type": "text", "text": "이어서"}


def test_message_text_joins_partial_and_fallback_text():
    msg = message([text('{"a": '), SimpleNamespace(type="fallback"), text("1}")])
    assert message_text([msg]) == '{"a": 1}'


def test_collect_notes_and_question_matching(plan):
    msg = message([SimpleNamespace(type="web_search_tool_result", tool_use_id="x",
                                   content=SimpleNamespace(type="web_search_tool_result_error", error_code="max_uses_exceeded"))])
    notes = collect_research_notes([msg])
    assert notes["tool_errors"] == ["web_search 오류: max_uses_exceeded"]
    assert match_question("생성형 AI 이용률 2025", plan.questions) == "q2"
    assert match_question("소상공인 통계", plan.questions) == "q1"
