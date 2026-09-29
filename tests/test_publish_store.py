"""Credential storage and secret masking (package A): ``credentials/secrets.sqlite`` 0600 in a 0700 folder, one
transaction for a token and its account id, nothing secret in ``insia.db``, logs or tracebacks (DESIGN.md 2-2, 2-3)."""

from __future__ import annotations

import logging
import os
import stat
import sys
import traceback

import pytest

from insia_agents.publishers import FakeTransport, json_response
from insia_agents.publishers.redact import SecretFilter, get_logger, redact, register_secret
from insia_agents.publishers.store import CredentialStore

pytestmark = pytest.mark.usefixtures("no_network")
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_nothing_is_created_until_something_is_saved(tmp_path):
    store = CredentialStore(tmp_path / "credentials")
    assert store.get("linkedin") == {} and store.has_any() is False and store.permission_problems() == []
    store.delete("linkedin")
    assert not (tmp_path / "credentials").exists()


@posix_only
def test_permissions_and_repair(tmp_path, caplog):
    store = CredentialStore(tmp_path / "credentials")
    store.set_many("linkedin", {"access_token": "tok-secret-value-1", "sub": "abc"})
    assert _mode(tmp_path / "credentials") == 0o700 and _mode(store.path) == 0o600
    assert _mode(tmp_path / "credentials" / "README.txt") == 0o600
    assert "백업" in (tmp_path / "credentials" / "README.txt").read_text(encoding="utf-8")
    os.chmod(store.path, 0o644)
    os.chmod(store.directory, 0o755)
    assert len(store.permission_problems()) == 2
    with caplog.at_level(logging.WARNING, logger="insia_agents"):
        assert store.get("linkedin")["sub"] == "abc"
    assert _mode(store.path) == 0o600 and _mode(store.directory) == 0o700 and "권한" in caplog.text
    assert "tok-secret-value-1" not in caplog.text


def test_token_and_account_id_share_one_transaction(tmp_path):
    store = CredentialStore(tmp_path / "credentials")
    store.set_many("instagram", {"access_token": "old-token-000001", "user_id": "111"})

    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("disk trouble")

    with pytest.raises(RuntimeError):
        store.set_many("instagram", {"access_token": "new-token-000002", "user_id": Exploding()})  # type: ignore[dict-item]
    assert store.get("instagram") == {"access_token": "old-token-000001", "user_id": "111"}
    store.set_many("instagram", {"access_token": "new-token-000002", "user_id": "222", "stale": None})
    assert store.get("instagram") == {"access_token": "new-token-000002", "user_id": "222"}


def test_workspace_db_never_holds_a_token(publish_kit):
    fake = FakeTransport()
    service = publish_kit.service(transport=fake, instagram=True)
    fake.add("GET", r"/v25\.0/me$", json_response(200, {"user_id": "1784", "username": "a", "account_type": "BUSINESS"}))
    fake.add("GET", r"/refresh_access_token$", json_response(200, {"access_token": "IGAA-refreshed-ZZZZ-9", "expires_in": 5184000}))
    service.save_instagram_token("IGAA-pasted-YYYY-8")
    service.save_linkedin_app(client_id="86id", client_secret="client-secret-XXXX-7")
    publish_kit.connect_linkedin(service, token="AQUv-member-WWWW-6")
    publish_kit.workspace.close()
    raw = b"".join(p.read_bytes() for p in publish_kit.home.glob("insia.db*"))
    for secret in (b"IGAA-refreshed-ZZZZ-9", b"IGAA-pasted-YYYY-8", b"client-secret-XXXX-7", b"AQUv-member-WWWW-6"):
        assert secret not in raw
    secrets_file = (publish_kit.home / "credentials" / "secrets.sqlite").read_bytes()
    assert b"IGAA-refreshed-ZZZZ-9" in secrets_file  # it lives here, and only here


def test_exceptions_and_logs_never_show_a_registered_secret(tmp_path, caplog, capsys):
    store = CredentialStore(tmp_path / "credentials")
    store.set_many("linkedin", {"access_token": "AQUv-very-secret-token-42", "client_secret": "cs-hidden-value-9"})
    store.get("linkedin")  # reading registers them too
    register_secret("oauth-state-value-abc")
    logger = get_logger("insia_agents.publishers.test_store")
    with caplog.at_level(logging.DEBUG, logger="insia_agents"):
        try:
            raise ValueError("platform said: token=AQUv-very-secret-token-42 secret cs-hidden-value-9")
        except ValueError as exc:
            logger.error("send failed: %s", exc, exc_info=True)
            print(redact(traceback.format_exc()), file=sys.stderr)  # the CLI's error path
    logger.warning("Authorization: Bearer abcdefghij0123 and access_token=unregistered-value&x=1 state=oauth-state-value-abc")
    err = capsys.readouterr().err
    for text in (caplog.text, err):
        assert "AQUv-very-secret-token-42" not in text and "cs-hidden-value-9" not in text
        assert "oauth-state-value-abc" not in text
    assert "unregistered-value" not in caplog.text and "abcdefghij0123" not in caplog.text
    assert redact('{"access_token": "zzz-unregistered"}') == '{"access_token": "***"}'


def test_secret_filter_keeps_records_and_short_values_alone():
    record = logging.LogRecord("insia_agents.x", logging.INFO, __file__, 1, "value %s", ("abc",), None)
    assert SecretFilter().filter(record) is True and record.getMessage() == "value abc"
    register_secret("abc")  # too short to be masked (would hide ordinary words)
    assert redact("abc") == "abc"
