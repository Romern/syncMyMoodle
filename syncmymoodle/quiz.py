"""Quiz review capture: offline HTML snapshots and optional Chromium PDF rendering.

The snapshot pipeline strips active content, inlines same-origin assets as data URIs within size budgets,
converts LaTeX to MathML, removes network-bearing attributes, and pins a restrictive Content-Security-Policy,
so the saved page renders offline without executing or fetching anything."""

import base64
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import latex2mathml.converter
import requests

from syncmymoodle import course_cache, pathing, storage
from syncmymoodle.config import Config
from syncmymoodle.constants import (
    CHROMIUM_BINARY_NAMES,
    CHROMIUM_KNOWN_PATHS,
    CHROMIUM_PDF_TIMEOUT_MS,
    CHROMIUM_PROCESS_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MOODLE_URL,
    QUIZ_ASSET_MAX_BYTES,
    QUIZ_SNAPSHOT_MAX_ASSET_BYTES,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    content_type_without_parameters,
    parse_html,
    parse_xml,
    read_capped_body,
    redact_url_secrets,
    request_following_safe_redirects,
    same_origin,
)
from syncmymoodle.node import Node
from syncmymoodle.outcomes import (
    FAILED_DOWNLOAD,
    HANDLED_DOWNLOAD,
    PLANNED_DOWNLOAD,
    DownloadOutcome,
    completed_download,
)

logger = logging.getLogger(__name__)

CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?[^;]+;", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_FONT_FACE_RE = re.compile(
    r"@font-face\s*\{(?P<body>.*?)}", re.IGNORECASE | re.DOTALL
)
CSS_RULE_RE = re.compile(r"(?P<selectors>[^{}@][^{}]*)\{(?P<body>[^{}]*)}", re.DOTALL)
CSS_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*(?P<value>[^;{}]+)", re.IGNORECASE)
CSS_PSEUDO_ELEMENT_RE = re.compile(
    r"::?(?:after|backdrop|before|cue|cue-region|first-letter|first-line|"
    r"file-selector-button|grammar-error|marker|part\([^)]*\)|placeholder|"
    r"selection|slotted\([^)]*\)|spelling-error|target-text)"
)
TEX_MATH_RE = re.compile(
    r"\\\((?P<inline>.+?)\\\)|\\\[(?P<block>.+?)\\]|\$\$(?P<dollar_block>.+?)\$\$",
    re.DOTALL,
)
ICON_FONT_FAMILY_MARKERS = ("font awesome", "fontawesome")
QUIZ_URL_ATTRS = {
    "action",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "longdesc",
    "manifest",
    "ping",
    "poster",
    "src",
    "srcset",
    "xlink:href",
}


def _is_moodle_asset_url(url: str) -> bool:
    return same_origin(url, MOODLE_URL)


def _quiz_asset_url(raw_url: Any, base_url: str) -> str | None:
    raw = str(raw_url or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("data:"):
        return raw
    if (
        raw.startswith("#")
        or lowered.startswith("javascript:")
        or lowered.startswith("mailto:")
        or lowered.startswith("tel:")
        or lowered.startswith("blob:")
    ):
        return None

    url = urllib.parse.urljoin(base_url, raw)
    if not _is_moodle_asset_url(url):
        return None
    return url


def _response_content_type(
    response: Any,
    url: str,
    default: str = "application/octet-stream",
) -> str:
    content_type = content_type_without_parameters(response)
    if not content_type:
        guessed = mimetypes.guess_type(urllib.parse.urlparse(url).path)[0]
        content_type = guessed or default
    return content_type


def _content_length_too_large(response: Any) -> bool:
    content_length = response.headers.get("Content-Length")
    if not content_length:
        return False
    try:
        return int(content_length) > QUIZ_ASSET_MAX_BYTES
    except ValueError:
        return False


@dataclass
class _QuizAssetContext:
    session: Any | None
    base_url: str
    log: logging.Logger
    remaining_bytes: int = QUIZ_SNAPSHOT_MAX_ASSET_BYTES
    cache: dict[str, str | None] = field(default_factory=dict)

    def fetch_body(
        self,
        url: str,
        accept_content_type: Callable[[str], bool],
        description: str,
        default_content_type: str,
    ) -> tuple[bytes, str, str | None] | None:
        """Fetch one same-origin resource within the per-file and total budgets."""
        if self.session is None:
            return None
        try:
            with closing(
                request_following_safe_redirects(
                    self.session,
                    "GET",
                    url,
                    _is_moodle_asset_url,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    stream=True,
                )
            ) as response:
                if not (200 <= response.status_code < 300):
                    self.log.info(
                        "Skipping quiz snapshot %s %s because Moodle returned HTTP %s",
                        description,
                        redact_url_secrets(url),
                        response.status_code,
                    )
                    return None
                if _content_length_too_large(response):
                    self.log.info(
                        "Skipping oversized quiz snapshot %s %s",
                        description,
                        redact_url_secrets(url),
                    )
                    return None

                content_type = _response_content_type(
                    response, url, default_content_type
                )
                if not accept_content_type(content_type):
                    self.log.info(
                        "Skipping quiz snapshot %s %s with content type %s",
                        description,
                        redact_url_secrets(url),
                        content_type,
                    )
                    return None

                encoding = getattr(response, "encoding", None)
                body = read_capped_body(
                    response, min(QUIZ_ASSET_MAX_BYTES, self.remaining_bytes)
                )
        except (OSError, ValueError, requests.RequestException):
            self.log.info(
                "Skipping quiz snapshot %s %s because it could not be fetched",
                description,
                redact_url_secrets(url),
            )
            return None

        if body is None:
            self.log.info(
                "Skipping oversized quiz snapshot %s %s",
                description,
                redact_url_secrets(url),
            )
            return None

        self.remaining_bytes -= len(body)
        return body, content_type, encoding

    def fetch_data_uri(self, raw_url: Any) -> str | None:
        url = _quiz_asset_url(raw_url, self.base_url)
        if url is None:
            return None
        if url.lower().startswith("data:"):
            return url
        if url in self.cache:
            return self.cache[url]

        fetched = self.fetch_body(
            url,
            # Never embed HTML (login/error pages) as an asset.
            lambda content_type: content_type not in HTML_CONTENT_TYPES,
            "asset",
            "application/octet-stream",
        )
        if fetched is None or not fetched[0]:
            self.cache[url] = None
            return None

        body, content_type, _ = fetched
        encoded = base64.b64encode(body).decode("ascii")
        data_uri = f"data:{content_type};base64,{encoded}"
        self.cache[url] = data_uri
        return data_uri

    def fetch_stylesheet(self, raw_url: Any) -> str | None:
        url = _quiz_asset_url(raw_url, self.base_url)
        if url is None or url.lower().startswith("data:"):
            return None

        def accepts(content_type: str) -> bool:
            if content_type in {"text/css", "text/plain", "application/x-css"}:
                return True
            return Path(urllib.parse.urlparse(url).path).suffix.lower() == ".css"

        fetched = self.fetch_body(url, accepts, "stylesheet", "text/css")
        if fetched is None:
            return None

        body, _, encoding = fetched
        css = body.decode(encoding or "utf-8", errors="replace")
        return _resolve_quiz_css_urls(CSS_IMPORT_RE.sub("", css), url)

    def inline_css_urls(self, css: str, *, fetch_assets: bool = True) -> str:
        css = CSS_IMPORT_RE.sub("", css)

        def replace(match: re.Match[str]) -> str:
            raw_url = match.group(2).strip()
            if not raw_url or raw_url.startswith("#"):
                return match.group(0)
            if raw_url.lower().startswith("data:"):
                return match.group(0)
            if not fetch_assets:
                return 'url("data:,")'
            data_uri = self.fetch_data_uri(raw_url)
            return 'url("data:,")' if data_uri is None else f'url("{data_uri}")'

        return CSS_URL_RE.sub(replace, css)


def _resolve_quiz_css_urls(css: str, base_url: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(2).strip()
        if (
            not raw_url
            or raw_url.startswith("#")
            or raw_url.lower().startswith("data:")
        ):
            return match.group(0)
        url = _quiz_asset_url(raw_url, base_url)
        if url is None:
            return match.group(0)
        return f'url("{url}")'

    return CSS_URL_RE.sub(replace, css)


def _css_font_family_names(value: str) -> set[str]:
    quoted = {
        match.group(1).strip()
        for match in re.finditer(r"""["']([^"']+)["']""", value)
        if match.group(1).strip()
    }
    if quoted:
        return quoted

    families: set[str] = set()
    for part in value.split(","):
        family = part.strip()
        family = re.sub(r"\s*!important\s*$", "", family, flags=re.IGNORECASE)
        family = family.strip()
        if family and not family.lower().startswith(("var(", "inherit", "initial")):
            families.add(family)
    return families


def _css_font_face_families(font_face_body: str) -> set[str]:
    families: set[str] = set()
    for match in CSS_FONT_FAMILY_RE.finditer(font_face_body):
        families.update(_css_font_family_names(match.group("value")))
    return families


def _is_quiz_icon_font_family(family: str) -> bool:
    family_lower = family.lower()
    return any(marker in family_lower for marker in ICON_FONT_FAMILY_MARKERS)


def _split_css_selectors(selectors: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in selectors:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            continue
        if char in "([":  # commas inside :is(), :not(), attributes, etc.
            depth += 1
        elif char in ")]" and depth:
            depth -= 1
        if char == "," and depth == 0:
            selector = "".join(current).strip()
            if selector:
                result.append(selector)
            current = []
            continue
        current.append(char)

    selector = "".join(current).strip()
    if selector:
        result.append(selector)
    return result


def _quiz_selector_matches(soup: Any, selector: str) -> bool:
    selector = CSS_PSEUDO_ELEMENT_RE.sub("", selector).strip()
    if not selector:
        return False
    try:
        return soup.select_one(selector) is not None
    except Exception:
        return False


def _needed_quiz_icon_font_families(css: str, soup: Any) -> set[str]:
    needed: set[str] = set()
    css_without_font_faces = CSS_FONT_FACE_RE.sub("", css)

    for match in CSS_RULE_RE.finditer(css_without_font_faces):
        body = match.group("body")
        if "font-family" not in body.lower():
            continue

        families: set[str] = set()
        for family_match in CSS_FONT_FAMILY_RE.finditer(body):
            families.update(_css_font_family_names(family_match.group("value")))
        icon_families = {
            family for family in families if _is_quiz_icon_font_family(family)
        }
        if not icon_families:
            continue

        if any(
            _quiz_selector_matches(soup, selector)
            for selector in _split_css_selectors(match.group("selectors"))
        ):
            needed.update(icon_families)

    return needed


def _inline_needed_quiz_stylesheet_assets(
    css: str,
    soup: Any,
    assets: _QuizAssetContext,
) -> str:
    needed_icon_families = _needed_quiz_icon_font_families(css, soup)

    def replace_font_face(match: re.Match[str]) -> str:
        families = _css_font_face_families(match.group("body"))
        if not families.intersection(needed_icon_families):
            return ""
        return assets.inline_css_urls(match.group(0))

    css = CSS_FONT_FACE_RE.sub(replace_font_face, css)
    return assets.inline_css_urls(css, fetch_assets=False)


def _ensure_quiz_snapshot_head(soup: Any) -> Any:
    html_tag = soup.find("html")
    if html_tag is None:
        html_tag = soup.new_tag("html")
        html_tag.extend(soup.contents)
        soup.append(html_tag)

    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        html_tag.insert(0, head)
    return head


def _add_quiz_snapshot_meta(soup: Any, head: Any) -> None:
    for meta in soup.find_all("meta"):
        http_equiv = str(meta.get("http-equiv") or "").lower()
        name = str(meta.get("name") or "").lower()
        if http_equiv in {"content-security-policy", "refresh"} or name == "referrer":
            meta.decompose()

    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = (
        "default-src 'none'; "
        "img-src data:; "
        "font-src data:; "
        "style-src 'unsafe-inline'; "
        "script-src 'none'; "
        "connect-src 'none'; "
        "media-src 'none'; "
        "object-src 'none'; "
        "frame-src 'none'; "
        "form-action 'none'; "
        "base-uri 'none'"
    )
    head.insert(0, csp)

    referrer = soup.new_tag("meta")
    referrer["name"] = "referrer"
    referrer["content"] = "no-referrer"
    head.insert(1, referrer)

    # The snapshot is written as UTF-8; make that explicit so it decodes
    # correctly when opened from disk even if the source page relied on the
    # HTTP charset header. Keep it first so detection happens early.
    has_charset = soup.find("meta", attrs={"charset": True}) is not None or any(
        str(meta.get("http-equiv") or "").lower() == "content-type"
        for meta in soup.find_all("meta")
    )
    if not has_charset:
        charset = soup.new_tag("meta")
        charset["charset"] = "utf-8"
        head.insert(0, charset)


def _strip_quiz_snapshot_active_content(soup: Any) -> None:
    for tag in soup.find_all(["script", "iframe", "object", "embed", "video", "audio"]):
        tag.decompose()
    for base in soup.find_all("base"):
        base.decompose()
    for footer in soup.select(
        "footer, #page-footer, #footnote, .footer-popover, "
        ".footer-container, [data-region='footer-container']"
    ):
        footer.decompose()
    for nav in soup.select("nav[aria-label='Site-Navigation'], .activity-navigation"):
        nav.decompose()
    for nav in soup.find_all("div", {"id": "nav-drawer"}):
        nav.decompose()
    for form in soup.find_all("form"):
        form.attrs.pop("action", None)
        form.attrs.pop("method", None)


def _inline_quiz_snapshot_stylesheets(
    soup: Any,
    assets: _QuizAssetContext,
) -> None:
    for link in list(soup.find_all("link")):
        rel = link.get("rel") or []
        rel_values = {str(value).lower() for value in rel}
        href = link.get("href")
        if "stylesheet" in rel_values and href:
            css = assets.fetch_stylesheet(href)
            if css is not None:
                style = soup.new_tag("style")
                style.string = css
                link.replace_with(style)
                continue
        link.decompose()


def _inline_quiz_snapshot_element_assets(
    soup: Any,
    assets: _QuizAssetContext,
) -> None:
    for style in soup.find_all("style"):
        if style.string:
            style.string.replace_with(
                _inline_needed_quiz_stylesheet_assets(
                    str(style.string),
                    soup,
                    assets,
                )
            )

    for tag in soup.find_all(True):
        style_value = tag.get("style")
        if style_value:
            tag["style"] = assets.inline_css_urls(str(style_value))

        if tag.name == "img":
            data_uri = assets.fetch_data_uri(tag.get("src"))
            if data_uri is not None:
                tag["src"] = data_uri
            else:
                tag.attrs.pop("src", None)
                tag["alt"] = tag.get("alt") or "[image omitted from offline snapshot]"
            tag.attrs.pop("srcset", None)


def _remove_quiz_snapshot_network_attributes(soup: Any) -> None:
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            attr_lower = str(attr).lower()
            value = tag.attrs[attr]
            if attr_lower.startswith("on"):
                del tag.attrs[attr]
                continue
            if attr_lower not in QUIZ_URL_ATTRS:
                continue
            value_str = str(value or "").strip()
            if attr_lower in {"src", "xlink:href"} and value_str.lower().startswith(
                "data:"
            ):
                continue
            if attr_lower == "href" and value_str.startswith("#"):
                continue
            del tag.attrs[attr]


def _latex_match_to_node(soup: Any, match: re.Match[str], log: logging.Logger) -> Any:
    """Convert one TEX_MATH_RE match into a MathML tag.

    Falls back to the original text when the expression has no capture or
    cannot be converted.
    """
    display = "inline"
    latex = match.group("inline")
    if latex is None:
        display = "block"
        latex = match.group("block") or match.group("dollar_block")
    if latex is None:
        return soup.new_string(match.group(0))

    try:
        mathml = latex2mathml.converter.convert(latex.strip(), display=display)
        math_tag = parse_xml(mathml).find("math")
    except Exception:
        log.info("Could not convert quiz LaTeX expression to MathML: %s", latex)
        math_tag = None

    if math_tag is None:
        return soup.new_string(match.group(0))
    return math_tag


def _convert_quiz_latex_to_mathml(
    soup: Any,
    log: logging.Logger = logger,
) -> None:
    for text_node in list(soup.find_all(string=TEX_MATH_RE)):
        if text_node.find_parent(["style", "script", "template", "textarea"]):
            continue
        text = str(text_node)
        pieces: list[Any] = []
        last_end = 0
        for match in TEX_MATH_RE.finditer(text):
            if match.start() > last_end:
                pieces.append(soup.new_string(text[last_end : match.start()]))
            pieces.append(_latex_match_to_node(soup, match, log))
            last_end = match.end()

        if last_end < len(text):
            pieces.append(soup.new_string(text[last_end:]))

        for piece in pieces:
            text_node.insert_before(piece)
        text_node.extract()


def build_quiz_snapshot(
    html: str,
    session: Any | None = None,
    base_url: str = MOODLE_URL,
    log: logging.Logger = logger,
) -> str:
    """Turn a fetched quiz-review page into an offline HTML snapshot.

    The output contains a restrictive CSP, no active script/frame content, and
    no network-bearing URL attributes. Stylesheets are kept for layout but their
    unused assets are stripped; direct quiz images and referenced inline-style
    assets are embedded as data URIs with size budgets.
    """
    soup = parse_html(html)
    head = _ensure_quiz_snapshot_head(soup)
    assets = _QuizAssetContext(session, base_url, log)

    _strip_quiz_snapshot_active_content(soup)
    _inline_quiz_snapshot_stylesheets(soup, assets)
    _inline_quiz_snapshot_element_assets(soup, assets)
    _convert_quiz_latex_to_mathml(soup, log)
    _remove_quiz_snapshot_network_attributes(soup)
    _add_quiz_snapshot_meta(soup, head)
    return str(soup)


def find_chromium(config: Config, log: logging.Logger = logger) -> str | None:
    """Locate a Chromium-family browser for PDF rendering.

    Prefers an explicitly configured ``browser``, then binaries on PATH,
    then well-known macOS/Windows install locations. Returns ``None`` when no
    browser is found.
    """
    if config.browser:
        if Path(config.browser).exists():
            return config.browser
        log.warning(
            "Configured browser %s does not exist; falling back to auto-discovery.",
            config.browser,
        )
    for name in CHROMIUM_BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in CHROMIUM_KNOWN_PATHS:
        if Path(path).exists():
            return path
    return None


def render_pdf_with_chromium(
    browser: str,
    html_path: Path,
    pdf_path: Path,
    log: logging.Logger = logger,
) -> bool:
    """Render a local HTML snapshot to PDF via headless Chromium.

    Uses the browser's built-in ``--print-to-pdf`` so we depend only on an
    (actively maintained, sandboxed) browser the user already has.
    Returns ``True`` only if a PDF was produced.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="syncmymoodle-chromium-") as profile:
            cmd = [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-file-system",
                "--disable-javascript",
                "--disable-sync",
                "--js-flags=--jitless",
                "--no-default-browser-check",
                "--no-first-run",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile}",
                f"--virtual-time-budget={CHROMIUM_PDF_TIMEOUT_MS}",
                f"--print-to-pdf={os.fspath(pdf_path)}",
                html_path.resolve().as_uri(),
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=CHROMIUM_PROCESS_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Failed to run %s for quiz PDF rendering: %s", browser, exc)
        return False
    if result.returncode != 0 or not pdf_path.exists():
        log.warning(
            "Chromium (%s) did not produce a quiz PDF (exit code %s).",
            browser,
            result.returncode,
        )
        return False
    return True


def render_quiz_pdf(
    ctx: SyncContext,
    node: Node,
    html_path: Path,
    pdf_path: Path,
    display_path: Path,
    log: logging.Logger = logger,
) -> bool:
    browser = find_chromium(ctx.config, log)
    if browser is None:
        log.warning(
            "No Chromium-family browser found to render the quiz PDF for %s; "
            "keeping the HTML snapshot instead. Install Chrome, Chromium or "
            "Edge, or set 'browser' in the [paths] table of your config.",
            node.name,
        )
        return False

    ctx.output.action("Rendering", display_path, "Quiz PDF")
    pdf_ok = render_pdf_with_chromium(browser, html_path, pdf_path, log)
    if not pdf_ok:
        log.warning(
            "Keeping the HTML snapshot for %s after PDF rendering failed.",
            node.name,
        )
    return pdf_ok


def report_quiz_dry_run(
    ctx: SyncContext,
    html_path: Path,
    pdf_path: Path,
    *,
    want_html: bool,
    want_pdf: bool,
    refresh: bool,
) -> DownloadOutcome:
    outcome = HANDLED_DOWNLOAD
    if want_html and (refresh or not html_path.exists()):
        verb = "Would update" if html_path.exists() else "Would download"
        ctx.output.action(verb, html_path, "Quiz", dry_run=True)
        outcome = outcome.merge(PLANNED_DOWNLOAD)
    if want_pdf and (refresh or not pdf_path.exists()):
        verb = "Would update" if pdf_path.exists() else "Would render"
        ctx.output.action(verb, pdf_path, "Quiz PDF", dry_run=True)
        outcome = outcome.merge(PLANNED_DOWNLOAD)
    return outcome


def _same_quiz_revision(node: Node, old_node: Node | None) -> bool:
    if node.etag is None:
        return True
    return (
        old_node is not None
        and old_node.etag == node.etag
        and old_node.etag_kind == node.etag_kind
    )


def _known_quiz_artifacts(
    node: Node,
    old_node: Node | None,
    baselines: dict[str, storage.FileSnapshot],
) -> set[str]:
    existing = {kind for kind, baseline in baselines.items() if baseline.exists}
    if node.etag is None or (old_node is not None and old_node.is_verified):
        return existing
    if old_node is None:
        return set()
    return existing & old_node.artifact_hashes.keys()


def _initialize_quiz_artifact_hashes(
    node: Node,
    old_node: Node | None,
    same_revision: bool,
) -> None:
    if same_revision and old_node is not None:
        node.artifact_hashes = dict(old_node.artifact_hashes)
    elif not same_revision:
        node.artifact_hashes = {}


def _retain_old_quiz_revision(
    node: Node,
    old_node: Node | None,
    unchanged: int,
) -> DownloadOutcome:
    if old_node is None or not old_node.is_verified:
        node.etag = None
        node.etag_kind = None
        node.artifact_hashes = {}
        return DownloadOutcome(unchanged=unchanged, cache_verified=False)
    node.timemodified = old_node.timemodified
    node.etag = old_node.etag
    node.etag_kind = old_node.etag_kind
    node.artifact_hashes = dict(old_node.artifact_hashes)
    return DownloadOutcome(unchanged=unchanged)


def _temporary_quiz_path(path: Path) -> Path:
    return pathing.with_windows_extended_length_prefix(
        path.with_name(f".{path.name}.smmpart")
    )


def _install_quiz_artifact(
    ctx: SyncContext,
    node: Node,
    kind: str,
    staged_path: Path,
    target_path: Path,
    baseline: storage.FileSnapshot,
    rename_local: bool,
    log: logging.Logger,
) -> DownloadOutcome:
    existed = target_path.exists()
    install_result = storage.install_staged_file(
        staged_path,
        target_path,
        baseline=baseline,
        rename_local=rename_local,
        target_change_policy=ctx.config.conflict_handling,
        description="the updated quiz artifact",
        log=log,
    )
    if install_result is not storage.InstallResult.INSTALLED:
        staged_path.unlink(missing_ok=True)
        if install_result is storage.InstallResult.KEPT_LOCAL:
            return DownloadOutcome(unchanged=1, cache_verified=False)
        return FAILED_DOWNLOAD

    digest = storage.file_sha256(target_path)
    if digest is None:
        return FAILED_DOWNLOAD
    node.artifact_hashes[kind] = digest
    ctx.downloaded_paths.add(target_path)
    return completed_download(existed=existed)


def _quiz_modified_artifacts(
    old_node: Node | None,
    baselines: dict[str, storage.FileSnapshot],
) -> set[str]:
    baseline = old_node if old_node is not None and old_node.is_verified else None
    return {
        kind
        for kind, snapshot in baselines.items()
        if snapshot.exists
        and (
            baseline is None
            or baseline.artifact_hashes.get(kind) is None
            or snapshot.digest != baseline.artifact_hashes[kind]
        )
    }


def _quiz_policy_outcome(
    ctx: SyncContext,
    node: Node,
    old_node: Node | None,
    *,
    same_revision: bool,
    existing: set[str],
    known: set[str],
    modified: set[str],
) -> DownloadOutcome | None:
    replacing_existing = bool(existing - known) or (
        not same_revision and bool(existing)
    )
    pending_revision = (
        same_revision and old_node is not None and not old_node.is_verified
    )
    if replacing_existing and not ctx.config.update_files:
        return (
            FAILED_DOWNLOAD
            if pending_revision
            else _retain_old_quiz_revision(node, old_node, len(existing))
        )
    if modified and ctx.config.conflict_handling == "keep":
        return (
            FAILED_DOWNLOAD
            if pending_revision
            else _retain_old_quiz_revision(node, old_node, len(existing))
        )
    return None


def _stage_quiz_snapshot(
    ctx: SyncContext,
    node: Node,
    html_path: Path,
    log: logging.Logger,
) -> Path | None:
    if node.url is None:
        log.warning("No token-derived quiz review is available for %s", node.name)
        return None
    review_html = ctx.quiz_review_cache.get(node.url)
    if review_html is None:
        log.warning(
            "No token-derived quiz review is available for %s",
            redact_url_secrets(node.url),
        )
        return None
    ctx.output.action("Downloading", html_path, "Quiz")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = _temporary_quiz_path(html_path)
    staged_path.unlink(missing_ok=True)
    try:
        snapshot = build_quiz_snapshot(
            review_html,
            ctx.require_session(),
            node.url,
            log,
        )
        staged_path.write_text(snapshot, encoding="utf-8")
    except (OSError, ValueError):
        log.exception("Failed to create quiz snapshot %s", html_path)
        staged_path.unlink(missing_ok=True)
        return None
    return staged_path


def _prepare_quiz_artifacts(
    ctx: SyncContext,
    node: Node,
    html_path: Path,
    pdf_path: Path,
    *,
    snapshot_needed: bool,
    pdf_needed: bool,
    log: logging.Logger,
) -> tuple[Path | None, Path | None, bool]:
    html_stage = (
        _stage_quiz_snapshot(ctx, node, html_path, log) if snapshot_needed else None
    )
    if snapshot_needed and html_stage is None:
        return None, None, False
    if not pdf_needed:
        return html_stage, None, True

    html_source = html_stage or html_path
    pdf_stage = _temporary_quiz_path(pdf_path)
    pdf_stage.unlink(missing_ok=True)
    if not render_quiz_pdf(ctx, node, html_source, pdf_stage, pdf_path, log):
        pdf_stage.unlink(missing_ok=True)
        return html_stage, None, False
    return html_stage, pdf_stage, True


def _install_prepared_quiz_artifacts(
    ctx: SyncContext,
    node: Node,
    html_path: Path,
    pdf_path: Path,
    html_stage: Path | None,
    pdf_stage: Path | None,
    baselines: dict[str, storage.FileSnapshot],
    *,
    want_html: bool,
    prepared: bool,
    modified: set[str],
    log: logging.Logger,
) -> DownloadOutcome:
    outcome = HANDLED_DOWNLOAD
    rename_conflicts = ctx.config.conflict_handling == "rename"
    install_html = html_stage is not None and (
        (prepared and want_html)
        or (not prepared and not html_path.exists() and not pdf_path.exists())
    )
    if install_html:
        assert html_stage is not None
        outcome = outcome.merge(
            _install_quiz_artifact(
                ctx,
                node,
                "html",
                html_stage,
                html_path,
                baselines["html"],
                rename_conflicts and "html" in modified,
                log,
            )
        )
        html_stage = None
    if not outcome.is_handled:
        if pdf_stage is not None:
            pdf_stage.unlink(missing_ok=True)
        return outcome

    if pdf_stage is not None:
        outcome = outcome.merge(
            _install_quiz_artifact(
                ctx,
                node,
                "pdf",
                pdf_stage,
                pdf_path,
                baselines["pdf"],
                rename_conflicts and "pdf" in modified,
                log,
            )
        )
        expected_html_hash = node.artifact_hashes.get("html")
        if (
            outcome.is_handled
            and not want_html
            and expected_html_hash is not None
            and storage.file_sha256(html_path) == expected_html_hash
        ):
            try:
                html_path.unlink(missing_ok=True)
                node.artifact_hashes.pop("html", None)
            except OSError:
                log.warning("Could not remove temporary quiz snapshot %s", html_path)
    if html_stage is not None:
        html_stage.unlink(missing_ok=True)
    if not prepared:
        outcome = outcome.merge(FAILED_DOWNLOAD)
    return outcome


def download_quiz(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> DownloadOutcome:
    """Save a quiz review attempt as an HTML snapshot and/or a rendered PDF.

    The output is controlled by ``config.quiz_mode`` (off/html/pdf/both). The
    snapshot is always written first because it doubles as the source Chromium
    prints from; in pure ``pdf`` mode it is removed once a PDF exists, but kept
    as a usable fallback when no browser is available.
    """
    mode = ctx.config.quiz_mode
    if mode == "off" or node.parent is None:
        return FAILED_DOWNLOAD
    want_html = mode in ("html", "both")
    want_pdf = mode in ("pdf", "both")

    path = pathing.get_sanitized_node_path(node.parent, Path(ctx.config.sync_directory))
    safe_name = pathing.sanitize_path_part(str(node.name or "quiz")) or "quiz"
    html_path = pathing.with_windows_extended_length_prefix(path / f"{safe_name}.html")
    pdf_path = pathing.with_windows_extended_length_prefix(path / f"{safe_name}.pdf")

    old_node = course_cache.get_old_node_for(ctx, node, log)
    same_revision = _same_quiz_revision(node, old_node)
    refresh = not same_revision
    artifacts = {
        kind: artifact_path
        for kind, artifact_path, wanted in (
            ("html", html_path, want_html),
            ("pdf", pdf_path, want_pdf),
        )
        if wanted
    }
    baselines = {
        "html": storage.snapshot_file(html_path),
        "pdf": storage.snapshot_file(pdf_path),
    }
    artifact_baselines = {kind: baselines[kind] for kind in artifacts}
    existing = {
        kind for kind, baseline in artifact_baselines.items() if baseline.exists
    }
    _initialize_quiz_artifact_hashes(node, old_node, same_revision)
    known = (
        _known_quiz_artifacts(node, old_node, artifact_baselines)
        if same_revision
        else set()
    )
    if same_revision:
        for kind in known:
            digest = artifact_baselines[kind].digest
            if digest is not None:
                node.artifact_hashes.setdefault(kind, digest)
    if same_revision and len(known) == len(artifacts):
        return DownloadOutcome(unchanged=len(known))

    unverified_existing = existing - known if same_revision else set()
    modified = set(unverified_existing)
    if refresh:
        modified.update(_quiz_modified_artifacts(old_node, artifact_baselines))
    policy_outcome = _quiz_policy_outcome(
        ctx,
        node,
        old_node,
        same_revision=same_revision,
        existing=existing,
        known=known,
        modified=modified,
    )
    if policy_outcome is not None:
        return policy_outcome

    if ctx.config.dry_run:
        return report_quiz_dry_run(
            ctx,
            html_path,
            pdf_path,
            want_html=want_html,
            want_pdf=want_pdf,
            refresh=refresh or bool(unverified_existing),
        )

    html_needed = want_html and (refresh or "html" not in known)
    pdf_needed = want_pdf and (refresh or "pdf" not in known)
    snapshot_needed = html_needed or (
        pdf_needed and (refresh or not html_path.exists())
    )
    html_stage, pdf_stage, prepared = _prepare_quiz_artifacts(
        ctx,
        node,
        html_path,
        pdf_path,
        snapshot_needed=snapshot_needed,
        pdf_needed=pdf_needed,
        log=log,
    )
    outcome = DownloadOutcome(unchanged=0 if refresh else len(known))
    return outcome.merge(
        _install_prepared_quiz_artifacts(
            ctx,
            node,
            html_path,
            pdf_path,
            html_stage,
            pdf_stage,
            baselines,
            want_html=want_html,
            prepared=prepared,
            modified=modified,
            log=log,
        )
    )
