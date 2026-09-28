"""Backends: ``AnthropicBackend`` (live, Claude API) and ``MockBackend`` (offline)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    APICallError,
    Backend,
    BackendError,
    ConfigError,
    InvalidOutputError,
    OutputTruncatedError,
    RefusalError,
)

if TYPE_CHECKING:
    from ..config import Settings


def create_backend(mode: str, settings: "Settings", client: object | None = None) -> Backend:
    """Build the backend for a concrete mode ("live" or "mock")."""
    if mode == "live":
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend(settings, client=client)
    if mode == "mock":
        from .mock_backend import MockBackend

        return MockBackend(settings)
    raise ConfigError(f"알 수 없는 모드예요: {mode!r}")


__all__ = [
    "APICallError",
    "Backend",
    "BackendError",
    "ConfigError",
    "InvalidOutputError",
    "OutputTruncatedError",
    "RefusalError",
    "create_backend",
]
