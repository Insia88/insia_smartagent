"""Errors with Korean messages shared by the command line and the library helpers it uses."""

from __future__ import annotations


class UsageError(Exception):
    """Wrong use of the command line or a wrong input file (CLI exit code 2). ``str(exc)`` is Korean."""


class CommandError(Exception):
    """The command could not do its job (CLI exit code 1). ``str(exc)`` is Korean."""


def error_text(exc: BaseException) -> str:
    """Korean messages pass through; unknown exceptions keep their type name."""
    from .backends.base import BackendError
    from .db import WorkspaceError

    known: tuple[type[BaseException], ...] = (WorkspaceError, BackendError, UsageError, CommandError)
    try:
        from .exporters import ExportError
        from .pipeline import PipelineError
        from .planner import PlanningError

        known += (ExportError, PipelineError, PlanningError)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, known):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
