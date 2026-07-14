import logging
import urllib.parse
from fnmatch import fnmatchcase
from typing import Any

from syncmymoodle.config import Config, PatternConfig
from syncmymoodle.constants import (
    COURSE_PREFIX_HANDLING_OPTIONS,
    COURSE_PREFIX_RE,
    MOODLE_URL,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import RequestPolicyError, redact_url_secrets

logger = logging.getLogger(__name__)


class FilteredRequestError(RequestPolicyError):
    """A request intentionally blocked by a configured URL filter."""


def matching_course_filter_entry(course_id: Any, entries: list[str]) -> str | None:
    """Return the configured entry referencing ``course_id``, if any.

    Entries are course URLs (``.../course/view.php?id=NNN``). The ``id``
    query parameter is compared exactly, so e.g. ``id=12`` does not also
    match courses ``1`` or ``2``. A bare numeric id entry is also accepted.
    """
    course_id = str(course_id)
    for entry in entries:
        parsed = urllib.parse.urlparse(entry)
        if course_id in urllib.parse.parse_qs(parsed.query).get("id", []):
            return entry
        if entry.strip() == course_id:
            return entry
    return None


def pattern_list(value: PatternConfig, course_id: Any = None) -> list[str]:
    patterns = list(value.get("*", []))
    if course_id is not None:
        patterns.extend(value.get(str(course_id), []))
    return patterns


def format_course_name(
    course_name: str, config: Config, log: logging.Logger = logger
) -> str:
    prefix_handling = config.course_prefix_handling
    if prefix_handling == "keep":
        return course_name
    if prefix_handling not in COURSE_PREFIX_HANDLING_OPTIONS:
        log.warning(
            "Unsupported course_prefix_handling value %r; using keep",
            prefix_handling,
        )
        return course_name

    match = COURSE_PREFIX_RE.match(course_name)
    if not match:
        return course_name

    name = match.group("course_name")
    prefix = match.group("prefix")
    if prefix_handling == "remove":
        return name
    return f"{name} ({prefix})"


def matching_pattern(values: list[Any], patterns: list[str]) -> str | None:
    for value in values:
        if value is None:
            continue
        value = str(value)
        for pattern in patterns:
            if value == pattern or fnmatchcase(value, pattern):
                return pattern
    return None


def domain_matches(netloc: str, allowed_domain: str) -> bool:
    host = netloc.split("@")[-1].split(":")[0].lower()
    domain = str(allowed_domain).strip().lower()
    domain = urllib.parse.urlparse(domain).netloc or domain
    domain = domain.split("@")[-1].split(":")[0]
    if not domain:
        return False
    if fnmatchcase(host, domain):
        return True
    if domain.startswith("*."):
        return host.endswith(domain[1:])
    return host == domain or host.endswith(f".{domain}")


def should_skip_url(
    ctx: SyncContext,
    url: str | None,
    context: str = "link",
) -> bool:
    if not url:
        return False

    config = ctx.config
    url = str(url).replace("&amp;", "&")
    pattern = matching_pattern([url], pattern_list(config.exclude_links))
    if pattern is not None:
        ctx.record_filtered(
            "filters.exclude_links",
            "link",
            f"{context}: {redact_url_secrets(url)}",
            f"matches {redact_url_secrets(pattern)!r}",
        )
        return True

    allowed_domains = pattern_list(config.allowed_domains)
    if allowed_domains:
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
            if not any(
                domain_matches(parsed_url.netloc, domain) for domain in allowed_domains
            ):
                ctx.record_filtered(
                    "filters.allowed_domains",
                    "link",
                    f"{context}: {redact_url_secrets(url)}",
                    f"host {parsed_url.hostname or parsed_url.netloc!r} is not allowed",
                )
                return True

    return False


def require_url_allowed(ctx: SyncContext, url: str, context: str) -> bool:
    if should_skip_url(ctx, url, context):
        raise FilteredRequestError(
            f"request excluded by configured filters: {redact_url_secrets(url)}"
        )
    return True


def should_skip_section(
    ctx: SyncContext,
    section: dict[str, Any],
    course_id: Any,
) -> bool:
    config = ctx.config
    patterns = pattern_list(config.exclude_sections, course_id=course_id)
    if not patterns:
        return False

    values = [section.get("name"), section.get("id")]
    pattern = matching_pattern(values, patterns)
    if pattern is not None:
        ctx.record_filtered(
            "filters.exclude_sections",
            "section",
            f"{section.get('name')} ({section.get('id')}) in course {course_id}",
            f"matches {pattern!r}",
        )
        return True
    return False


def should_skip_module(
    ctx: SyncContext,
    module: dict[str, Any],
    course_id: Any,
) -> bool:
    config = ctx.config
    patterns = pattern_list(config.exclude_modules, course_id=course_id)
    if not patterns:
        return False

    module_id = module.get("id")
    module_name = module.get("name")
    modname = module.get("modname")
    module_urls = []
    if module.get("url"):
        module_urls.append(module.get("url"))
    if module_id and modname:
        module_urls.extend(
            [
                f"{MOODLE_URL}mod/{modname}/view.php?id={module_id}",
                f"{MOODLE_URL}mod/{modname}/launch.php?id={module_id}",
            ]
        )

    values = [module_id, module_name, modname, *module_urls]
    pattern = matching_pattern(values, patterns)
    if pattern is not None:
        ctx.record_filtered(
            "filters.exclude_modules",
            "module",
            f"{module_name} ({module_id}) in course {course_id}",
            f"matches {redact_url_secrets(pattern)!r}",
        )
        return True
    return False
