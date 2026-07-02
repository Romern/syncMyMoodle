import logging
import urllib.parse
from fnmatch import fnmatchcase
from typing import Any

from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS, COURSE_PREFIX_RE

logger = logging.getLogger(__name__)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def course_id_in_filter(course_id: Any, entries: Any) -> bool:
    """Return True if ``course_id`` is referenced by a configured entry.

    Entries are course URLs (``.../course/view.php?id=NNN``). The ``id``
    query parameter is compared exactly, so e.g. ``id=12`` does not also
    match courses ``1`` or ``2``. A bare numeric id entry is also accepted.
    """
    course_id = str(course_id)
    for entry in entries or []:
        entry = str(entry)
        parsed = urllib.parse.urlparse(entry)
        if course_id in urllib.parse.parse_qs(parsed.query).get("id", []):
            return True
        if entry.strip() == course_id:
            return True
    return False


def configured_patterns(
    config: dict[str, Any], *keys: str, course_id=None
) -> list[str]:
    patterns = []
    for key in keys:
        value = config.get(key)
        if isinstance(value, dict):
            patterns.extend(as_list(value.get("*")))
            if course_id is not None:
                patterns.extend(as_list(value.get(str(course_id))))
        else:
            patterns.extend(as_list(value))
    return [str(pattern) for pattern in patterns if pattern is not None]


def format_course_name(
    course_name: str, config: dict[str, Any], log: logging.Logger = logger
) -> str:
    prefix_handling = config.get("course_prefix_handling", "keep")
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


def matches_any_pattern(values: list[Any], patterns: list[str]) -> bool:
    for value in values:
        if value is None:
            continue
        value = str(value)
        for pattern in patterns:
            if value == pattern or fnmatchcase(value, pattern):
                return True
    return False


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
    config: dict[str, Any],
    url: str | None,
    context: str = "link",
    log: logging.Logger = logger,
) -> bool:
    if not url:
        return False

    url = str(url).replace("&amp;", "&")
    if matches_any_pattern([url], configured_patterns(config, "exclude_links")):
        log.info("Skipping %s %s because it matches exclude_links", context, url)
        return True

    allowed_domains = configured_patterns(config, "allowed_domains")
    if allowed_domains:
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
            if not any(
                domain_matches(parsed_url.netloc, domain) for domain in allowed_domains
            ):
                log.info(
                    "Skipping %s %s because it is outside allowed_domains",
                    context,
                    url,
                )
                return True

    return False


def should_skip_section(
    config: dict[str, Any],
    section: dict[str, Any],
    course_id: Any,
    log: logging.Logger = logger,
) -> bool:
    patterns = configured_patterns(
        config, "exclude_sections", "skip_sections", course_id=course_id
    )
    if not patterns:
        return False

    values = [section.get("name"), section.get("id")]
    if matches_any_pattern(values, patterns):
        log.info(
            "Skipping section %s (%s) in course %s because it matches "
            "exclude_sections",
            section.get("name"),
            section.get("id"),
            course_id,
        )
        return True
    return False


def should_skip_module(
    config: dict[str, Any],
    module: dict[str, Any],
    course_id: Any,
    log: logging.Logger = logger,
) -> bool:
    patterns = configured_patterns(
        config, "exclude_modules", "skip_modules", course_id=course_id
    )
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
                f"https://moodle.rwth-aachen.de/mod/{modname}/view.php?id={module_id}",
                f"https://moodle.rwth-aachen.de/mod/{modname}/launch.php?id={module_id}",
            ]
        )

    values = [module_id, module_name, modname, *module_urls]
    if matches_any_pattern(values, patterns):
        log.info(
            "Skipping module %s (%s) in course %s because it matches "
            "exclude_modules",
            module_name,
            module_id,
            course_id,
        )
        return True
    return False
