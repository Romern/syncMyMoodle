import json
import os
import stat

import pytest

from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.moodle_tokens import (
    ENV_FILE_PRIVATE_TOKEN_KEY,
    ENV_FILE_USER_ID_KEY,
    ENV_FILE_USERNAME_KEY,
    ENV_FILE_WSTOKEN_KEY,
    EnvFileTokenStore,
    KeyringTokenStore,
    MoodleTokens,
    overwrite_tokens_verified,
    store_tokens_verified,
)
from syncmymoodle.secret_providers import (
    KEYRING_SERVICE,
    KeyringProvider,
    ProviderSecretError,
)

from .helpers import FakeKeyring


def tokens():
    return MoodleTokens(
        username="ab123456",
        wstoken="webservice-token",
        private_token="private-token",
        moodle_user_id=123,
    )


def test_moodle_tokens_roundtrip_keeps_secrets_out_of_repr():
    original = tokens()

    restored = MoodleTokens.from_json(original.to_json())

    assert restored == original
    assert "webservice-token" not in repr(original)
    assert "private-token" not in repr(original)


def test_moodle_tokens_reject_wrong_account():
    with pytest.raises(ProviderSecretError, match="different Moodle account"):
        tokens().require_account("xy123456")


def test_keyring_token_store_writes_one_versioned_record():
    stored = {}
    store = KeyringTokenStore(
        KeyringProvider(FakeKeyring(stored)),
        "ab123456",
    )

    store.store(tokens())

    assert len(stored) == 1
    (((service, reference), raw_record),) = stored.items()
    assert service == KEYRING_SERVICE
    assert reference == "mobile-tokens:moodle.rwth-aachen.de:ab123456"
    assert json.loads(raw_record)["version"] == 1
    assert json.loads(raw_record)["moodle_user_id"] == 123
    assert store.load() == tokens()


def test_keyring_token_store_rejects_mismatched_record():
    stored = {}
    provider = KeyringProvider(FakeKeyring(stored))
    source = KeyringTokenStore(provider, "ab123456")
    source.store(tokens())
    target = KeyringTokenStore(provider, "xy123456")
    stored[(KEYRING_SERVICE, target.reference)] = tokens().to_json()

    with pytest.raises(ProviderSecretError, match="different Moodle account"):
        target.load()


def test_keyring_token_store_delete_is_idempotent_for_invalid_record():
    stored = {}
    store = KeyringTokenStore(
        KeyringProvider(FakeKeyring(stored)),
        "ab123456",
    )
    stored[(KEYRING_SERVICE, store.reference)] = "not-json"

    store.delete()
    store.delete()

    assert stored == {}


def test_env_file_token_store_roundtrip_is_atomic_and_private(tmp_path):
    path = tmp_path / "mobile-token.env"
    store = EnvFileTokenStore(path, "ab123456")

    store.store(tokens())

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == (
        f"{ENV_FILE_USERNAME_KEY}=ab123456\n"
        f"{ENV_FILE_WSTOKEN_KEY}=webservice-token\n"
        f"{ENV_FILE_PRIVATE_TOKEN_KEY}=private-token\n"
        f"{ENV_FILE_USER_ID_KEY}=123\n"
    )
    assert store.load() == tokens()


def test_verified_store_restores_prior_record_when_write_cannot_be_read_back():
    original = tokens()
    replacement = MoodleTokens(
        original.username,
        "replacement-webservice-token",
        "replacement-private-token",
        moodle_user_id=original.moodle_user_id,
    )

    class ForgetfulStore:
        description = "forgetful store"

        def __init__(self):
            self.current = original

        def load(self):
            return None if self.current == replacement else self.current

        def store(self, value):
            self.current = value

        def delete(self):
            self.current = None

    store = ForgetfulStore()

    with pytest.raises(ProviderSecretError, match="verification failed"):
        store_tokens_verified(store, replacement)

    assert store.current == original


def test_env_file_token_store_wraps_write_failures(tmp_path, monkeypatch):
    def fail_write(*args):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr("syncmymoodle.moodle_tokens.write_private_text", fail_write)
    store = EnvFileTokenStore(tmp_path / "mobile-token.env", "ab123456")

    with pytest.raises(ProviderSecretError, match="could not write Moodle token file"):
        store.store(tokens())


def test_env_file_token_store_supports_record_without_private_token(tmp_path):
    path = tmp_path / "mobile-token.env"
    path.write_text(
        f"{ENV_FILE_USERNAME_KEY}=ab123456\n"
        f"{ENV_FILE_WSTOKEN_KEY}=webservice-token\n"
        f"{ENV_FILE_USER_ID_KEY}=123\n",
        encoding="utf-8",
    )
    store = EnvFileTokenStore(path, "ab123456")

    loaded = store.load()

    assert loaded is not None
    assert loaded.private_token is None


def test_env_file_token_store_rejects_record_without_account_identity(tmp_path):
    path = tmp_path / "mobile-token.env"
    path.write_text(
        f"{ENV_FILE_USERNAME_KEY}=ab123456\n{ENV_FILE_WSTOKEN_KEY}=webservice-token\n",
        encoding="utf-8",
    )

    with pytest.raises(ProviderSecretError, match=ENV_FILE_USER_ID_KEY):
        EnvFileTokenStore(path, "ab123456").load()


def test_env_file_token_store_rejects_record_for_another_sso_account(tmp_path):
    path = tmp_path / "mobile-token.env"
    EnvFileTokenStore(path, "ab123456").store(tokens())

    with pytest.raises(ProviderSecretError, match="different Moodle account"):
        EnvFileTokenStore(path, "xy123456").load()


def test_unreadable_managed_token_file_can_be_replaced_and_verified(tmp_path):
    path = tmp_path / "mobile-token.env"
    EnvFileTokenStore(path, "ab123456").store(tokens())
    replacement = MoodleTokens(
        "xy123456",
        "replacement-webservice-token",
        "replacement-private-token",
        moodle_user_id=456,
    )
    store = EnvFileTokenStore(path, replacement.username)

    with pytest.raises(ProviderSecretError, match="different Moodle account"):
        store.load()

    overwrite_tokens_verified(store, replacement)

    assert store.load() == replacement


def test_env_file_token_store_fails_closed_when_permissions_cannot_be_hardened(
    tmp_path, monkeypatch
):
    path = tmp_path / "mobile-token.env"
    path.write_text(
        f"{ENV_FILE_USERNAME_KEY}=ab123456\n"
        f"{ENV_FILE_WSTOKEN_KEY}=webservice-token\n"
        f"{ENV_FILE_USER_ID_KEY}=123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "syncmymoodle.secret_providers.harden_private_file", lambda *args: False
    )

    with pytest.raises(ProviderSecretError, match="not safe to read"):
        EnvFileTokenStore(path, "ab123456").load()


def test_moodle_tokens_reject_noncanonical_site_record():
    raw = tokens().to_json().replace(MOODLE_URL, "https://example.test/")

    parsed = MoodleTokens.from_json(raw)

    with pytest.raises(ProviderSecretError, match="different Moodle account"):
        parsed.require_account("ab123456")
