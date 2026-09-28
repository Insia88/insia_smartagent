"""Live backend: Claude API via the official ``anthropic`` SDK (1.x).

Request shape (every call):

- ``anthropic.Anthropic()`` zero-arg client (credentials from the environment
  or an ``ant auth login`` profile; keys are never hardcoded),
- ``model`` = ``Settings.model`` (default ``claude-opus-5``, env ``INSIA_MODEL``),
- ``thinking={"type": "adaptive"}``, ``output_config.effort`` per role,
- streaming with ``.get_final_message()`` (no idle-connection timeouts on long
  drafts), a generous ``max_tokens`` (it caps thinking + answer together),
- server-side refusal fallback opted in by default: ``client.beta.messages.stream``
  with ``betas=["server-side-fallback-2026-07-01"]`` and ``fallbacks="default"``
  (``INSIA_FALLBACKS=0`` switches to ``client.messages.stream`` without both),
- structured outputs via ``output_config.format`` built from the pydantic
  models (``schema.json_format``), prompt caching on the stable system prompt,
- ``stop_reason == "refusal"`` is checked before any content is read;
  ``pause_turn`` (server tools hit their iteration limit) is resumed by
  re-sending the paused assistant turn, with no extra user message; no prefill.

The researcher runs two calls: a tool call with ``web_search_20260209`` +
``web_fetch_20260209`` (no ``code_execution``: dynamic filtering is built in)
that ends in a research memo, then a tool-less structured call that converts
the memo and the collected search results into a ``ResearchPack``. The split
exists because structured outputs cannot be combined with citations, which
web-search answers carry. Sources in that pack whose URL no search, fetch or
memo produced are dropped (the second call has no tools to verify them).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ValidationError

from ..channels import CHANNELS
from ..config import Settings
from ..models import Brief, ChannelId, Draft, FormatCheck, Plan, ResearchPack, ResearchQuestion, Review
from ..prompt_loader import agent_prompt, channel_guide
from ..schema import json_format
from .base import (
    APICallError,
    APIConnectionFailed,
    AuthError,
    BackendError,
    ConfigError,
    EmitFn,
    InvalidOutputError,
    NoticeFn,
    OutputTruncatedError,
    RateLimitedError,
    RefusalError,
    RequestRejectedError,
    ServerSideError,
    next_ids,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"

MAX_TOKENS = {
    "plan": 16000,
    "search": 32000,
    "structure": 32000,
    "draft": 32000,
    "draft_bizplan": 64000,
    "review": 32000,
}

CREDENTIALS_HINT = "API 자격 증명을 찾을 수 없어요. ANTHROPIC_API_KEY를 설정하거나 `ant auth login`을 실행해 주세요."

SEARCH_INSTRUCTION = (
    "지금은 조사 단계입니다. web_search와 web_fetch 도구로 아래 리서치 질문마다 근거를 찾으세요. "
    "Tier 1 원문(정부·공공기관·공식 통계)을 먼저 열고, 핵심 수치는 가능하면 두 출처로 교차 확인하세요. "
    "조사가 끝나면 한국어 조사 메모를 작성하세요. 메모에는 질문 id별로 확인한 사실(원문 값·단위·기준시점), "
    "출처(제목, URL, 발행 기관, 발행일, tier 판단과 이유), 교차 확인 결과, 찾지 못한 것을 적습니다. "
    "JSON은 다음 단계에서 만들므로 지금은 메모만 쓰세요."
)
STRUCTURE_INSTRUCTION = (
    "검색 도구 없이, 아래 조사 메모와 검색·열람 결과만으로 ResearchPack JSON을 만드세요. "
    "여기에 없는 URL·수치·기관명은 넣지 마세요. 근거가 없는 질문은 gaps로 보내세요."
)
PLAN_INSTRUCTION = "브리프를 읽고 Plan JSON을 만드세요. 요청된 channels만 outlines에 넣으세요."
DRAFT_INSTRUCTION = "채널 가이드의 출력 형식을 그대로 따라 첫 초안(round 0) Draft JSON을 만드세요."
REVIEW_INSTRUCTION = "초안을 독립적으로 검수해 Review JSON을 만드세요. format_checks는 입력값을 그대로 넣으세요."
REVISE_INSTRUCTION = "검수 결과의 critical·major 이슈와 실패한 형식 검사를 모두 반영한 수정본 Draft JSON을 만드세요."


# ---------------------------------------------------------------------------
# Helpers (module-level so tests can exercise them directly)
# ---------------------------------------------------------------------------


def _type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type", ""))
    return str(getattr(block, "type", ""))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def echo_content(content: Iterable[Any]) -> list[Any]:
    """Assistant content to send back when resuming a ``pause_turn``.

    After a server-side fallback (a ``fallback`` block in ``content``), blocks
    before the last boundary are filtered as the fallback docs require: keep
    text and *paired* server-tool blocks (a ``server_tool_use`` together with
    its ``*_tool_result`` — web search/fetch, or the code execution that
    dynamic filtering runs), drop thinking / client tool_use / unpaired
    server-tool blocks / unknown internal blocks. A trailing text block is
    right-stripped (continuations reject trailing whitespace).
    """
    blocks = list(content)
    boundary = max((i for i, b in enumerate(blocks) if _type(b) == "fallback"), default=-1)
    # Server-executed uses end in "_tool_use" (server_tool_use, mcp_tool_use);
    # the client "tool_use" does not. Assistant content never holds client
    # tool_result blocks, so every "*_tool_result" here is a server-tool result.
    use_ids = {_get(b, "id") for b in blocks if _type(b).endswith("_tool_use")}
    result_ids = {_get(b, "tool_use_id") for b in blocks if _type(b).endswith("_tool_result")}
    paired = (use_ids & result_ids) - {None}
    out: list[Any] = []
    for i, block in enumerate(blocks):
        kind = _type(block)
        if kind == "fallback":
            continue
        if i < boundary and kind != "text":
            if kind.endswith("_tool_use"):
                if _get(block, "id") not in paired:
                    continue
            elif kind.endswith("_tool_result"):
                if _get(block, "tool_use_id") not in paired:
                    continue
            else:
                continue
        out.append(block)
    if out and _type(out[-1]) == "text":
        text = _get(out[-1], "text", "") or ""
        if text != text.rstrip():
            out[-1] = {"type": "text", "text": text.rstrip()}
    return out


def message_text(messages: Iterable[Any]) -> str:
    """Concatenate every text block. After a mid-stream fallback the fallback
    model continues the partial text, so joining all text blocks in order
    yields the complete answer."""
    parts: list[str] = []
    for message in messages:
        for block in _get(message, "content", None) or []:
            if _type(block) == "text":
                parts.append(_get(block, "text", "") or "")
    return "".join(parts)


def parse_json_output(text: str, model_cls: type[BaseModel]) -> BaseModel:
    raw = text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise InvalidOutputError(f"{model_cls.__name__} JSON을 읽을 수 없어요", raw=raw[:2000]) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise InvalidOutputError(f"{model_cls.__name__} JSON을 읽을 수 없어요: {exc}", raw=raw[:2000]) from exc
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise InvalidOutputError(f"{model_cls.__name__} 형식이 스키마와 달라요: {exc.error_count()}개 오류", raw=raw[:2000]) from exc


def map_api_error(exc: Exception, model: str) -> BackendError:
    """Translate an ``anthropic`` SDK exception into a typed ``BackendError``."""
    import anthropic

    rid = getattr(exc, "request_id", None)
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "message", None) or str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return AuthError("API 키가 올바르지 않아요 (401). ANTHROPIC_API_KEY를 확인해 주세요.", kind="auth", status_code=status, request_id=rid)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AuthError(f"이 API 키로는 {model} 모델을 쓸 권한이 없어요 (403).", kind="permission", status_code=status, request_id=rid)
    if isinstance(exc, anthropic.NotFoundError):
        return RequestRejectedError(f"모델 또는 엔드포인트를 찾을 수 없어요 (404). INSIA_MODEL={model} 값을 확인해 주세요.", kind="not_found", status_code=status, request_id=rid)
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitedError("요청 한도를 넘었어요 (429). 잠시 후 다시 실행해 주세요.", kind="rate_limit", status_code=status, request_id=rid, retryable=True)
    if isinstance(exc, anthropic.BadRequestError):
        return RequestRejectedError(f"API가 요청을 거부했어요 (400): {detail}", kind="bad_request", status_code=status, request_id=rid)
    if isinstance(exc, anthropic.APIStatusError):
        # Classify by the error body's type first: an SSE `error` event that
        # arrives after HTTP 200 (e.g. a mid-stream overloaded_error) surfaces
        # as a bare APIStatusError whose status_code is still 200.
        etype = getattr(exc, "type", None)
        code = f" ({status})" if status is not None and status >= 400 else ""
        if etype == "rate_limit_error":
            return RateLimitedError(f"요청 한도를 넘었어요{code}. 잠시 후 다시 실행해 주세요.", kind="rate_limit", status_code=status, request_id=rid, retryable=True)
        if etype == "overloaded_error":
            return ServerSideError(f"Anthropic 서버가 지금 혼잡해요{code}. 잠시 후 다시 실행해 주세요.", kind="server", status_code=status, request_id=rid, retryable=True)
        if etype in ("api_error", "timeout_error") or (status is not None and status >= 500):
            return ServerSideError(f"Anthropic 서버 오류예요{code}. 잠시 후 다시 실행해 주세요.", kind="server", status_code=status, request_id=rid, retryable=True)
        return RequestRejectedError(f"API 오류 ({status}): {detail}", kind="status", status_code=status, request_id=rid)
    if isinstance(exc, anthropic.APITimeoutError):
        return APIConnectionFailed("API 응답 시간이 초과됐어요. 네트워크를 확인하고 다시 실행해 주세요.", kind="timeout", retryable=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return APIConnectionFailed("Anthropic API에 연결할 수 없어요. 네트워크·프록시 설정을 확인해 주세요.", kind="connection", retryable=True)
    credentials_error = getattr(anthropic, "CredentialsError", None)  # exported from SDK 1.5 on
    if credentials_error is not None and isinstance(exc, credentials_error):
        return AuthError(CREDENTIALS_HINT, kind="credentials")
    if credentials_error is None and type(exc) is anthropic.AnthropicError and _looks_like_credentials(detail):
        # SDK 1.0-1.4: the credential providers raise a plain AnthropicError.
        return AuthError(f"{CREDENTIALS_HINT} ({detail})", kind="credentials")
    return APICallError(f"API 호출 중 오류가 났어요: {detail}", kind="unknown", status_code=status, request_id=rid)


def _looks_like_credentials(detail: str) -> bool:
    text = detail.lower()
    return any(word in text for word in ("credential", "config file", "identity token"))


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[\s·,./()\[\]\"'“”‘’:;!?]+", text.lower()) if len(w) >= 2}


def match_question(query: str, questions: list[ResearchQuestion]) -> str:
    """Best-effort mapping of a model-written search query to a question id."""
    if not questions:
        return ""
    q_tokens = _tokens(query)
    best, best_score = questions[0].id, 0.0
    for question in questions:
        tokens = _tokens(question.question)
        score = float(len(q_tokens & tokens))
        if score == 0:  # Korean compounds: fall back to 2-char overlap
            grams = {query[i:i + 2] for i in range(len(query) - 1) if " " not in query[i:i + 2]}
            qgrams = {question.question[i:i + 2] for i in range(len(question.question) - 1)}
            score = len(grams & qgrams) / 10.0
        if score > best_score:
            best, best_score = question.id, score
    return best


class _SearchWatcher:
    """Turns server-tool activity into ``research.query`` / reading events."""

    def __init__(self, questions: list[ResearchQuestion], emit: EmitFn) -> None:
        self.questions = questions
        self.emit = emit
        self.seen: set[str] = set()

    def __call__(self, event: Any) -> None:
        if _type(event) == "content_block_stop":
            block = _get(event, "content_block")
            if block is not None:
                self._block(block)

    def scan(self, content: Iterable[Any]) -> None:
        for block in content or []:
            self._block(block)

    def _block(self, block: Any) -> None:
        if _type(block) != "server_tool_use":
            return
        block_id = _get(block, "id") or ""
        if block_id and block_id in self.seen:
            return
        self.seen.add(block_id)
        name = _get(block, "name", "")
        payload = _get(block, "input", None) or {}
        if name == "web_search":
            query = str(_get(payload, "query", "") or "").strip()
            if query:
                self.emit("research.query", {"question_id": match_question(query, self.questions), "query": query})
        elif name == "web_fetch":
            url = str(_get(payload, "url", "") or "")
            short = re.sub(r"^https?://(www\.)?", "", url)[:60]
            self.emit("agent.status", {"status": "reading", "message": f"원문을 읽는 중이에요: {short}" if short else "원문을 읽는 중이에요"})


def collect_research_notes(messages: Iterable[Any]) -> dict[str, Any]:
    """Memo text + every search result / fetched page seen during the tool call."""
    memo: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    fetched: dict[str, dict[str, Any]] = {}
    cited: dict[str, str] = {}
    errors: list[str] = []
    for message in messages:
        for block in _get(message, "content", None) or []:
            kind = _type(block)
            if kind == "text":
                memo.append(_get(block, "text", "") or "")
                for citation in _get(block, "citations", None) or []:
                    url = _get(citation, "url")
                    if url:
                        cited[url] = _get(citation, "title") or ""
            elif kind == "web_search_tool_result":
                content = _get(block, "content")
                if isinstance(content, list):
                    for item in content:
                        url = _get(item, "url")
                        if url and url not in results:
                            results[url] = {"url": url, "title": _get(item, "title") or "", "page_age": _get(item, "page_age") or ""}
                else:
                    errors.append(f"web_search 오류: {_get(content, 'error_code', 'unknown')}")
            elif kind == "web_fetch_tool_result":
                content = _get(block, "content")
                if _type(content) == "web_fetch_result":
                    url = _get(content, "url")
                    document = _get(content, "content")
                    if url:
                        fetched[url] = {"url": url, "title": (_get(document, "title") if document is not None else "") or "",
                                        "retrieved_at": _get(content, "retrieved_at") or ""}
                else:
                    errors.append(f"web_fetch 오류: {_get(content, 'error_code', 'unknown')}")
    return {
        "memo": "".join(memo).strip(),
        "cited_urls": [{"url": u, "title": t} for u, t in cited.items()],
        "fetched_pages": list(fetched.values()),
        "search_results": list(results.values())[:80],
        "tool_errors": errors,
    }


_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def url_key(url: str) -> str:
    """Lenient URL identity for grounding checks: ignores scheme, ``www.``,
    fragment, trailing slash and case."""
    key = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", url.strip().lower())
    key = key.removeprefix("www.").split("#", 1)[0]
    return key.rstrip("/")


def _literal_url_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in _URL_RE.findall(text or ""):
        keys.add(url_key(match))
        keys.add(url_key(match.rstrip(".,;:!?)]}'")))  # prose / markdown-link punctuation
    return keys


def grounded_url_keys(messages: Iterable[Any]) -> set[str]:
    """Every URL the tool call actually saw: all search results (not only the
    first 80 sent to the structuring call), fetched pages, citations, URLs in
    the memo text and in dynamic-filtering code output."""
    keys: set[str] = set()
    for message in messages:
        for block in _get(message, "content", None) or []:
            kind = _type(block)
            if kind == "text":
                keys |= _literal_url_keys(_get(block, "text", "") or "")
                for citation in _get(block, "citations", None) or []:
                    if _get(citation, "url"):
                        keys.add(url_key(_get(citation, "url")))
            elif kind == "web_search_tool_result":
                content = _get(block, "content")
                for item in content if isinstance(content, list) else []:
                    if _get(item, "url"):
                        keys.add(url_key(_get(item, "url")))
            elif kind == "web_fetch_tool_result":
                content = _get(block, "content")
                if _type(content) == "web_fetch_result" and _get(content, "url"):
                    keys.add(url_key(_get(content, "url")))
            elif kind.endswith("_tool_result"):  # e.g. code_execution run by dynamic filtering
                content = _get(block, "content")
                stdout = _get(content, "stdout", "") if content is not None else ""
                if isinstance(stdout, str):
                    keys |= _literal_url_keys(stdout)
    keys.discard("")
    return keys


def drop_ungrounded_sources(pack: ResearchPack, allowed: set[str]) -> tuple[ResearchPack, list[str]]:
    """Remove sources whose URL no search/fetch produced (the structuring call
    has no tools, so such a URL is invented). Findings lose those source ids;
    ``merge_research`` then moves findings left without a source to ``gaps``."""
    kept = [s for s in pack.sources if url_key(s.url) in allowed]
    if len(kept) == len(pack.sources):
        return pack, []
    dropped = [s for s in pack.sources if url_key(s.url) not in allowed]
    dropped_ids = {s.id for s in dropped}
    findings = [f.model_copy(update={"source_ids": [sid for sid in f.source_ids if sid not in dropped_ids]})
                for f in pack.findings]
    return pack.model_copy(update={"sources": kept, "findings": findings}), [s.url for s in dropped]


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class AnthropicBackend:
    name = "live"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.model = settings.model
        self.on_notice: NoticeFn | None = None
        if client is None:
            import anthropic

            try:
                client = anthropic.Anthropic()
            except anthropic.AnthropicError as exc:
                raise ConfigError(
                    "Anthropic 클라이언트를 만들 수 없어요. ANTHROPIC_API_KEY를 설정하거나 `ant auth login`을 실행해 주세요."
                ) from exc
        self._client = client

    # -- request construction -------------------------------------------------
    def system_blocks(self, role: str, channel: ChannelId | None = None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": agent_prompt(role), "cache_control": {"type": "ephemeral"}}]
        if channel is not None:
            label = CHANNELS[channel].label
            blocks.append({"type": "text", "text": f"# 채널 가이드 — {label} (`{channel}`)\n\n{channel_guide(channel)}",
                           "cache_control": {"type": "ephemeral"}})
        return blocks

    def compose(self, task: str, payload: dict[str, Any], instruction: str) -> str:
        body = {"task": task, "today": self.settings.today, **payload}
        return f"작업: {task}\n\n{instruction}\n\n입력(JSON):\n```json\n{json.dumps(body, ensure_ascii=False, indent=1)}\n```"

    def build_request(self, *, role: str, system: list[dict[str, Any]], user_text: str, max_tokens: int,
                      schema_model: type[BaseModel] | None = None, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self.settings.effort[role]}
        if schema_model is not None:
            output_config["format"] = json_format(schema_model)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if tools:
            request["tools"] = tools
        if self.settings.fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def research_tools(self) -> list[dict[str, Any]]:
        return [
            {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": self.settings.web_search_max_uses},
            {"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": self.settings.web_fetch_max_uses},
        ]

    # -- transport --------------------------------------------------------------
    def _notice(self, agent: str, level: str, message: str) -> None:
        if self.on_notice is not None:
            try:
                self.on_notice(agent, level, message)
            except Exception:
                pass

    def stream_call(self, request: dict[str, Any], *, agent: str, on_event: Callable[[Any], None] | None = None,
                    after_turn: Callable[[Any], None] | None = None) -> list[Any]:
        """Run one logical call; returns every assistant turn (``pause_turn`` resumes included)."""
        import anthropic
        import httpx2  # the SDK's HTTP transport (a dependency of every anthropic 1.x)

        stream_fn = self._client.beta.messages.stream if self.settings.fallbacks else self._client.messages.stream
        messages = list(request["messages"])
        turns: list[Any] = []
        for _ in range(self.settings.max_continuations + 1):
            try:
                with stream_fn(**{**request, "messages": messages}) as stream:
                    for event in stream:
                        if on_event is not None:
                            on_event(event)
                    message = stream.get_final_message()
            except anthropic.AnthropicError as exc:
                raise map_api_error(exc, self.model) from exc
            except httpx2.RequestError as exc:
                # The SDK wraps transport errors only before the response starts;
                # a drop or timeout while the SSE body is being read escapes raw.
                if isinstance(exc, httpx2.TimeoutException):
                    raise APIConnectionFailed("응답을 받는 도중 시간이 초과됐어요. 네트워크를 확인하고 다시 실행해 주세요.",
                                              kind="timeout", retryable=True) from exc
                raise APIConnectionFailed("응답을 받는 도중 Anthropic API 연결이 끊겼어요. 네트워크·프록시를 확인하고 다시 실행해 주세요.",
                                          kind="connection", retryable=True) from exc
            except TypeError as exc:
                if "authentication" in str(exc).lower():  # the SDK raises this when no credentials resolve
                    raise AuthError(CREDENTIALS_HINT, kind="credentials") from exc
                raise
            turns.append(message)
            stop = _get(message, "stop_reason")
            if stop == "refusal":  # check before reading any content
                details = _get(message, "stop_details")
                category = _get(details, "category") if details is not None else None
                explanation = _get(details, "explanation") if details is not None else None
                why = f" (분류: {category})" if category else ""
                raise RefusalError(
                    f"모델이 요청을 거절했어요{why}. 브리프 표현을 바꿔 다시 시도해 주세요.",
                    category=category, explanation=explanation, request_id=_get(message, "_request_id"),
                )
            self._check_fallback(message, agent)
            if after_turn is not None:
                after_turn(message)
            if stop == "pause_turn":
                messages = [*messages, {"role": "assistant", "content": echo_content(_get(message, "content", []))}]
                continue
            if stop == "max_tokens":
                raise OutputTruncatedError(f"응답이 max_tokens({request['max_tokens']:,})에서 잘렸어요.")
            return turns
        raise BackendError(f"서버 도구가 {self.settings.max_continuations}번 이어 붙인 뒤에도 끝나지 않았어요 (pause_turn).")

    def _check_fallback(self, message: Any, agent: str) -> None:
        usage = _get(message, "usage")
        iterations = _get(usage, "iterations", None) if usage is not None else None
        if any(_type(it) == "fallback_message" for it in (iterations or [])):
            self._notice(agent, "warn", f"안전 분류기가 요청을 거절해 {_get(message, 'model', '대체 모델')} 모델이 대신 응답했어요")

    def structured_call(self, request: dict[str, Any], model_cls: type[BaseModel], *, agent: str) -> BaseModel:
        turns = self.stream_call(request, agent=agent)
        return parse_json_output(message_text(turns), model_cls)

    # -- Backend protocol ---------------------------------------------------------
    def plan(self, brief: Brief) -> Plan:
        request = self.build_request(
            role="orchestrator", system=self.system_blocks("orchestrator"),
            user_text=self.compose("plan", {"brief": brief.model_dump(mode="json")}, PLAN_INSTRUCTION),
            schema_model=Plan, max_tokens=MAX_TOKENS["plan"],
        )
        plan = self.structured_call(request, Plan, agent="orchestrator")
        assert isinstance(plan, Plan)
        wanted = set(brief.channels)
        return plan.model_copy(update={"outlines": [o for o in plan.outlines if o.channel in wanted]})

    def research(self, brief: Brief, questions: list[ResearchQuestion], emit: EmitFn,
                 existing: ResearchPack | None = None) -> ResearchPack:
        system = self.system_blocks("researcher")
        payload: dict[str, Any] = {"brief": brief.model_dump(mode="json"), "questions": [q.model_dump(mode="json") for q in questions]}
        if existing is not None:
            payload["followup"] = True
            payload["existing_sources"] = [{"id": s.id, "url": s.url, "title": s.title} for s in existing.sources]
        search_request = self.build_request(
            role="researcher", system=system, user_text=self.compose("research", payload, SEARCH_INSTRUCTION),
            tools=self.research_tools(), max_tokens=MAX_TOKENS["search"],
        )
        watcher = _SearchWatcher(questions, emit)
        turns = self.stream_call(search_request, agent="researcher", on_event=watcher,
                                 after_turn=lambda m: watcher.scan(_get(m, "content", [])))

        emit("agent.status", {"status": "writing", "message": "조사 메모를 리서치 팩으로 정리하는 중이에요"})
        next_s, next_f = next_ids(existing)
        notes = collect_research_notes(turns)
        structure_payload = {**payload, "notes": notes, "id_start": {"source": f"s{next_s}", "finding": f"f{next_f}"}}
        structure_request = self.build_request(
            role="researcher", system=system, user_text=self.compose("research_pack", structure_payload, STRUCTURE_INSTRUCTION),
            schema_model=ResearchPack, max_tokens=MAX_TOKENS["structure"],
        )
        pack = self.structured_call(structure_request, ResearchPack, agent="researcher")
        assert isinstance(pack, ResearchPack)
        allowed = grounded_url_keys(turns)
        if existing is not None:
            allowed |= {url_key(s.url) for s in existing.sources}
        pack, dropped = drop_ungrounded_sources(pack, allowed)
        if dropped:
            shown = ", ".join(dropped[:3]) + (f" 외 {len(dropped) - 3}개" if len(dropped) > 3 else "")
            self._notice("researcher", "warn", f"검색·열람 결과에서 확인되지 않은 출처 {len(dropped)}개를 리서치 팩에서 뺐어요: {shown}")
        return pack

    def _draft_tokens(self, channel: ChannelId) -> int:
        return MAX_TOKENS["draft_bizplan"] if channel == "bizplan" else MAX_TOKENS["draft"]

    def draft(self, brief: Brief, plan: Plan, research: ResearchPack, channel: ChannelId) -> Draft:
        payload = {"channel": channel, "round": 0, "brief": brief.model_dump(mode="json"),
                   "plan": plan.model_dump(mode="json"), "research": research.model_dump(mode="json")}
        request = self.build_request(
            role="orchestrator", system=self.system_blocks("orchestrator", channel),
            user_text=self.compose("draft", payload, DRAFT_INSTRUCTION),
            schema_model=Draft, max_tokens=self._draft_tokens(channel),
        )
        draft = self.structured_call(request, Draft, agent="orchestrator")
        assert isinstance(draft, Draft)
        return draft.model_copy(update={"channel": channel, "round": 0})

    def review(self, brief: Brief, research: ResearchPack, draft: Draft, format_checks: list[FormatCheck]) -> Review:
        payload = {"brief": brief.model_dump(mode="json"), "research": research.model_dump(mode="json"),
                   "draft": draft.model_dump(mode="json"), "format_checks": [c.model_dump(mode="json") for c in format_checks]}
        request = self.build_request(
            role="reviewer", system=self.system_blocks("reviewer", draft.channel),
            user_text=self.compose("review", payload, REVIEW_INSTRUCTION),
            schema_model=Review, max_tokens=MAX_TOKENS["review"],
        )
        review = self.structured_call(request, Review, agent="reviewer")
        assert isinstance(review, Review)
        return review.model_copy(update={"channel": draft.channel, "round": draft.round, "format_checks": list(format_checks)})

    def revise(self, brief: Brief, plan: Plan, research: ResearchPack, draft: Draft, review: Review) -> Draft:
        payload = {"channel": draft.channel, "round": draft.round + 1, "brief": brief.model_dump(mode="json"),
                   "plan": plan.model_dump(mode="json"), "research": research.model_dump(mode="json"),
                   "draft": draft.model_dump(mode="json"), "review": review.model_dump(mode="json")}
        request = self.build_request(
            role="orchestrator", system=self.system_blocks("orchestrator", draft.channel),
            user_text=self.compose("revise", payload, REVISE_INSTRUCTION),
            schema_model=Draft, max_tokens=self._draft_tokens(draft.channel),
        )
        revised = self.structured_call(request, Draft, agent="orchestrator")
        assert isinstance(revised, Draft)
        return revised.model_copy(update={"channel": draft.channel, "round": draft.round + 1})
