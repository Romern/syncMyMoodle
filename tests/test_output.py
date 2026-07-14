import io
import logging
import sys
import time

import pytest

from syncmymoodle.output import RunStatistics, TerminalOutput


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def test_redirected_output_is_plain_unwrapped_and_markup_safe(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    terminal = TerminalOutput("auto")
    message = f"Course [bold red]not markup[/] {'x' * 100}"

    terminal.phase(f"{message}\x1b[31m")
    with terminal.transfer("lecture.mp4", total=10) as progress:
        progress.advance(10)

    assert stdout.getvalue() == f"{message}\n"
    assert "\x1b[" not in stdout.getvalue()
    assert terminal.interactive is False

    stdout.seek(0)
    stdout.truncate()
    monkeypatch.setenv("FORCE_COLOR", "1")
    forced_color = TerminalOutput("auto")
    with forced_color.transfer("lecture.mp4", total=10) as progress:
        progress.advance(10)
    assert forced_color.interactive is False
    assert stdout.getvalue() == ""


@pytest.mark.parametrize(
    ("mode", "no_color", "has_ansi"),
    [
        ("always", False, True),
        ("never", False, False),
        ("always", True, False),
    ],
)
def test_color_modes_and_no_color(monkeypatch, mode, no_color, has_ansi):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    TerminalOutput(mode).success("Complete")

    assert ("\x1b[" in stdout.getvalue()) is has_ansi


def test_warnings_and_logging_use_stderr_without_parsing_markup(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("always")
    logger = logging.Logger("test-output")
    logger.addHandler(terminal.logging_handler())

    terminal.warning("course [red]name[/]")
    logger.error(f"download [bold]failed[/] {'x' * 100}")

    assert stdout.getvalue() == ""
    assert "course [red]name[/]" in stderr.getvalue()
    assert "download [bold]failed[/]" in stderr.getvalue()
    assert len(stderr.getvalue().splitlines()) == 2


def test_cautions_and_failures_are_colored_on_stdout(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.delenv("NO_COLOR", raising=False)
    terminal = TerminalOutput("always")

    terminal.caution("Needs attention")
    terminal.failure("Invalid state")

    assert stdout.getvalue() == (
        "\x1b[33mNeeds attention\x1b[0m\n\x1b[31mInvalid state\x1b[0m\n"
    )
    assert stderr.getvalue() == ""


def test_prompts_share_colors_streams_defaults_and_secret_handling(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    answers = iter(["", " YES "])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    monkeypatch.setattr(
        "syncmymoodle.output.getpass.getpass",
        lambda prompt: "private-password" if prompt == "" else pytest.fail(),
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    terminal = TerminalOutput("always")

    selected = terminal.prompt("Course [red]name[/]", "all")
    confirmed = terminal.confirm("Continue")
    secret = terminal.prompt_secret("RWTH password")

    assert selected == "all"
    assert confirmed is True
    assert secret == "private-password"
    assert "\x1b[36mCourse [red]name[/] [all]: \x1b[0m" in stdout.getvalue()
    assert "\x1b[33mContinue [y/N]: \x1b[0m" in stdout.getvalue()
    assert stderr.getvalue() == "\x1b[36mRWTH password: \x1b[0m"
    assert secret not in stdout.getvalue() + stderr.getvalue()


def test_transfer_progress_tracks_bytes_and_only_animates_on_a_tty(monkeypatch):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.transfer("lecture.mp4", total=10, completed=2) as progress:
        progress.advance(3)
        progress.update(8, 10)

    assert terminal.interactive is True
    assert progress.transferred_bytes == 6
    assert "lecture.mp4" in stderr.getvalue()


def test_redirected_sync_progress_uses_numbered_plain_milestones(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("auto")

    with terminal.sync_progress as progress:
        progress.discovering_courses()
        progress.begin_courses(2)
        progress.start_course(1, "Course [red]one[/]")
        progress.finish_course(1)
        progress.start_course(2, "Course two")
        progress.finish_course(2)
        progress.begin_items(2)
        progress.start_item(1, "File: Course one/slides.pdf")
        progress.finish_item(1)
        progress.start_item(2, "Video: Course two/lecture.mp4")
        progress.finish_item(2)
        progress.finalizing("saving course metadata")

    output = stdout.getvalue()
    assert "Discovering courses..." in output
    assert "Scanning 2 courses..." in output
    assert "[1/2] Scanning Course [red]one[/]..." in output
    assert "[2/2] Scanning Course two..." in output
    assert "Processing 2 items..." in output
    assert "[1/2] Processing File: Course one/slides.pdf" in output
    assert "[2/2] Processing Video: Course two/lecture.mp4" in output
    assert "Finalizing: saving course metadata..." in output
    assert "\x1b[" not in output
    assert stderr.getvalue() == ""


def test_redirected_item_progress_is_throttled_for_large_runs(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    terminal = TerminalOutput("auto")

    with terminal.sync_progress as progress:
        progress.begin_items(100)
        for index in range(1, 101):
            progress.start_item(index, f"File {index}")
            progress.finish_item(index)

    output = stdout.getvalue()
    assert "[1/100] Processing File 1" in output
    assert "[2/100]" not in output
    assert "[10/100] Processing File 10" in output
    assert "[100/100] Processing File 100" in output
    assert output.count("] Processing File ") == 11


def test_tty_sync_progress_combines_aggregate_detail_and_transfer(monkeypatch):
    stdout = TtyBuffer()
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        progress.begin_courses(2)
        progress.start_course(1, "Operating Systems")
        progress.update_course(
            "Operating Systems",
            section=1,
            sections=3,
            module=2,
            modules=8,
        )
        time.sleep(0.15)
        progress.finish_course(1)
        progress.begin_items(2)
        progress.start_item(1, "Video: Operating Systems/lecture.mp4")
        terminal.action("Downloading", "/sync/lecture.mp4", "Video")
        with terminal.transfer("lecture.mp4", total=100) as transfer:
            transfer.advance(100)
            time.sleep(0.15)
        progress.finish_item(1)

    rendered = stderr.getvalue()
    assert "Scanning courses" in rendered
    assert "Operating Systems" in rendered
    assert "2/8 modules" in rendered
    assert "section 1/3" in rendered
    assert "Processing items" in rendered
    assert "lecture.mp4" in rendered
    assert "100/100 bytes" in rendered
    assert "Downloading /sync/lecture.mp4 [Video]" in rendered
    assert stdout.getvalue() == ""


def test_run_summary_reports_outcomes_and_transferred_size(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    stats = RunStatistics(
        courses=2,
        downloaded=3,
        updated=1,
        unchanged=4,
        failed=1,
        transferred_bytes=1536,
        started_at=time.monotonic(),
    )

    TerminalOutput().summary(stats, filtered=5, dry_run=False)

    summary = stdout.getvalue()
    assert "2 courses, 3 downloaded, 1 updated, 4 unchanged" in summary
    assert "5 filtered, 1 failed, 1.5 KiB transferred" in summary

    stdout.seek(0)
    stdout.truncate()
    stats.planned = 7
    TerminalOutput().summary(stats, filtered=5, dry_run=True)
    assert "7 would download, 4 unchanged, 5 filtered, 1 failed" in stdout.getvalue()
