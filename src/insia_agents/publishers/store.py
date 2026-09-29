"""``CredentialStore``: publishing tokens and app secrets in ``credentials/secrets.sqlite`` (DESIGN.md 2-2).

* Folder ``0700``, file ``0600`` (created with ``os.open(…, 0o600)`` before SQLite opens it); permissions are checked
  on every open and put back (with a warning) when group/other got access. Windows has no POSIX modes: the user
  profile's ACL protects it (documented; ``doctor`` skips the check there).
* One small table ``secrets(platform, key, value, updated_at)``. A token and its account id are always written in
  one transaction (``set_many``), so the worker's "account unchanged" check before sending reads a consistent pair.
* Nothing is created until something is saved: reading a store that does not exist returns empty values (a user who
  never set publishing up gets no ``credentials/`` folder).
* Every secret value read or written is registered with ``redact.register_secret`` (logs never show it).
* A connection per operation (journal mode ``DELETE``, busy timeout): the server and a CLI command may use the
  same store at the same time.

Keys (platform · key): ``linkedin`` · ``client_id``, ``client_secret``, ``redirect_uri``, ``access_token``, ``scope``,
``sub``, ``expires_at``, ``issued_at``; ``instagram`` · ``access_token``, ``user_id``, ``expires_at``, ``issued_at``,
``refreshed_at``, ``expires_estimated``. LinkedIn member names are never stored (API terms 4.1).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .redact import get_logger, register_secret

log = get_logger(__name__)

DB_FILE = "secrets.sqlite"
README_FILE = "README.txt"
README_TEXT = ("이 폴더에는 API 게시용 토큰과 앱 비밀값이 들어 있어요. 백업·공유·클라우드 동기화에 넣지 마세요.\n"
               "복원한 뒤에는 대시보드의 브랜드·자료 → API 게시 연결에서 다시 연결하면 돼요.\n")
SECRET_KEYS = frozenset({"access_token", "client_secret"})
_POSIX = os.name == "posix"
_LOCK = threading.RLock()


class CredentialStoreError(RuntimeError):
    """The store could not be read or written (``str(exc)`` is Korean, never contains a value)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CredentialStore:
    """Tokens, app secrets and account ids per platform (see the module docstring)."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory).expanduser()
        self.path = self.directory / DB_FILE

    def __repr__(self) -> str:
        return f"CredentialStore({str(self.directory)!r})"

    # -- files ---------------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.is_file()

    def _fix_mode(self, path: Path, wanted: int) -> None:
        if not _POSIX:
            return
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return
        if mode & 0o077:
            log.warning("자격 증명 파일 권한이 너무 넓어서(%o) %o으로 되돌렸어요: %s", mode, wanted, path.name)
            try:
                os.chmod(path, wanted)
            except OSError:
                log.warning("자격 증명 파일 권한을 바꾸지 못했어요: %s", path.name)

    def _ensure(self) -> None:
        """Create the folder (0700), the database file (0600) and the README before first use."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._fix_mode(self.directory, 0o700)
            if not self.path.exists():
                fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            self._fix_mode(self.path, 0o600)
            readme = self.directory / README_FILE
            if not readme.exists():
                fd = os.open(str(readme), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(README_TEXT)
        except OSError as exc:
            raise CredentialStoreError(f"자격 증명 폴더를 만들 수 없어요: {self.directory} ({exc.strerror or exc})") from None

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if create:
            self._ensure()
        elif not self.exists():
            return None
        else:
            self._fix_mode(self.directory, 0o700)
            self._fix_mode(self.path, 0o600)
        try:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("CREATE TABLE IF NOT EXISTS secrets (platform TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                         "updated_at TEXT NOT NULL, PRIMARY KEY (platform, key))")
        except sqlite3.Error as exc:
            raise CredentialStoreError(f"자격 증명 파일을 열 수 없어요: {self.path.name} ({exc.__class__.__name__})") from None
        return conn

    # -- reads -------------------------------------------------------------------------
    def get(self, platform: str) -> dict[str, str]:
        """Every stored value of ``platform`` (``{}`` when nothing is stored or the store does not exist)."""
        with _LOCK:
            conn = self._connect(create=False)
            if conn is None:
                return {}
            try:
                rows = conn.execute("SELECT key, value FROM secrets WHERE platform = ?", (platform,)).fetchall()
            except sqlite3.Error as exc:
                raise CredentialStoreError(f"자격 증명을 읽지 못했어요 ({exc.__class__.__name__})") from None
            finally:
                conn.close()
        values = {str(k): str(v) for k, v in rows}
        for key in SECRET_KEYS & values.keys():
            register_secret(values[key])
        return values

    def value(self, platform: str, key: str) -> str:
        return self.get(platform).get(key, "")

    def has_any(self) -> bool:
        with _LOCK:
            conn = self._connect(create=False)
            if conn is None:
                return False
            try:
                return conn.execute("SELECT 1 FROM secrets LIMIT 1").fetchone() is not None
            finally:
                conn.close()

    # -- writes ------------------------------------------------------------------------
    def set_many(self, platform: str, values: Mapping[str, str | None], *, delete: Iterable[str] = ()) -> None:
        """Write ``values`` (``None`` deletes that key) and delete ``delete`` keys, all in one transaction."""
        for key in SECRET_KEYS & set(values):
            register_secret(values[key])
        now = _now()
        with _LOCK:
            conn = self._connect(create=True)
            assert conn is not None
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for key, value in values.items():
                        if value is None:
                            conn.execute("DELETE FROM secrets WHERE platform = ? AND key = ?", (platform, key))
                        else:
                            conn.execute("INSERT INTO secrets (platform, key, value, updated_at) VALUES (?, ?, ?, ?) "
                                         "ON CONFLICT (platform, key) DO UPDATE SET value = excluded.value, "
                                         "updated_at = excluded.updated_at", (platform, key, str(value), now))
                    for key in delete:
                        conn.execute("DELETE FROM secrets WHERE platform = ? AND key = ?", (platform, key))
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                raise CredentialStoreError(f"자격 증명을 저장하지 못했어요 ({exc.__class__.__name__})") from None
            finally:
                conn.close()

    def delete(self, platform: str, keys: Iterable[str] | None = None) -> None:
        """Delete ``keys`` of ``platform`` (all of them when ``keys`` is None). No-op when the store does not exist."""
        with _LOCK:
            conn = self._connect(create=False)
            if conn is None:
                return
            try:
                if keys is None:
                    conn.execute("DELETE FROM secrets WHERE platform = ?", (platform,))
                else:
                    for key in keys:
                        conn.execute("DELETE FROM secrets WHERE platform = ? AND key = ?", (platform, key))
            except sqlite3.Error as exc:
                raise CredentialStoreError(f"자격 증명을 지우지 못했어요 ({exc.__class__.__name__})") from None
            finally:
                conn.close()

    # -- checks ------------------------------------------------------------------------
    def permission_problems(self) -> list[str]:
        """Korean notes when the folder is not 0700 or the file not 0600 (POSIX only; empty when fine or absent)."""
        if not _POSIX:
            return []
        problems: list[str] = []
        for path, wanted, label in ((self.directory, 0o700, "credentials 폴더"), (self.path, 0o600, "secrets.sqlite")):
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                continue
            if mode & 0o077:
                problems.append(f"{label} 권한이 {mode:o}예요 ({wanted:o}이어야 해요).")
        return problems


__all__ = ["CredentialStore", "CredentialStoreError", "DB_FILE", "README_TEXT"]
