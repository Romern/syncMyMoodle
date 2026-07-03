import gzip
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


def harden_private_file(path: Path, description: str) -> bool:
    if not path.exists():
        return True
    if path.is_symlink():
        logger.warning("Refusing to use symlinked %s file: %s", description, path)
        return False
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning(
            "Could not restrict permissions for %s file: %s", description, path
        )
    return True


def write_private_gzip_json(path: Path, payload: Any) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    data = gzip.compress(json_bytes)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
        path.chmod(0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


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


def cookies_to_data(cookie_jar: Any) -> dict[str, Any]:
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
    return {"format": "syncmymoodle.cookies.v1", "cookies": cookies}


def load_cookies_from_data(cookie_jar: Any, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("format") != "syncmymoodle.cookies.v1":
        logger.warning("Ignoring unsupported cookie file format")
        return

    for cookie_data in payload.get("cookies", []):
        if not isinstance(cookie_data, dict):
            continue
        if not cookie_data.get("name"):
            continue
        cookie = requests.cookies.create_cookie(
            name=cookie_data["name"],
            value=cookie_data.get("value", ""),
            domain=cookie_data.get("domain") or "",
            path=cookie_data.get("path") or "/",
            secure=bool(cookie_data.get("secure")),
            expires=cookie_data.get("expires"),
            rest=cookie_data.get("rest") or {},
        )
        cookie_jar.set_cookie(cookie)


def save_session_cookies(cookie_file: Path, cookie_jar: Any) -> None:
    write_private_gzip_json(cookie_file, cookies_to_data(cookie_jar))
