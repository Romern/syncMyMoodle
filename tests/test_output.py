import io
import logging
import sys
import time

import pytest
from rich.console import Console
from rich.progress import Progress

from syncmymoodle.outcomes import RemovedContent, RunStatistics
from syncmymoodle.output import TerminalOutput, safe_terminal_text


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def render_progress_frame(
    progress: Progress,
    *,
    legacy_windows: bool | None = None,
) -> str:
    output = io.StringIO()
    Console(
        file=output,
        width=120,
        color_system=None,
        legacy_windows=legacy_windows,
    ).print(progress.get_renderable())
    return output.getvalue()


@pytest.fixture(autouse=True)
def stable_terminal_detection(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("TTY_INTERACTIVE", raising=False)


def test_redirected_output_is_plain_unwrapped_and_markup_safe(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    terminal = TerminalOutput("auto")
    message = f"Course [bold red]not markup[/] {'x' * 100}"

    terminal.phase(f"{message}\x1b[31m")
    with terminal.transfer(total=10) as progress:
        progress.advance(10)

    assert stdout.getvalue() == f"{message}\n"
    assert "\x1b[" not in stdout.getvalue()
    assert terminal.interactive is False

    stdout.seek(0)
    stdout.truncate()
    monkeypatch.setenv("FORCE_COLOR", "1")
    forced_color = TerminalOutput("auto")
    with forced_color.transfer(total=10) as progress:
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


def test_color_always_overrides_term_dumb_without_enabling_animation(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("NO_COLOR", raising=False)

    terminal = TerminalOutput("always")
    terminal.success("Complete")

    assert "\x1b[32mComplete\x1b[0m" in stdout.getvalue()
    assert terminal.interactive is False


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
    secret_prompts = []
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    monkeypatch.setattr(
        "syncmymoodle.output.getpass.getpass",
        lambda prompt: secret_prompts.append(prompt) or "private-password",
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
    assert secret_prompts == ["RWTH password: "]
    assert stderr.getvalue() == ""
    assert secret not in stdout.getvalue() + stderr.getvalue()


def test_transfer_progress_tracks_bytes_and_only_animates_on_a_tty(monkeypatch):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.transfer(total=10, completed=2) as progress:
        progress.advance(3)
        progress.update(8, 10)

    assert terminal.interactive is True
    assert progress.transferred_bytes == 6
    assert "Downloading" in stderr.getvalue()


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
    assert "Scanned Course [red]one[/] [Course]" in output
    assert "Scanned Course two [Course]" in output
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


@pytest.mark.parametrize(
    ("dry_run", "stage_label"),
    [(False, "Processing items"), (True, "Planning downloads")],
)
def test_tty_sync_progress_uses_a_stable_item_check_label(
    monkeypatch, dry_run, stage_label
):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        renderer = progress.renderer
        assert renderer is not None
        progress.begin_items(1, dry_run=dry_run)
        progress.start_item(1, "File: a very long and slow metadata check")
        renderer.refresh()
        progress.finish_item(1)

    rendered = stderr.getvalue()
    assert stage_label in rendered
    assert "Checking item" in rendered
    assert "a very long and slow metadata check" not in rendered


@pytest.mark.parametrize("legacy_windows", [False, True])
def test_tty_item_display_stays_stable_during_fast_transfers(
    monkeypatch, legacy_windows
):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        renderer = progress.renderer
        assert renderer is not None
        progress.begin_items(2)
        progress.start_item(1, "File: first")
        checking_frame = render_progress_frame(
            renderer,
            legacy_windows=legacy_windows,
        )
        long_path = "/sync/" + "very-long-directory/" * 8 + "first.pdf"
        with terminal.tracked_action("Downloading", long_path, "File") as action:
            with terminal.transfer(total=100) as transfer:
                transfer.advance(10)
                transfer_frame = render_progress_frame(
                    renderer,
                    legacy_windows=legacy_windows,
                )
            completed_transfer_frame = render_progress_frame(
                renderer,
                legacy_windows=legacy_windows,
            )
            action.complete()
        retained_action_frame = render_progress_frame(
            renderer,
            legacy_windows=legacy_windows,
        )
        progress.finish_item(1)
        progress.start_item(2, "File: second")
        next_item_frame = render_progress_frame(
            renderer,
            legacy_windows=legacy_windows,
        )
        with terminal.tracked_action("Rendering", "/sync/second.pdf", "Quiz PDF"):
            replacement_action_frame = render_progress_frame(
                renderer,
                legacy_windows=legacy_windows,
            )

    checking_line = next(
        line for line in checking_frame.splitlines() if "Checking item" in line
    )
    transfer_line = next(
        line
        for line in transfer_frame.splitlines()
        if "Downloading" in line and "10/100 bytes" in line
    )
    checking_stage = next(
        line for line in checking_frame.splitlines() if "Processing items" in line
    )
    transfer_stage = next(
        line for line in transfer_frame.splitlines() if "Processing items" in line
    )
    assert checking_stage.index("Processing items") == transfer_stage.index(
        "Processing items"
    )
    assert checking_line.index("Checking item") == transfer_line.index("Downloading")
    assert checking_frame.splitlines().index(checking_stage) == (
        transfer_frame.splitlines().index(transfer_stage)
    )
    assert any(
        "Downloading" in line and "10/100 bytes" in line
        for line in completed_transfer_frame.splitlines()
    )
    assert any(
        "Downloading" in line and "10/100 bytes" in line
        for line in retained_action_frame.splitlines()
    )
    assert retained_action_frame.splitlines()[0].startswith("Downloading ")
    assert "Checking item" in next_item_frame
    assert replacement_action_frame.splitlines()[0].startswith("Rendering ")
    assert not replacement_action_frame.splitlines()[0].startswith("Downloading ")


def test_tty_tracked_action_replaces_the_checking_status(monkeypatch):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        renderer = progress.renderer
        assert renderer is not None
        progress.begin_items(1)
        progress.start_item(1, "Quiz: test")
        with terminal.tracked_action("Rendering", "/sync/test.pdf", "Quiz PDF"):
            rendering_frame = render_progress_frame(renderer)

    rendering_line = next(
        line for line in rendering_frame.splitlines() if "Rendering item" in line
    )
    assert "rendering" in rendering_line
    assert "Checking item" not in rendering_frame


def test_tty_incomplete_action_does_not_linger_during_skipped_items(monkeypatch):
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        renderer = progress.renderer
        assert renderer is not None
        progress.begin_items(2)
        progress.start_item(1, "File: old-file.pdf")
        with terminal.tracked_action("Downloading", "/sync/old-file.pdf", "File"):
            with terminal.transfer(total=100) as transfer:
                transfer.advance(50)
        incomplete_frame = render_progress_frame(renderer)
        progress.finish_item(1)
        progress.start_item(2, "File: skipped-file.pdf")
        skipped_frame = render_progress_frame(renderer)

    assert "old-file.pdf" not in incomplete_frame
    assert "old-file.pdf" not in skipped_frame
    assert "Checking item" in skipped_frame


def test_interrupted_sync_retains_the_active_transfer_frame(monkeypatch):
    stdout = TtyBuffer()
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    stopped = []
    original_stop = Progress.stop

    def record_stop(progress):
        stopped.append(progress.live.transient)
        original_stop(progress)

    monkeypatch.setattr(Progress, "stop", record_stop)
    terminal = TerminalOutput("never")

    with pytest.raises(KeyboardInterrupt):
        with terminal.sync_progress as progress:
            renderer = progress.renderer
            assert renderer is not None
            progress.begin_items(1)
            progress.start_item(1, "Video: interrupted.mp4")
            with terminal.tracked_action(
                "Downloading", "/sync/interrupted.mp4", "Video"
            ):
                with terminal.transfer(total=100) as transfer:
                    transfer.advance(40)
                    renderer.refresh()
                    raise KeyboardInterrupt

    assert stopped == [False]
    rendered = stderr.getvalue()
    assert "Downloading /sync/interrupted.mp4 [Video]" in rendered
    assert "Downloading" in rendered
    assert "40/100 bytes" in rendered
    assert stdout.getvalue() == ""


def test_tty_sync_progress_combines_aggregate_detail_and_transfer(monkeypatch):
    stdout = TtyBuffer()
    stderr = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    terminal = TerminalOutput("never")

    with terminal.sync_progress as progress:
        renderer = progress.renderer
        assert renderer is not None
        progress.begin_courses(2)
        course = "Operating Systems with a very long course name"
        progress.start_course(1, course)
        progress.update_course(
            section=1,
            sections=3,
            module=2,
            modules=8,
            module_active=True,
        )
        progress.module_status("resolving Opencast episode 2/5")
        renderer.refresh()
        course_frame = render_progress_frame(renderer)
        progress.finish_course(1)
        finished_course_frame = render_progress_frame(renderer)
        progress.begin_items(2)
        path = "/sync/a-very-long-course-name/lecture.mp4"
        progress.start_item(1, f"Video: {path}")
        with terminal.tracked_action("Downloading", path, "Video") as action:
            with terminal.transfer(total=100) as transfer:
                transfer.advance(100)
                renderer.refresh()
                transfer_frame = render_progress_frame(renderer)
            action.complete("Downloaded")
        finished_transfer_frame = render_progress_frame(renderer)
        progress.finish_item(1)

    rendered = stderr.getvalue()
    normalized = " ".join(safe_terminal_text(rendered).split())
    assert "Scanning courses" in rendered
    assert f"Scanned {course} [Course]" in rendered
    assert "3/8 modules" in rendered
    assert "section 1/3" in rendered
    assert "resolving Opencast episode 2/5" in normalized
    assert "Processing items" in rendered
    assert "100/100 bytes" in rendered
    assert f"Downloaded {path} [Video]" in rendered
    course_lines = course_frame.splitlines()
    assert any(
        "Scanning course" in line and "3/8 modules" in line for line in course_lines
    )
    assert not any(course in line and "3/8 modules" in line for line in course_lines)
    assert course not in finished_course_frame
    transfer_lines = transfer_frame.splitlines()
    assert any(
        "Downloading" in line and "100/100 bytes" in line for line in transfer_lines
    )
    assert not any(path in line and "100/100 bytes" in line for line in transfer_lines)
    assert path in finished_transfer_frame
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


def test_removed_content_report_states_that_local_files_are_kept(
    monkeypatch,
):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    TerminalOutput().removed_content(
        [
            RemovedContent(
                "Operating Systems (123)",
                "Week 1/notes.pdf",
                "https://moodle.example/file.php?id=456",
            )
        ]
    )

    assert stdout.getvalue() == (
        "No longer present in Moodle (1 item; local files kept):\n"
        "  Operating Systems (123): Week 1/notes.pdf "
        "[remote: https://moodle.example/file.php?id=456]\n"
    )
