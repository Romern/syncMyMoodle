import gzip
import importlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import requests

from syncmymoodle import pathing

logger = logging.getLogger(__name__)


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
