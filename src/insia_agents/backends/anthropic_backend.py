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

Run context (``backend.context``): the company profile goes into every
plan/draft/revise/review/plan_calendar request, and the user's documents into
the first research structuring call (as ``origin="user"`` sources with fixed
ids, text capped at ``settings.max_document_chars`` with a notice when cut).
Both travel in the *user* message: the system blocks (agent prompt + channel
guide, each with a cache breakpoint) stay byte-identical across runs and
workspaces, so they keep hitting the prompt cache; the profile block carries
its own breakpoint (reused by a channel's draft → revise and review rounds).

Usage: ``on_usage`` receives a priced ``UsageRecord`` after every API
response, ``pause_turn`` continuations and refusals included.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import unquote

from pydantic import BaseModel, ValidationError

from ..channels import CHANNELS
from ..config import Settings
from ..costs import env_key, load_prices, price_for, usage_from_response
from ..models import (Brief, ChannelId, ContentItem, ContentPlan, Draft, FormatCheck, Plan, Profile, ResearchPack,
                      ResearchQuestion, Review, Source)
from ..planner import cap_counts, normalize_counts, normalize_plan, slot_days, weekday_label
from ..prompt_loader import (PLANNER_PROMPT, agent_prompt, budget_documents, channel_guide, is_user_url,
                             render_documents, render_profile, user_sources)
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
    RunContext,
    ServerSideError,
    UsageFn,
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
    "plan_calendar": 16000,
}
HISTORY_IN_PROMPT = 60  # past items shown to the planner

CREDENTIALS_HINT = "API 자격 증명을 찾을 수 없어요. ANTHROPIC_API_KEY를 설정하거나 `ant auth login`을 실행해 주세요."

SEARCH_INSTRUCTION = (
    "지금은 조사 단계입니다. web_search와 web_fetch 도구로 아래 리서치 질문마다 근거를 찾으세요. "
    "Tier 1 원문(정부·공공기관·공식 통계)을 먼저 열고, 핵심 수치는 가능하면 두 출처로 교차 확인하세요. "
    "조사가 끝나면 한국어 조사 메모를 작성하세요. 메모에는 질문 id별로 확인한 사실(원문 값·단위·기준시점), "
    "출처(제목, URL, 발행 기관, 발행일, tier 판단과 이유), 교차 확인 결과, 찾지 못한 것을 적습니다. "
    "JSON은 다음 단계에서 만들므로 지금은 메모만 쓰세요."
)
SEARCH_USER_MATERIALS = (
    " user_materials는 사용자가 올린 회사 자료 목록이며 정리 단계에서 출처로 들어갑니다. "
    "자료에 있을 회사 내부 사실은 웹에서 다시 찾지 말고 시장·통계·정책·경쟁 같은 외부 근거에 집중하세요."
)
STRUCTURE_INSTRUCTION = (
    "검색 도구 없이, 아래 조사 메모와 검색·열람 결과만으로 ResearchPack JSON을 만드세요. "
    "여기에 없는 URL·수치·기관명은 넣지 마세요. 근거가 없는 질문은 gaps로 보내세요. "
    "웹 출처의 origin은 \"web\"입니다."
)
STRUCTURE_USER_MATERIALS = (
    " '사용자 제공 자료' 블록의 자료는 user_sources에 출처 id가 이미 정해져 있습니다(sources에 다시 쓰지 않아도 됩니다). "
    "자료에서 질문에 답하는 사실을 finding으로 뽑을 때는 그 id를 source_ids에 쓰고, 외부 검증 전인 자체 주장이므로 "
    "confidence는 medium 이하로, note에 '사용자 제공 자료(외부 검증 전)'라고 쓰세요. 새 웹 출처 id는 id_start부터 씁니다."
)
PLAN_INSTRUCTION = "브리프를 읽고 Plan JSON을 만드세요. 요청된 channels만 outlines에 넣으세요."
PLAN_CONTEXT = (
    " 회사 프로필이나 user_materials로 이미 답이 있는 회사 내부 사실은 리서치 질문으로 만들지 말고, "
    "외부 근거(시장·통계·정책·경쟁)가 필요한 질문에 집중하세요."
)
DRAFT_INSTRUCTION = "채널 가이드의 출력 형식을 그대로 따라 첫 초안(round 0) Draft JSON을 만드세요."
REVIEW_INSTRUCTION = "초안을 독립적으로 검수해 Review JSON을 만드세요. format_checks는 입력값을 그대로 넣으세요."
REVISE_INSTRUCTION = "검수 결과의 critical·major 이슈와 실패한 형식 검사를 모두 반영한 수정본 Draft JSON을 만드세요."
REVISE_HUMAN = (
    " 맨 아래 '사람의 수정 지시'를 가장 먼저 반영하고 change_log 첫 줄에 '[사람 지시] 무엇을 어떻게 고쳤는지'를 쓰세요. "
    "지시가 절대 규칙(리서치·프로필에 없는 사실, 금지 표현, 블라인드 규정)과 부딪히면 규칙을 지키고 '[미반영] 이유'를 남기세요."
)
HUMAN_INSTRUCTIONS_TITLE = "사람의 수정 지시"
CALENDAR_INSTRUCTION = (
    "회사 프로필, 주제(theme), 지난 게시물(history)을 보고 기간 안의 콘텐츠 계획 ContentPlan JSON을 만드세요. "
    "채널별 개수(counts)를 정확히 지키고, 날짜는 available_days 중에서만 고르며, 같은 채널은 하루에 한 편만 둡니다. "
    "history와 같은 주제·관점은 반복하지 마세요."
)


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


# Any run of non-space characters after the scheme (Korean paths included), up
# to quotes, angle brackets and closing CJK brackets.
_URL_RE = re.compile(r"https?://[^\s<>\"'）」』\]]+")
_ASCII_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_TRAILING = ".,;:!?)]}'。、，"


def url_key(url: str) -> str:
    """Lenient URL identity for grounding checks: ignores scheme, ``www.``,
    fragment, trailing slash, case and percent-encoding (``%EC%86%8C`` and
    ``소`` are the same URL)."""
    key = unquote(url.strip()).lower()
    key = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", key)
    key = key.removeprefix("www.").split("#", 1)[0]
    return key.rstrip("/")


def _literal_url_keys(text: str) -> set[str]:
    """Keys for URLs written in prose. Each match is added as written, without
    trailing punctuation, and cut at its first non-URL-safe character (a URL
    glued to a Korean particle, e.g. ``…/a에서``)."""
    keys: set[str] = set()
    for pattern in (_URL_RE, _ASCII_URL_RE):
        for match in pattern.findall(text or ""):
            keys.add(url_key(match))
            keys.add(url_key(match.rstrip(_TRAILING)))  # prose / markdown-link punctuation
    keys.discard("")
    return keys


def grounded_url_keys(messages: Iterable[Any]) -> set[str]:
    """Every URL the tool call actually saw: all search results (not only the
    first 80 sent to the structuring call), fetched pages and the URLs asked
    for (a redirect can change the result URL), citations, URLs in the memo
    text and in dynamic-filtering code output."""
    keys: set[str] = set()
    for message in messages:
        for block in _get(message, "content", None) or []:
            kind = _type(block)
            if kind == "text":
                keys |= _literal_url_keys(_get(block, "text", "") or "")
                for citation in _get(block, "citations", None) or []:
                    if _get(citation, "url"):
                        keys.add(url_key(_get(citation, "url")))
            elif kind == "server_tool_use" and _get(block, "name") == "web_fetch":
                requested = _get(_get(block, "input", None) or {}, "url")
                if isinstance(requested, str) and requested.strip():
                    keys.add(url_key(requested))
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


def _source_number(source_id: str) -> int:
    match = re.fullmatch(r"s(\d+)", source_id.strip().lower())
    return int(match.group(1)) if match else 0


def attach_user_sources(pack: ResearchPack, user: list[Source]) -> ResearchPack:
    """Put the canonical user-material sources into a structured pack.

    ``user`` sources have fixed ids (sent to the model as ``user_sources``);
    the model's own copy of one (same ``user://`` URL) is replaced by the
    canonical source and its findings re-pointed. A model source that reuses a
    reserved id gets a fresh id (findings follow it: they cite the model's own
    list). ``origin`` is set from the URL scheme for every source.
    """
    canonical = {s.url.strip().lower(): s for s in user}
    reserved = {s.id for s in user}
    next_free = max([_source_number(s.id) for s in [*user, *pack.sources]] + [0]) + 1
    id_map: dict[str, str] = {}
    others: list[Source] = []
    used = set(reserved)
    for src in pack.sources:
        match = canonical.get(src.url.strip().lower())
        if match is not None:
            id_map[src.id] = match.id
            continue
        new_id = src.id
        if new_id in used:
            new_id = f"s{next_free}"
            next_free += 1
            id_map[src.id] = new_id
        used.add(new_id)
        others.append(src.model_copy(update={"id": new_id, "origin": "user" if is_user_url(src.url) else "web"}))
    findings = []
    for fin in pack.findings:
        ids: list[str] = []
        for sid in fin.source_ids:
            mapped = id_map.get(sid, sid)
            if mapped not in ids:
                ids.append(mapped)
        findings.append(fin.model_copy(update={"source_ids": ids}))
    return ResearchPack(findings=findings, sources=[*user, *others], gaps=list(pack.gaps))


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def _text_block(text: str, *, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _history_rows(history: list[ContentItem]) -> list[dict[str, str]]:
    rows = []
    for item in history[:HISTORY_IN_PROMPT]:
        when = (item.published_at or item.scheduled_at or item.updated_at or item.created_at or "")[:10]
        rows.append({"date": when, "channel": item.channel, "status": item.status, "title": item.title})
    return rows


class AnthropicBackend:
    name = "live"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.model = settings.model
        self.on_notice: NoticeFn | None = None
        self.on_usage: UsageFn | None = None
        self.context = RunContext(today=settings.today)
        self._unpriced: set[str] = set()
        if client is None:
            import anthropic

            try:
                client = anthropic.Anthropic()
            except anthropic.AnthropicError as exc:
                raise ConfigError(
                    "Anthropic 클라이언트를 만들 수 없어요. ANTHROPIC_API_KEY를 설정하거나 `ant auth login`을 실행해 주세요."
                ) from exc
        self._client = client

    @property
    def today(self) -> str:
        return (self.context.today if self.context is not None else "") or self.settings.today

    # -- request construction -------------------------------------------------
    def system_blocks(self, role: str, channel: ChannelId | None = None) -> list[dict[str, Any]]:
        """Stable, cached system prompt: agent prompt (+ channel guide). Never
        contains per-run data, so it hits the cache across runs."""
        blocks: list[dict[str, Any]] = [_text_block(agent_prompt(role), cache=True)]
        if channel is not None:
            label = CHANNELS[channel].label
            blocks.append(_text_block(f"# 채널 가이드 — {label} (`{channel}`)\n\n{channel_guide(channel)}", cache=True))
        return blocks

    def profile_blocks(self, channel: ChannelId | None = None, *, profile: Profile | None = None,
                       include_names: bool | None = None, include_contact: bool = True) -> list[dict[str, Any]]:
        """The company-profile block (first user-content block, own cache
        breakpoint) or ``[]`` when there is no profile."""
        if profile is None and self.context is not None:
            profile = self.context.profile
        text = render_profile(profile, channel=channel, include_names=include_names, include_contact=include_contact)
        return [_text_block(text, cache=True)] if text else []

    def compose(self, task: str, payload: dict[str, Any], instruction: str) -> str:
        body = {"task": task, "today": self.today, **payload}
        return f"작업: {task}\n\n{instruction}\n\n입력(JSON):\n```json\n{json.dumps(body, ensure_ascii=False, indent=1)}\n```"

    def build_request(self, *, role: str, system: list[dict[str, Any]], user_text: str, max_tokens: int,
                      schema_model: type[BaseModel] | None = None, tools: list[dict[str, Any]] | None = None,
                      context: list[dict[str, Any]] | None = None, after: list[dict[str, Any]] | None = None,
                      ) -> dict[str, Any]:
        """One request. User content = ``context`` blocks (profile, documents)
        + the task text + ``after`` blocks (human instructions)."""
        output_config: dict[str, Any] = {"effort": self.settings.effort[role]}
        if schema_model is not None:
            output_config["format"] = json_format(schema_model)
        content = [*(context or []), _text_block(user_text), *(after or [])]
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
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

    def _record_usage(self, message: Any, *, agent: str, task: str) -> None:
        """Price one response and hand it to ``on_usage`` (see ``Backend``)."""
        if self.on_usage is None:
            return
        prices, web = load_prices(home=self.settings.home)
        record = usage_from_response(message, agent=agent, task=task, model=self.model,
                                     prices=prices, web_search_per_1k=web)
        if price_for(record.model, prices=prices) is None and record.model not in self._unpriced:
            self._unpriced.add(record.model)
            self._notice("system", "warn",
                         f"{record.model} 모델의 가격 정보가 없어 비용을 0달러로 기록해요. 워크스페이스의 prices.json이나 "
                         f"INSIA_PRICE_{env_key(record.model)}_INPUT·_OUTPUT 환경 변수로 가격을 넣어 주세요.")
        try:
            self.on_usage(record)
        except BackendError:
            raise
        except Exception as exc:  # a broken recorder must not fail the run
            self._notice("system", "warn", f"사용량 기록 중 오류가 나서 이번 호출 비용을 저장하지 못했어요: {exc}")

    def stream_call(self, request: dict[str, Any], *, agent: str, task: str = "",
                    on_event: Callable[[Any], None] | None = None,
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
            self._record_usage(message, agent=agent, task=task or agent)  # billed even when refused or truncated
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

    def structured_call(self, request: dict[str, Any], model_cls: type[BaseModel], *, agent: str,
                        task: str = "") -> BaseModel:
        turns = self.stream_call(request, agent=agent, task=task)
        return parse_json_output(message_text(turns), model_cls)

    # -- Backend protocol ---------------------------------------------------------
    def _documents_overview(self) -> list[dict[str, Any]]:
        docs = self.context.documents if self.context is not None else []
        return [{"doc_id": d.id, "title": d.title, "chars": len(d.text or "")} for d in docs if (d.text or "").strip()][:30]

    def plan(self, brief: Brief) -> Plan:
        payload: dict[str, Any] = {"brief": brief.model_dump(mode="json")}
        overview = self._documents_overview()
        if overview:
            payload["user_materials"] = overview
        context = self.profile_blocks(None, include_names="bizplan" not in brief.channels)
        instruction = PLAN_INSTRUCTION + (PLAN_CONTEXT if context or overview else "")
        request = self.build_request(
            role="orchestrator", system=self.system_blocks("orchestrator"),
            user_text=self.compose("plan", payload, instruction), context=context,
            schema_model=Plan, max_tokens=MAX_TOKENS["plan"],
        )
        plan = self.structured_call(request, Plan, agent="orchestrator", task="plan")
        assert isinstance(plan, Plan)
        wanted = set(brief.channels)
        return plan.model_copy(update={"outlines": [o for o in plan.outlines if o.channel in wanted]})

    def research(self, brief: Brief, questions: list[ResearchQuestion], emit: EmitFn,
                 existing: ResearchPack | None = None) -> ResearchPack:
        system = self.system_blocks("researcher")
        context = self.profile_blocks(None, include_names=False, include_contact=False)
        payload: dict[str, Any] = {"brief": brief.model_dump(mode="json"), "questions": [q.model_dump(mode="json") for q in questions]}
        next_s, next_f = next_ids(existing)
        excerpts, mine = [], []
        if existing is None:  # user materials enter the first pack only; follow-ups reuse their ids
            excerpts, notice = budget_documents(list(self.context.documents) if self.context else [],
                                                self.settings.max_document_chars)
            if notice:
                self._notice("researcher", "warn", notice)
            mine = user_sources(excerpts, next_s, self.today)
            if mine:
                payload["user_materials"] = [{"source_id": s.id, "doc_id": e.document.id, "title": s.title,
                                              "chars_sent": len(e.text)} for s, e in zip(mine, excerpts)]
        else:
            payload["followup"] = True
            payload["existing_sources"] = [{"id": s.id, "url": s.url, "title": s.title} for s in existing.sources]
        search_request = self.build_request(
            role="researcher", system=system, context=context,
            user_text=self.compose("research", payload, SEARCH_INSTRUCTION + (SEARCH_USER_MATERIALS if mine else "")),
            tools=self.research_tools(), max_tokens=MAX_TOKENS["search"],
        )
        watcher = _SearchWatcher(questions, emit)
        turns = self.stream_call(search_request, agent="researcher", task="research", on_event=watcher,
                                 after_turn=lambda m: watcher.scan(_get(m, "content", [])))

        emit("agent.status", {"status": "writing", "message": "조사 메모를 리서치 팩으로 정리하는 중이에요"})
        notes = collect_research_notes(turns)
        structure_payload: dict[str, Any] = {**payload, "notes": notes,
                                             "id_start": {"source": f"s{next_s + len(mine)}", "finding": f"f{next_f}"}}
        if mine:
            structure_payload["user_sources"] = [s.model_dump(mode="json") for s in mine]
        documents = render_documents(excerpts, mine)
        structure_request = self.build_request(
            role="researcher", system=system,
            context=[*context, *([_text_block(documents)] if documents else [])],
            user_text=self.compose("research_pack", structure_payload,
                                   STRUCTURE_INSTRUCTION + (STRUCTURE_USER_MATERIALS if mine else "")),
            schema_model=ResearchPack, max_tokens=MAX_TOKENS["structure"],
        )
        pack = self.structured_call(structure_request, ResearchPack, agent="researcher", task="research")
        assert isinstance(pack, ResearchPack)
        pack = attach_user_sources(pack, mine)
        allowed = grounded_url_keys(turns) | {url_key(s.url) for s in mine}
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
            role="orchestrator", system=self.system_blocks("orchestrator", channel), context=self.profile_blocks(channel),
            user_text=self.compose("draft", payload, DRAFT_INSTRUCTION),
            schema_model=Draft, max_tokens=self._draft_tokens(channel),
        )
        draft = self.structured_call(request, Draft, agent="orchestrator", task="draft")
        assert isinstance(draft, Draft)
        return draft.model_copy(update={"channel": channel, "round": 0})

    def review(self, brief: Brief, research: ResearchPack, draft: Draft, format_checks: list[FormatCheck]) -> Review:
        payload = {"brief": brief.model_dump(mode="json"), "research": research.model_dump(mode="json"),
                   "draft": draft.model_dump(mode="json"), "format_checks": [c.model_dump(mode="json") for c in format_checks]}
        request = self.build_request(
            role="reviewer", system=self.system_blocks("reviewer", draft.channel), context=self.profile_blocks(draft.channel),
            user_text=self.compose("review", payload, REVIEW_INSTRUCTION),
            schema_model=Review, max_tokens=MAX_TOKENS["review"],
        )
        review = self.structured_call(request, Review, agent="reviewer", task="review")
        assert isinstance(review, Review)
        return review.model_copy(update={"channel": draft.channel, "round": draft.round, "format_checks": list(format_checks)})

    def revise(self, brief: Brief, plan: Plan, research: ResearchPack, draft: Draft, review: Review,
               instructions: str = "") -> Draft:
        human = (instructions or (self.context.instructions if self.context is not None else "") or "").strip()
        payload = {"channel": draft.channel, "round": draft.round + 1, "brief": brief.model_dump(mode="json"),
                   "plan": plan.model_dump(mode="json"), "research": research.model_dump(mode="json"),
                   "draft": draft.model_dump(mode="json"), "review": review.model_dump(mode="json")}
        after = [_text_block(f"# {HUMAN_INSTRUCTIONS_TITLE}\n\n{human}")] if human else []
        request = self.build_request(
            role="orchestrator", system=self.system_blocks("orchestrator", draft.channel),
            context=self.profile_blocks(draft.channel), after=after,
            user_text=self.compose("revise", payload, REVISE_INSTRUCTION + (REVISE_HUMAN if human else "")),
            schema_model=Draft, max_tokens=self._draft_tokens(draft.channel),
        )
        revised = self.structured_call(request, Draft, agent="orchestrator", task="revise")
        assert isinstance(revised, Draft)
        return revised.model_copy(update={"channel": draft.channel, "round": draft.round + 1})

    def plan_calendar(self, profile: Profile, theme: str, start: str, end: str, counts: dict[str, int],
                      history: list[ContentItem]) -> ContentPlan:
        days = slot_days(start, end)
        capped, _ = cap_counts(normalize_counts(counts), len(days))
        payload = {
            "theme": theme.strip(),
            "start": start,
            "end": end,
            "counts": dict(capped),
            "available_days": [{"date": d, "weekday": weekday_label(d)} for d in days],
            "history": _history_rows(history),
        }
        request = self.build_request(
            role="orchestrator", system=[_text_block(agent_prompt(PLANNER_PROMPT), cache=True)],
            context=self.profile_blocks(None, profile=profile or Profile(), include_names=False),
            user_text=self.compose("plan_calendar", payload, CALENDAR_INSTRUCTION),
            schema_model=ContentPlan, max_tokens=MAX_TOKENS["plan_calendar"],
        )
        plan = self.structured_call(request, ContentPlan, agent="orchestrator", task="plan_calendar")
        assert isinstance(plan, ContentPlan)
        return normalize_plan(plan, start, end, capped)
