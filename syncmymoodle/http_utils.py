"""Small helpers for interpreting HTTP responses, shared across modules."""

from typing import Any


def content_type_without_parameters(response: Any) -> str:
    """Return the media type of a response, without parameters like charset."""
    content_type = str(response.headers.get("Content-Type", ""))
    return content_type.split(";", 1)[0].strip().lower()
