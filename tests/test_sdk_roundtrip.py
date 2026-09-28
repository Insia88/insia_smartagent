"""The live backend against the real ``anthropic`` SDK with a mocked HTTP transport.

No network: an ``httpx2.MockTransport`` answers ``POST /v1/messages`` with a
server-sent-event stream. This proves the installed SDK accepts every request
parameter we send (betas, fallbacks, output_config, thinking, tools, cache
control) and that streaming + ``get_final_message()`` parse the response.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from insia_agents.backends.anthropic_backend import AnthropicBackend
from insia_agents.backends.base import APIConnectionFailed, RateLimitedError, RefusalError, ServerSideError
from insia_agents.models import ChannelOutline, Plan, ResearchQuestion


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events).encode("utf-8")


def _text_stream(text: str, stop_reason: str = "end_turn") -> bytes:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}
    return _sse([
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 10}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _refusal_stream() -> bytes:
    message = {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 0}}
    return _sse([
        ("message_start", {"type": "message_start", "message": message}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "refusal", "stop_sequence": None,
                                     "stop_details": {"type": "refusal", "category": "cyber", "explanation": "테스트"}},
                           "usage": {"output_tokens": 0}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _client(bodies: list[bytes], seen: list[httpx2.Request]) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_mock"},
                               content=bodies.pop(0))

    return anthropic.Anthropic(api_key="sk-test-not-real", max_retries=0,
                               http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


@pytest.fixture
def plan() -> Plan:
    return Plan(summary="요약", key_messages=["a", "b", "c"],
                questions=[ResearchQuestion(id="q1", question="질문", why="이유", channels=["linkedin"])],
                outlines=[ChannelOutline(channel="linkedin", sections=["훅"])])


def test_plan_through_real_sdk(settings, prompts_dir, brief, plan):
    seen: list[httpx2.Request] = []
    backend = AnthropicBackend(settings, client=_client([_text_stream(plan.model_dump_json())], seen))
    result = backend.plan(brief.model_copy(update={"channels": ["linkedin"]}))
    assert result == plan
    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in request.headers.get("anthropic-beta", "")
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5" and body["stream"] is True
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["role"] == "user"
    assert "betas" not in body  # sent as a header, not in the body


def test_research_tools_through_real_sdk(settings, prompts_dir, brief, plan):
    seen: list[httpx2.Request] = []
    pack = {"findings": [], "sources": [], "gaps": ["근거 없음"]}
    backend = AnthropicBackend(settings, client=_client([_text_stream("조사 메모"), _text_stream(json.dumps(pack))], seen))
    result = backend.research(brief, plan.questions, lambda t, d: None)
    assert result.gaps == ["근거 없음"]
    first, second = (json.loads(r.content) for r in seen)
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert "format" not in first["output_config"]
    assert "tools" not in second and second["output_config"]["format"]["type"] == "json_schema"


def test_plain_endpoint_without_fallbacks(settings, prompts_dir, brief, plan):
    from dataclasses import replace

    seen: list[httpx2.Request] = []
    backend = AnthropicBackend(replace(settings, fallbacks=False), client=_client([_text_stream(plan.model_dump_json())], seen))
    backend.plan(brief)
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body
    assert "server-side-fallback" not in seen[0].headers.get("anthropic-beta", "")


def test_refusal_through_real_sdk(settings, prompts_dir, brief):
    seen: list[httpx2.Request] = []
    backend = AnthropicBackend(settings, client=_client([_refusal_stream()], seen))
    with pytest.raises(RefusalError) as info:
        backend.plan(brief)
    assert info.value.category == "cyber"


def _server_tool_pause_stream() -> bytes:
    message = {"id": "msg_3", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}
    query = json.dumps({"query": "소상공인 실태조사 2025"}, ensure_ascii=False)
    return _sse([
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "검색할게요."}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
            "type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": query}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {
            "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://kosis.kr/a", "title": "실태조사", "encrypted_content": "xyz",
                 "page_age": "2025-01-01"}]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "pause_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 10}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def test_pause_turn_resume_through_real_sdk(settings, prompts_dir, brief, plan):
    seen: list[httpx2.Request] = []
    pack = {"findings": [], "sources": [], "gaps": []}
    bodies = [_server_tool_pause_stream(), _text_stream("메모 끝"), _text_stream(json.dumps(pack))]
    emitted = []
    backend = AnthropicBackend(settings, client=_client(bodies, seen))
    backend.research(brief, plan.questions, lambda t, d: emitted.append((t, d)))
    assert len(seen) == 3
    resumed = json.loads(seen[1].content)
    assert [m["role"] for m in resumed["messages"]] == ["user", "assistant"]
    blocks = resumed["messages"][1]["content"]
    assert [b["type"] for b in blocks] == ["text", "server_tool_use", "web_search_tool_result"]
    assert blocks[1]["id"] == "srvtoolu_1" and blocks[1]["input"] == {"query": "소상공인 실태조사 2025"}
    assert blocks[2]["content"][0]["encrypted_content"] == "xyz"
    assert ("research.query", {"question_id": "q1", "query": "소상공인 실태조사 2025"}) in emitted
    structure = json.loads(seen[2].content)["messages"][0]["content"]
    assert "https://kosis.kr/a" in structure


def _partial_events() -> list[tuple[str, dict]]:
    message = {"id": "msg_4", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}
    return [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"summary"'}}),
    ]


@pytest.mark.parametrize("error, kind", [
    (httpx2.RemoteProtocolError("peer closed connection without sending complete message body"), "connection"),
    (httpx2.ReadTimeout("The read operation timed out"), "timeout"),
])
def test_mid_stream_transport_error_is_typed(settings, prompts_dir, brief, error, kind):
    """A drop after the SSE body started is not wrapped by the SDK; it must still
    become a BackendError so the pipeline keeps the channel's earlier rounds."""
    head = _sse(_partial_events())

    class DroppingBody(httpx2.SyncByteStream):
        def __iter__(self):
            yield head
            raise error

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=DroppingBody())

    client = anthropic.Anthropic(api_key="sk-test-not-real", max_retries=0,
                                 http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(APIConnectionFailed) as info:
        AnthropicBackend(settings, client=client).plan(brief)
    assert info.value.kind == kind and info.value.retryable
    assert info.value.__cause__ is error
    assert "도중" in str(info.value)


@pytest.mark.parametrize("etype, expected", [
    ("overloaded_error", ServerSideError),
    ("api_error", ServerSideError),
    ("rate_limit_error", RateLimitedError),
])
def test_mid_stream_error_event_is_classified_by_type(settings, prompts_dir, brief, etype, expected):
    """An SSE `error` event after HTTP 200 arrives as APIStatusError(status 200)."""
    body = _sse(_partial_events() + [("error", {"type": "error", "error": {"type": etype, "message": "boom"}})])
    backend = AnthropicBackend(settings, client=_client([body], []))
    with pytest.raises(expected) as info:
        backend.plan(brief)
    assert info.value.retryable
    assert "(200)" not in str(info.value) and "잠시 후 다시 실행해 주세요" in str(info.value)
