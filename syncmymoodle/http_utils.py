"""Small helpers for interpreting HTTP responses and fetched pages."""

import urllib.parse
from typing import Any, cast

from bs4 import BeautifulSoup

# Media types that indicate an HTML page rather than a downloadable file
# (e.g. a login or error page served in place of the expected content).
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def parse_html(markup: str) -> BeautifulSoup:
    """Parse HTML with the project's canonical parser.

    BeautifulSoup parsers differ in how they repair broken markup
    """
    return BeautifulSoup(markup, features="lxml")


def parse_xml(markup: str) -> BeautifulSoup:
    """Parse XML with the project's canonical parser."""
    return BeautifulSoup(markup, features="xml")


def media_type_without_parameters(value: Any) -> str:
    """Strip parameters like charset from a media type string."""
    return str(value or "").split(";", 1)[0].strip().lower()


def content_type_without_parameters(response: Any) -> str:
    """Return the media type of a response, without parameters like charset."""
    return media_type_without_parameters(response.headers.get("Content-Type", ""))


def content_length(response: Any, extra_bytes: int = 0) -> int | None:
    value = response.headers.get("Content-Length") or response.headers.get(
        "content-length"
    )
    if not value:
        return None
    try:
        size = int(value) + max(extra_bytes, 0)
    except ValueError:
        return None
    return size if size >= 0 else None


def filename_from_url(url: Any) -> str:
    """Return the last path segment of a URL, ignoring query and fragment."""
    return urllib.parse.urlsplit(str(url)).path.split("/")[-1]


def get_input_value(soup: Any, name: str) -> str | None:
    """Return the value of a named ``<input>`` field in a parsed page."""
    input_tag = soup.find("input", {"name": name})
    if input_tag and input_tag.get("value"):
        return cast(str, input_tag["value"])
    return None
