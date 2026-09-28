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
    drop_ungrounded_sources,
    echo_content,
    map_api_error,
    match_question,
    message_text,
)
from insia_agents.backends.base import (
    APICallError,
    APIConnectionFailed,
    AuthError,
    InvalidOutputError,
    OutputTruncatedError,
    RateLimitedError,
    RefusalError,
    merge_research,
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


def test_echo_content_keeps_code_execution_pairs_before_fallback():
    """Dynamic filtering runs code execution; its use/result pairs must echo together."""
    blocks = [
        {"type": "server_tool_use", "id": "c1", "name": "code_execution", "input": {"code": "..."}},
        {"type": "code_execution_tool_result", "tool_use_id": "c1", "content": {"type": "code_execution_result", "stdout": ""}},
        {"type": "server_tool_use", "id": "b1", "name": "bash_code_execution", "input": {"command": "ls"}},
        {"type": "bash_code_execution_tool_result", "tool_use_id": "b1", "content": {"type": "bash_code_execution_result"}},
        {"type": "server_tool_use", "id": "u1", "name": "code_execution", "input": {}},  # unpaired: dropped
        {"type": "tool_use", "id": "t1", "name": "client_tool", "input": {}},  # client tool_use: dropped
        {"type": "web_fetch_tool_result", "tool_use_id": "zz", "content": {}},  # result without a use: dropped
        {"type": "text", "text": "부분"},
        {"type": "fallback"},
        {"type": "server_tool_use", "id": "w1", "name": "web_search", "input": {"query": "q"}},
    ]
    out = echo_content(blocks)
    assert [(b["type"], b.get("id") or b.get("tool_use_id")) for b in out] == [
        ("server_tool_use", "c1"), ("code_execution_tool_result", "c1"),
        ("server_tool_use", "b1"), ("bash_code_execution_tool_result", "b1"),
        ("text", None), ("server_tool_use", "w1"),
    ]


def test_map_api_error_without_credentials_error(monkeypatch):
    """SDK 1.0-1.4 export no CredentialsError and raise a plain AnthropicError."""
    missing = anthropic.AnthropicError("Credentials file not found at ~/.config/anthropic/credentials/default.json (profile 'default').")
    credentials_error = getattr(anthropic, "CredentialsError", None)
    if credentials_error is not None:  # SDK >= 1.5
        assert type(map_api_error(missing, "claude-opus-5")) is APICallError  # not a CredentialsError there
        assert isinstance(map_api_error(credentials_error("x"), "claude-opus-5"), AuthError)
        monkeypatch.delattr(anthropic, "CredentialsError")
    mapped = map_api_error(missing, "claude-opus-5")
    assert isinstance(mapped, AuthError) and mapped.kind == "credentials" and "ant auth login" in str(mapped)
    other = map_api_error(anthropic.AnthropicError("something else"), "claude-opus-5")
    assert type(other) is APICallError and other.kind == "unknown"


def test_research_drops_sources_no_tool_produced(settings, prompts_dir, brief, plan):
    many = [SimpleNamespace(type="web_search_result", url=f"https://example.org/r{i}", title=f"r{i}", page_age="")
            for i in range(100)]
    search_result = SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1", content=[
        SimpleNamespace(type="web_search_result", url="https://www.mss.go.kr/a", title="실태조사", page_age=""), *many])
    memo = text("q1: 확인함. 원문은 https://kosis.kr/stat?id=1 참고.")
    structured = ResearchPack(
        findings=[Finding(id="f1", question_id="q1", claim="진짜", source_ids=["s1", "s4"], confidence="high"),
                  Finding(id="f2", question_id="q1", claim="지어낸 수치", source_ids=["s4"], confidence="high")],
        sources=[Source(id="s1", title="실태조사", url="http://mss.go.kr/a/", tier=1),       # scheme/www/slash differ
                 Source(id="s2", title="KOSIS", url="https://kosis.kr/stat?id=1", tier=1),   # only in the memo
                 Source(id="s3", title="r95", url="https://example.org/r95", tier=2),        # beyond the 80 sent on
                 Source(id="s4", title="가짜 통계", url="https://made-up.example/stat", tier=1)],
        gaps=[])
    client = FakeClient([message([search_result, memo]), message([json_text(structured)])])
    notices = []
    backend = AnthropicBackend(settings, client=client)
    backend.on_notice = lambda agent, level, msg: notices.append((agent, level, msg))
    pack = backend.research(brief, plan.questions, lambda t, d: None)

    assert [s.id for s in pack.sources] == ["s1", "s2", "s3"]
    assert [f.source_ids for f in pack.findings] == [["s1"], []]
    assert notices and notices[0][:2] == ("researcher", "warn") and "made-up.example" in notices[0][2]
    merged, _ = merge_research(None, pack)
    assert [f.claim for f in merged.findings] == ["진짜"]
    assert any("지어낸 수치" in g for g in merged.gaps)


def test_followup_research_keeps_existing_source_urls(settings, prompts_dir, brief, plan, pack):
    reuse = ResearchPack(findings=[Finding(id="f2", question_id="q1", claim="재인용", source_ids=["s2"], confidence="medium")],
                         sources=[Source(id="s2", title="실태조사", url="https://www.mss.go.kr/a", tier=1)], gaps=[])
    client = FakeClient([message([text("새로 찾은 것 없음")]), message([json_text(reuse)])])
    got = AnthropicBackend(settings, client=client).research(brief, plan.questions, lambda t, d: None, existing=pack)
    assert got == reuse


def test_drop_ungrounded_sources_is_a_noop_when_all_grounded(pack):
    same, dropped = drop_ungrounded_sources(pack, {"mss.go.kr/a"})
    assert same is pack and dropped == []
