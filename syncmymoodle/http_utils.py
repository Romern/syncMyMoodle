"""Small helpers for interpreting HTTP responses and fetched pages."""

import logging
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from typing import Any, Protocol, cast

import requests
from bs4 import BeautifulSoup

from syncmymoodle.constants import DEFAULT_BLOCK_SIZE

# Media types that indicate an HTML page rather than a downloadable file
# (e.g. a login or error page served in place of the expected content).
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "if-modified-since",
        "if-none-match",
        "proxy-authorization",
        "requesttoken",
    }
)
_SENSITIVE_QUERY_PARAMETER_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "key",
        "oauth_signature",
        "passport",
        "password",
        "private_token",
        "privatetoken",
        "samlresponse",
        "sesskey",
        "signature",
        "token",
        "wstoken",
    }
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:"
    + "|".join(re.escape(name) for name in _SENSITIVE_QUERY_PARAMETER_NAMES)
    + r")=)[^&#\s'\"]*",
    re.IGNORECASE,
)
SERVICE_OUTAGE_THRESHOLD = 3


class RedactedRequestError(requests.RequestException):
    """A request failure whose message is safe to include in logs."""


class RequestPolicyError(RedactedRequestError):
    """A resource-specific refusal that must not count as a service outage."""


class HttpFailureKind(Enum):
    """Whether an HTTP failure applies to one resource or likely its service."""

    RESOURCE = "resource"
    TRANSIENT = "transient"


@dataclass
class ServiceOutageTracker:
    """Track consecutive backend failures and open per-sync service circuits."""

    _failure_counts: dict[str, int] = field(default_factory=dict)
    _unavailable_services: set[str] = field(default_factory=set)

    def should_skip(self, service: str) -> bool:
        return service in self._unavailable_services

    def record_available(self, service: str) -> None:
        """Clear outage evidence after a definitive non-transient result."""
        if not self.should_skip(service):
            self._failure_counts.pop(service, None)

    def record_failure(self, service: str) -> bool:
        """Record a failure and return whether it newly opened the circuit."""
        if self.should_skip(service):
            return False
        count = self._failure_counts.get(service, 0) + 1
        self._failure_counts[service] = count
        if count < SERVICE_OUTAGE_THRESHOLD:
            return False
        self._unavailable_services.add(service)
        return True


def record_service_failure(
    tracker: ServiceOutageTracker,
    service: str,
    label: str,
    kind: HttpFailureKind,
    reason: str,
    log: logging.Logger,
    outage_hint: str | None = None,
) -> None:
    """Apply the shared failure policy and log transient failures."""
    if kind is HttpFailureKind.RESOURCE:
        tracker.record_available(service)
        return
    if tracker.should_skip(service):
        return
    if tracker.record_failure(service):
        message = (
            "%s unavailable after %s consecutive transient failures: %s; "
            "skipping remaining requests for this sync"
        )
        if outage_hint:
            message += ". %s"
            log.warning(
                message,
                label,
                SERVICE_OUTAGE_THRESHOLD,
                reason,
                outage_hint,
            )
        else:
            log.warning(message, label, SERVICE_OUTAGE_THRESHOLD, reason)
        return
    log.warning("%s transient failure: %s", label, reason)


class _ByteWriter(Protocol):
    def write(self, data: bytes, /) -> object: ...


def redact_url_secrets(value: Any) -> str:
    """Redact credential-like query values from a URL or error message."""
    return _SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", str(value))


def safe_request_error(error: requests.RequestException) -> str:
    """Return a request exception message without URL query credentials."""
    message = redact_url_secrets(error)
    request = error.request
    if request is None and error.response is not None:
        request = error.response.request
    request_url = getattr(request, "url", None)
    if request_url:
        for name, secret in urllib.parse.parse_qsl(
            urllib.parse.urlsplit(str(request_url)).query,
            keep_blank_values=True,
        ):
            if name.lower() not in _SENSITIVE_QUERY_PARAMETER_NAMES or len(secret) < 4:
                continue
            message = message.replace(secret, "[REDACTED]")
            message = message.replace(
                urllib.parse.quote_plus(secret),
                "[REDACTED]",
            )
    return message or type(error).__name__


def safe_error_message(error: BaseException) -> str:
    """Return an exception message with request URL credentials redacted."""
    if isinstance(error, requests.RequestException):
        return safe_request_error(error)
    return redact_url_secrets(error)


def classify_http_failure(status_code: int) -> HttpFailureKind | None:
    """Classify a non-success response without conflating it with an outage."""
    if 200 <= status_code < 300:
        return None
    if status_code in {408, 425, 429} or 500 <= status_code < 600:
        return HttpFailureKind.TRANSIENT
    return HttpFailureKind.RESOURCE


def classify_request_failure(error: requests.RequestException) -> HttpFailureKind:
    """Classify request failures that happen before an HTTP status is available."""
    if isinstance(error, RequestPolicyError):
        return HttpFailureKind.RESOURCE
    return HttpFailureKind.TRANSIENT


def _http_origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or hostname is None:
        return None
    default_port = 80 if scheme == "http" else 443
    return scheme, hostname.lower(), port if port is not None else default_port


def same_origin(url: str, expected_origin: str) -> bool:
    """Return whether two HTTP(S) URLs have the same normalized origin."""
    origin = _http_origin(url)
    return origin is not None and origin == _http_origin(expected_origin)


def normalized_http_origin(url: Any) -> str | None:
    """Return a stable HTTP origin suitable for per-host outage tracking."""
    origin = _http_origin(str(url))
    if origin is None:
        return None
    scheme, hostname, port = origin
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{hostname}{port_suffix}"


def request_following_safe_redirects(
    session: Any,
    method: str,
    url: str,
    url_allowed: Callable[[str], bool],
    **kwargs: Any,
) -> Any:
    """Make a GET/HEAD request while validating each redirect before following it."""
    current_url = url
    request_kwargs = dict(kwargs)
    request_headers = dict(request_kwargs.get("headers") or {})
    for _ in range(11):
        current_origin = _http_origin(current_url)
        if current_origin is None or not url_allowed(current_url):
            raise RequestPolicyError(
                f"refusing request to disallowed URL {redact_url_secrets(current_url)}"
            )
        try:
            response = session.request(
                method,
                current_url,
                allow_redirects=False,
                **request_kwargs,
            )
        except requests.RequestException as error:
            raise RedactedRequestError(safe_request_error(error)) from None

        location = response.headers.get("Location")
        if response.status_code not in _REDIRECT_STATUS_CODES or not location:
            # requests.Response.url includes query parameters injected by AuthBase.
            # Keep the validated public URL so callers cannot cache those secrets.
            response.url = current_url
            return response

        next_url = urllib.parse.urljoin(current_url, location)
        response.close()
        next_origin = _http_origin(next_url)
        if next_origin is None or not url_allowed(next_url):
            raise RequestPolicyError(
                f"refusing redirect to disallowed URL {redact_url_secrets(next_url)}"
            )
        if next_origin != current_origin and request_headers:
            request_headers = {
                name: value
                for name, value in request_headers.items()
                if name.lower() not in _SENSITIVE_REDIRECT_HEADERS
            }
            request_kwargs["headers"] = request_headers
        request_kwargs.pop("params", None)
        current_url = next_url

    raise RequestPolicyError("request exceeded 10 redirects")


def _response_body_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if content is not None:
        return bytes(content)

    chunks = list(response.iter_content(DEFAULT_BLOCK_SIZE))
    if chunks:
        return b"".join(chunks)

    text = getattr(response, "text", "")
    return str(text).encode("utf-8")


def copy_capped_body(response: Any, destination: _ByteWriter, cap: int) -> bool:
    """Copy a response incrementally, stopping once it exceeds ``cap``."""
    if cap < 0:
        return False
    total = 0
    streamed = False
    for chunk in response.iter_content(DEFAULT_BLOCK_SIZE):
        streamed = True
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            return False
        destination.write(chunk)
    if streamed:
        return True

    # Some response fakes and adapters expose only a buffered body.
    body = _response_body_bytes(response)
    if len(body) > cap:
        return False
    destination.write(body)
    return True


def read_capped_body(response: Any, cap: int) -> bytes | None:
    """Read a response incrementally, returning ``None`` once it exceeds ``cap``."""
    destination = BytesIO()
    if not copy_capped_body(response, destination, cap):
        return None
    return destination.getvalue()


def parse_html(markup: str) -> BeautifulSoup:
    """Parse HTML with the project's canonical parser.

    BeautifulSoup parsers differ in how they repair broken markup
    """
    return BeautifulSoup(markup, features="lxml")


def parse_xml(markup: str) -> BeautifulSoup:
    """Parse XML with the project's canonical parser."""
    return BeautifulSoup(markup, features="xml")


def session_key_from_html(markup: str) -> str | None:
    soup = parse_html(markup)
    script = soup.find("script", string=lambda text: text and "sesskey" in text)
    if script is None:
        return None
    match = re.search(r'"sesskey"\s*:\s*"(.*?)"', script.text)
    return match.group(1) if match else None


def moodle_user_id_from_html(markup: str) -> int | None:
    soup = parse_html(markup)
    script = soup.find("script", string=lambda text: text and '"userId"' in text)
    if script is None:
        return None
    match = re.search(r'"userId"\s*:\s*"?(\d+)"?', script.text)
    if match is None:
        return None
    user_id = int(match.group(1))
    return user_id if user_id > 0 else None


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
