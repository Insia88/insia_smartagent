"""Keeping credentials out of logs, error output and tracebacks (DESIGN.md 1-4, 2-3).

* ``register_secret(value, pin=False)`` — remember an actual secret value this process holds (a token, a client
  secret, an OAuth state it issued, a code whose state it validated). ``CredentialStore`` pins every secret it reads
  or writes (kept for the life of the process); everything else is a bounded, least-recently-registered list
  (``MAX_TRANSIENT_SECRETS``), so no request can make the register grow without limit. Values shorter than
  ``MIN_SECRET_LENGTH`` or longer than ``MAX_SECRET_LENGTH`` are ignored (the patterns below still apply). Only
  values the process issued or checked are registered: a string from an unauthenticated request never is (it could
  otherwise blank out any text in the logs).
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
from collections import OrderedDict
from collections.abc import Iterable

MASK = "***"
MIN_SECRET_LENGTH = 6          # shorter values would mask ordinary words
MAX_SECRET_LENGTH = 4096       # the longest token INSIA accepts; longer text is not a credential it holds
MAX_TRANSIENT_SECRETS = 256    # OAuth states/codes and other one-off values: the oldest is forgotten first

_LOCK = threading.Lock()
_PINNED: set[str] = set()                              # stored credentials: never forgotten
_TRANSIENT: OrderedDict[str, None] = OrderedDict()     # bounded, least recently registered first
_ORDERED: tuple[str, ...] = ()                         # longest first, rebuilt lazily by ``redact``
_DIRTY = False

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{4,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(access_token|client_secret|refresh_token|code|state)=([^&\s\"'#<>]+)"), r"\1=" + MASK),
    (re.compile(r"(?i)([\"'](?:access_token|client_secret|refresh_token)[\"']\s*:\s*[\"'])[^\"']*([\"'])"),
     r"\1" + MASK + r"\2"),
)


def register_secret(value: str | None, *, pin: bool = False) -> None:
    """Remember ``value`` so that ``redact`` and ``SecretFilter`` mask it everywhere. ``pin=True`` (stored
    credentials) keeps it for the life of the process; otherwise it joins the bounded transient list. Values
    shorter than ``MIN_SECRET_LENGTH`` or longer than ``MAX_SECRET_LENGTH`` characters are ignored."""
    global _DIRTY
    text = str(value or "").strip()
    if not MIN_SECRET_LENGTH <= len(text) <= MAX_SECRET_LENGTH:
        return
    with _LOCK:
        if text in _PINNED:
            return
        if pin:
            _TRANSIENT.pop(text, None)
            _PINNED.add(text)
        elif text in _TRANSIENT:
            _TRANSIENT.move_to_end(text)
            return
        else:
            _TRANSIENT[text] = None
            while len(_TRANSIENT) > MAX_TRANSIENT_SECRETS:
                _TRANSIENT.popitem(last=False)
        _DIRTY = True


def register_secrets(values: Iterable[str | None], *, pin: bool = False) -> None:
    for value in values:
        register_secret(value, pin=pin)


def forget_secrets() -> None:
    """Tests only: start from an empty register."""
    global _ORDERED, _DIRTY
    with _LOCK:
        _PINNED.clear()
        _TRANSIENT.clear()
        _ORDERED = ()
        _DIRTY = False


def registered_count() -> int:
    """How many values are registered (pinned + transient) — for tests and diagnostics, never the values."""
    with _LOCK:
        return len(_PINNED) + len(_TRANSIENT)


def _ordered() -> tuple[str, ...]:
    global _ORDERED, _DIRTY
    if _DIRTY:
        with _LOCK:
            if _DIRTY:
                _ORDERED = tuple(sorted((*_PINNED, *_TRANSIENT), key=len, reverse=True))
                _DIRTY = False
    return _ORDERED


def redact(text: object) -> str:
    """``text`` with registered secrets and secret-looking parameters masked as ``***``."""
    out = text if isinstance(text, str) else str(text)
    for secret in _ordered():
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


__all__ = ["MASK", "MAX_SECRET_LENGTH", "MAX_TRANSIENT_SECRETS", "MIN_SECRET_LENGTH", "SECRET_FILTER", "SecretFilter",
           "forget_secrets", "get_logger", "install_secret_filter", "redact", "redact_exception", "register_secret",
           "register_secrets", "registered_count"]
