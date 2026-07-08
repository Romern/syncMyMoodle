"""Quiz review capture: offline HTML snapshots and optional Chromium PDF rendering.

The snapshot pipeline strips active content, inlines same-origin assets as
data URIs within size budgets, converts LaTeX to MathML, removes network-
bearing attributes, and pins a restrictive Content-Security-Policy, so the
saved page renders offline without executing or fetching anything.
"""

import base64
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any

import latex2mathml.converter
from bs4 import BeautifulSoup as bs

from syncmymoodle import pathing
from syncmymoodle.config import Config
from syncmymoodle.constants import (
    CHROMIUM_BINARY_NAMES,
    CHROMIUM_KNOWN_PATHS,
    CHROMIUM_PDF_TIMEOUT_MS,
    CHROMIUM_PROCESS_TIMEOUT_SECONDS,
    DEFAULT_BLOCK_SIZE,
    MOODLE_NETLOC,
    MOODLE_URL,
    QUIZ_ASSET_MAX_BYTES,
    QUIZ_SNAPSHOT_MAX_ASSET_BYTES,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    content_type_without_parameters,
    parse_html,
)

logger = logging.getLogger(__name__)

CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?[^;]+;", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_FONT_FACE_RE = re.compile(
    r"@font-face\s*\{(?P<body>.*?)\}", re.IGNORECASE | re.DOTALL
)
CSS_RULE_RE = re.compile(r"(?P<selectors>[^{}@][^{}]*)\{(?P<body>[^{}]*)\}", re.DOTALL)
CSS_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*(?P<value>[^;{}]+)", re.IGNORECASE)
CSS_PSEUDO_ELEMENT_RE = re.compile(
    r"::?(?:after|backdrop|before|cue|cue-region|first-letter|first-line|"
    r"file-selector-button|grammar-error|marker|part\([^)]*\)|placeholder|"
    r"selection|slotted\([^)]*\)|spelling-error|target-text)"
)
TEX_MATH_RE = re.compile(
    r"\\\((?P<inline>.+?)\\\)|\\\[(?P<block>.+?)\\\]|\$\$(?P<dollar_block>.+?)\$\$",
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
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != MOODLE_NETLOC:
        return None
    return url


def _response_body_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if content is not None:
        return bytes(content)

    chunks = list(response.iter_content(DEFAULT_BLOCK_SIZE))
    if chunks:
        return b"".join(chunks)

    text = getattr(response, "text", "")
    return str(text).encode("utf-8")


def _read_capped_body(response: Any, cap: int) -> bytes | None:
    """Read a response body incrementally, returning None once it exceeds cap.

    Streaming means an asset that omits Content-Length cannot buffer an
    unbounded amount into memory before we notice it is too large.
    """
    if cap < 0:
        return None
    total = 0
    chunks: list[bytes] = []
    streamed = False
    for chunk in response.iter_content(DEFAULT_BLOCK_SIZE):
        streamed = True
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    if streamed:
        return b"".join(chunks)

    # No streamed content (e.g. a fake exposing only .text/.content); fall back
    # to the buffered body but still honor the cap.
    body = _response_body_bytes(response)
    return body if len(body) <= cap else None


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


def _fetch_quiz_body(
    session: Any,
    url: str,
    remaining_bytes: list[int],
    accept_content_type: Any,
    description: str,
    default_content_type: str,
    log: logging.Logger = logger,
) -> tuple[bytes, str, str | None] | None:
    """Shared, size-capped transport for quiz snapshot resources.

    Performs the streamed same-origin GET with the status, Content-Length,
    content-type and byte-budget checks in one place. Returns ``(body, content_type,
    response_encoding)`` or ``None`` when the resource must be skipped.
    """
    try:
        with closing(session.get(url, timeout=15, stream=True)) as response:
            if not (200 <= response.status_code < 300):
                log.info(
                    "Skipping quiz snapshot %s %s because Moodle returned HTTP %s",
                    description,
                    url,
                    response.status_code,
                )
                return None
            if _content_length_too_large(response):
                log.info("Skipping oversized quiz snapshot %s %s", description, url)
                return None

            content_type = _response_content_type(response, url, default_content_type)
            if not accept_content_type(content_type):
                log.info(
                    "Skipping quiz snapshot %s %s with content type %s",
                    description,
                    url,
                    content_type,
                )
                return None

            encoding = getattr(response, "encoding", None)
            body = _read_capped_body(
                response, min(QUIZ_ASSET_MAX_BYTES, remaining_bytes[0])
            )
    except Exception:
        log.info(
            "Skipping quiz snapshot %s %s because it could not be fetched",
            description,
            url,
        )
        return None

    if body is None:
        log.info("Skipping oversized quiz snapshot %s %s", description, url)
        return None

    remaining_bytes[0] -= len(body)
    return body, content_type, encoding


def _fetch_quiz_asset_data_uri(
    session: Any,
    raw_url: Any,
    base_url: str,
    asset_cache: dict[str, str | None],
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> str | None:
    url = _quiz_asset_url(raw_url, base_url)
    if url is None:
        return None
    if url.lower().startswith("data:"):
        return url
    if url in asset_cache:
        return asset_cache[url]

    fetched = _fetch_quiz_body(
        session,
        url,
        remaining_bytes,
        # Never embed HTML (login/error pages) as an asset.
        lambda ct: ct not in HTML_CONTENT_TYPES,
        "asset",
        "application/octet-stream",
        log,
    )
    if fetched is None or not fetched[0]:
        asset_cache[url] = None
        return None

    body, content_type, _ = fetched
    encoded = base64.b64encode(body).decode("ascii")
    data_uri = f"data:{content_type};base64,{encoded}"
    asset_cache[url] = data_uri
    return data_uri


def _fetch_quiz_stylesheet(
    session: Any,
    raw_url: Any,
    base_url: str,
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> str | None:
    url = _quiz_asset_url(raw_url, base_url)
    if url is None or url.lower().startswith("data:"):
        return None

    def accepts(content_type: str) -> bool:
        if content_type in {"text/css", "text/plain", "application/x-css"}:
            return True
        return Path(urllib.parse.urlparse(url).path).suffix.lower() == ".css"

    fetched = _fetch_quiz_body(
        session, url, remaining_bytes, accepts, "stylesheet", "text/css", log
    )
    if fetched is None:
        return None

    body, _, encoding = fetched
    css = body.decode(encoding or "utf-8", errors="replace")
    return _resolve_quiz_css_urls(CSS_IMPORT_RE.sub("", css), url)


def _inline_quiz_css_urls(
    css: str,
    session: Any | None,
    base_url: str,
    asset_cache: dict[str, str | None],
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> str:
    css = CSS_IMPORT_RE.sub("", css)

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(2).strip()
        if not raw_url or raw_url.startswith("#"):
            return match.group(0)
        if raw_url.lower().startswith("data:"):
            return match.group(0)
        if session is None:
            return 'url("data:,")'
        data_uri = _fetch_quiz_asset_data_uri(
            session,
            raw_url,
            base_url,
            asset_cache,
            remaining_bytes,
            log,
        )
        if data_uri is None:
            return 'url("data:,")'
        return f'url("{data_uri}")'

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
    session: Any | None,
    base_url: str,
    asset_cache: dict[str, str | None],
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> str:
    needed_icon_families = _needed_quiz_icon_font_families(css, soup)

    def replace_font_face(match: re.Match[str]) -> str:
        families = _css_font_face_families(match.group("body"))
        if not families.intersection(needed_icon_families):
            return ""
        return _inline_quiz_css_urls(
            match.group(0), session, base_url, asset_cache, remaining_bytes, log
        )

    css = CSS_FONT_FACE_RE.sub(replace_font_face, css)
    return _inline_quiz_css_urls(css, None, base_url, {}, remaining_bytes, log)


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
    session: Any | None,
    base_url: str,
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> None:
    for link in list(soup.find_all("link")):
        rel = link.get("rel") or []
        rel_values = {str(value).lower() for value in rel}
        href = link.get("href")
        if "stylesheet" in rel_values and href and session is not None:
            css = _fetch_quiz_stylesheet(
                session,
                href,
                base_url,
                remaining_bytes,
                log,
            )
            if css is not None:
                style = soup.new_tag("style")
                style.string = css
                link.replace_with(style)
                continue
        link.decompose()


def _inline_quiz_snapshot_element_assets(
    soup: Any,
    session: Any | None,
    base_url: str,
    asset_cache: dict[str, str | None],
    remaining_bytes: list[int],
    log: logging.Logger = logger,
) -> None:
    for style in soup.find_all("style"):
        if style.string:
            style.string.replace_with(
                _inline_needed_quiz_stylesheet_assets(
                    str(style.string),
                    soup,
                    session,
                    base_url,
                    asset_cache,
                    remaining_bytes,
                    log,
                )
            )

    for tag in soup.find_all(True):
        style_value = tag.get("style")
        if style_value:
            tag["style"] = _inline_quiz_css_urls(
                str(style_value),
                session,
                base_url,
                asset_cache,
                remaining_bytes,
                log,
            )

        if tag.name == "img":
            data_uri = (
                _fetch_quiz_asset_data_uri(
                    session,
                    tag.get("src"),
                    base_url,
                    asset_cache,
                    remaining_bytes,
                    log,
                )
                if session is not None
                else None
            )
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
        math_tag = bs(mathml, features="xml").find("math")
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
    referenced assets are stripped, direct quiz images and inline-style assets
    are embedded as data URIs with size budgets.
    """
    soup = parse_html(html)
    head = _ensure_quiz_snapshot_head(soup)
    asset_cache: dict[str, str | None] = {}
    remaining_bytes = [QUIZ_SNAPSHOT_MAX_ASSET_BYTES]

    _strip_quiz_snapshot_active_content(soup)
    _inline_quiz_snapshot_stylesheets(
        soup,
        session,
        base_url,
        remaining_bytes,
        log,
    )
    _inline_quiz_snapshot_element_assets(
        soup,
        session,
        base_url,
        asset_cache,
        remaining_bytes,
        log,
    )
    _convert_quiz_latex_to_mathml(soup, log)
    _remove_quiz_snapshot_network_attributes(soup)
    _add_quiz_snapshot_meta(soup, head)
    return str(soup)


def find_chromium(config: Config, log: logging.Logger = logger) -> str | None:
    """Locate a Chromium-family browser for PDF rendering.

    Prefers an explicitly configured ``chromium_path``, then binaries on PATH,
    then well-known macOS/Windows install locations. Returns ``None`` when no
    browser is found.
    """
    if config.chromium_path:
        if Path(config.chromium_path).exists():
            return config.chromium_path
        log.warning(
            "Configured chromium_path %s does not exist; falling back to "
            "auto-discovery.",
            config.chromium_path,
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


def quiz_response_is_usable(
    response: Any,
    requested_url: str,
    log: logging.Logger = logger,
) -> bool:
    if not (200 <= response.status_code < 300):
        log.warning(
            "Skipping quiz snapshot for %s because Moodle returned HTTP %s",
            requested_url,
            response.status_code,
        )
        return False

    content_type = content_type_without_parameters(response)
    if content_type and content_type not in HTML_CONTENT_TYPES:
        log.warning(
            "Skipping quiz snapshot for %s because Moodle returned %s",
            requested_url,
            content_type,
        )
        return False

    final_url = response.url or requested_url
    parsed = urllib.parse.urlparse(final_url)
    if parsed.netloc and parsed.netloc.lower() != MOODLE_NETLOC:
        log.warning(
            "Skipping quiz snapshot for %s because it redirected to %s",
            requested_url,
            final_url,
        )
        return False
    if parsed.path and not parsed.path.endswith("/mod/quiz/review.php"):
        log.warning(
            "Skipping quiz snapshot for %s because the response URL is not a "
            "quiz review page: %s",
            requested_url,
            final_url,
        )
        return False
    return True


def download_quiz(ctx: SyncContext, node: Any, log: logging.Logger = logger) -> bool:
    """Save a quiz review attempt as an HTML snapshot and/or a rendered PDF.

    The output is controlled by ``config.quiz_mode`` (off/html/pdf/both). The
    snapshot is always written first because it doubles as the source Chromium
    prints from; in pure ``pdf`` mode it is removed once a PDF exists, but kept
    as a usable fallback when no browser is available.
    """
    mode = ctx.config.quiz_mode
    if mode == "off":
        return False
    want_html = mode in ("html", "both")
    want_pdf = mode in ("pdf", "both")

    path = pathing.get_sanitized_node_path(node.parent, Path(ctx.config.basedir))
    safe_name = pathing.sanitize_path_part(str(node.name or "quiz")) or "quiz"
    html_path = pathing.with_windows_extended_length_prefix(path / f"{safe_name}.html")
    pdf_path = pathing.with_windows_extended_length_prefix(path / f"{safe_name}.pdf")

    # Idempotency: skip when every wanted artifact is already on disk.
    html_done = html_path.exists() if want_html else True
    pdf_done = pdf_path.exists() if want_pdf else True
    if html_done and pdf_done:
        return True

    # Only (re)build the snapshot when it is not already on disk. When just the
    # PDF is missing (e.g. a "both"/"pdf" run that previously found no browser),
    # we render from the existing snapshot rather than re-downloading the page
    # and re-inlining every asset.
    if not html_path.exists():
        print(f"Downloading {html_path} [Quiz]")
        try:
            response = ctx.require_session().get(node.url)
        except Exception:
            log.exception("Failed to fetch quiz page %s", node.url)
            return False
        if not quiz_response_is_usable(response, node.url, log):
            return False

        path.mkdir(parents=True, exist_ok=True)
        html_path.write_text(
            build_quiz_snapshot(
                response.text,
                ctx.require_session(),
                response.url or node.url,
                log,
            ),
            encoding="utf-8",
        )

    if not want_pdf or pdf_path.exists():
        return True

    browser = find_chromium(ctx.config, log)
    if browser is None:
        log.warning(
            "No Chromium-family browser found to render the quiz PDF for %s; "
            "keeping the HTML snapshot instead. Install Chrome, Chromium or "
            "Edge, or set 'chromium_path' in your config.",
            node.name,
        )
        pdf_ok = False
    else:
        print(f"Rendering {pdf_path} [Quiz PDF]")
        pdf_ok = render_pdf_with_chromium(browser, html_path, pdf_path, log)
        if not pdf_ok:
            log.warning(
                "Keeping the HTML snapshot for %s after PDF rendering failed.",
                node.name,
            )

    # In pure "pdf" mode the HTML was only a means to the PDF; drop it on
    # success but keep it as a fallback when rendering was not possible.
    if mode == "pdf" and pdf_ok:
        html_path.unlink(missing_ok=True)

    return not want_pdf or pdf_ok
