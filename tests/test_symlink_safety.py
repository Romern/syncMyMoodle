from pathlib import Path

import pytest
import requests

from syncmymoodle import rwth
from syncmymoodle.moodle_tokens import (
    ENV_FILE_PRIVATE_TOKEN_KEY,
    ENV_FILE_USER_ID_KEY,
    ENV_FILE_USERNAME_KEY,
    ENV_FILE_WSTOKEN_KEY,
    EnvFileTokenStore,
)
from syncmymoodle.secret_providers import EnvFileProvider, ProviderSecretError
from syncmymoodle.storage import save_session


def symlink_to(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are not available: {error}")


def test_environment_secret_provider_refuses_symlink_without_touching_target(tmp_path):
    target = tmp_path / "actual-secrets.env"
    content = b"SYNCMYMOODLE_PASSWORD=private-password\n"
    target.write_bytes(content)
    link = tmp_path / "configured-secrets.env"
    symlink_to(link, target)

    with pytest.raises(ProviderSecretError, match="not safe to read"):
        EnvFileProvider(link).load_credentials()

    assert link.is_symlink()
    assert target.read_bytes() == content


def test_cached_session_loader_refuses_symlink_without_touching_target(tmp_path):
    target = tmp_path / "actual-session"
    cookies = requests.cookies.RequestsCookieJar()
    cookies.set("MoodleSession", "private-cookie", domain="moodle.example", path="/")
    save_session(target, cookies, "private-session-key")
    content = target.read_bytes()
    link = tmp_path / "configured-session"
    symlink_to(link, target)

    assert rwth.load_cached_session(link) is None
    assert link.is_symlink()
    assert target.read_bytes() == content


def test_moodle_token_store_refuses_symlink_for_load_and_delete(tmp_path):
    target = tmp_path / "actual-tokens.env"
    content = (
        f"{ENV_FILE_USERNAME_KEY}=ab123456\n"
        f"{ENV_FILE_WSTOKEN_KEY}=private-webservice-token\n"
        f"{ENV_FILE_PRIVATE_TOKEN_KEY}=private-browser-token\n"
        f"{ENV_FILE_USER_ID_KEY}=10001\n"
    ).encode()
    target.write_bytes(content)
    link = tmp_path / "configured-tokens.env"
    symlink_to(link, target)
    store = EnvFileTokenStore(link, "ab123456")

    with pytest.raises(ProviderSecretError, match="not safe to read"):
        store.load()
    with pytest.raises(ProviderSecretError, match="not safe to delete"):
        store.delete()

    assert link.is_symlink()
    assert target.read_bytes() == content
