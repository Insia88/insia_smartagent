from __future__ import annotations

import re
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


def user_text(call: dict) -> str:
    """All text of the first user message (content is a list of text blocks)."""
    content = call["messages"][0]["content"]
    assert isinstance(content, list) and all(b["type"] == "text" for b in content)
    return "\n".join(b["text"] for b in content)


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
    assert '"today": "2026-09-28"' in user_text(call)
    # per-run context never goes into the cached system prompt
    assert all("회사 프로필" not in b["text"] and "사용자 제공 자료" not in b["text"] for b in call["system"])


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
    assert "https://www.mss.go.kr/a" in user_text(convert)
    assert "실태조사 원문" in user_text(convert)
    # the search was surfaced once (stream event + final-message scan are de-duplicated)
    queries = [d for t, d in emitted if t == "research.query"]
    assert queries == [{"question_id": "q1", "query": "소상공인 사업체 수 2024"}]
    assert any(t == "agent.status" and d["status"] == "reading" for t, d in emitted)


def test_followup_research_passes_existing_ids(settings, prompts_dir, brief, plan, pack):
    client = FakeClient([message([text("메모")]), message([json_text(ResearchPack(findings=[], sources=[], gaps=["없음"]))])])
    backend = AnthropicBackend(settings, client=client)
    backend.research(brief, plan.questions, lambda t, d: None, existing=pack)
    content = user_text(client.calls[1])
    assert '"source": "s2"' in content and '"finding": "f2"' in content
    assert "https://www.mss.go.kr/a" in user_text(client.calls[0])


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


# ---------------------------------------------------------------------------
# Run context: profile, user documents, human instructions
# ---------------------------------------------------------------------------

from insia_agents.backends.anthropic_backend import attach_user_sources, grounded_url_keys, url_key  # noqa: E402
from insia_agents.backends.base import BackendError, RunContext  # noqa: E402
from insia_agents.models import ContentItem, ContentPlan, PlannedSlot, Profile, TeamMember, UserDocument  # noqa: E402


@pytest.fixture
def profile() -> Profile:
    return Profile(
        company_name="인시아랩", service_name="INSIA", one_liner="1인 창업자를 위한 AI 콘텐츠 비서",
        target_customers="1인 창업자", traction=["베타 사용자 120명(2026-08 기준)"],
        team=[TeamMember(role="대표", name="김철수", background="마케팅 10년"),
              TeamMember(role="개발", name="이영희", background="백엔드 7년")],
        tone="친근한 전문가", banned_words=["완벽한"], required_phrases=["#광고아님"], default_hashtags=["#INSIA"],
        cta="프로필 링크에서 무료 체험", contact="hello@insia.kr", linkedin_url="https://linkedin.com/company/insia",
        brand_colors=["#6D5EF5"],
    )


def usage_message(content, *, stop_reason="end_turn", model="claude-opus-5", input_tokens=1000, output_tokens=500,
                  cache_read=0, cache_write=0, searches=0):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model, stop_details=None, _request_id="req_u",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=cache_read,
                              cache_creation_input_tokens=cache_write, iterations=None,
                              server_tool_use=SimpleNamespace(web_search_requests=searches)),
    )


def blocks(call: dict) -> list[dict]:
    return call["messages"][0]["content"]


def breakpoints(call: dict) -> int:
    marked = [b for b in call["system"] if "cache_control" in b]
    for message_ in call["messages"]:
        content = message_["content"]
        if isinstance(content, list):
            marked += [b for b in content if isinstance(b, dict) and "cache_control" in b]
    return len(marked)


def test_research_with_profile_and_documents(settings, prompts_dir, brief, plan, profile):
    long_text = "INSIA는 1인 창업자를 위한 서비스다. " * 200  # ~4,400 chars
    docs = [UserDocument(id="u1", title="회사 소개서", text=long_text), UserDocument(id="u2", title="IR 메모", text="베타 사용자 120명.")]
    memo = text("q1: 공식 통계 https://kosis.kr/a 확인")
    structured = ResearchPack(
        findings=[
            Finding(id="f1", question_id="q1", claim="베타 사용자 120명", source_ids=["s1"], confidence="medium"),
            Finding(id="f2", question_id="q1", claim="회사 설립 2026년", source_ids=["s9"], confidence="medium"),
            Finding(id="f3", question_id="q1", claim="공식 통계", source_ids=["s2"], confidence="high"),
        ],
        sources=[
            Source(id="s9", title="회사 소개서(모델 사본)", url="user://u1", tier=2),  # the model's copy of a user source
            Source(id="s2", title="KOSIS", url="https://kosis.kr/a", tier=1, origin="user"),  # reuses a reserved id
            Source(id="s4", title="지어낸 자료", url="user://u7", tier=1),  # a user document that does not exist
        ],
        gaps=[],
    )
    client = FakeClient([message([memo]), message([json_text(structured)])])
    backend = AnthropicBackend(replace(settings, max_document_chars=1000), client=client)
    backend.context = RunContext(profile=profile, documents=docs, today="2026-09-30")
    notices = []
    backend.on_notice = lambda agent, level, msg: notices.append((agent, level, msg))
    pack = backend.research(brief, plan.questions, lambda t, d: None)

    search, convert = client.calls
    # cache: system blocks are byte-identical to a run without any context
    assert search["system"] == backend.system_blocks("researcher") == convert["system"]
    assert all(breakpoints(c) <= 4 for c in client.calls)
    # search call: profile block (own breakpoint) + task text; only document titles, no names/contact
    first, task = blocks(search)
    assert first["text"].startswith("# 회사 프로필 (사용자 제공 사실)") and first["cache_control"] == {"type": "ephemeral"}
    assert "김철수" not in first["text"] and "hello@insia.kr" not in first["text"] and "대표 — 마케팅 10년" in first["text"]
    assert "cache_control" not in task and '"today": "2026-09-30"' in task["text"]
    assert '"user_materials"' in task["text"] and "회사 소개서" in task["text"] and long_text[:200] not in task["text"]
    # structuring call: profile, documents (no breakpoint: sent once), task
    prof, docs_block, task2 = blocks(convert)
    assert prof == first
    assert "cache_control" not in docs_block and "cache_control" not in task2
    assert '<document source_id="s1" doc_id="u1" title="회사 소개서">' in docs_block["text"]
    assert '<document source_id="s2" doc_id="u2" title="IR 메모">' in docs_block["text"]
    assert "앞 " in docs_block["text"] and "자만 보냈음" in docs_block["text"]
    sent = sum(len(e) for e in re.findall(r'title="[^"]*">\n(.*?)\n</document>', docs_block["text"], re.S))
    assert sent <= 1000
    assert '"user_sources"' in task2["text"] and '"source": "s3"' in task2["text"]  # web ids start after user ids
    # truncation is announced, never silent
    assert any(agent == "researcher" and level == "warn" and "max_document_chars=1,000" in msg and "회사 소개서" in msg
               for agent, level, msg in notices)
    # canonical user sources first; the copy maps onto s1; the id clash is renamed; the invented doc is dropped
    assert [(s.id, s.url, s.origin, s.tier, s.publisher) for s in pack.sources[:2]] == [
        ("s1", "user://u1", "user", 1, "사용자 제공 자료"), ("s2", "user://u2", "user", 1, "사용자 제공 자료")]
    kosis = next(s for s in pack.sources if s.url == "https://kosis.kr/a")
    assert kosis.origin == "web" and kosis.id not in ("s1", "s2")
    assert not any(s.url == "user://u7" for s in pack.sources)
    assert [f.source_ids for f in pack.findings] == [["s1"], ["s1"], [kosis.id]]
    assert any("user://u7" in msg for _, _, msg in notices)


def test_followup_research_does_not_resend_documents(settings, prompts_dir, brief, plan, pack, profile):
    docs = [UserDocument(id="u1", title="회사 소개서", text="자료 본문")]
    client = FakeClient([message([text("메모")]), message([json_text(ResearchPack(findings=[], sources=[], gaps=[]))])])
    backend = AnthropicBackend(settings, client=client)
    backend.context = RunContext(profile=profile, documents=docs, today="2026-09-28")
    backend.research(brief, plan.questions, lambda t, d: None, existing=pack)
    assert all("자료 본문" not in user_text(c) and "user_materials" not in user_text(c) for c in client.calls)
    assert len(blocks(client.calls[1])) == 2  # profile + task, no documents block


def test_no_context_means_a_single_task_block(settings, prompts_dir, brief, plan):
    client = FakeClient([message([json_text(plan)])])
    AnthropicBackend(settings, client=client).plan(brief)
    assert len(blocks(client.calls[0])) == 1 and "cache_control" not in blocks(client.calls[0])[0]


def test_plan_lists_document_titles_only(settings, prompts_dir, brief, plan, profile):
    client = FakeClient([message([json_text(plan)])])
    backend = AnthropicBackend(settings, client=client)
    backend.context = RunContext(profile=profile, documents=[UserDocument(id="u3", title="IR 자료", text="비밀 본문 " * 50)],
                                 today="2026-09-28")
    backend.plan(brief)  # brief includes bizplan → no team names
    body = user_text(client.calls[0])
    assert '"doc_id": "u3"' in body and "IR 자료" in body and "비밀 본문" not in body
    assert "김철수" not in body and "회사 내부 사실은 리서치 질문으로 만들지 말고" in body


def test_profile_block_follows_the_channel(settings, prompts_dir, brief, plan, pack, profile):
    draft = Draft(channel="bizplan", round=0, title="t", content="# t")
    review = Review(channel="linkedin", round=0, score=50, passed=False, rubric=[], issues=[], summary="s")
    li = Draft(channel="linkedin", round=0, title="t", content="본문")
    client = FakeClient([message([json_text(draft)]), message([json_text(review)]), message([json_text(li)]),
                         message([json_text(li)])])
    backend = AnthropicBackend(settings, client=client)
    backend.context = RunContext(profile=profile, documents=[], today="2026-09-28", instructions="첫 문장을 더 짧게")
    backend.draft(brief, plan, pack, "bizplan")
    backend.review(brief, pack, li, [])
    backend.revise(brief, plan, pack, li, review)                       # context.instructions
    backend.revise(brief, plan, pack, li, review, instructions="표를 빼 주세요")  # kwarg wins

    biz = blocks(client.calls[0])[0]["text"]
    assert "김철수" not in biz and "이영희" not in biz and "실명·학교명·직장명은 쓰지 않음" in biz
    assert "베타 사용자 120명(2026-08 기준)" in biz and "금지 표현" in biz
    assert "기본 행동 유도" not in biz and "기본 해시태그" not in biz and "필수 문구" not in biz and "브랜드 색" not in biz
    rev = blocks(client.calls[1])[0]["text"]
    assert "대표 · 김철수" in rev and "프로필 링크에서 무료 체험" in rev and "#INSIA" in rev and "#광고아님" in rev
    assert "링크드인 (본문에 링크를 넣지 않음)" in rev and "브랜드 색" not in rev
    # reviewer and orchestrator share the same profile block bytes for a channel (cache reuse)
    assert blocks(client.calls[2])[0] == blocks(client.calls[1])[0]
    human = blocks(client.calls[2])[-1]["text"]
    assert human == "# 사람의 수정 지시\n\n첫 문장을 더 짧게"
    assert "[사람 지시]" in blocks(client.calls[2])[1]["text"]
    assert blocks(client.calls[3])[-1]["text"].endswith("표를 빼 주세요")
    assert all(breakpoints(c) <= 4 for c in client.calls)


def test_revise_without_instructions_has_no_human_block(settings, prompts_dir, brief, plan, pack):
    li = Draft(channel="linkedin", round=0, title="t", content="본문")
    review = Review(channel="linkedin", round=0, score=50, passed=False, rubric=[], issues=[], summary="s")
    client = FakeClient([message([json_text(li)])])
    AnthropicBackend(settings, client=client).revise(brief, plan, pack, li, review)
    assert "사람의 수정 지시" not in user_text(client.calls[0])


# ---------------------------------------------------------------------------
# Usage reporting
# ---------------------------------------------------------------------------


def test_usage_is_reported_after_every_response(settings, prompts_dir, brief, plan, pack):
    search_use = SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "소상공인"})
    paused = usage_message([search_use], stop_reason="pause_turn", input_tokens=2000, output_tokens=100, searches=3)
    finished = usage_message([text("메모")], input_tokens=500, output_tokens=800, cache_read=4000, searches=1)
    structured = usage_message([json_text(pack.model_copy(update={"sources": []}))], cache_write=3000)
    client = FakeClient([paused, finished, structured])
    records = []
    backend = AnthropicBackend(settings, client=client)
    backend.on_usage = records.append
    backend.research(brief, plan.questions, lambda t, d: None)
    assert [(r.agent, r.task) for r in records] == [("researcher", "research")] * 3
    assert [r.web_search_requests for r in records] == [3, 1, 0]
    assert records[1].cache_read_tokens == 4000 and records[2].cache_write_tokens == 3000
    assert records[0].cost_usd == pytest.approx((2000 * 5 + 100 * 25) / 1e6 + 0.03)
    assert all(r.run_id == "" and r.model == "claude-opus-5" for r in records)


def test_usage_is_reported_for_refusals_too(settings, prompts_dir, brief):
    records = []
    backend = AnthropicBackend(settings, client=FakeClient([RefusedMessage()]))
    backend.on_usage = records.append
    with pytest.raises(RefusalError):
        backend.plan(brief)
    assert len(records) == 1 and records[0].task == "plan"


def test_unknown_model_price_is_announced_once(settings, prompts_dir, brief, plan):
    client = FakeClient([usage_message([json_text(plan)], model="claude-mystery"),
                         usage_message([json_text(plan)], model="claude-mystery")])
    backend = AnthropicBackend(replace(settings, model="claude-mystery"), client=client)
    records, notices = [], []
    backend.on_usage = records.append
    backend.on_notice = lambda agent, level, msg: notices.append(msg)
    backend.plan(brief)
    backend.plan(brief)
    assert [r.cost_usd for r in records] == [0.0, 0.0]
    assert len([n for n in notices if "가격 정보가 없어" in n]) == 1
    assert "INSIA_PRICE_CLAUDE_MYSTERY_INPUT" in notices[0]


def test_a_broken_usage_recorder_never_fails_the_call(settings, prompts_dir, brief, plan):
    backend = AnthropicBackend(settings, client=FakeClient([message([json_text(plan)])]))
    notices = []
    backend.on_notice = lambda agent, level, msg: notices.append(msg)

    def broken(record):
        raise OSError("disk full")

    backend.on_usage = broken
    assert backend.plan(brief) == plan
    assert any("disk full" in n for n in notices)

    def stop(record):
        raise BackendError("예산 상한을 넘었어요")

    backend = AnthropicBackend(settings, client=FakeClient([message([json_text(plan)])]))
    backend.on_usage = stop
    with pytest.raises(BackendError, match="예산"):
        backend.plan(brief)


# ---------------------------------------------------------------------------
# Grounding: percent-encoding, Korean URLs, fetch redirects
# ---------------------------------------------------------------------------


def test_percent_encoded_and_korean_urls_are_the_same_source():
    encoded = "https://ko.wikipedia.org/wiki/%EC%86%8C%EC%83%81%EA%B3%B5%EC%9D%B8"
    assert url_key(encoded) == url_key("https://ko.wikipedia.org/wiki/소상공인") == "ko.wikipedia.org/wiki/소상공인"
    search = message([SimpleNamespace(type="web_search_tool_result", tool_use_id="s", content=[
        SimpleNamespace(type="web_search_result", url=encoded, title="소상공인", page_age="")])])
    memo = message([text("참고: https://ko.wikipedia.org/wiki/소상공인에서 확인. 또 (https://www.mss.go.kr/통계/2025) 참고.")])
    keys = grounded_url_keys([search, memo])
    assert "ko.wikipedia.org/wiki/소상공인" in keys
    assert "mss.go.kr/통계/2025" in keys  # Korean path kept, closing parenthesis stripped
    pack = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim="c", source_ids=["s1", "s2"], confidence="high")],
                        sources=[Source(id="s1", title="위키", url="https://ko.wikipedia.org/wiki/소상공인", tier=3),
                                 Source(id="s2", title="중기부", url="https://www.mss.go.kr/%ED%86%B5%EA%B3%84/2025", tier=1)],
                        gaps=[])
    kept, dropped = drop_ungrounded_sources(pack, keys)
    assert dropped == [] and kept is pack


def test_requested_fetch_url_counts_as_grounded_after_a_redirect():
    fetch_use = SimpleNamespace(type="server_tool_use", id="f1", name="web_fetch", input={"url": "http://mss.go.kr/old"})
    fetch_result = SimpleNamespace(type="web_fetch_tool_result", tool_use_id="f1", content=SimpleNamespace(
        type="web_fetch_result", url="https://www.mss.go.kr/new", content=SimpleNamespace(type="document", title="t")))
    keys = grounded_url_keys([message([fetch_use, fetch_result])])
    assert {"mss.go.kr/old", "mss.go.kr/new"} <= keys


def test_attach_user_sources_without_user_documents_sets_origins():
    pack = ResearchPack(findings=[], gaps=[], sources=[
        Source(id="s1", title="웹", url="https://a.example", tier=2, origin="user"),
        Source(id="s2", title="자료", url="user://u1", tier=1)])
    out = attach_user_sources(pack, [])
    assert [(s.id, s.origin) for s in out.sources] == [("s1", "web"), ("s2", "user")]


# ---------------------------------------------------------------------------
# Content calendar
# ---------------------------------------------------------------------------


def test_plan_calendar_request_and_normalization(settings, prompts_dir, profile):
    (prompts_dir / "agents" / "planner.md").write_text("# planner 테스트 프롬프트", encoding="utf-8")
    raw = ContentPlan(summary="이번 주 전략", slots=[
        PlannedSlot(date="2026-10-05", channel="naver_blog", topic="블로그 1", angle="체크리스트", keywords=["AI 마케팅", "AI 마케팅"], goal="검색 유입"),
        PlannedSlot(date="2026-10-10", channel="naver_blog", topic="토요일 글", angle="사례", keywords=["k"], goal="g"),   # Saturday → moved
        PlannedSlot(date="2026-10-12", channel="linkedin", topic="기간 밖", angle="a", keywords=["k"], goal="g"),       # outside
        PlannedSlot(date="2026-10-06", channel="instagram", topic="요청 안 한 채널", angle="a", keywords=["k"], goal="g"),
        PlannedSlot(date="2026-10-07", channel="linkedin", topic="링크드인 1", angle="관점", keywords=["k"], goal="g"),
        PlannedSlot(date="2026-10-08", channel="linkedin", topic="링크드인 2 (초과)", angle="관점", keywords=["k"], goal="g"),
    ])
    client = FakeClient([message([json_text(raw)])])
    backend = AnthropicBackend(settings, client=client)
    history = [ContentItem(id="it_1", channel="naver_blog", title="지난주 블로그 글", status="published",
                           published_at="2026-09-29T01:00:00Z")]
    plan = backend.plan_calendar(profile, "AI 마케팅 자동화", "2026-10-05", "2026-10-11", {"blog": 2, "linkedin": 1}, history)

    call = client.calls[0]
    _assert_common(call, effort="high", schema_model=ContentPlan)
    assert call["system"] == [{"type": "text", "text": "# planner 테스트 프롬프트", "cache_control": {"type": "ephemeral"}}]
    prof, task = blocks(call)
    assert prof["cache_control"] == {"type": "ephemeral"} and "김철수" not in prof["text"] and "INSIA" in prof["text"]
    body = task["text"]
    assert '"counts": {\n  "naver_blog": 2,\n  "linkedin": 1\n }' in body
    assert '"date": "2026-10-09"' in body and '"weekday": "금"' in body and "2026-10-10" not in body.split("history")[0]
    assert "지난주 블로그 글" in body and '"date": "2026-09-29"' in body
    assert [(s.date, s.channel, s.topic) for s in plan.slots] == [
        ("2026-10-05", "naver_blog", "블로그 1"), ("2026-10-07", "linkedin", "링크드인 1"), ("2026-10-09", "naver_blog", "토요일 글")]
    assert plan.slots[0].keywords == ["AI 마케팅"] and plan.summary == "이번 주 전략"


# ---------------------------------------------------------------------------
# Context rendering helpers and packaged prompts
# ---------------------------------------------------------------------------

from insia_agents.prompt_loader import (  # noqa: E402
    agent_prompt,
    budget_documents,
    check_prompts,
    render_profile,
    user_sources,
)


def test_budget_documents_shares_space_fairly_and_never_silently():
    docs = [UserDocument(id="u1", title="긴 자료", text="가" * 5000), UserDocument(id="u2", title="짧은 자료", text="나" * 100),
            UserDocument(id="u3", title="빈 자료", text=" \n "), UserDocument(id="u4", title="중간 자료", text="다" * 800)]
    excerpts, notice = budget_documents(docs, 1000)
    assert [(e.document.id, len(e.text), e.truncated) for e in excerpts] == [("u1", 450, True), ("u2", 100, False), ("u4", 450, True)]
    assert sum(len(e.text) for e in excerpts) <= 1000
    assert notice is not None and "5,900자 중 1,000자" in notice and "「긴 자료」 5,000자 → 450자" in notice
    assert "빈 자료" not in notice
    whole, none = budget_documents(docs, 60_000)
    assert none is None and [len(e.text) for e in whole] == [5000, 100, 800]
    skipped, why = budget_documents(docs, 0)
    assert skipped == [] and "3개" in why and "INSIA_MAX_DOCUMENT_CHARS" in why
    assert budget_documents([], 1000) == ([], None)
    sources = user_sources(whole, 7, "2026-09-28")
    assert [(s.id, s.url, s.origin, s.accessed) for s in sources] == [
        ("s7", "user://u1", "user", "2026-09-28"), ("s8", "user://u2", "user", "2026-09-28"), ("s9", "user://u4", "user", "2026-09-28")]


def test_render_profile_is_deterministic_and_skips_empty_fields(profile):
    assert render_profile(None) == "" and render_profile(Profile()) == "" and render_profile(Profile(updated_at="2026")) == ""
    assert render_profile(profile, channel="linkedin") == render_profile(profile.model_copy(), channel="linkedin")
    minimal = render_profile(Profile(service_name="INSIA"), channel="naver_blog")
    assert minimal.splitlines()[-1] == "- 서비스명: INSIA" and "브랜드 규칙" not in minimal
    long = render_profile(Profile(description="설명 " * 2000), channel=None)
    assert "…(이하 생략)" in long and len(long) < 2500


def test_packaged_prompts_load_and_teach_the_context_rules():
    assert check_prompts(planner=True) == []
    orchestrator = agent_prompt("orchestrator")
    assert "회사 프로필 (사용자 제공 사실)" in orchestrator and "(자사 자료)" in orchestrator
    assert "(자사 자료: 자료 제목)" in orchestrator and "사람의 수정 지시" in orchestrator
    assert "성명·학교명·직장명은 절대 쓰지 않는다" in orchestrator
    assert "user_sources" in agent_prompt("researcher") and "medium`을 넘기지 않고" in agent_prompt("researcher")
    assert "자사 프로필" in agent_prompt("reviewer") and "어긋나는 내용은 critical" in agent_prompt("reviewer")
    planner = agent_prompt("planner")
    assert "available_days" in planner and "history" in planner and "ContentPlan" in planner


# ---------------------------------------------------------------------------
# Budget cap inside one backend call (pause_turn continuations, structure call)
# ---------------------------------------------------------------------------

from insia_agents.pipeline import BudgetExceeded, UsageMeter  # noqa: E402

_SEARCH_USE = SimpleNamespace(type="server_tool_use", id="srv_b", name="web_search", input={"query": "소상공인 통계"})


def _paid_turn(stop_reason: str = "pause_turn", content=None) -> SimpleNamespace:
    # 180k input + 2k output on Opus 5 + 10 searches ≈ $1.05 per request
    return usage_message(content if content is not None else [_SEARCH_USE], stop_reason=stop_reason,
                         input_tokens=180_000, output_tokens=2_000, searches=10)


def test_budget_cap_stops_research_before_the_next_paid_continuation(settings, prompts_dir, brief, plan, pack):
    client = FakeClient([_paid_turn() for _ in range(5)] + [_paid_turn("end_turn", [text("메모")]),
                                                              usage_message([json_text(pack)])])
    backend = AnthropicBackend(replace(settings, max_continuations=5), client=client)
    meter = UsageMeter("r1", cap=0.5)
    backend.on_usage = meter
    with pytest.raises(BudgetExceeded, match="예산 상한"):
        backend.research(brief, plan.questions, lambda t, d: None)
    assert len(client.calls) == 1  # the first turn crossed the cap; no second paid request
    assert meter.calls == 1 and meter.spent == pytest.approx(1.05, abs=0.01)


def test_budget_cap_also_guards_the_research_structuring_call(settings, prompts_dir, brief, plan, pack):
    client = FakeClient([_paid_turn("end_turn", [text("메모")]), usage_message([json_text(pack)])])
    backend = AnthropicBackend(settings, client=client)
    backend.on_usage = UsageMeter("r1", cap=0.5)
    with pytest.raises(BudgetExceeded):
        backend.research(brief, plan.questions, lambda t, d: None)
    assert len(client.calls) == 1  # the search call finished over the cap; the structure call never started


def test_a_response_that_already_arrived_is_kept_even_over_the_cap(settings, prompts_dir, brief, plan):
    client = FakeClient([usage_message([json_text(plan)], input_tokens=500_000)])  # $2.5 for one plan call
    backend = AnthropicBackend(settings, client=client)
    meter = UsageMeter("r1", cap=0.5)
    backend.on_usage = meter
    assert backend.plan(brief) == plan  # paid for: returned, not thrown away
    assert meter.exceeded()
    with pytest.raises(BudgetExceeded):  # but the next call does not start
        backend.plan(brief)
    assert len(client.calls) == 1


def test_under_the_cap_every_continuation_runs(settings, prompts_dir, brief, plan, pack):
    client = FakeClient([_paid_turn(), _paid_turn("end_turn", [text("메모")]), usage_message([json_text(pack)])])
    backend = AnthropicBackend(settings, client=client)
    backend.on_usage = UsageMeter("r1", cap=50.0)
    backend.research(brief, plan.questions, lambda t, d: None)
    assert len(client.calls) == 3


def test_a_pipeline_run_stops_right_after_the_cap_is_crossed(settings, prompts_dir, tmp_path):
    """The review's repro: cap $0.50, plan ≈ $0.01, then search turns ≈ $1.05 each."""
    from insia_agents.db import Workspace
    from insia_agents.events import EventBus, RealClock
    from insia_agents.models import Brief
    from insia_agents.pipeline import run_pipeline

    small_plan = Plan(summary="요약", key_messages=["a", "b", "c"],
                      questions=[ResearchQuestion(id="q1", question="질문", why="왜", channels=["linkedin"])],
                      outlines=[ChannelOutline(channel="linkedin", sections=["도입"])])
    client = FakeClient([usage_message([json_text(small_plan)], input_tokens=1000, output_tokens=200)]
                        + [_paid_turn() for _ in range(5)] + [_paid_turn("end_turn", [text("메모")])]
                        + [usage_message([json_text(ResearchPack(findings=[], sources=[], gaps=["없음"]))])])
    live = replace(settings, mode="live", max_cost_usd=0.5, max_continuations=5, home=tmp_path / "ws")
    workspace = Workspace(tmp_path / "ws")
    try:
        with pytest.raises(BudgetExceeded):
            run_pipeline(Brief(topic="테스트", channels=["linkedin"]), AnthropicBackend(live, client=client),
                         EventBus("run-cap", clock=RealClock()), live, workspace=workspace)
        assert len(client.calls) == 2  # plan + the one search turn that crossed the cap (was 8)
        assert workspace.run_cost("run-cap") == pytest.approx(1.06, abs=0.01)  # was $7.08
    finally:
        workspace.close()


# ---------------------------------------------------------------------------
# Refusal fallback billing through the backend (and the real SDK stream)
# ---------------------------------------------------------------------------


def test_an_unpriced_fallback_model_is_announced_and_the_declined_attempt_billed(settings, prompts_dir, brief, plan):
    served = usage_message([json_text(plan)], model="claude-future-9", input_tokens=20_000, output_tokens=5_000)
    served.usage.iterations = [
        SimpleNamespace(type="message", model="claude-opus-5", input_tokens=20_000, output_tokens=3_000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-future-9", input_tokens=20_000, output_tokens=5_000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0)]
    backend = AnthropicBackend(settings, client=FakeClient([served]))
    records, notices = [], []
    backend.on_usage = records.append
    backend.on_notice = lambda agent, level, msg: notices.append(msg)
    backend.plan(brief)
    assert records[0].cost_usd == pytest.approx((20_000 * 5 + 3_000 * 25) / 1e6)  # never silently $0
    priced = [n for n in notices if "가격 정보가 없어" in n]
    assert len(priced) == 1 and "claude-future-9" in priced[0] and "INSIA_PRICE_CLAUDE_FUTURE_9_INPUT" in priced[0]
    assert "예산 상한" in priced[0]


def _fallback_sse(plan_json: str) -> bytes:
    import json as _json

    def sse(events):
        return "".join(f"event: {n}\ndata: {_json.dumps(d, ensure_ascii=False)}\n\n" for n, d in events).encode()

    start = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
             "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 20000, "output_tokens": 1}}
    iterations = [
        {"type": "message", "model": "claude-opus-5", "input_tokens": 20000, "output_tokens": 3000,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 20000, "output_tokens": 5000,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ]
    return sse([
        ("message_start", {"type": "message_start", "message": start}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": plan_json[:10]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
            "type": "fallback", "from": {"model": "claude-opus-5"}, "to": {"model": "claude-opus-4-8"},
            "trigger": {"type": "refusal", "category": "cyber"}}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": plan_json[10:]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"input_tokens": 20000, "output_tokens": 5000, "iterations": iterations}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def test_fallback_served_response_is_billed_in_full_through_the_real_sdk(settings, prompts_dir, brief, plan, monkeypatch):
    for name in [n for n in __import__("os").environ if n.startswith("INSIA_PRICE_")]:
        monkeypatch.delenv(name)
    bodies = [_fallback_sse(plan.model_dump_json())]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_mock"},
                               content=bodies.pop(0))

    client = anthropic.Anthropic(api_key="sk-test-not-real", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    backend = AnthropicBackend(replace(settings, home=settings.out_dir / "no-prices"), client=client)
    records, notices = [], []
    backend.on_usage = records.append
    backend.on_notice = lambda agent, level, msg: notices.append(msg)
    assert backend.plan(brief).summary == plan.summary
    record = records[0]
    assert record.model == "claude-opus-4-8" and (record.input_tokens, record.output_tokens) == (40_000, 8_000)
    assert record.cost_usd == pytest.approx(0.4)  # was $0.0 (unpriced fallback model, declined attempt dropped)
    assert not any("가격 정보가 없어" in n for n in notices)
    assert any("대신 응답" in n for n in notices)


# ---------------------------------------------------------------------------
# Calendar: per-channel days and history rows
# ---------------------------------------------------------------------------


def test_plan_calendar_sends_per_channel_days_and_keeps_slots_on_them(settings, prompts_dir, profile):
    import json as _json

    (prompts_dir / "agents" / "planner.md").write_text("# planner", encoding="utf-8")
    raw = ContentPlan(summary="s", slots=[
        PlannedSlot(date="2026-10-10", channel="instagram", topic="토요일 인스타", angle="a", keywords=["k"], goal="g"),
        PlannedSlot(date="2026-10-10", channel="linkedin", topic="토요일 링크드인", angle="a", keywords=["k"], goal="g"),
        PlannedSlot(date="2026-10-06", channel="linkedin", topic="이미 찬 날", angle="a", keywords=["k"], goal="g"),
    ])
    client = FakeClient([message([json_text(raw)])])
    days = {"instagram": ["2026-10-05", "2026-10-10", "2026-10-11"], "linkedin": ["2026-10-07", "2026-10-09"]}
    plan = AnthropicBackend(settings, client=client).plan_calendar(
        profile, "주제", "2026-10-05", "2026-10-11", {"instagram": 1, "linkedin": 2}, [], days=days)
    body = client.calls[0]["messages"][0]["content"][-1]["text"]
    payload = _json.loads(body.split("```json\n", 1)[1].rsplit("```", 1)[0])
    assert payload["channel_days"] == days
    assert [d["date"] for d in payload["available_days"]] == ["2026-10-05", "2026-10-07", "2026-10-09", "2026-10-10", "2026-10-11"]
    assert "channel_days" in body.split("입력(JSON)")[0]  # the instruction names the per-channel days
    assert [(s.date, s.channel) for s in plan.slots] == [
        ("2026-10-07", "linkedin"), ("2026-10-09", "linkedin"), ("2026-10-10", "instagram")]
    empty = AnthropicBackend(settings, client=FakeClient([])).plan_calendar(
        profile, "주제", "2026-10-05", "2026-10-11", {"linkedin": 1}, [], days={"linkedin": []})
    assert empty.slots == []  # no free day: no API call at all


def test_history_rows_put_upcoming_plans_first_and_keep_room_for_published():
    from insia_agents.backends.anthropic_backend import HISTORY_IN_PROMPT, _history_rows

    published = [ContentItem(id=f"p{i}", channel="linkedin", title=f"게시 {i}", status="published",
                             published_at=f"2026-0{1 + i % 8}-{10 + i % 18}T00:00:00Z") for i in range(61)]
    approved = [ContentItem(id=f"a{i}", channel="instagram", title=f"승인 {i}", status="approved",
                            updated_at="2026-09-20T00:00:00Z") for i in range(10)]
    planned = [ContentItem(id="sl_1", channel="naver_blog", title="재고 관리 엑셀 체크리스트 10가지", status="scheduled",
                           scheduled_at="2026-10-06")]
    rows = _history_rows(published + approved + planned)
    assert len(rows) == HISTORY_IN_PROMPT
    titles = [r["title"] for r in rows]
    assert "재고 관리 엑셀 체크리스트 10가지" in titles and sum(t.startswith("승인") for t in titles) == 10
    assert sum(t.startswith("게시") for t in titles) == HISTORY_IN_PROMPT - 11
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    many_upcoming = [ContentItem(id=f"s{i}", channel="linkedin", title=f"예약 {i}", status="scheduled",
                                 scheduled_at="2026-10-01") for i in range(100)]
    rows = _history_rows(published + many_upcoming)
    assert sum(r["status"] == "published" for r in rows) == HISTORY_IN_PROMPT // 3
