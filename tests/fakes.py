"""A fake ``anthropic.Anthropic`` client that records request kwargs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


def text(value: str, citations: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value, citations=citations)


def json_text(model_or_dict: Any) -> SimpleNamespace:
    data = model_or_dict.model_dump(mode="json") if hasattr(model_or_dict, "model_dump") else model_or_dict
    return text(json.dumps(data, ensure_ascii=False))


def message(content: list, stop_reason: str = "end_turn", model: str = "claude-opus-5", iterations: list | None = None,
            stop_details: Any = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model, stop_details=stop_details,
                           usage=SimpleNamespace(iterations=iterations, input_tokens=10, output_tokens=10),
                           _request_id="req_test")


class RefusedMessage:
    """A refusal whose content must not be read before stop_reason is checked."""

    stop_reason = "refusal"
    model = "claude-opus-5"
    usage = SimpleNamespace(iterations=None)
    _request_id = "req_refused"

    def __init__(self, category: str | None = "cyber") -> None:
        self.stop_details = SimpleNamespace(type="refusal", category=category, explanation="테스트 거절")

    @property
    def content(self):  # pragma: no cover - reaching this is the failure
        raise AssertionError("content was read before checking stop_reason")


class FakeStream:
    def __init__(self, final: Any, events: list | None = None) -> None:
        self.final = final
        self.events = events or []

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self) -> Any:
        return self.final


class FakeMessages:
    def __init__(self, owner: "FakeClient") -> None:
        self.owner = owner

    def stream(self, **kwargs: Any) -> FakeStream:
        self.owner.calls.append(kwargs)
        self.owner.endpoints.append(self.endpoint)
        if not self.owner.responses:
            raise AssertionError("unexpected extra API call")
        item = self.owner.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeStream):
            return item
        return FakeStream(item)

    def create(self, **kwargs: Any) -> Any:  # pragma: no cover - the backend must stream
        raise AssertionError("backend must use streaming, not messages.create")


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.endpoints: list[str] = []
        beta_messages = FakeMessages(self)
        beta_messages.endpoint = "beta.messages.stream"
        plain_messages = FakeMessages(self)
        plain_messages.endpoint = "messages.stream"
        self.beta = SimpleNamespace(messages=beta_messages)
        self.messages = plain_messages
