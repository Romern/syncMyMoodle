import base64
import hashlib
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from contextlib import closing
from fnmatch import fnmatchcase
from pathlib import Path

import requests
import yt_dlp
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

from syncmymoodle.constants import (
    COURSE_PREFIX_HANDLING_OPTIONS,
    COURSE_PREFIX_RE,
    MOODLE_URL,
    OPENCAST_LINK_RE,
    RWTH_DISRUPTIVE_STATUS_CLASSES,
    RWTH_HOMEPAGE_URL,
    RWTH_MOODLE_STATUS_URL,
    RWTH_SSO_STATUS_URL,
    RWTH_STATUS_URL,
    SCIEBO_LINK_RE,
    YOUTUBE_ID_LENGTH,
    YOUTUBE_LINK_RE,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.node import NAME_CLASH_ID_UNSET, Node
from syncmymoodle.pathing import (
    get_sanitized_node_path,
    make_conflict_path,
    sanitize_path_part,
)
from syncmymoodle.storage import (
    load_cookies_from_data,
    read_private_gzip_json,
    save_session_cookies,
    write_private_gzip_json,
)
from syncmymoodle.totp import totp as generate_totp

logger = logging.getLogger(__name__)


class SyncMyMoodle:
    params = {"lang": "en"}  # Titles for some pages differ
    block_size = 1024
    invalid_chars = '~"#%&*:<>?/\\{|}'

    def __init__(self, config):
        self.ctx = SyncContext(config=config)

    @property
    def config(self):
        return self.ctx.config

    @config.setter
    def config(self, value):
        self.ctx.config = value

    @property
    def session(self):
        return self.ctx.session

    @session.setter
    def session(self, value):
        self.ctx.session = value

    @property
    def session_key(self):
        return self.ctx.session_key

    @session_key.setter
    def session_key(self, value):
        self.ctx.session_key = value

    @property
    def wstoken(self):
        return self.ctx.wstoken

    @wstoken.setter
    def wstoken(self, value):
        self.ctx.wstoken = value

    @property
    def user_id(self):
        return self.ctx.user_id

    @user_id.setter
    def user_id(self, value):
        self.ctx.user_id = value

    @property
    def user_private_access_key(self):
        return self.ctx.user_private_access_key

    @user_private_access_key.setter
    def user_private_access_key(self, value):
        self.ctx.user_private_access_key = value

    @property
    def root_node(self):
        return self.ctx.root_node

    @root_node.setter
    def root_node(self, value):
        self.ctx.root_node = value

    @property
    def _course_caches(self):
        return self.ctx.course_caches

    @_course_caches.setter
    def _course_caches(self, value):
        self.ctx.course_caches = value

    @property
    def _opencast_error_count(self):
        return self.ctx.opencast_error_count

    @_opencast_error_count.setter
    def _opencast_error_count(self, value):
        self.ctx.opencast_error_count = value

    @property
    def _opencast_status_hint_logged(self):
        return self.ctx.opencast_status_hint_logged

    @_opencast_status_hint_logged.setter
    def _opencast_status_hint_logged(self, value):
        self.ctx.opencast_status_hint_logged = value

    @property
    def _sciebo_link_cache(self):
        return self.ctx.sciebo_link_cache

    @_sciebo_link_cache.setter
    def _sciebo_link_cache(self, value):
        self.ctx.sciebo_link_cache = value

    @property
    def _opencast_episode_auth_cache(self):
        return self.ctx.opencast_episode_auth_cache

    @_opencast_episode_auth_cache.setter
    def _opencast_episode_auth_cache(self, value):
        self.ctx.opencast_episode_auth_cache = value

    @property
    def _opencast_track_cache(self):
        return self.ctx.opencast_track_cache

    @_opencast_track_cache.setter
    def _opencast_track_cache(self, value):
        self.ctx.opencast_track_cache = value

    @property
    def _downloaded_paths(self):
        if self.ctx.downloaded_paths is None:
            raise AttributeError("_downloaded_paths")
        return self.ctx.downloaded_paths

    @_downloaded_paths.setter
    def _downloaded_paths(self, value):
        self.ctx.downloaded_paths = value

    def _match_old_cache_child(self, old_node, child):
        """Find the previous cache node corresponding to ``child``, if any."""
        if old_node is None:
            return None
        candidates = [
            c
            for c in getattr(old_node, "children", [])
            if c.name == child.name and c.type == child.type
        ]
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.url == child.url:
                return candidate
        return candidates[0]

    def _node_to_cache_data(self, node: Node, old_node: Node | None = None):
        timemodified = node.timemodified
        etag = node.etag
        is_downloaded = node.is_downloaded
        # If this file was not (re)downloaded this run but a previously
        # downloaded version is still on disk, keep the previously cached version
        # markers. Otherwise the cache would record Moodle's new timemodified/etag
        # for a file we never actually fetched, which either skips the file
        # forever or moves the on-disk copy aside as a spurious conflict on the
        # next run's retry.
        if (
            not node.is_downloaded
            and old_node is not None
            and getattr(old_node, "is_downloaded", False)
            and self.get_sanitized_node_path(node).exists()
        ):
            timemodified = getattr(old_node, "timemodified", None)
            etag = getattr(old_node, "etag", None)
            is_downloaded = True
        return {
            "name": node.name,
            "id": node.id,
            "type": node.type,
            "url": node.url,
            "timemodified": timemodified,
            "etag": etag,
            "name_clash_id": node.name_clash_id,
            "is_downloaded": is_downloaded,
            "children": [
                self._node_to_cache_data(
                    child, self._match_old_cache_child(old_node, child)
                )
                for child in node.children
            ],
        }

    def _node_from_cache_data(self, data, parent=None):
        node = Node(
            data.get("name", ""),
            data.get("id"),
            data.get("type", "Unknown"),
            parent,
            url=data.get("url"),
            timemodified=data.get("timemodified"),
            etag=data.get("etag"),
            name_clash_id=data.get("name_clash_id", NAME_CLASH_ID_UNSET),
            is_downloaded=data.get("is_downloaded", False),
        )
        node.children = [
            self._node_from_cache_data(child, node)
            for child in data.get("children", [])
            if isinstance(child, dict)
        ]
        return node

    def cache_root_node(self):
        """Persist per-course caches into .syncmymoodle_cache files.

        Each course directory beneath basedir receives its own cache file
        containing the course subtree, which makes caching less brittle than
        a single global root cache.
        """
        if not self.root_node:
            return

        for semester_node in self.root_node.children:
            if semester_node.type != "Semester":
                continue
            for course_node in semester_node.children:
                if course_node.type != "Course":
                    continue
                course_path = self.get_sanitized_node_path(course_node)
                # Read the previous course cache before overwriting it, so we can
                # preserve version markers for files that were not downloaded
                # this run (see _node_to_cache_data).
                old_course_root = self._get_course_cache_root(course_node)
                course_path.mkdir(parents=True, exist_ok=True)
                cache_path = course_path / ".syncmymoodle_cache"
                write_private_gzip_json(
                    cache_path,
                    {
                        "format": "syncmymoodle.course-cache.v1",
                        "course": self._node_to_cache_data(
                            course_node, old_course_root
                        ),
                    },
                )

    def _ensure_timemodified_attribute(self, node):
        # Old cached root nodes might not have the timemodified attribute yet.
        if not hasattr(node, "timemodified"):
            node.timemodified = None
        if not hasattr(node, "etag"):
            node.etag = None
        if not hasattr(node, "name_clash_id"):
            node.name_clash_id = getattr(node, "id", None)
        for child in getattr(node, "children", []):
            self._ensure_timemodified_attribute(child)

    def _get_course_node(self, node: Node) -> Node:
        """Return the enclosing course node for the given node."""
        cur = node
        while cur is not None and cur.parent is not None:
            if cur.type == "Course":
                return cur
            cur = cur.parent
        raise Exception("Node is not part of a course subtree")

    def _get_course_cache_root(self, course_node: Node):
        """Load and return the cached course root for the given course node."""
        course_path = self.get_sanitized_node_path(course_node)
        if course_path in self._course_caches:
            return self._course_caches[course_path]

        cache_path = course_path / ".syncmymoodle_cache"
        if not cache_path.exists():
            return None

        payload = read_private_gzip_json(cache_path, "course cache")
        if not isinstance(payload, dict):
            return None
        if payload.get("format") != "syncmymoodle.course-cache.v1":
            logger.warning("Ignoring unsupported course cache format: %s", cache_path)
            return None
        course_data = payload.get("course")
        if not isinstance(course_data, dict):
            return None

        cached_course_root = self._node_from_cache_data(course_data)
        self._ensure_timemodified_attribute(cached_course_root)

        self._course_caches[course_path] = cached_course_root
        return cached_course_root

    def _get_old_node_for(self, node: Node):
        """Return the cached node for this node from the course cache, if any."""
        try:
            course_node = self._get_course_node(node)
        except Exception:
            return None

        cached_course_root = self._get_course_cache_root(course_node)
        if cached_course_root is None:
            return None

        full_path = node.get_path()
        course_path = course_node.get_path()
        # Compute the path segments beneath the course root
        rel_segments = full_path[len(course_path) :]
        if not rel_segments:
            return cached_course_root

        try:
            return cached_course_root.go_to_path(rel_segments)
        except Exception:
            return None

    def _get_or_add_child(self, parent_node, name, id, type):
        for child in parent_node.children:
            if child.name == name and child.type == type:
                return child
        return parent_node.add_child(name, id, type)

    def _add_moodle_file_node(
        self,
        parent_node,
        moodle_filepath,
        filename,
        id,
        type,
        url,
        timemodified=None,
        name_clash_id=NAME_CLASH_ID_UNSET,
    ):
        target_node = parent_node
        path_segments = [
            self.sanitize(segment)
            for segment in str(moodle_filepath or "").strip("/").split("/")
            if segment
        ]

        for segment in path_segments:
            target_node = self._get_or_add_child(target_node, segment, None, "Folder")
            if target_node is None:
                return None

        return target_node.add_child(
            filename,
            id,
            type,
            url=url,
            timemodified=timemodified,
            name_clash_id=name_clash_id,
        )

    def _add_moodle_content_file_node(self, parent_node, content, file_type=None):
        file_url = content.get("fileurl")
        if not file_url:
            return None

        mimetype = content.get("mimetype") or "unknown"
        filename = urllib.parse.urlsplit(file_url).path.split("/")[-1]
        if not filename:
            filename = content.get("filename")
        return self._add_moodle_file_node(
            parent_node,
            "/",
            filename,
            file_url,
            file_type or f"Linked file [{mimetype}]",
            file_url,
            timemodified=content.get("timemodified"),
            name_clash_id=None,
        )

    def _is_direct_moodle_file_content(self, module, content):
        file_url = content.get("fileurl")
        if not file_url or content.get("type") != "file":
            return False

        mimetype = str(content.get("mimetype") or "").split(";", 1)[0].lower()
        if not mimetype or mimetype in {
            "document/unknown",
            "unknown",
            "text/html",
            "application/xhtml+xml",
        }:
            return False
        if mimetype.startswith("text/"):
            return False

        modname = module.get("modname")
        if modname in {"resource", "pdfannotator"}:
            return True

        # Page modules often expose their rendered body as index.html. Keep
        # that path in the HTML scanner, but direct-add binary attachments.
        if modname == "page" and content.get("filename") != "index.html":
            return True

        return False

    def _scan_html_text_for_links(
        self, html_text, base_url, parent_node, course_id, module_title=None
    ):
        if "video-js" in html_text and "<source" in html_text.lower():
            soup = bs(html_text, features="lxml")
            videojs = soup.select_one(".video-js")
            if videojs:
                videojs = videojs.select_one("source")
                if videojs and videojs.get("src"):
                    link = urllib.parse.urljoin(str(base_url or ""), videojs["src"])
                    if not self._should_skip_url(link, "embedded video"):
                        parent_node.add_child(
                            videojs["src"].split("/")[-1],
                            None,
                            "Embedded videojs",
                            url=link,
                        )

        self.scanForLinks(
            html_text,
            parent_node,
            course_id,
            module_title=module_title,
            single=False,
        )

    def _as_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _course_id_in_filter(self, course_id, entries) -> bool:
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

    def _configured_patterns(self, *keys, course_id=None):
        patterns = []
        for key in keys:
            value = self.config.get(key)
            if isinstance(value, dict):
                patterns.extend(self._as_list(value.get("*")))
                if course_id is not None:
                    patterns.extend(self._as_list(value.get(str(course_id))))
            else:
                patterns.extend(self._as_list(value))
        return [str(pattern) for pattern in patterns if pattern is not None]

    def _format_course_name(self, course_name):
        prefix_handling = self.config.get("course_prefix_handling", "keep")
        if prefix_handling == "keep":
            return course_name
        if prefix_handling not in COURSE_PREFIX_HANDLING_OPTIONS:
            logger.warning(
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

    def _matches_any_pattern(self, values, patterns):
        for value in values:
            if value is None:
                continue
            value = str(value)
            for pattern in patterns:
                if value == pattern or fnmatchcase(value, pattern):
                    return True
        return False

    def _domain_matches(self, netloc, allowed_domain):
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

    def _should_skip_url(self, url, context="link"):
        if not url:
            return False

        url = str(url).replace("&amp;", "&")
        if self._matches_any_pattern([url], self._configured_patterns("exclude_links")):
            logger.info("Skipping %s %s because it matches exclude_links", context, url)
            return True

        allowed_domains = self._configured_patterns("allowed_domains")
        if allowed_domains:
            parsed_url = urllib.parse.urlparse(url)
            if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
                if not any(
                    self._domain_matches(parsed_url.netloc, domain)
                    for domain in allowed_domains
                ):
                    logger.info(
                        "Skipping %s %s because it is outside allowed_domains",
                        context,
                        url,
                    )
                    return True

        return False

    def _should_skip_section(self, section, course_id):
        patterns = self._configured_patterns(
            "exclude_sections", "skip_sections", course_id=course_id
        )
        if not patterns:
            return False

        values = [section.get("name"), section.get("id")]
        if self._matches_any_pattern(values, patterns):
            logger.info(
                "Skipping section %s (%s) in course %s because it matches "
                "exclude_sections",
                section.get("name"),
                section.get("id"),
                course_id,
            )
            return True
        return False

    def _should_skip_module(self, module, course_id):
        patterns = self._configured_patterns(
            "exclude_modules", "skip_modules", course_id=course_id
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
        if self._matches_any_pattern(values, patterns):
            logger.info(
                "Skipping module %s (%s) in course %s because it matches "
                "exclude_modules",
                module_name,
                module_id,
                course_id,
            )
            return True
        return False

    def _make_conflict_path(self, path: Path) -> Path:
        return make_conflict_path(path)

    def _local_file_matches_etag(self, path: Path, etag: str) -> bool:
        """Return True if the local file content matches the given ETag hash.

        We currently support strong ETags that contain a plain hex digest for
        MD5 (32 chars), SHA1 (40 chars) or SHA256 (64 chars). Other formats are
        ignored and treated as non-matching.
        """
        # Extract a plausible hex digest from the ETag value, ignoring weak
        # prefixes (W/) and surrounding quotes or algorithm markers.
        match = re.search(r"([0-9a-fA-F]{32,64})", etag)
        if not match:
            return False
        hex_str = match.group(1).lower()

        algo = None
        if len(hex_str) == 32:
            algo = "md5"
        elif len(hex_str) == 40:
            algo = "sha1"
        elif len(hex_str) == 64:
            algo = "sha256"
        else:
            return False

        with path.open("rb") as f:
            digest = hashlib.file_digest(f, algo)
            return digest.hexdigest() == hex_str

    def _log_opencast_backend_issue(self, response_body: str | None = None) -> None:
        """Log additional context for repeated Opencast backend issues.

        We keep the response body at INFO level (only shown with --verbose) and
        emit a hint to the RWTH ITC status page once the error
        counter exceeds a small threshold.
        """
        self._opencast_error_count += 1

        if response_body:
            logger.info(f"Opencast response body (truncated): {response_body[:1000]}")

        if self._opencast_error_count >= 5 and not self._opencast_status_hint_logged:
            logger.warning(
                "Multiple Opencast backend errors occurred. Please check the RWTH "
                "ITC status page before reporting an issue on GitHub: "
                "https://maintenance.itc.rwth-aachen.de/ticket/status/messages/499"
            )
            self._opencast_status_hint_logged = True

    def _check_general_connectivity(self):
        try:
            response = requests.get(RWTH_HOMEPAGE_URL, timeout=10)
        except requests.RequestException as exc:
            logger.warning(
                "General connectivity check to %s failed: %s",
                RWTH_HOMEPAGE_URL,
                exc,
            )
            return False

        if response.status_code >= 500:
            logger.warning(
                "General connectivity check to %s returned status %s",
                RWTH_HOMEPAGE_URL,
                response.status_code,
            )
            return False

        logger.info("General connectivity check to %s succeeded", RWTH_HOMEPAGE_URL)
        return True

    def _current_rwth_service_issues(self, service_name, status_url):
        try:
            response = requests.get(status_url, timeout=10)
        except requests.RequestException as exc:
            logger.warning(
                "Could not fetch RWTH ITC status page for %s: %s", service_name, exc
            )
            return []

        if not (200 <= response.status_code < 300):
            logger.warning(
                "RWTH ITC status page for %s returned status %s",
                service_name,
                response.status_code,
            )
            return []

        soup = bs(response.text, features="lxml")
        issues = []
        for card in soup.select(".notification-card"):
            indicator = card.select_one(".notification-status-indicator")
            status_label = card.select_one(".incident_queue-statuses div")
            if indicator and "old" in indicator.get("class", []):
                continue
            if status_label and "old" in status_label.get("class", []):
                continue

            status_classes = set(status_label.get("class", []) if status_label else [])
            if not status_classes.intersection(RWTH_DISRUPTIVE_STATUS_CLASSES):
                continue

            title = card.select_one(".report_title h3")
            issue_link = card.select_one("[id^=link-to-copy-]")
            issues.append(
                {
                    "service": service_name,
                    "status": (
                        status_label.get_text(" ", strip=True)
                        if status_label
                        else "Status issue"
                    ),
                    "title": (
                        title.get_text(" ", strip=True)
                        if title
                        else "Current service issue"
                    ),
                    "url": (
                        issue_link.get_text(" ", strip=True)
                        if issue_link
                        else status_url
                    ),
                }
            )
        return issues

    def _check_rwth_status_page(self):
        logger.warning("Check the RWTH ITC status page: %s", RWTH_STATUS_URL)
        issues = []
        for service_name, status_url in [
            ("RWTHmoodle", RWTH_MOODLE_STATUS_URL),
            ("RWTH Single Sign-On", RWTH_SSO_STATUS_URL),
        ]:
            issues.extend(self._current_rwth_service_issues(service_name, status_url))

        if not issues:
            logger.info(
                "No current RWTHmoodle or RWTH Single Sign-On outage was found "
                "on the RWTH ITC status pages"
            )
            return

        for issue in issues:
            logger.warning(
                "%s may currently be affected: %s - %s. See %s",
                issue["service"],
                issue["status"],
                issue["title"],
                issue["url"],
            )

    def _check_moodle_availability(self):
        if not self.session:
            raise Exception("You need a requests session first.")

        try:
            response = self.session.get(MOODLE_URL, timeout=15)
        except requests.RequestException as exc:
            logger.critical("Could not reach RWTHmoodle at %s: %s", MOODLE_URL, exc)
            self._check_general_connectivity()
            self._check_rwth_status_page()
            sys.exit(1)

        if response.status_code >= 500:
            logger.critical(
                "RWTHmoodle returned status %s before login",
                response.status_code,
            )
            self._check_rwth_status_page()
            sys.exit(1)

        if response.status_code >= 400:
            logger.warning(
                "RWTHmoodle availability check returned status %s; login may fail",
                response.status_code,
            )
            self._check_rwth_status_page()

        return response

    # RWTH SSO Login

    def login(self):
        def get_session_key(soup):
            script = soup.find("script", string=lambda text: text and "sesskey" in text)
            match = (
                re.search(r'"sesskey":"(.*?)"', script.text)
                if script is not None
                else None
            )
            if match:
                return match.group(1)
            else:
                logger.critical("Can't retrieve session key from JavaScript config")
                sys.exit(1)

        def require_input_value(soup, name, context):
            value = self._get_input_value(soup, name)
            if value is None:
                logger.critical(
                    "Failed to login: expected form field %r was missing at the "
                    "%s. The RWTH login flow may have changed or the servers may "
                    "have difficulties. For current service status, see %s.",
                    name,
                    context,
                    RWTH_STATUS_URL,
                )
                self._check_rwth_status_page()
                logger.info("-------Login-Error-Soup--------")
                logger.info(soup)
                sys.exit(1)
            return value

        self.session = requests.Session()
        cookie_file = Path(self.config.get("cookie_file", "./session")).expanduser()
        cookie_payload = read_private_gzip_json(cookie_file, "session cookie")
        if cookie_payload is not None:
            load_cookies_from_data(self.session.cookies, cookie_payload)
        self._check_moodle_availability()
        try:
            resp = self.session.get(
                urllib.parse.urljoin(MOODLE_URL, "auth/shibboleth/index.php"),
                timeout=15,
            )
        except requests.RequestException as exc:
            logger.critical("Could not reach RWTH SSO login endpoint: %s", exc)
            self._check_general_connectivity()
            self._check_rwth_status_page()
            sys.exit(1)
        if resp.url.startswith("https://moodle.rwth-aachen.de/my/"):
            soup = bs(resp.text, features="lxml")
            self.session_key = get_session_key(soup)
            save_session_cookies(cookie_file, self.session.cookies)
            return

        # Create a separate soup for maintenance detection
        soup_check = bs(resp.text, features="lxml")

        # Remove known info banners by class
        for banner in soup_check.select(".themeboostunioninfobanner"):
            banner.decompose()

        # Also remove Bootstrap-style alert boxes marked as informational alerts
        for alert in soup_check.select('div.alert[role="alert"]'):
            alert.decompose()

        # Extract body text after cleanup
        body = soup_check.find("body")
        body_text = body.get_text(separator=" ", strip=True) if body else ""

        # Check for maintenance notice
        if "Wartungsarbeiten" in body_text:
            logger.critical(
                "Detected Maintenance mode! If this is an error, please report it on GitHub."
            )
            logger.info(f"Cleaned page body:\n{body_text}")
            sys.exit()

        soup = bs(resp.text, features="lxml")
        if soup.find("input", {"name": "RelayState"}) is None:
            csrf_token = require_input_value(
                soup, "csrf_token", "username/password form"
            )
            login_data = {
                "j_username": self.config["user"],
                "j_password": self.config["password"],
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }
            resp2 = self.session.post(resp.url, data=login_data)

            soup = bs(resp2.text, features="lxml")

            if soup.find(id="fudis_selected_token_ids_input") is None:
                logger.critical(
                    "Failed to login. Maybe your login-info was wrong or the "
                    "RWTH servers have difficulties. For current service "
                    "status, see %s. For more info use the --verbose argument.",
                    RWTH_STATUS_URL,
                )
                self._check_rwth_status_page()
                logger.info("-------Login-Error-Soup--------")
                logger.info(soup)
                sys.exit(1)

            csrf_token = require_input_value(
                soup, "csrf_token", "TOTP generator selection form"
            )

            print("Setting TOTP generator")
            totp_selection_data = {
                "fudis_selected_token_ids_input": self.config["totp"],
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }

            resp3 = self.session.post(resp2.url, data=totp_selection_data)

            soup = bs(resp3.text, features="lxml")
            if soup.find(id="fudis_otp_input") is None:
                logger.critical(
                    "Failed to select TOTP generator. Maybe your TOTP serial "
                    "number is wrong or the RWTH servers have difficulties. "
                    "For current service status, see %s. For more info use "
                    "the --verbose argument.",
                    RWTH_STATUS_URL,
                )
                self._check_rwth_status_page()
                logger.info("-------Login-Error-Soup--------")
                logger.info(soup)
                sys.exit(1)

            csrf_token = require_input_value(soup, "csrf_token", "TOTP entry form")
            if not self.config.get("totpsecret"):
                totp_input = input(f"Enter TOTP for generator {self.config['totp']}:\n")
            else:
                totp_input = generate_totp(self.config.get("totpsecret"))
                print(f"Generated TOTP from provided secret: {totp_input}")

            totp_login_data = {
                "fudis_otp_input": totp_input,
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }

            resp4 = self.session.post(resp3.url, data=totp_login_data)

            time.sleep(1)  # if we go too fast, we might have our connection closed
            soup = bs(resp4.text, features="lxml")
        if soup.find("input", {"name": "RelayState"}) is None:
            logger.critical(
                "Failed to login. Maybe your login-info was wrong or the RWTH "
                "servers have difficulties. For current service status, see "
                "%s. For more info use the --verbose argument.",
                RWTH_STATUS_URL,
            )
            self._check_rwth_status_page()
            logger.info("-------Login-Error-Soup--------")
            logger.info(soup)
            sys.exit(1)
        data = {
            "RelayState": require_input_value(soup, "RelayState", "SAML response"),
            "SAMLResponse": require_input_value(soup, "SAMLResponse", "SAML response"),
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST", data=data
        )
        soup = bs(resp.text, features="lxml")
        self.session_key = get_session_key(soup)
        save_session_cookies(cookie_file, self.session.cookies)

    # Moodle Web Services API

    def get_moodle_wstoken(self):
        if not self.session:
            raise Exception("You need to login() first.")
        params = {
            "service": "moodle_mobile_app",
            "passport": 1,
            "urlscheme": "moodlemobile",
        }
        # response = self.session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)

        def getCookies(cookie_jar, domain):
            # workaround for macos
            cookie_dict = cookie_jar.get_dict(domain=domain)
            found = ["%s=%s" % (name, value) for (name, value) in cookie_dict.items()]
            return ";".join(found)

        conn = http.client.HTTPSConnection("moodle.rwth-aachen.de")
        conn.request(
            "GET",
            "/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(params),
            headers={
                "Cookie": getCookies(self.session.cookies, "moodle.rwth-aachen.de")
            },
        )
        response = conn.getresponse()

        # token is in an app schema, which contains the wstoken base64-encoded along with some other token
        location = response.getheader("Location")
        if location is None or "token=" not in location:
            location_path = urllib.parse.urlparse(location).path if location else None
            body_prefix = response.read(1000).decode("utf-8", errors="replace")
            conn.close()

            if location_path and location_path.startswith("/admin/tool/policy/"):
                logger.critical(
                    "RWTHmoodle requires you to accept updated policies/terms "
                    "before syncmymoodle can create a webservice token. Please "
                    "open https://moodle.rwth-aachen.de/ in your browser, accept "
                    "the pending policy page, and rerun syncmymoodle."
                )
                logger.info(
                    "Unexpected mobile launch redirect target: "
                    f"{location_path or '<missing>'}"
                )
                sys.exit(1)

            if location_path == "/login/index.php":
                logger.critical(
                    "Failed to retrieve the Moodle webservice token because "
                    "Moodle redirected back to the login page. Your saved "
                    "session is probably stale or the SSO login did not finish "
                    "correctly. Delete the cookie file and try again."
                )
                logger.info(
                    "Unexpected mobile launch redirect target: "
                    f"{location_path or '<missing>'}"
                )
                sys.exit(1)

            logger.critical(
                "Failed to retrieve the Moodle webservice token because Moodle "
                "returned an unexpected redirect instead of a token."
            )
            logger.info(
                "Unexpected mobile launch redirect target: "
                f"{location_path or '<missing>'}"
            )
            if body_prefix:
                logger.info(
                    "Unexpected mobile launch response body (truncated): "
                    f"{body_prefix}"
                )
            sys.exit(1)

        # The redirect looks like moodlemobile://token=BASE64[&...]; isolate the
        # token value and decode it defensively so a malformed redirect yields a
        # clear message instead of a traceback.
        token_base64d = location.split("token=", 1)[1].split("&")[0]
        conn.close()
        try:
            token_parts = base64.b64decode(token_base64d).decode().split(":::")
        except (ValueError, UnicodeDecodeError):
            token_parts = []
        if len(token_parts) < 2 or not token_parts[1]:
            logger.critical(
                "Failed to parse the Moodle webservice token from the mobile "
                "launch redirect. Your saved session may be stale; delete the "
                "cookie file and try again."
            )
            sys.exit(1)
        self.wstoken = token_parts[1]
        return self.wstoken

    def get_all_courses(self):
        data = {
            "requests[0][function]": "core_enrol_get_users_courses",
            "requests[0][arguments]": json.dumps(
                {"userid": str(self.user_id), "returnusercount": "0"}
            ),
            "requests[0][settingfilter]": 1,
            "requests[0][settingfileurl]": 1,
            "wsfunction": "tool_mobile_call_external_functions",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "tool_mobile_call_external_functions",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return json.loads(resp.json()["responses"][0]["data"])

    def get_course(self, course_id):
        data = {
            "courseid": int(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "core_course_get_contents",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "core_course_get_contents",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return resp.json()

    def get_userid(self):
        data = {
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "core_webservice_get_site_info",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "core_webservice_get_site_info",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        payload = resp.json()
        if not payload.get("userid") or not payload["userprivateaccesskey"]:
            logger.critical(
                f"Error while getting userid and access key: {json.dumps(payload, indent=4)}"
            )
            sys.exit(1)
        self.user_id = payload["userid"]
        self.user_private_access_key = payload["userprivateaccesskey"]
        return self.user_id, self.user_private_access_key

    def get_assignment(self, course_id):
        data = {
            "courseids[0]": int(course_id),
            "includenotenrolledcourses": 1,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_assign_get_assignments",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_assignments",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        courses = resp.json()["courses"]
        return courses[0] if courses else None

    def get_assignment_submission_files(self, assignment_id):
        data = {
            "assignid": assignment_id,
            "userid": self.user_id,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_assign_get_submission_status",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_submission_status",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )

        logger.info(f"------ASSIGNMENT-{assignment_id}-DATA------")
        logger.info(response.text)

        payload = response.json()
        files = payload.get("lastattempt", {}).get("submission", {}).get("plugins", [])
        files += (
            payload.get("lastattempt", {}).get("teamsubmission", {}).get("plugins", [])
        )
        files += payload.get("feedback", {}).get("plugins", [])

        files = [
            f.get("files", [])
            for p in files
            for f in p.get("fileareas", [])
            if f["area"] in ["download", "submission_files", "feedback_files"]
        ]
        files = [f for folder in files for f in folder]
        return files

    def get_folders_by_courses(self, course_id):
        data = {
            "courseids[0]": str(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_folder_get_folders_by_courses",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_folder_get_folders_by_courses",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        folder = response.json()["folders"]
        return folder

    def sync(self):
        """Retrives the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        self.root_node = Node("", -1, "Root", None)

        # Syncing all courses
        for course in self.get_all_courses():
            course_name = self._format_course_name(
                course.get("shortname") or f"course-{course.get('id')}"
            )
            course_id = course["id"]

            selected_courses = self.config.get("selected_courses", [])
            if selected_courses:
                # selected_courses is an explicit allowlist that overrides
                # skip_courses (and, below, only_sync_semester).
                if not self._course_id_in_filter(course_id, selected_courses):
                    continue
            elif self._course_id_in_filter(
                course_id, self.config.get("skip_courses", [])
            ):
                continue

            semestername = (course.get("idnumber") or "")[:4] or "unknown-semester"
            # Skip not selected semesters (selected_courses overrides this)
            if (
                not selected_courses
                and self.config.get("only_sync_semester", [])
                and semestername not in self.config.get("only_sync_semester", [])
            ):
                continue

            semester_node = [
                s for s in self.root_node.children if s.name == semestername
            ]
            if len(semester_node) == 0:
                semester_node = self.root_node.add_child(semestername, None, "Semester")
            else:
                semester_node = semester_node[0]

            course_node = semester_node.add_child(course_name, course_id, "Course")

            print(f"Syncing {course_name}...")
            course_sections = self.get_course(course_id)
            module_names = {
                module.get("modname")
                for section in course_sections
                if isinstance(section, dict)
                for module in section.get("modules", [])
            }

            assignments = None
            if self.config.get("used_modules", {}).get("assign", {}) and (
                "assign" in module_names
            ):
                assignments = self.get_assignment(course_id)
            assignments_by_cmid = {
                assignment["cmid"]: assignment
                for assignment in ((assignments or {}).get("assignments") or [])
                if "cmid" in assignment
            }

            folders = []
            if self.config.get("used_modules", {}).get("folder", {}) and (
                "folder" in module_names
            ):
                folders = self.get_folders_by_courses(course_id)
            folders_by_coursemodule = {
                folder.get("coursemodule"): folder for folder in folders
            }

            logger.info("-----------------------")
            logger.info(f"------{semestername} - {course_name}------")
            logger.info("------COURSE-DATA------")
            logger.info(json.dumps(course))
            logger.info("------ASSIGNMENT-DATA------")
            logger.info(json.dumps(assignments))
            logger.info("------FOLDER-DATA------")
            logger.info(json.dumps(folders))

            for section in course_sections:
                if isinstance(section, str):
                    logger.error(f"Error syncing section in {course_name}: {section}")
                    continue
                if self._should_skip_section(section, course_id):
                    continue
                logger.info("------SECTION-DATA------")
                logger.info(json.dumps(section))
                section_node = course_node.add_child(
                    section["name"], section["id"], "Section"
                )
                for module in section["modules"]:
                    try:
                        if self._should_skip_module(module, course_id):
                            continue

                        # Get Assignments
                        if module["modname"] == "assign" and self.config.get(
                            "used_modules", {}
                        ).get("assign", {}):
                            ass = assignments_by_cmid.get(module["id"])
                            if not ass:
                                continue
                            assignment_id = ass["id"]
                            assignment_name = module["name"]
                            assignment_node = section_node.add_child(
                                assignment_name, assignment_id, "Assignment"
                            )

                            assignment_intro = ass.get("intro")
                            if assignment_intro:
                                self.scanForLinks(
                                    assignment_intro,
                                    assignment_node,
                                    course_id,
                                    module_title=assignment_name,
                                )

                            ass = ass[
                                "introattachments"
                            ] + self.get_assignment_submission_files(assignment_id)
                            for c in ass:
                                if self._should_skip_url(
                                    c.get("fileurl"), "assignment file"
                                ):
                                    continue
                                self._add_moodle_file_node(
                                    assignment_node,
                                    c.get("filepath", "/"),
                                    c["filename"],
                                    c["fileurl"],
                                    "Assignment File",
                                    c["fileurl"],
                                    timemodified=c.get("timemodified"),
                                )

                        # Get Resources or URLs
                        if module["modname"] in [
                            "resource",
                            "url",
                            "book",
                            "page",
                            "pdfannotator",
                        ]:
                            if module["modname"] == "resource" and not self.config.get(
                                "used_modules", {}
                            ).get("resource", {}):
                                continue
                            for c in module.get("contents", []):
                                file_url = c.get("fileurl")
                                if not file_url:
                                    continue
                                if self._should_skip_url(file_url, "resource link"):
                                    continue
                                if self._is_direct_moodle_file_content(module, c):
                                    self._add_moodle_content_file_node(section_node, c)
                                elif not (
                                    module["modname"] == "page"
                                    and c.get("filename") == "index.html"
                                ):
                                    self.scanForLinks(
                                        file_url,
                                        section_node,
                                        course_id,
                                        single=True,
                                        module_title=module["name"],
                                    )

                        # Get Folders
                        if module["modname"] == "folder" and self.config.get(
                            "used_modules", {}
                        ).get("folder", {}):
                            folder_node = section_node.add_child(
                                module["name"], module["id"], "Folder"
                            )

                            # Scan intro for links
                            folder_info = folders_by_coursemodule.get(module["id"])
                            if folder_info and folder_info.get("intro"):
                                self.scanForLinks(
                                    folder_info["intro"], folder_node, course_id
                                )

                            for c in module.get("contents", []):
                                if self._should_skip_url(
                                    c.get("fileurl"), "folder file"
                                ):
                                    continue
                                self._add_moodle_file_node(
                                    folder_node,
                                    c.get("filepath", "/"),
                                    c["filename"],
                                    c["fileurl"],
                                    "Folder File",
                                    c["fileurl"],
                                    timemodified=c.get("timemodified"),
                                )

                        # Get embedded videos in pages or labels
                        if module["modname"] in [
                            "page",
                            "label",
                            "h5pactivity",
                        ] and self.config.get("used_modules", {}).get("url", {}):
                            if module["modname"] == "page":
                                opencast_enabled = (
                                    self.config.get("used_modules", {})
                                    .get("url", {})
                                    .get("opencast", {})
                                )
                                html_url = (
                                    module.get("url")
                                    or f'https://moodle.rwth-aachen.de/mod/page/view.php?id={module["id"]}'
                                )
                                scan_page_links = not self.config.get(
                                    "nolinks"
                                ) and not self._should_skip_url(html_url, "page link")
                                if opencast_enabled or scan_page_links:
                                    try:
                                        response = self.session.get(html_url)
                                    except Exception:
                                        logger.exception(
                                            "Failed to fetch page module %s",
                                            module["id"],
                                        )
                                        response = None
                                    if response and not (
                                        200 <= response.status_code < 300
                                    ):
                                        logger.warning(
                                            "Page module %s returned status %s",
                                            module["id"],
                                            response.status_code,
                                        )
                                        response = None
                                    if response:
                                        if opencast_enabled:
                                            html = bs(
                                                response.text,
                                                features="lxml",
                                            )
                                            for iframe in html.find_all("iframe"):
                                                iframe_src = iframe.get("src")
                                                if not iframe_src:
                                                    continue
                                                iframe_src = urllib.parse.urljoin(
                                                    response.url or html_url,
                                                    iframe_src,
                                                )
                                                vid_id = (
                                                    self._extract_opencast_episode_id(
                                                        iframe_src
                                                    )
                                                )
                                                if not vid_id:
                                                    continue
                                                if not self._authenticate_opencast_episode(
                                                    course_id, vid_id
                                                ):
                                                    continue
                                                vid = self.extractTrackFromEpisode(
                                                    vid_id
                                                )
                                                if not vid:
                                                    continue

                                                if self._should_skip_url(
                                                    vid, "Opencast video URL"
                                                ):
                                                    continue

                                                section_node.add_child(
                                                    module["name"],
                                                    vid_id,
                                                    "Opencast",
                                                    url=vid,
                                                    additional_info=course_id,
                                                )

                                        if scan_page_links:
                                            self._scan_html_text_for_links(
                                                response.text,
                                                response.url or html_url,
                                                section_node,
                                                course_id,
                                                module_title=module["name"],
                                            )
                            # "Interactive" h5p videos
                            elif module["modname"] == "h5pactivity":
                                html_url = f'https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id={module["id"]}'
                                html = bs(
                                    self.session.get(html_url).text,
                                    features="lxml",
                                )
                                # Get h5p iframe
                                iframe = html.find("iframe")
                                iframe_src = iframe.get("src") if iframe else None
                                if iframe_src:
                                    iframe_src = urllib.parse.urljoin(
                                        html_url, iframe_src
                                    )
                                    iframe_html = str(
                                        bs(
                                            self.session.get(iframe_src).text,
                                            features="lxml",
                                        )
                                    )
                                    # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
                                    sanitized_html = iframe_html.replace("\\", "")
                                else:
                                    # H5P outside iframes
                                    sanitized_html = str(html).replace("\\", "")

                                self.scanForLinks(
                                    sanitized_html,
                                    section_node,
                                    course_id,
                                    module_title=module["modname"],
                                    single=False,
                                )
                            else:
                                self.scanForLinks(
                                    module.get("description", ""),
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                )

                        # New OpenCast integration
                        if module["modname"] == "lti" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("opencast", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/lti/launch.php?id={module["id"]}&triggerview=0'
                            try:
                                info_response = self.session.get(info_url)
                            except Exception:
                                logger.exception(
                                    "Opencast: failed to fetch LTI module %s",
                                    module["id"],
                                )
                                continue
                            if not (200 <= info_response.status_code < 300):
                                logger.warning(
                                    "Opencast: LTI module %s returned status %s",
                                    module["id"],
                                    info_response.status_code,
                                )
                                self._log_opencast_backend_issue(info_response.text)
                                continue

                            info_res = bs(info_response.text, features="lxml")

                            engage_series_id = self._get_input_value(
                                info_res, "custom_series"
                            )
                            engage_single_id = self._get_input_value(
                                info_res, "custom_id"
                            )
                            name = (
                                self._get_input_value(info_res, "resource_link_title")
                                or module["name"]
                            )
                            engage_data = self._extract_lti_form_data(info_res)

                            if engage_series_id:
                                # Found an Opencast "series" page
                                series_id = engage_series_id

                                series_node = course_node.add_child(
                                    name, series_id, "Section"
                                )

                                if not self._submit_opencast_lti_form(
                                    engage_data, f"LTI series module {module['id']}"
                                ):
                                    continue

                                series_url = f"https://engage.streaming.rwth-aachen.de/search/episode.json?limit=100&offset=0&sid={series_id}"
                                series_response = self._fetch_opencast_json(
                                    series_url, f"series {series_id}"
                                )
                                if series_response is None:
                                    continue

                                for episode in self._get_opencast_result_list(
                                    series_response, f"series {series_id}"
                                ):
                                    if not isinstance(episode, dict):
                                        continue
                                    mediapackage = episode.get("mediapackage", {})
                                    if not isinstance(mediapackage, dict):
                                        continue
                                    episode_id = mediapackage.get("id")
                                    if not episode_id:
                                        logger.warning(
                                            "Opencast: series %s contains episode without id",
                                            series_id,
                                        )
                                        continue
                                    vid = self.extractTrackFromEpisode(episode_id)
                                    if not vid:
                                        continue
                                    if self._should_skip_url(vid, "Opencast video URL"):
                                        continue
                                    series_node.add_child(
                                        mediapackage.get("title") or episode_id,
                                        episode_id,
                                        "Opencast",
                                        url=vid,
                                        additional_info=module["id"],
                                    )
                            else:
                                if not engage_single_id:
                                    logger.info(
                                        "Failed to find either custom_id or custom_series on lti page."
                                    )
                                    logger.info("------LTI-ERROR-HTML------")
                                    logger.info(f"url: {info_url}")
                                    logger.info(info_res)
                                else:
                                    if not self._submit_opencast_lti_form(
                                        engage_data, f"LTI module {module['id']}"
                                    ):
                                        continue
                                    vid = self.extractTrackFromEpisode(engage_single_id)
                                    if not vid:
                                        continue
                                    if self._should_skip_url(vid, "Opencast video URL"):
                                        continue
                                    section_node.add_child(
                                        name,
                                        engage_single_id,
                                        "Opencast",
                                        url=vid,
                                        additional_info=module["id"],
                                    )
                        # Integration for Quizzes
                        if module["modname"] == "quiz" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("quiz", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/quiz/view.php?id={module["id"]}'
                            info_res = bs(
                                self.session.get(info_url).text, features="lxml"
                            )
                            attempts = info_res.find_all(
                                "a",
                                {
                                    "title": "Überprüfung der eigenen Antworten dieses Versuchs"
                                },
                            )
                            attempt_cnt = 0
                            for attempt in attempts:
                                attempt_cnt += 1
                                review_url = attempt.get("href")
                                quiz_res = bs(
                                    self.session.get(review_url).text,
                                    features="lxml",
                                )
                                name = (
                                    quiz_res.find("title")
                                    .get_text()
                                    .replace(": Überprüfung des Testversuchs", "")
                                    + ", Versuch "
                                    + str(attempt_cnt)
                                )
                                section_node.add_child(
                                    self.sanitize(name),
                                    urllib.parse.urlparse(review_url)[1],
                                    "Quiz",
                                    url=review_url,
                                )

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

    def download_all_files(self):
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        if not self.root_node:
            raise Exception("You need to sync() first.")

        self._download_all_files(self.root_node)

    def _download_all_files(self, cur_node):
        if len(cur_node.children) == 0:
            if cur_node.url and not cur_node.is_downloaded:
                if cur_node.type == "Youtube":
                    try:
                        self.scanAndDownloadYouTube(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.error(
                            "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or your package manager."
                        )
                elif cur_node.type == "Opencast":
                    try:
                        # download Opencast videos
                        if ".mp4" not in cur_node.name:
                            if cur_node.name is not None and cur_node.name != "":
                                cur_node.name += ".mp4"
                            else:
                                cur_node.name = cur_node.url.split("/")[-1]
                        if self.download_file(cur_node):
                            cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                elif cur_node.type == "Quiz":
                    logger.warning(
                        "Skipping quiz PDF generation for %s because it is disabled "
                        "for security.",
                        cur_node.name,
                    )
                else:
                    try:
                        if self.download_file(cur_node):
                            cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
            return

        for child in cur_node.children:
            self._download_all_files(child)

    def get_sanitized_node_path(self, node: Node) -> Path:
        return get_sanitized_node_path(
            node, Path(self.config.get("basedir", "./")), self.invalid_chars
        )

    def sanitize(self, path):
        return sanitize_path_part(path, self.invalid_chars)

    def _content_type_without_parameters(self, response):
        content_type = response.headers.get("Content-Type", "")
        return content_type.split(";", 1)[0].strip().lower()

    def _node_allows_html_download(self, node):
        html_suffixes = {".htm", ".html", ".xhtml"}
        node_suffix = Path(str(node.name or "")).suffix.lower()
        url_suffix = Path(
            urllib.parse.urlparse(str(node.url or "")).path
        ).suffix.lower()
        return node_suffix in html_suffixes or url_suffix in html_suffixes

    def _chunk_looks_like_html(self, chunk):
        body_start = chunk.lstrip().lower()
        return body_start.startswith(b"<!doctype html") or body_start.startswith(
            b"<html"
        )

    def _download_response_is_usable(self, node, response, downloadpath):
        if response.status_code == 204:
            logger.warning(
                "Skipping download of %s from %s because the server returned no "
                "content",
                downloadpath,
                node.url,
            )
            return False

        if not (200 <= response.status_code < 300):
            logger.warning(
                "Skipping download of %s from %s because the server returned "
                "HTTP %s",
                downloadpath,
                node.url,
                response.status_code,
            )
            return False

        content_type = self._content_type_without_parameters(response)
        if content_type in {"text/html", "application/xhtml+xml"}:
            if not self._node_allows_html_download(node):
                logger.warning(
                    "Skipping download of %s from %s because the server returned "
                    "HTML instead of the expected file. This usually means the "
                    "link requires a separate login or points to an error page.",
                    downloadpath,
                    node.url,
                )
                return False

        return True

    def download_file(self, node):
        """Download file with progress bar if it isn't already downloaded"""
        downloadpath = self.get_sanitized_node_path(node)

        if self._should_skip_url(node.url, f"{node.type} file"):
            return True

        # Respect filetype/name exclusions up front so that excluded files never
        # trigger conflict handling, displace local files, or create temp files.
        if node.name.split(".")[-1] in self.config.get("exclude_filetypes", []):
            return True
        if any(
            fnmatchcase(node.name, pattern)
            for pattern in self.config.get("exclude_files", [])
        ):
            return True

        # If we already downloaded this path during the current run, skip any
        # further processing. This avoids duplicate downloads and spurious
        # conflicts when the same remote file appears multiple times in the
        # node tree (e.g. Sciebo links reused in a course).
        if hasattr(self, "_downloaded_paths"):
            if downloadpath in self._downloaded_paths:
                return True
        else:
            # Initialise on first use to keep __init__ simple.
            self._downloaded_paths = set()

        # Decide whether we need to (re-)download the file at all
        cached_timemodified = None
        old_node = None
        conflict_rename_pending = False
        if downloadpath.exists():
            if not self.config.get("updatefiles"):
                return True

            # Try to find a cached node for this file from the per-course cache.
            old_node = self._get_old_node_for(node)
            # Only trust the cached version markers when the previous run
            # actually downloaded the file. Otherwise an update that failed last
            # time (e.g. an expired session) gets cached with Moodle's new
            # timemodified and would be skipped forever, leaving a stale file.
            # Treat a non-downloaded cache entry as if there were no cache at all.
            if old_node is not None and not getattr(old_node, "is_downloaded", False):
                old_node = None
            if old_node is not None:
                cached_timemodified = getattr(old_node, "timemodified", None)
                old_etag = getattr(old_node, "etag", None)
                # If Moodle did not change the file, skip re-download. Only when
                # timemodified is meaningful: Sciebo files have no timemodified
                # (always None), so this must fall through to the etag check
                # below instead of treating None == None as "unchanged".
                if cached_timemodified is not None and (
                    node.timemodified == cached_timemodified
                ):
                    return True
                # For Sciebo, we use the etag from the previous run as the
                # remote version marker. If it matches the current etag from
                # the PROPFIND response, the remote file has not changed.
                if (
                    cached_timemodified is None
                    and old_etag
                    and getattr(node, "etag", None) == old_etag
                ):
                    # Additionally, on the first run with a cache, the local file
                    # may already match this etag (e.g. previously downloaded
                    # manually). If so, we can safely skip any download.
                    if self._local_file_matches_etag(downloadpath, old_etag):
                        return True

            # At this point, either there is no cache for this course/path, or
            # Moodle reports a different modification time. This means the
            # remote file might have changed.

            # Check for potential local modifications since the last sync to avoid
            # silently overwriting user changes.
            conflict_mode = self.config.get("update_files_conflict", "rename")
            if conflict_mode not in {"rename", "keep", "none", "overwrite"}:
                conflict_mode = "rename"

            local_conflict = False
            old_etag = getattr(old_node, "etag", None) if old_node is not None else None
            etag_check_failed = False
            if old_etag:
                # Prefer using the old ETag (hash) to detect whether the local file
                # still matches the previously downloaded version.
                try:
                    if not self._local_file_matches_etag(downloadpath, old_etag):
                        local_conflict = True
                except Exception:
                    # A faulty/unusable ETag cache is treated as if we had no
                    # cached ETag at all: fall back to the timestamp/HEAD
                    # heuristic below to decide whether this is a conflict.
                    etag_check_failed = True

            if not old_etag or etag_check_failed:
                if cached_timemodified is not None:
                    # Fallback: compare local mtime with the previous Moodle timestamp.
                    try:
                        local_mtime = int(downloadpath.stat().st_mtime)
                        if local_mtime != int(cached_timemodified):
                            local_conflict = True
                    except (OSError, ValueError):
                        local_conflict = True
                else:
                    # No previous etag and no previous timemodified: this usually
                    # means the file existed before we ever cached it. Before we
                    # treat this as a conflict, try to see if the local file
                    # already matches the *current* remote content using the
                    # ETag from either the Sciebo PROPFIND or a Moodle HEAD
                    # request.
                    remote_etag = getattr(node, "etag", None)
                    if remote_etag is None and node.url:
                        try:
                            head_resp = self.session.head(
                                node.url, allow_redirects=True
                            )
                            remote_etag = head_resp.headers.get("ETag")
                        except Exception:
                            remote_etag = None

                    if remote_etag and self._local_file_matches_etag(
                        downloadpath, remote_etag
                    ):
                        # Local file already equals the current remote content,
                        # so there is no conflict and no need to download again.
                        node.etag = remote_etag
                        if getattr(node, "timemodified", None) is not None:
                            try:
                                ts = int(node.timemodified)
                                os.utime(downloadpath, (ts, ts))
                            except (OSError, OverflowError, ValueError):
                                pass
                        return True

                    # At this point we know the local file differs from the
                    # current remote version (or we couldn't verify), and we
                    # have no prior cached state. Treat this as a potential
                    # conflict to avoid silently overwriting user changes.
                    local_conflict = True

            if local_conflict:
                if conflict_mode in {"keep", "none"}:
                    # Keep the locally modified file and skip updating from Moodle
                    logger.info(
                        "Detected local changes for %s, skipping Moodle update "
                        "due to update_files_conflict=%s",
                        downloadpath,
                        conflict_mode,
                    )
                    return True
                if conflict_mode == "rename":
                    # Defer moving the locally modified file aside until the
                    # replacement has been fully downloaded, so an aborted or
                    # failed download (e.g. an expired session returning an HTML
                    # error page) never leaves the canonical path empty.
                    conflict_rename_pending = True
                # conflict_mode == "overwrite": fall through and overwrite

        # Hidden, namespaced temp/sidecar names so we never resume from or
        # overwrite a file the user happens to own. The sidecar records the
        # ETag a partial download was fetched against.
        tmp_downloadpath = downloadpath.parent / f".{downloadpath.name}.smmpart"
        etag_sidecar = tmp_downloadpath.with_name(tmp_downloadpath.name + ".etag")

        # Only resume a previous partial when we recorded the ETag it was fetched
        # against, so we can ask the server (via If-Range) to confirm the remote
        # content is unchanged. Without that proof a blind range request could
        # splice bytes from a newer version onto an older partial and silently
        # corrupt the file.
        resume_size = 0
        partial_etag: str | None = None
        header = dict()
        if tmp_downloadpath.exists():
            if etag_sidecar.exists():
                try:
                    partial_etag = etag_sidecar.read_text(encoding="utf-8").strip()
                except OSError:
                    partial_etag = None
            if partial_etag:
                resume_size = tmp_downloadpath.stat().st_size
                header = {"Range": f"bytes={resume_size}-", "If-Range": partial_etag}
            else:
                # Cannot validate the partial; discard it and start fresh.
                tmp_downloadpath.unlink(missing_ok=True)
                etag_sidecar.unlink(missing_ok=True)
        if node.type.lower() == "sciebo file":
            header = {**header, **node.additional_info}

        with closing(
            self.session.get(node.url, headers=header, stream=True)
        ) as response:
            etag_header = response.headers.get("ETag")

            if resume_size:
                # The remote content differs from our partial when the server
                # ignores the range (any non-206) or cannot prove that the
                # returned tail belongs to the same ETag as the saved partial.
                valid_resume = (
                    response.status_code == 206 and etag_header == partial_etag
                )
                version_changed = not valid_resume
                if version_changed:
                    resume_size = 0
                    tmp_downloadpath.unlink(missing_ok=True)
                    etag_sidecar.unlink(missing_ok=True)
                    if response.status_code == 206:
                        # This 206 body is only a tail, and without an exact
                        # ETag match it cannot be safely appended. Restart fresh
                        # on the next run.
                        return False

            if not self._download_response_is_usable(node, response, downloadpath):
                return False

            content = response.iter_content(self.block_size)
            first_chunk = next((chunk for chunk in content if chunk), b"")
            if (
                first_chunk
                and self._chunk_looks_like_html(first_chunk)
                and not self._node_allows_html_download(node)
            ):
                logger.warning(
                    "Skipping download of %s from %s because the response body "
                    "starts with HTML instead of the expected file. This usually "
                    "means the link requires a separate login or points to an "
                    "error page.",
                    downloadpath,
                    node.url,
                )
                return False

            print(f"Downloading {downloadpath} [{node.type}]")
            total_size_in_bytes = int(response.headers.get("content-length", 0)) + max(
                resume_size, 0
            )
            progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
            if resume_size:
                progress_bar.update(resume_size)
            downloadpath.parent.mkdir(parents=True, exist_ok=True)
            # Record the ETag this partial is being fetched against so an
            # interrupted download can be safely resumed next time.
            if etag_header:
                try:
                    etag_sidecar.write_text(etag_header, encoding="utf-8")
                except OSError:
                    pass
            mode = "ab" if resume_size else "wb"
            with tmp_downloadpath.open(mode) as file:
                if first_chunk:
                    progress_bar.update(len(first_chunk))
                    file.write(first_chunk)
                for data in content:
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()

            # The replacement is now fully on disk. Only at this point do we move
            # a conflicting local file aside, so a failure above never empties
            # the canonical path.
            if conflict_rename_pending:
                conflict_path = self._make_conflict_path(downloadpath)
                try:
                    downloadpath.rename(conflict_path)
                    logger.warning(
                        "Detected local changes for %s, moved to %s before "
                        "installing the updated file from Moodle",
                        downloadpath,
                        conflict_path,
                    )
                except OSError:
                    logger.exception(
                        "Failed to move locally modified file %s to %s; keeping "
                        "it and discarding the downloaded update to avoid data "
                        "loss",
                        downloadpath,
                        conflict_path,
                    )
                    tmp_downloadpath.unlink(missing_ok=True)
                    etag_sidecar.unlink(missing_ok=True)
                    return True

            os.replace(tmp_downloadpath, downloadpath)
            etag_sidecar.unlink(missing_ok=True)
            # Align the local mtime with Moodle's timemodified to detect local
            # changes on subsequent runs.
            if getattr(node, "timemodified", None) is not None:
                try:
                    ts = int(node.timemodified)
                    os.utime(downloadpath, (ts, ts))
                except (OSError, OverflowError, ValueError):
                    # If updating timestamps fails, fall back to the current time.
                    pass
            # Persist the ETag of the downloaded file on the node so it can be
            # used on the next run to detect local modifications.
            if etag_header is not None:
                try:
                    node.etag = etag_header
                except Exception:
                    # If for some reason we cannot set it, just ignore.
                    pass
            # Remember that we downloaded this path during the current run.
            self._downloaded_paths.add(downloadpath)
            return True

    def _extract_opencast_episode_id(self, url):
        if not url:
            return None

        url = str(url).replace("&amp;", "&")
        parsed = urllib.parse.urlparse(url)
        episode_ids = urllib.parse.parse_qs(parsed.query).get("episodeid", [])
        if episode_ids and episode_ids[0]:
            return episode_ids[0]

        match = re.match(
            r"^https://engage\.streaming\.rwth-aachen\.de/play/([a-zA-Z0-9-]{36})(?:[/?#].*)?$",
            url,
        )
        if match:
            return match.group(1)

        return None

    def _extract_lti_form_data(self, soup):
        return {
            input_tag["name"]: input_tag.get("value", "")
            for input_tag in soup.find_all("input")
            if input_tag.get("name")
        }

    def _get_input_value(self, soup, name):
        input_tag = soup.find("input", {"name": name})
        if input_tag and input_tag.get("value"):
            return input_tag["value"]
        return None

    def _submit_opencast_lti_form(self, engage_data, context):
        if not engage_data:
            logger.warning("Opencast: missing LTI form fields for %s", context)
            return False

        try:
            response = self.session.post(
                "https://engage.streaming.rwth-aachen.de/lti", data=engage_data
            )
        except Exception:
            logger.exception("Opencast: failed to submit LTI form for %s", context)
            self._log_opencast_backend_issue(None)
            return False

        if not (200 <= response.status_code < 300):
            logger.warning(
                "Opencast: LTI form returned status %s for %s",
                response.status_code,
                context,
            )
            self._log_opencast_backend_issue(response.text)
            return False

        return True

    def _fetch_lti_form_data(self, url, context):
        try:
            response = self.session.get(url)
        except Exception:
            logger.exception("Opencast: failed to fetch LTI form for %s", context)
            self._log_opencast_backend_issue(None)
            return None

        if not (200 <= response.status_code < 300):
            logger.warning(
                "Opencast: LTI form returned status %s for %s",
                response.status_code,
                context,
            )
            self._log_opencast_backend_issue(response.text)
            return None

        soup = bs(response.text, features="lxml")
        engage_data = self._extract_lti_form_data(soup)
        if not engage_data:
            logger.info("Opencast: no LTI form fields found for %s", context)
            logger.info("------LTI-ERROR-HTML------")
            logger.info(f"url: {url}")
            logger.info(soup)
            return None

        return engage_data

    def _authenticate_opencast_episode(self, course_id, episode_id):
        if not self.session_key:
            logger.warning("Opencast: cannot launch episode without Moodle sesskey")
            return False

        cache_key = (course_id, episode_id)
        if cache_key in self._opencast_episode_auth_cache:
            return True

        params = urllib.parse.urlencode(
            {
                "courseid": course_id,
                "episodeid": episode_id,
                "sesskey": self.session_key,
                "ocinstanceid": 1,
            }
        )
        info_url = (
            f"https://moodle.rwth-aachen.de/filter/opencast/ltilaunch.php?{params}"
        )
        context = f"episode {episode_id} in course {course_id}"
        engage_data = self._fetch_lti_form_data(info_url, context)
        if engage_data is None:
            return False
        if not self._submit_opencast_lti_form(engage_data, context):
            return False
        self._opencast_episode_auth_cache.add(cache_key)
        return True

    def _fetch_opencast_json(self, url, context):
        try:
            response = self.session.get(url)
        except Exception:
            logger.exception("Opencast: failed to fetch %s from %s", context, url)
            self._log_opencast_backend_issue(None)
            return None

        if not (200 <= response.status_code < 300):
            logger.error(
                "Opencast: %s returned status %s for %s",
                context,
                response.status_code,
                url,
            )
            self._log_opencast_backend_issue(response.text)
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.error("Opencast: failed to decode JSON for %s from %s", context, url)
            self._log_opencast_backend_issue(response.text)
            return None

        if not isinstance(payload, dict):
            logger.warning(
                "Opencast: expected JSON object for %s, got %s",
                context,
                type(payload).__name__,
            )
            self._log_opencast_backend_issue(response.text)
            return None

        if payload.get("error") or payload.get("errorcode"):
            logger.error(
                "Opencast: %s returned error%s: %s",
                context,
                f" {payload.get('errorcode')}" if payload.get("errorcode") else "",
                payload.get("error") or payload,
            )
            self._log_opencast_backend_issue(response.text)
            return None

        return payload

    def _get_opencast_result_list(self, payload, context):
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list):
            logger.warning("Opencast: missing result list for %s", context)
            self._log_opencast_backend_issue(
                json.dumps(payload, ensure_ascii=False) if payload is not None else None
            )
            return []
        if not result:
            logger.warning("Opencast: empty result list for %s", context)
            return []
        return result

    def _resolution_width(self, resolution):
        match = re.match(r"(\d+)\s*x\s*\d+", str(resolution or ""))
        if not match:
            return 0
        return int(match.group(1))

    def extractTrackFromEpisode(self, episode_id):
        if episode_id in self._opencast_track_cache:
            return self._opencast_track_cache[episode_id]

        episode_url = (
            "https://engage.streaming.rwth-aachen.de/search/episode.json"
            f"?id={episode_id}"
        )
        episodejson = self._fetch_opencast_json(episode_url, f"episode {episode_id}")
        if episodejson is None:
            return False

        tracks = []
        for entry in self._get_opencast_result_list(
            episodejson, f"episode {episode_id}"
        ):
            if not isinstance(entry, dict):
                continue
            mediapackage = entry.get("mediapackage")
            media = (
                mediapackage.get("media") if isinstance(mediapackage, dict) else None
            )
            track_data = media.get("track") if isinstance(media, dict) else None
            if isinstance(track_data, dict):
                track_data = [track_data]
            if not isinstance(track_data, list):
                continue
            for track in track_data:
                if not isinstance(track, dict):
                    continue
                video = track.get("video")
                url = track.get("url")
                if (
                    url
                    and track.get("mimetype") == "video/mp4"
                    and "transport" not in track
                    and isinstance(video, dict)
                ):
                    tracks.append(
                        (self._resolution_width(video.get("resolution")), url)
                    )

        if not tracks:
            logger.warning(
                "Opencast: no downloadable mp4 track found for %s", episode_id
            )
            return False

        # Prefer the highest resolution plain HTTPS mp4 track.
        track_url = sorted(tracks, key=lambda track: track[0])[-1][1]
        self._opencast_track_cache[episode_id] = track_url
        return track_url

    def scanAndDownloadYouTube(self, node):
        """Download Youtube-Videos using yt_dlp"""
        path = self.get_sanitized_node_path(node.parent)
        link = node.url
        if self._should_skip_url(link, "YouTube link"):
            return True
        if path.exists():
            if any(link[-YOUTUBE_ID_LENGTH:] in f.name for f in path.iterdir()):
                return False
        ydl_opts = {
            "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
            "ignoreerrors": True,
            "nooverwrites": True,
            "retries": 15,
            "match_filter": yt_dlp.match_filter_func("!is_live"),
        }
        path.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
        return True

    def downloadQuiz(self, node):
        logger.warning(
            "Quiz PDF generation is disabled until the pdfkit/wkhtmltopdf "
            "renderer is replaced with a safer implementation."
        )
        return False

    def scanForLinks(
        self, text, parent_node, course_id, module_title=None, single=False
    ):
        # A single link is supplied and the contents of it are checked
        if single:
            try:
                text = text.replace("webservice/pluginfile.php", "pluginfile.php")
                if self._should_skip_url(text, "link"):
                    return
                response = self.session.head(text, allow_redirects=True)
                content_type = self._content_type_without_parameters(response)
                if "youtube.com" in text or "youtu.be" in text:
                    # workaround for youtube providing bad headers when using HEAD
                    pass
                elif (
                    200 <= response.status_code < 300
                    and content_type
                    and content_type not in {"text/html", "application/xhtml+xml"}
                ):
                    # non html links, assume the filename is in the path
                    filename = urllib.parse.urlsplit(text).path.split("/")[-1]
                    parent_node.add_child(
                        filename,
                        None,
                        f'Linked file [{response.headers["Content-Type"]}]',
                        url=text,
                    )
                    # instantly return as it was a direct link
                    return
                elif not self.config.get("nolinks"):
                    response = self.session.get(text)
                    self._scan_html_text_for_links(
                        response.text,
                        response.url or text,
                        parent_node,
                        course_id,
                        module_title=module_title,
                    )
            except Exception:
                # Maybe the url is down?
                logger.exception(f"Error while downloading url {text}")
        if self.config.get("nolinks"):
            return

        # Youtube videos
        if self.config.get("used_modules", {}).get("url", {}).get("youtube", {}):
            youtube_links = [
                match.group(1)
                # finds youtube.com, youtu.be and embed links
                for match in YOUTUBE_LINK_RE.finditer(text)
            ]
            for link in youtube_links:
                if self._should_skip_url(link, "YouTube link"):
                    continue
                parent_node.add_child(
                    f"Youtube: {module_title or link}", link, "Youtube", url=link
                )

        # OpenCast videos
        if self.config.get("used_modules", {}).get("url", {}).get("opencast", {}):
            opencast_links = OPENCAST_LINK_RE.findall(text)
            for vid in opencast_links:
                if self._should_skip_url(vid, "Opencast link"):
                    continue
                vid_id = self._extract_opencast_episode_id(vid)
                if not vid_id:
                    logger.warning(
                        f"Opencast: could not extract episode id from url {vid}"
                    )
                    continue
                if not self._authenticate_opencast_episode(course_id, vid_id):
                    continue
                vid = self.extractTrackFromEpisode(vid_id)
                if not vid:
                    continue
                if self._should_skip_url(vid, "Opencast video URL"):
                    continue

                parent_node.add_child(
                    module_title or vid.split("/")[-1],
                    vid_id,
                    "Opencast",
                    url=vid,
                    additional_info=course_id,
                )

        # https://rwth-aachen.sciebo.de/s/XXX
        if self.config.get("used_modules", {}).get("url", {}).get("sciebo", {}):
            sciebo_links = set(SCIEBO_LINK_RE.findall(text))
            sciebo_url = "https://rwth-aachen.sciebo.de"
            webdav_location = "/public.php/webdav/"
            for link in sciebo_links:
                logger.info(f"Found Sciebo Link: {link}")
                if self._should_skip_url(link, "Sciebo link"):
                    continue
                cached_sciebo_root = self._sciebo_link_cache.get(link)
                if cached_sciebo_root is not None:
                    if any(
                        child.name == cached_sciebo_root.name
                        and child.type == cached_sciebo_root.type
                        for child in parent_node.children
                    ):
                        continue
                    parent_node.children.append(cached_sciebo_root.clone(parent_node))
                    continue

                # get the download page
                try:
                    response = self.session.get(link)
                except Exception:
                    logger.exception(f"Failed to fetch Sciebo link {link}")
                    continue

                # parse html code
                soup = bs(response.text, features="lxml")

                # get the requesttoken
                requestToken = (
                    soup.head.get("data-requesttoken")
                    if soup.head is not None
                    else None
                )
                if not requestToken:
                    logger.warning(
                        "Sciebo: missing request token for link %s, skipping", link
                    )
                    continue
                logger.info(f"Sciebo request token: {requestToken}")

                # get the property value of the input tag with the name sharingToken
                sharing_input = soup.find("input", {"name": "sharingToken"})
                if not sharing_input or not sharing_input.get("value"):
                    logger.warning(
                        "Sciebo: missing sharingToken for link %s, skipping", link
                    )
                    continue
                sharingToken = sharing_input["value"]
                logger.info(f"Sciebo sharingToken: {sharingToken}")

                # get baseauthentication secret
                baseAuthSecret = base64.b64encode(
                    f"{sharingToken}:null".encode()
                ).decode()
                logger.info("Sciebo base auth secret derived")

                # get auth header
                auth_header = {
                    "Authorization": f"Basic {baseAuthSecret}",
                    "requesttoken": requestToken,
                }

                sciebo_root = parent_node.add_child(
                    f"sciebo-{sharingToken}", None, "Sciebo Folder"
                )
                if sciebo_root is None:
                    # Duplicate folder/link, nothing more to do here
                    continue

                # recursive function to get all files in the sciebo folder
                def get_sciebo_files(
                    href: str, parent_node: Node, sharingToken: str, auth_header: dict
                ):

                    # request the URL with the PROPFIND method and a body that
                    # also asks Sciebo/Nextcloud to include content checksums
                    # (oc:checksums) for each item. These checksums are stable
                    # content hashes (e.g. SHA1) and allow us to safely compare
                    # local files against the current remote content without
                    # relying on ETags.
                    propfind_body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:getlastmodified/>
    <d:getetag/>
    <oc:checksums/>
  </d:prop>
</d:propfind>"""
                    headers = {
                        **auth_header,
                        "Depth": "1",
                        "Content-Type": "application/xml",
                    }
                    try:
                        propfind_response = self.session.request(
                            "PROPFIND",
                            sciebo_url + href,
                            headers=headers,
                            data=propfind_body,
                        )
                    except Exception:
                        logger.exception(
                            "Sciebo PROPFIND failed for href %s (share %s)",
                            href,
                            sharingToken,
                        )
                        return

                    if not (200 <= propfind_response.status_code < 300):
                        logger.warning(
                            "Sciebo PROPFIND returned status %s for href %s (share %s)",
                            propfind_response.status_code,
                            href,
                            sharingToken,
                        )
                        return

                    # parse the response
                    soup_xml = bs(propfind_response.text, features="xml")

                    for resp in soup_xml.find_all("d:response"):
                        # get the href of the response
                        href_tag = resp.find("d:href")
                        if href_tag is None or not href_tag.text:
                            continue
                        new_href = href_tag.text

                        if new_href == href:
                            logger.info(
                                "Sciebo: skipping %s because it is the current folder",
                                new_href,
                            )
                            continue

                        # Extract a stable content hash for this item. Prefer the
                        # SHA1 checksum from oc:checksums if available; fall back
                        # to the raw ETag otherwise.
                        etag_value = None
                        prop = resp.find("d:prop")
                        if prop is not None:
                            checksums_tag = prop.find("oc:checksums")
                            if checksums_tag is not None:
                                for cs in checksums_tag.find_all("oc:checksum"):
                                    text = (cs.text or "").strip()
                                    if text.upper().startswith("SHA1:"):
                                        etag_value = text.split(":", 1)[1]
                                        break

                            if etag_value is None:
                                etag_tag = prop.find("d:getetag")
                                if etag_tag and etag_tag.text:
                                    etag_value = etag_tag.text.strip()

                        logger.info(f"Sciebo response href: {new_href}")
                        # get the displayname of the response
                        displayname = (
                            new_href.split("/")[-2]
                            if new_href.endswith("/")
                            else new_href.split("/")[-1]
                        )
                        displayname = (
                            f"sciebo-{sharingToken}"
                            if displayname == "webdav"
                            else displayname
                        )

                        # check if the response is a folder
                        if new_href.endswith("/"):
                            # create a new node for the folder
                            folder_node = parent_node.add_child(
                                displayname, None, "Sciebo Folder", etag=etag_value
                            )
                            if folder_node is None:
                                continue
                            # recursive call to get all files in the folder
                            get_sciebo_files(
                                new_href, folder_node, sharingToken, auth_header
                            )
                        else:
                            # create a new node for the file
                            parent_node.add_child(
                                displayname,
                                None,
                                "Sciebo File",
                                url=sciebo_url + new_href,
                                additional_info=auth_header,
                                etag=etag_value,
                            )

                get_sciebo_files(
                    webdav_location, sciebo_root, sharingToken, auth_header
                )
                self._sciebo_link_cache[link] = sciebo_root.clone()
