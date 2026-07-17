import gzip
import hashlib
import importlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO

import requests

from syncmymoodle import pathing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileSnapshot:
    """Content identity observed for a target before staging an update."""

    exists: bool
    digest: str | None = None
    md5: str | None = None
    sha1: str | None = None
    identity: tuple[int, int, int, int, int] | None = None

    def digest_for(self, algorithm: str) -> str | None:
        """Return a digest captured during the snapshot's single file read."""
        return {
            "md5": self.md5,
            "sha1": self.sha1,
            "sha256": self.digest,
        }.get(algorithm)

    def still_matches(self, path: Path) -> bool:
        """Verify that both metadata and content still match the snapshot."""
        if not self.metadata_still_matches(path):
            return False
        if not self.exists:
            return True
        return file_sha256(path) == self.digest and self.metadata_still_matches(path)

    def metadata_still_matches(self, path: Path) -> bool:
        """Check for changes without reading file content again."""
        try:
            current = path.stat()
        except FileNotFoundError:
            return not self.exists
        except OSError:
            return False
        return bool(
            self.exists
            and self.digest is not None
            and self.identity == _file_identity(current)
        )


class InstallResult(Enum):
    INSTALLED = "installed"
    KEPT_LOCAL = "kept_local"
    FAILED = "failed"


class SyncRunLockedError(RuntimeError):
    pass


def file_sha256(path: Path) -> str | None:
    """Return a file's SHA-256 digest, or ``None`` when it cannot be read."""
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _file_identity(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def snapshot_file(path: Path) -> FileSnapshot:
    """Capture stable stat data and common digests in one file read."""
    try:
        before = path.stat()
    except FileNotFoundError:
        return FileSnapshot(False)
    except OSError:
        return FileSnapshot(True)

    digests = (
        hashlib.md5(usedforsecurity=False),
        hashlib.sha1(usedforsecurity=False),
        hashlib.sha256(),
    )
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                for digest in digests:
                    digest.update(chunk)
    except OSError:
        return FileSnapshot(True, identity=_file_identity(before))
    try:
        after = path.stat()
    except OSError:
        return FileSnapshot(True)
    identity = _file_identity(before)
    if identity != _file_identity(after):
        return FileSnapshot(True)
    return FileSnapshot(
        True,
        digest=digests[2].hexdigest(),
        md5=digests[0].hexdigest(),
        sha1=digests[1].hexdigest(),
        identity=identity,
    )


def install_staged_file(
    staged_path: Path,
    target_path: Path,
    *,
    baseline: FileSnapshot,
    rename_local: bool,
    target_change_policy: str,
    description: str,
    log: logging.Logger = logger,
) -> InstallResult:
    """Atomically install a staged file, preserving a conflicting local copy."""
    conflict_path: Path | None = None
    try:
        if not baseline.still_matches(target_path):
            if target_change_policy == "keep":
                log.warning(
                    "Keeping %s because it changed while %s was being prepared",
                    target_path,
                    description,
                )
                return InstallResult.KEPT_LOCAL
            if target_change_policy == "rename":
                rename_local = True
            elif target_change_policy != "overwrite":
                raise ValueError(
                    f"unsupported target change policy: {target_change_policy}"
                )
        if target_path.exists() and rename_local:
            conflict_path = pathing.make_conflict_path(target_path)
            target_path.rename(conflict_path)
            log.warning(
                "Detected local changes for %s, moved to %s before installing %s",
                target_path,
                conflict_path,
                description,
            )
        os.replace(staged_path, target_path)
        return InstallResult.INSTALLED
    except OSError:
        log.exception("Failed to install %s at %s", description, target_path)
        if conflict_path is not None and not target_path.exists():
            try:
                conflict_path.rename(target_path)
            except OSError:
                log.exception("Failed to restore local file %s", target_path)
        return InstallResult.FAILED


def _lock_file(handle: BinaryIO) -> None:
    if pathing.is_windows():
        msvcrt: Any = importlib.import_module("msvcrt")
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl: Any = importlib.import_module("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    if pathing.is_windows():
        msvcrt: Any = importlib.import_module("msvcrt")
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl: Any = importlib.import_module("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def sync_run_lock(sync_directory: Path) -> Iterator[None]:
    """Prevent concurrent writers from targeting one sync directory."""
    lock_path = pathing.with_windows_extended_length_prefix(
        sync_directory.expanduser() / ".syncmymoodle-cache" / "run.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            _lock_file(handle)
        except OSError as error:
            raise SyncRunLockedError(
                f"another sync is already using {sync_directory}"
            ) from error
        try:
            yield
        finally:
            _unlock_file(handle)


def restrict_private_file_windows(path: Path) -> None:
    win32api: Any = importlib.import_module("win32api")
    win32con: Any = importlib.import_module("win32con")
    win32security: Any = importlib.import_module("win32security")
    ntsecuritycon: Any = importlib.import_module("ntsecuritycon")

    process = win32api.GetCurrentProcess()
    token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        access_mask = (
            ntsecuritycon.FILE_GENERIC_READ
            | ntsecuritycon.FILE_GENERIC_WRITE
            | ntsecuritycon.DELETE
        )

        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access_mask, user_sid)
        win32security.SetNamedSecurityInfo(
            os.path.abspath(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    finally:
        win32api.CloseHandle(token)


def harden_private_file(path: Path, description: str) -> bool:
    if not path.exists():
        return True
    if path.is_symlink():
        logger.warning("Refusing to use symlinked %s file: %s", description, path)
        return False
    return chmod_private_best_effort(path, description)


def chmod_private_best_effort(path: Path, description: str) -> bool:
    if pathing.is_windows():
        try:
            restrict_private_file_windows(path)
        except Exception as error:
            logger.warning(
                "Could not restrict permissions for %s file on Windows: %s: %s",
                description,
                path,
                error,
            )
            return False
        return True
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning(
            "Could not restrict permissions for %s file: %s", description, path
        )
        return False
    return True


def write_private_bytes(path: Path, data: bytes, description: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        # The temporary file is created beside the destination, so its mode or
        # Windows ACL survives the atomic replace. Secure it before writing any
        # private data rather than trying to repair permissions afterwards.
        if pathing.is_windows():
            if not chmod_private_best_effort(tmp_path, f"temporary {description}"):
                raise PermissionError(
                    f"could not restrict permissions for temporary {description} file"
                )
        elif (fchmod := getattr(os, "fchmod", None)) is not None:
            try:
                fchmod(fd, 0o600)
            except OSError as error:
                raise PermissionError(
                    f"could not restrict permissions for temporary {description} file"
                ) from error
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
        os.replace(tmp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary private file: %s", tmp_path)


def write_private_text(path: Path, text: str, description: str) -> None:
    write_private_bytes(path, text.encode("utf-8"), description)


def write_private_gzip_json(path: Path, payload: Any) -> None:
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    write_private_bytes(path, gzip.compress(json_bytes), "private data")


def read_private_gzip_json(path: Path, description: str) -> Any:
    path = path.expanduser()
    if not path.exists():
        return None
    if not harden_private_file(path, description):
        return None
    try:
        with path.open("rb") as f:
            return json.loads(gzip.decompress(f.read()).decode("utf-8"))
    except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "Ignoring legacy or invalid %s file %s. Delete it if this warning repeats.",
            description,
            path,
        )
        return None


SESSION_CACHE_FORMAT = "syncmymoodle.session.v2"
LEGACY_COOKIE_FORMAT = "syncmymoodle.cookies.v1"


def session_to_data(cookie_jar: Any, session_key: str) -> dict[str, Any]:
    cookies = []
    for cookie in cookie_jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
                "rest": getattr(cookie, "_rest", {}),
            }
        )
    return {
        "format": SESSION_CACHE_FORMAT,
        "session_key": session_key,
        "cookies": cookies,
    }


def cookie_from_data(cookie_data: Any) -> Any:
    if not isinstance(cookie_data, dict):
        raise ValueError("cookie entry is not an object")
    name = cookie_data.get("name")
    value = cookie_data.get("value", "")
    domain = cookie_data.get("domain")
    path = cookie_data.get("path")
    secure = cookie_data.get("secure", False)
    expires = cookie_data.get("expires")
    raw_rest = cookie_data.get("rest")
    rest = {} if raw_rest is None else raw_rest
    if (
        not isinstance(name, str)
        or not name
        or (value is not None and not isinstance(value, str))
        or (domain is not None and not isinstance(domain, str))
        or (path is not None and not isinstance(path, str))
        or not isinstance(secure, bool)
        or (
            expires is not None
            and (not isinstance(expires, int) or isinstance(expires, bool))
        )
        or not isinstance(rest, dict)
        or not all(
            isinstance(key, str) and (rest_value is None or isinstance(rest_value, str))
            for key, rest_value in rest.items()
        )
    ):
        raise ValueError("cookie entry has invalid fields")
    return requests.cookies.create_cookie(
        name=name,
        value=value,
        domain=domain or "",
        path=path or "/",
        secure=secure,
        expires=expires,
        rest=rest,
    )


def load_session_from_data(cookie_jar: Any, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    cache_format = payload.get("format")
    if cache_format not in {SESSION_CACHE_FORMAT, LEGACY_COOKIE_FORMAT}:
        logger.warning("Ignoring unsupported cookie file format")
        return None

    cookie_data_items = payload.get("cookies", [])
    if not isinstance(cookie_data_items, list):
        logger.warning("Ignoring malformed cookie file")
        return None

    try:
        cookies = [cookie_from_data(cookie_data) for cookie_data in cookie_data_items]
    except (AttributeError, TypeError, ValueError):
        logger.warning("Ignoring malformed cookie file")
        return None

    for cookie in cookies:
        cookie_jar.set_cookie(cookie)
    session_key = payload.get("session_key")
    return session_key if isinstance(session_key, str) and session_key else None


def save_session(cookie_file: Path, cookie_jar: Any, session_key: str) -> None:
    write_private_gzip_json(cookie_file, session_to_data(cookie_jar, session_key))
