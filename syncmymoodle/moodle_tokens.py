"""Persistent Moodle tokens and their local stores."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.secret_providers import (
    KeyringProvider,
    ProviderAvailability,
    ProviderSecretError,
    read_secure_env_file,
)
from syncmymoodle.storage import harden_private_file, write_private_text

MOODLE_TOKEN_VERSION = 1
MOBILE_TOKEN_KEY_PREFIX = "mobile-tokens"
ENV_FILE_USERNAME_KEY = "SYNCMYMOODLE_USERNAME"
ENV_FILE_WSTOKEN_KEY = "SYNCMYMOODLE_WSTOKEN"
ENV_FILE_PRIVATE_TOKEN_KEY = "SYNCMYMOODLE_PRIVATE_TOKEN"
ENV_FILE_USER_ID_KEY = "SYNCMYMOODLE_USER_ID"


@dataclass(frozen=True)
class MoodleTokens:
    """One inseparable Moodle token pair with its account identity.

    Both token fields are excluded from representations because the private
    token can bootstrap a full browser session.
    """

    username: str
    wstoken: str = field(repr=False)
    private_token: str | None = field(repr=False)
    site: str = MOODLE_URL
    version: int = MOODLE_TOKEN_VERSION
    moodle_user_id: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "site": self.site,
                "username": self.username,
                "wstoken": self.wstoken,
                "private_token": self.private_token or "",
                "moodle_user_id": self.moodle_user_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> MoodleTokens:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ProviderSecretError(
                "Moodle token record is not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise ProviderSecretError("Moodle token record must be an object")
        if payload.get("version") != MOODLE_TOKEN_VERSION:
            raise ProviderSecretError("unsupported Moodle token record version")
        site = payload.get("site")
        username = payload.get("username")
        wstoken = payload.get("wstoken")
        private_token = payload.get("private_token")
        moodle_user_id = payload.get("moodle_user_id")
        if not isinstance(site, str) or not site:
            raise ProviderSecretError("Moodle token record is incomplete")
        if not isinstance(username, str) or not username:
            raise ProviderSecretError("Moodle token record is incomplete")
        if not isinstance(wstoken, str) or not wstoken:
            raise ProviderSecretError("Moodle token record is incomplete")
        if not isinstance(private_token, str):
            raise ProviderSecretError(
                "Moodle token record's browser login token must be a string"
            )
        if (
            not isinstance(moodle_user_id, int)
            or isinstance(moodle_user_id, bool)
            or moodle_user_id <= 0
        ):
            raise ProviderSecretError("Moodle token record is incomplete")
        return cls(
            username=username,
            wstoken=wstoken,
            private_token=private_token or None,
            site=site,
            moodle_user_id=moodle_user_id,
        )

    def require_account(self, username: str, site: str = MOODLE_URL) -> None:
        if self.username != username or normalized_site(self.site) != normalized_site(
            site
        ):
            raise ProviderSecretError(
                "stored Moodle tokens belong to a different Moodle account"
            )
        if self.moodle_user_id is None or self.moodle_user_id <= 0:
            raise ProviderSecretError(
                "stored Moodle tokens have no verified Moodle account identity"
            )


def normalized_site(site: str) -> str:
    parsed = urllib.parse.urlsplit(site)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") + "/",
            "",
            "",
        )
    )


class MoodleTokenStore(Protocol):
    def check_available(self) -> ProviderAvailability: ...

    def load(self) -> MoodleTokens | None: ...

    def store(self, tokens: MoodleTokens) -> None: ...

    def delete(self) -> None: ...

    @property
    def description(self) -> str: ...


def _replace_tokens_verified(
    store: MoodleTokenStore,
    tokens: MoodleTokens,
) -> None:
    store.store(tokens)
    if store.load() != tokens:
        raise ProviderSecretError(
            f"Moodle token verification failed for {store.description}"
        )


def _restore_tokens(
    store: MoodleTokenStore,
    tokens: MoodleTokens | None,
) -> None:
    if tokens is None:
        store.delete()
    else:
        store.store(tokens)
    if store.load() != tokens:
        raise ProviderSecretError(
            f"Moodle token rollback verification failed for {store.description}"
        )


@contextmanager
def token_store_transaction(
    store: MoodleTokenStore,
    tokens: MoodleTokens,
) -> Iterator[None]:
    """Install tokens and restore the prior record if the operation fails."""
    previous = store.load()
    try:
        _replace_tokens_verified(store, tokens)
        yield
    except BaseException as error:
        try:
            _restore_tokens(store, previous)
        except BaseException as rollback_error:
            raise ProviderSecretError(
                f"could not restore {store.description} after a failed operation: "
                f"{rollback_error}"
            ) from error
        raise


def store_tokens_verified(
    store: MoodleTokenStore,
    tokens: MoodleTokens,
) -> None:
    with token_store_transaction(store, tokens):
        pass


def overwrite_tokens_verified(
    store: MoodleTokenStore,
    tokens: MoodleTokens,
) -> None:
    """Replace an unreadable record when its store is managed by syncMyMoodle."""
    _replace_tokens_verified(store, tokens)


class KeyringTokenStore:
    def __init__(
        self,
        provider: KeyringProvider,
        username: str,
        site: str = MOODLE_URL,
    ) -> None:
        self.provider = provider
        self.username = username
        self.site = site

    @property
    def reference(self) -> str:
        host = urllib.parse.urlsplit(self.site).netloc.lower()
        return f"{MOBILE_TOKEN_KEY_PREFIX}:{host}:{self.username}"

    @property
    def description(self) -> str:
        return "system keyring"

    def check_available(self) -> ProviderAvailability:
        return self.provider.check_available()

    def load(self) -> MoodleTokens | None:
        value = self.provider.get_secret(self.reference)
        if value is None:
            return None
        tokens = MoodleTokens.from_json(value)
        tokens.require_account(self.username, self.site)
        return tokens

    def store(self, tokens: MoodleTokens) -> None:
        tokens.require_account(self.username, self.site)
        self.provider.store_secret(self.reference, tokens.to_json())

    def delete(self) -> None:
        if self.provider.get_secret(self.reference) is not None:
            self.provider.delete_secret(self.reference)


class EnvFileTokenStore:
    def __init__(
        self,
        path: Path,
        username: str,
        site: str = MOODLE_URL,
    ) -> None:
        self.path = path.expanduser()
        self.username = username
        self.site = site

    @property
    def description(self) -> str:
        return f"environment file ({self.path})"

    def check_available(self) -> ProviderAvailability:
        if self.path.exists() and not harden_private_file(self.path, "Moodle token"):
            return ProviderAvailability(False, f"file is not safe to use: {self.path}")
        return ProviderAvailability(True)

    def load(self) -> MoodleTokens | None:
        if not self.path.exists():
            return None
        values = read_secure_env_file(self.path, "Moodle token file")
        username = values.get(ENV_FILE_USERNAME_KEY)
        wstoken = values.get(ENV_FILE_WSTOKEN_KEY)
        private_token = values.get(ENV_FILE_PRIVATE_TOKEN_KEY)
        raw_user_id = values.get(ENV_FILE_USER_ID_KEY)
        if not username:
            raise ProviderSecretError(
                f"Moodle token file is missing {ENV_FILE_USERNAME_KEY}"
            )
        if not wstoken:
            raise ProviderSecretError(
                f"Moodle token file is missing {ENV_FILE_WSTOKEN_KEY}"
            )
        try:
            moodle_user_id = int(raw_user_id or "")
        except ValueError as error:
            raise ProviderSecretError(
                f"Moodle token file has invalid {ENV_FILE_USER_ID_KEY}"
            ) from error
        if moodle_user_id <= 0:
            raise ProviderSecretError(
                f"Moodle token file has invalid {ENV_FILE_USER_ID_KEY}"
            )
        tokens = MoodleTokens(
            username=username,
            wstoken=wstoken,
            private_token=private_token or None,
            site=self.site,
            moodle_user_id=moodle_user_id,
        )
        tokens.require_account(self.username, self.site)
        return tokens

    def store(self, tokens: MoodleTokens) -> None:
        tokens.require_account(self.username, self.site)
        private_token = tokens.private_token or ""
        try:
            write_private_text(
                self.path,
                f"{ENV_FILE_USERNAME_KEY}={tokens.username}\n"
                f"{ENV_FILE_WSTOKEN_KEY}={tokens.wstoken}\n"
                f"{ENV_FILE_PRIVATE_TOKEN_KEY}={private_token}\n"
                f"{ENV_FILE_USER_ID_KEY}={tokens.moodle_user_id}\n",
                "Moodle token",
            )
        except OSError as error:
            raise ProviderSecretError(
                f"could not write Moodle token file: {error}"
            ) from error

    def delete(self) -> None:
        if not self.path.exists():
            return
        if not harden_private_file(self.path, "Moodle token"):
            raise ProviderSecretError(
                f"Moodle token file is not safe to delete: {self.path}"
            )
        try:
            self.path.unlink()
        except OSError as error:
            raise ProviderSecretError(
                f"could not delete Moodle token file: {error}"
            ) from error
