import subprocess
from types import SimpleNamespace

from syncmymoodle import quiz
from syncmymoodle.node import Node

from .helpers import FakeResponse, FakeSession, make_context

QUIZ_URL = "https://moodle.rwth-aachen.de/mod/quiz/review.php?attempt=5"
CSS_URL = "https://moodle.rwth-aachen.de/theme/styles.css"
IMG_URL = "https://moodle.rwth-aachen.de/pluginfile.php/1/question.png"
BG_URL = "https://moodle.rwth-aachen.de/theme/bg.png"
CSS_ONLY_ASSET_URL = "https://moodle.rwth-aachen.de/theme/theme-only.png"
FONT_URL = "https://moodle.rwth-aachen.de/theme/fa-solid.woff2"
TEXT_FONT_URL = "https://moodle.rwth-aachen.de/theme/free-sans.woff2"
QUIZ_HTML = (
    "<html><head><title>Test: X</title>"
    '<link rel="stylesheet" href="/theme/styles.css">'
    '<script src="https://example.test/leak.js"></script>'
    "</head><body>"
    '<nav aria-label="Site-Navigation">Site nav</nav>'
    "<div id='nav-drawer'>navigation</div>"
    '<img src="/pluginfile.php/1/question.png" srcset="/pluginfile.php/1/big.png 2x">'
    '<img src="https://example.test/tracker.png">'
    '<a href="https://example.test/leak">external</a>'
    "<p style=\"background-image: url('/theme/bg.png')\">"
    "My answer with <span class='nolink'>\\(\\sigma_i \\mapsto G_i\\)</span>"
    "</p>"
    '<i class="icon fa fa-check"></i>'
    '<nav class="activity-navigation">Activity nav</nav>'
    '<footer id="page-footer">Moodle footer</footer>'
    '<div id="footnote">RWTH footnote footer</div>'
    "</body></html>"
)


def quiz_context(tmp_path, mode):
    ctx = make_context(
        {
            "basedir": str(tmp_path),
            "used_modules": {
                "assign": True,
                "resource": True,
                "folder": True,
                "url": {
                    "youtube": True,
                    "opencast": True,
                    "sciebo": True,
                    "quiz": mode,
                },
            },
        }
    )
    session = FakeSession()
    session.add(
        "GET",
        QUIZ_URL,
        FakeResponse(text=QUIZ_HTML, headers={"Content-Type": "text/html"}),
    )
    session.add(
        "GET",
        CSS_URL,
        FakeResponse(
            text=(
                "@import 'https://example.test/leak.css';"
                "@font-face{font-family:'Font Awesome 6 Free';"
                "src:url('fa-solid.woff2') format('woff2');}"
                "@font-face{font-family:'FreeSans';"
                "src:url('free-sans.woff2') format('woff2');}"
                ".fa{font-family:'Font Awesome 6 Free';font-weight:900}"
                ".fa-check:before{content:'\\f00c'}"
                "body{color:black;background:url('theme-only.png')}"
            ),
            headers={"Content-Type": "text/css"},
        ),
    )
    session.add(
        "GET",
        IMG_URL,
        FakeResponse(
            headers={"Content-Type": "image/png"},
            chunks=[b"question-image"],
        ),
    )
    session.add(
        "GET",
        BG_URL,
        FakeResponse(headers={"Content-Type": "image/png"}, chunks=[b"background"]),
    )
    session.add(
        "GET",
        FONT_URL,
        FakeResponse(headers={"Content-Type": "font/woff2"}, chunks=[b"font-awesome"]),
    )
    ctx.session = session
    return ctx


def quiz_node():
    root = Node("root", None, "Root", None)
    node = root.add_child("My Quiz, Versuch 1", 1, "Quiz", url=QUIZ_URL)
    assert node is not None
    return node


def test_build_quiz_snapshot_is_self_contained_and_network_silent(tmp_path):
    ctx = quiz_context(tmp_path, "html")

    snapshot = quiz.build_quiz_snapshot(QUIZ_HTML, ctx.session, QUIZ_URL)

    assert "Content-Security-Policy" in snapshot
    assert "default-src 'none'" in snapshot
    assert "data:image/png;base64," in snapshot
    assert "data:font/woff2;base64," in snapshot
    assert 'url("data:,")' in snapshot
    assert "My answer" in snapshot
    assert "<math" in snapshot
    assert "\\(" not in snapshot
    assert "\\mapsto" not in snapshot
    assert "Site nav" not in snapshot
    assert "nav-drawer" not in snapshot
    assert "Activity nav" not in snapshot
    assert "Moodle footer" not in snapshot
    assert "RWTH footnote footer" not in snapshot
    assert "<script" not in snapshot
    assert "<link" not in snapshot
    assert "srcset" not in snapshot
    assert "https://" not in snapshot
    assert "/pluginfile.php" not in snapshot
    assert ctx.session.count("GET", CSS_URL) == 1
    assert ctx.session.count("GET", IMG_URL) == 1
    assert ctx.session.count("GET", BG_URL) == 1
    assert ctx.session.count("GET", CSS_ONLY_ASSET_URL) == 0
    assert ctx.session.count("GET", FONT_URL) == 1
    assert ctx.session.count("GET", TEXT_FONT_URL) == 0


def test_quiz_latex_conversion_skips_stylesheets(tmp_path):
    ctx = quiz_context(tmp_path, "html")
    html = (
        "<html><head>"
        "<style>.size-\\(raw-css\\){background:url('/theme/theme-only.png')}</style>"
        "</head><body><p>\\(x_i\\)</p></body></html>"
    )

    snapshot = quiz.build_quiz_snapshot(html, ctx.session, QUIZ_URL)

    assert ".size-\\(raw-css\\)" in snapshot
    assert 'url("data:,")' in snapshot
    assert snapshot.count("<math") == 1
    assert ctx.session.count("GET", CSS_ONLY_ASSET_URL) == 0


def test_quiz_stylesheet_keeps_only_used_icon_fonts(tmp_path):
    ctx = quiz_context(tmp_path, "html")
    html = (
        "<html><head>"
        '<link rel="stylesheet" href="/theme/styles.css">'
        "</head><body><p>No icon here</p></body></html>"
    )

    snapshot = quiz.build_quiz_snapshot(html, ctx.session, QUIZ_URL)

    assert "data:font/woff2;base64," not in snapshot
    assert ctx.session.count("GET", FONT_URL) == 0
    assert ctx.session.count("GET", TEXT_FONT_URL) == 0


def test_html_mode_writes_snapshot_without_browser(tmp_path, monkeypatch, capsys):
    # A browser must never be needed for HTML output.
    monkeypatch.setattr(
        quiz,
        "find_chromium",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError),
    )
    ctx = quiz_context(tmp_path, "html")
    node = quiz_node()
    html_path = tmp_path / "root" / "My Quiz, Versuch 1.html"

    assert quiz.download_quiz(ctx, node) is True
    assert capsys.readouterr().out == f"Downloading {html_path} [Quiz]\n"
    assert html_path.exists()
    snapshot = html_path.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in snapshot
    assert "https://" not in snapshot
    assert not (tmp_path / "root" / "My Quiz, Versuch 1.pdf").exists()


def test_html_mode_is_idempotent(tmp_path, capsys):
    ctx = quiz_context(tmp_path, "html")
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is True
    capsys.readouterr()
    assert quiz.download_quiz(ctx, node) is True
    assert capsys.readouterr().out == ""
    # The page is fetched only on the first run; the second is a no-op.
    assert ctx.session.count("GET", QUIZ_URL) == 1


def test_pdf_mode_removes_html_on_success(tmp_path, monkeypatch, capsys):
    def fake_render(browser, html_path, pdf_path, log=quiz.logger):
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return True

    monkeypatch.setattr(quiz, "find_chromium", lambda *a, **k: "/fake/chrome")
    monkeypatch.setattr(quiz, "render_pdf_with_chromium", fake_render)

    ctx = quiz_context(tmp_path, "pdf")
    node = quiz_node()
    html_path = tmp_path / "root" / "My Quiz, Versuch 1.html"
    pdf_path = tmp_path / "root" / "My Quiz, Versuch 1.pdf"

    assert quiz.download_quiz(ctx, node) is True
    assert capsys.readouterr().out == (
        f"Downloading {html_path} [Quiz]\nRendering {pdf_path} [Quiz PDF]\n"
    )
    assert pdf_path.exists()
    assert not html_path.exists()


def test_pdf_mode_keeps_html_when_no_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(quiz, "find_chromium", lambda *a, **k: None)

    ctx = quiz_context(tmp_path, "pdf")
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is False
    # Falls back to the HTML snapshot so the attempt is not lost, but returns
    # False so the missing requested PDF is retried on future runs.
    assert (tmp_path / "root" / "My Quiz, Versuch 1.html").exists()
    assert not (tmp_path / "root" / "My Quiz, Versuch 1.pdf").exists()


def test_both_mode_retries_pdf_without_refetching(tmp_path, monkeypatch):
    # First run: no browser, so only the HTML snapshot is written.
    monkeypatch.setattr(quiz, "find_chromium", lambda *a, **k: None)
    ctx = quiz_context(tmp_path, "both")
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is False
    html_path = tmp_path / "root" / "My Quiz, Versuch 1.html"
    assert html_path.exists()
    assert ctx.session.count("GET", QUIZ_URL) == 1

    # Second run: a browser is now available. The PDF must be produced by
    # rendering the existing snapshot, without re-fetching the page or assets.
    def fake_render(browser, html_path, pdf_path, log=quiz.logger):
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return True

    monkeypatch.setattr(quiz, "find_chromium", lambda *a, **k: "/fake/chrome")
    monkeypatch.setattr(quiz, "render_pdf_with_chromium", fake_render)

    assert quiz.download_quiz(ctx, node) is True
    assert (tmp_path / "root" / "My Quiz, Versuch 1.pdf").exists()
    # No additional page fetch, no additional asset fetches.
    assert ctx.session.count("GET", QUIZ_URL) == 1
    assert ctx.session.count("GET", CSS_URL) == 1
    assert ctx.session.count("GET", IMG_URL) == 1


def test_snapshot_declares_utf8_charset(tmp_path):
    # The source QUIZ_HTML has no charset meta, so one must be injected.
    ctx = quiz_context(tmp_path, "html")
    snapshot = quiz.build_quiz_snapshot(QUIZ_HTML, ctx.session, QUIZ_URL)
    assert 'charset="utf-8"' in snapshot


def test_download_quiz_rejects_non_quiz_redirect(tmp_path):
    ctx = quiz_context(tmp_path, "html")
    ctx.session.routes.clear()
    ctx.session.add(
        "GET",
        QUIZ_URL,
        FakeResponse(
            text="<html>login</html>",
            status_code=200,
            headers={"Content-Type": "text/html"},
            url="https://moodle.rwth-aachen.de/login/index.php",
        ),
    )
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is False
    assert not (tmp_path / "root").exists()


def test_both_mode_writes_html_and_pdf(tmp_path, monkeypatch):
    def fake_render(browser, html_path, pdf_path, log=quiz.logger):
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return True

    monkeypatch.setattr(quiz, "find_chromium", lambda *a, **k: "/fake/chrome")
    monkeypatch.setattr(quiz, "render_pdf_with_chromium", fake_render)

    ctx = quiz_context(tmp_path, "both")
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is True
    assert (tmp_path / "root" / "My Quiz, Versuch 1.html").exists()
    assert (tmp_path / "root" / "My Quiz, Versuch 1.pdf").exists()


def test_off_mode_does_nothing(tmp_path):
    ctx = quiz_context(tmp_path, "off")
    node = quiz_node()

    assert quiz.download_quiz(ctx, node) is False
    assert not (tmp_path / "root").exists()
    assert ctx.session.count("GET", QUIZ_URL) == 0


def test_find_chromium_prefers_configured_path(tmp_path):
    browser = tmp_path / "my-chromium"
    browser.write_text("#!/bin/sh\n")
    ctx = quiz_context(tmp_path, "pdf")
    ctx.config.chromium_path = str(browser)
    assert quiz.find_chromium(ctx.config) == str(browser)


def test_find_chromium_auto_discovers_on_path(tmp_path, monkeypatch):
    ctx = quiz_context(tmp_path, "pdf")
    monkeypatch.setattr(
        quiz.shutil,
        "which",
        lambda name: "/usr/bin/chromium" if name == "chromium" else None,
    )
    assert quiz.find_chromium(ctx.config) == "/usr/bin/chromium"


def test_find_chromium_returns_none_when_missing(tmp_path, monkeypatch):
    ctx = quiz_context(tmp_path, "pdf")
    monkeypatch.setattr(quiz.shutil, "which", lambda name: None)
    monkeypatch.setattr(quiz, "CHROMIUM_KNOWN_PATHS", ())
    assert quiz.find_chromium(ctx.config) is None


def test_render_pdf_with_chromium_success(tmp_path, monkeypatch):
    html_path = tmp_path / "in.html"
    html_path.write_text("<html></html>", encoding="utf-8")
    pdf_path = tmp_path / "out.pdf"
    command = []
    run_kwargs = {}

    def fake_run(cmd, **kwargs):
        command.extend(cmd)
        run_kwargs.update(kwargs)
        # The output path is passed via --print-to-pdf=<path>.
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(quiz, "CHROMIUM_PROCESS_TIMEOUT_SECONDS", 12)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert quiz.render_pdf_with_chromium("/fake/chrome", html_path, pdf_path)
    assert pdf_path.exists()
    assert "--no-pdf-header-footer" in command
    assert "--disable-file-system" in command
    assert "--disable-javascript" in command
    assert "--js-flags=--jitless" in command
    assert run_kwargs["timeout"] == 12


def test_render_pdf_with_chromium_failure(tmp_path, monkeypatch):
    html_path = tmp_path / "in.html"
    html_path.write_text("<html></html>", encoding="utf-8")
    pdf_path = tmp_path / "out.pdf"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stderr=b"boom"),
    )
    assert quiz.render_pdf_with_chromium("/fake/chrome", html_path, pdf_path) is False
    assert not pdf_path.exists()


def test_download_quiz_applies_long_path_check_after_file_extension(
    tmp_path, monkeypatch
):
    ctx = quiz_context(tmp_path, "html")
    node = quiz_node()
    checked_paths = []

    def record_path(path):
        checked_paths.append(path)
        return path

    monkeypatch.setattr(
        quiz.pathing, "with_windows_extended_length_prefix", record_path
    )

    assert quiz.download_quiz(ctx, node)

    assert any(str(path).endswith("My Quiz, Versuch 1.html") for path in checked_paths)
    assert any(str(path).endswith("My Quiz, Versuch 1.pdf") for path in checked_paths)
