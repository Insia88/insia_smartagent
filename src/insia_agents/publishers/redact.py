"""Keeping credentials out of logs, error output and tracebacks (DESIGN.md 1-4, 2-3).

* ``register_secret(value)`` — remember an actual secret value (a token, a client secret, an OAuth code or state,
  an Instagram short-lived token). ``CredentialStore`` registers every secret it reads or writes; the OAuth code
  paths register codes and states as soon as they arrive.
* ``redact(text)`` — replace every registered value with ``***``, then mask the usual shapes (``Bearer …``,
  ``access_token=…``, ``client_secret=…``, ``code=…``, ``state=…`` and their JSON forms). Registered values are
  caught in any shape (a ``repr``, a traceback), the patterns catch values that were never registered.
* ``SecretFilter`` — a ``logging.Filter`` that runs ``redact`` over the message, its arguments and the formatted
  exception of each record. Every module of this package puts ``SECRET_FILTER`` on its logger; the server adds it
  to its access-log handler (``install_secret_filter``).

Nothing here ever logs a value.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from collections.abc import Iterable

MASK = "***"
MIN_SECRET_LENGTH = 6   # shorter values would mask ordinary words

_LOCK = threading.Lock()
_SECRETS: set[str] = set()
_ORDERED: tuple[str, ...] = ()

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{4,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(access_token|client_secret|refresh_token|code|state)=([^&\s\"'#<>]+)"), r"\1=" + MASK),
    (re.compile(r"(?i)([\"'](?:access_token|client_secret|refresh_token)[\"']\s*:\s*[\"'])[^\"']*([\"'])"),
     r"\1" + MASK + r"\2"),
)


def register_secret(value: str | None) -> None:
    """Remember ``value`` so that ``redact`` and ``SecretFilter`` mask it everywhere (values shorter than 6 characters
    are ignored)."""
    global _ORDERED
    text = str(value or "").strip()
    if len(text) < MIN_SECRET_LENGTH:
        return
    with _LOCK:
        if text in _SECRETS:
            return
        _SECRETS.add(text)
        _ORDERED = tuple(sorted(_SECRETS, key=len, reverse=True))


def register_secrets(values: Iterable[str | None]) -> None:
    for value in values:
        register_secret(value)


def forget_secrets() -> None:
    """Tests only: start from an empty register."""
    global _ORDERED
    with _LOCK:
        _SECRETS.clear()
        _ORDERED = ()


def redact(text: object) -> str:
    """``text`` with registered secrets and secret-looking parameters masked as ``***``."""
    out = text if isinstance(text, str) else str(text)
    for secret in _ORDERED:
        if secret in out:
            out = out.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_exception(exc: BaseException) -> str:
    """One redacted line for an exception (type and message), for error output."""
    return redact(f"{exc.__class__.__name__}: {exc}")


class SecretFilter(logging.Filter):
    """Masks secrets in every record it sees (message + args, exception text, stack info). Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string: keep the raw template
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            text = record.exc_text or "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


SECRET_FILTER = SecretFilter()


def install_secret_filter(target: logging.Logger | logging.Handler) -> None:
    """Attach ``SECRET_FILTER`` to a logger or handler (idempotent). Logger filters only see records created on
    that logger, so the server puts it on its handlers (the access log) and each publishing module on its logger."""
    if SECRET_FILTER not in target.filters:
        target.addFilter(SECRET_FILTER)


def get_logger(name: str) -> logging.Logger:
    """A module logger with the secret filter attached."""
    logger = logging.getLogger(name)
    install_secret_filter(logger)
    return logger


__all__ = ["MASK", "SECRET_FILTER", "SecretFilter", "forget_secrets", "get_logger", "install_secret_filter", "redact",
           "redact_exception", "register_secret", "register_secrets"]
