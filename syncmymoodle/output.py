"""Terminal output, progress, logging, and sync-run summaries."""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from types import TracebackType
from typing import IO, Iterator, Literal, Protocol, Sequence

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    Task,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

ColorMode = Literal["auto", "always", "never"]
COLOR_MODES: tuple[ColorMode, ...] = ("auto", "always", "never")
DEFAULT_COLOR_MODE: ColorMode = "auto"
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\][^\x1b\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
CONTROL_TRANSLATION = str.maketrans(
    {codepoint: None for codepoint in (*range(32), *range(127, 160))}
    | {ord("\t"): "\t", ord("\n"): "\n"}
)


def safe_terminal_text(value: str) -> str:
    """Remove terminal control sequences while preserving tabs and newlines."""
    return ANSI_ESCAPE_RE.sub("", value).translate(CONTROL_TRANSLATION)


class FilteredEntry(Protocol):
    @property
    def config_key(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def item(self) -> str: ...

    @property
    def reason(self) -> str: ...


def format_size(size: int) -> str:
    """Format a byte count using compact binary units."""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024
        if value < 1024 or unit == "PiB":
            if value.is_integer():
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    raise AssertionError("unreachable")


@dataclass
class RunStatistics:
    """User-relevant outcomes accumulated during one sync run."""

    courses: int = 0
    downloaded: int = 0
    updated: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0
    transferred_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic, repr=False)

    def record_transfer(
        self,
        *,
        existed: bool,
        size: int = 0,
        dry_run: bool = False,
    ) -> None:
        if dry_run:
            self.planned += 1
            return
        if existed:
            self.updated += 1
        else:
            self.downloaded += 1
        self.transferred_bytes += max(0, size)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class WorkCountColumn(ProgressColumn):
    """Render byte counts for transfers and semantic counters for other work."""

    def __init__(self) -> None:
        super().__init__()
        self._download = DownloadColumn(binary_units=True)

    def render(self, task: Task) -> Text:
        if task.fields.get("kind") == "transfer":
            return self._download.render(task)
        return Text(safe_terminal_text(str(task.fields.get("count", ""))))


class WorkSpeedColumn(ProgressColumn):
    def __init__(self) -> None:
        super().__init__()
        self._speed = TransferSpeedColumn()

    def render(self, task: Task) -> Text:
        if task.fields.get("kind") != "transfer":
            return Text()
        return self._speed.render(task)


class WorkRemainingColumn(ProgressColumn):
    def __init__(self) -> None:
        super().__init__()
        self._remaining = TimeRemainingColumn()

    def render(self, task: Task) -> Text:
        if task.fields.get("kind") != "transfer":
            return Text(safe_terminal_text(str(task.fields.get("detail", ""))))
        return self._remaining.render(task)


class SyncProgress:
    """One shared hierarchical display for course, item, and byte progress."""

    def __init__(self, terminal: TerminalOutput) -> None:
        self._terminal = terminal
        self._progress: Progress | None = None
        self._stage_task: TaskID | None = None
        self._detail_task: TaskID | None = None
        self._active = False
        self._course_total = 0
        self._item_total = 0
        self._item_verb = "Processing"
        self._current_item_label = ""

    def __enter__(self) -> SyncProgress:
        if self._active:
            raise RuntimeError("sync progress is already active")
        self._active = True
        if self._terminal.interactive:
            self._progress = Progress(
                TextColumn("{task.description}", markup=False),
                BarColumn(),
                WorkCountColumn(),
                WorkSpeedColumn(),
                WorkRemainingColumn(),
                console=self._terminal.error_console,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._progress is not None:
            self._progress.stop()
        self._progress = None
        self._stage_task = None
        self._detail_task = None
        self._active = False

    @property
    def is_live(self) -> bool:
        return self._progress is not None

    @property
    def renderer(self) -> Progress | None:
        return self._progress

    def _remove_task(self, task_id: TaskID | None) -> None:
        if self._progress is not None and task_id is not None:
            self._progress.remove_task(task_id)

    def _clear(self) -> None:
        self._remove_task(self._detail_task)
        self._remove_task(self._stage_task)
        self._detail_task = None
        self._stage_task = None

    def _add_task(
        self,
        description: str,
        *,
        total: int | None,
        completed: int = 0,
        kind: str,
        count: str = "",
        detail: str = "",
    ) -> TaskID | None:
        if self._progress is None:
            return None
        task_id = self._progress.add_task(
            safe_terminal_text(description),
            total=total,
            completed=completed,
            kind=kind,
            count=safe_terminal_text(count),
            detail=safe_terminal_text(detail),
        )
        self._progress.refresh()
        return task_id

    def discovering_courses(self) -> None:
        self._clear()
        if self._progress is None:
            self._terminal.phase("Discovering courses...")
            return
        self._stage_task = self._add_task(
            "Discovering courses", total=None, kind="status"
        )

    def begin_courses(self, total: int) -> None:
        self._clear()
        self._course_total = total
        if self._progress is None:
            noun = "course" if total == 1 else "courses"
            self._terminal.phase(f"Scanning {total} {noun}...")
            return
        self._stage_task = self._add_task(
            "Scanning courses",
            total=max(total, 1),
            completed=1 if total == 0 else 0,
            kind="aggregate",
            count=f"0/{total} courses",
        )

    def start_course(self, index: int, name: str) -> None:
        if self._progress is None:
            self._terminal.phase(f"[{index}/{self._course_total}] Scanning {name}...")
            return
        assert self._stage_task is not None
        self._progress.update(
            self._stage_task,
            completed=index - 1,
            count=f"{index - 1}/{self._course_total} courses",
        )
        self._remove_task(self._detail_task)
        self._detail_task = self._add_task(
            name,
            total=None,
            kind="status",
            count="fetching",
        )

    def update_course(
        self,
        name: str,
        *,
        section: int,
        sections: int,
        module: int,
        modules: int,
    ) -> None:
        if self._progress is None:
            return
        if modules:
            total = modules
            completed = module
            count = f"{module}/{modules} modules"
        else:
            total = max(sections, 1)
            completed = sections if sections == 0 else section
            count = f"{section}/{sections} sections"
        detail = f"section {section}/{sections}" if sections else ""
        if self._detail_task is None:
            self._detail_task = self._add_task(
                name,
                total=total,
                completed=completed,
                kind="course",
                count=count,
                detail=detail,
            )
        else:
            self._progress.update(
                self._detail_task,
                description=safe_terminal_text(name),
                total=total,
                completed=completed,
                kind="course",
                count=safe_terminal_text(count),
                detail=safe_terminal_text(detail),
            )

    def finish_course(self, index: int) -> None:
        if self._progress is None:
            return
        assert self._stage_task is not None
        self._remove_task(self._detail_task)
        self._detail_task = None
        self._progress.update(
            self._stage_task,
            completed=index,
            count=f"{index}/{self._course_total} courses",
            refresh=True,
        )

    def begin_items(self, total: int, *, dry_run: bool = False) -> None:
        self._clear()
        self._item_total = total
        self._item_verb = "Planning" if dry_run else "Processing"
        self._current_item_label = ""
        if self._progress is None:
            noun = "item" if total == 1 else "items"
            self._terminal.phase(f"{self._item_verb} {total} {noun}...")
            return
        self._stage_task = self._add_task(
            "Planning downloads" if dry_run else "Processing items",
            total=max(total, 1),
            completed=1 if total == 0 else 0,
            kind="aggregate",
            count=f"0/{total} items",
        )

    def start_item(self, index: int, label: str) -> None:
        self._current_item_label = safe_terminal_text(label)
        if self._progress is None:
            percentage_advanced = (
                index * 10 // self._item_total > (index - 1) * 10 // self._item_total
            )
            if self._item_total <= 20 or index == 1 or percentage_advanced:
                self._terminal.phase(
                    f"[{index}/{self._item_total}] {self._item_verb} {label}"
                )
            return
        assert self._stage_task is not None
        self._progress.update(
            self._stage_task,
            completed=index - 1,
            count=f"{index - 1}/{self._item_total} items",
        )
        self._remove_task(self._detail_task)
        self._detail_task = self._add_task(
            label,
            total=None,
            kind="status",
            count="checking",
        )

    def finish_item(self, index: int) -> None:
        self._current_item_label = ""
        if self._progress is None:
            return
        assert self._stage_task is not None
        self._remove_task(self._detail_task)
        self._detail_task = None
        self._progress.update(
            self._stage_task,
            completed=index,
            count=f"{index}/{self._item_total} items",
            refresh=True,
        )

    def begin_transfer(
        self,
        label: str,
        total: int | None,
        completed: int,
    ) -> TaskID | None:
        if self._progress is None:
            return None
        self._remove_task(self._detail_task)
        self._detail_task = self._add_task(
            self._current_item_label or label,
            total=total,
            completed=completed,
            kind="transfer",
        )
        return self._detail_task

    def finish_transfer(self, task_id: TaskID | None) -> None:
        self._remove_task(task_id)
        if task_id == self._detail_task:
            self._detail_task = None

    def finalizing(self, detail: str) -> None:
        self._clear()
        if self._progress is None:
            self._terminal.phase(f"Finalizing: {detail}...")
            return
        self._stage_task = self._add_task(
            "Finalizing",
            total=None,
            kind="status",
            count=detail,
        )


def _stream_is_interactive(stream: IO[str]) -> bool:
    interactive_override = os.environ.get("TTY_INTERACTIVE")
    if interactive_override == "0":
        return False
    if interactive_override == "1":
        return True
    if os.environ.get("TERM", "").casefold() in {"dumb", "unknown"}:
        return False
    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


class TransferProgress:
    """A single byte-transfer display that becomes a no-op outside a TTY."""

    def __init__(
        self,
        terminal: TerminalOutput,
        label: str,
        total: int | None,
        completed: int,
    ) -> None:
        self._terminal = terminal
        self._label = safe_terminal_text(label)
        self._total = total
        self._initial_completed = completed
        self._completed = completed
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._shared_progress = False
        self.transferred_bytes = 0

    def __enter__(self) -> TransferProgress:
        shared_renderer = self._terminal.sync_progress.renderer
        if shared_renderer is not None:
            self._progress = shared_renderer
            self._task_id = self._terminal.sync_progress.begin_transfer(
                self._label,
                self._total,
                self._completed,
            )
            self._shared_progress = True
            return self
        if not self._terminal.interactive:
            return self
        self._progress = Progress(
            TextColumn("{task.fields[label]}", markup=False),
            BarColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self._terminal.error_console,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._task_id = self._progress.add_task(
            "",
            label=self._label,
            total=self._total,
            completed=self._completed,
        )
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._shared_progress:
            self._terminal.sync_progress.finish_transfer(self._task_id)
        elif self._progress is not None:
            self._progress.stop()

    def advance(self, amount: int) -> None:
        if amount <= 0:
            return
        self._completed += amount
        self.transferred_bytes += amount
        if self._progress is not None and self._task_id is not None:
            self._progress.advance(self._task_id, amount)

    def update(self, completed: int, total: int | None = None) -> None:
        if completed < 0:
            return
        if total is not None and total > 0:
            self._total = total
        self._completed = completed
        self.transferred_bytes = max(
            self.transferred_bytes,
            completed - self._initial_completed,
        )
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                completed=completed,
                total=self._total,
            )


class TerminalLogHandler(logging.Handler):
    """Render Python logging through the same stable terminal output boundary."""

    def __init__(self, terminal: TerminalOutput) -> None:
        super().__init__()
        self.terminal = terminal
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = f"{record.levelname}: {self.format(record)}"
            if record.levelno >= logging.ERROR:
                style = "red"
            elif record.levelno >= logging.WARNING:
                style = "yellow"
            elif record.levelno >= logging.INFO:
                style = "cyan"
            else:
                style = None
            self.terminal.print(message, style=style, error=True)
        except Exception:
            self.handleError(record)


class TerminalOutput:
    """The single rendering boundary for user-facing terminal output."""

    def __init__(self, color: ColorMode = DEFAULT_COLOR_MODE) -> None:
        force_terminal = True if color == "always" else None
        no_color = True if color == "never" else None
        stdout_force_interactive = (
            _stream_is_interactive(sys.stdout) if color == "always" else None
        )
        stderr_force_interactive = (
            _stream_is_interactive(sys.stderr) if color == "always" else None
        )
        self.console = Console(
            force_terminal=force_terminal,
            force_interactive=stdout_force_interactive,
            no_color=no_color,
            markup=False,
            highlight=False,
        )
        self.error_console = Console(
            force_terminal=force_terminal,
            force_interactive=stderr_force_interactive,
            no_color=no_color,
            markup=False,
            highlight=False,
            stderr=True,
        )
        self.interactive = _stream_is_interactive(self.error_console.file)
        self.sync_progress = SyncProgress(self)

    def print(
        self,
        message: str = "",
        *,
        style: str | None = None,
        end: str = "\n",
        error: bool = False,
    ) -> None:
        console = self.error_console if error else self.console
        message = safe_terminal_text(message)
        renderable = Text(message) if style is None else Text(message, style=style)
        console.print(
            renderable,
            end=end,
            soft_wrap=True,
        )

    def raw(self, text: str) -> None:
        self.console.file.write(text)
        self.console.file.flush()

    def phase(self, message: str) -> None:
        self.print(message, style="cyan")

    def success(self, message: str) -> None:
        self.print(message, style="green")

    def caution(self, message: str) -> None:
        self.print(message, style="yellow")

    def failure(self, message: str) -> None:
        self.print(message, style="red")

    def prompt(
        self,
        label: str,
        default: str | None = None,
        *,
        style: str = "cyan",
    ) -> str:
        suffix = f" [{default}]" if default is not None else ""
        self.print(f"{label}{suffix}: ", style=style, end="")
        value = input().strip()
        return default if value == "" and default is not None else value

    def confirm(self, label: str, default: bool = False) -> bool:
        suffix = "Y/n" if default else "y/N"
        value = self.prompt(f"{label} [{suffix}]", style="yellow").casefold()
        return default if not value else value in {"y", "yes"}

    def prompt_secret(self, label: str) -> str:
        self.print(f"{label}: ", style="cyan", end="", error=True)
        return getpass.getpass("")

    def warning(self, message: str) -> None:
        self.print(f"warning: {message}", style="yellow", error=True)

    def error(self, message: str) -> None:
        self.print(message, style="red", error=True)

    def action(
        self,
        verb: str,
        target: str | Path,
        kind: str,
        *,
        dry_run: bool = False,
    ) -> None:
        style = "yellow" if dry_run else "cyan"
        message = Text()
        message.append(f"{safe_terminal_text(verb)} ", style=style)
        message.append(safe_terminal_text(str(target)))
        message.append(f" [{safe_terminal_text(kind)}]", style="magenta")
        console = (
            self.error_console
            if self.sync_progress.is_live and _stream_is_interactive(self.console.file)
            else self.console
        )
        console.print(message, soft_wrap=True)

    def filtered_items(
        self,
        items: Sequence[FilteredEntry],
        *,
        show_details: bool,
    ) -> None:
        count = len(items)
        if not show_details:
            noun = "item" if count == 1 else "items"
            self.print(
                f"Filtered {count} {noun}; use --show-filtered for details.",
                style="yellow",
            )
            return

        self.print(f"Filtered items ({count}):", style="yellow")
        for config_key, group in groupby(items, key=lambda item: item.config_key):
            grouped_items = list(group)
            heading = Text("  ")
            heading.append(safe_terminal_text(config_key), style="cyan")
            heading.append(f" ({len(grouped_items)}):")
            self.console.print(heading, soft_wrap=True)
            for item in grouped_items:
                line = Text("    ")
                line.append(safe_terminal_text(item.category), style="magenta")
                line.append(f": {safe_terminal_text(item.item)} - ")
                line.append(safe_terminal_text(item.reason), style="yellow")
                self.console.print(line, soft_wrap=True)

    def transfer(
        self,
        label: str,
        total: int | None,
        completed: int = 0,
    ) -> TransferProgress:
        return TransferProgress(self, label, total, completed)

    def summary(self, stats: RunStatistics, filtered: int, *, dry_run: bool) -> None:
        course_outcome = (
            f"{stats.courses} course"
            if stats.courses == 1
            else f"{stats.courses} courses"
        )
        if dry_run:
            outcomes = [
                course_outcome,
                f"{stats.planned} would download",
                f"{stats.unchanged} unchanged",
                f"{filtered} filtered",
                f"{stats.failed} failed",
            ]
            prefix = "Dry run complete"
        else:
            outcomes = [
                course_outcome,
                f"{stats.downloaded} downloaded",
                f"{stats.updated} updated",
                f"{stats.unchanged} unchanged",
                f"{filtered} filtered",
                f"{stats.failed} failed",
            ]
            if stats.transferred_bytes:
                outcomes.append(f"{format_size(stats.transferred_bytes)} transferred")
            prefix = "Sync complete"
        message = f"{prefix} in {stats.elapsed_seconds:.1f}s: {', '.join(outcomes)}."
        self.print(message, style="red" if stats.failed else "green")

    def logging_handler(self) -> logging.Handler:
        return TerminalLogHandler(self)


_active_output: TerminalOutput | None = None


def get_output() -> TerminalOutput:
    global _active_output
    if _active_output is None:
        _active_output = TerminalOutput()
    return _active_output


@contextmanager
def use_output(color: ColorMode) -> Iterator[TerminalOutput]:
    global _active_output
    previous = _active_output
    terminal = TerminalOutput(color)
    _active_output = terminal
    try:
        yield terminal
    finally:
        _active_output = previous


def configure_logging(level: int) -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[get_output().logging_handler()],
    )


def print(message: str = "", *, end: str = "\n") -> None:
    get_output().print(message, end=end)


def raw(text: str) -> None:
    get_output().raw(text)


def phase(message: str) -> None:
    get_output().phase(message)


def success(message: str) -> None:
    get_output().success(message)


def caution(message: str) -> None:
    get_output().caution(message)


def failure(message: str) -> None:
    get_output().failure(message)


def prompt(label: str, default: str | None = None) -> str:
    return get_output().prompt(label, default)


def confirm(label: str, default: bool = False) -> bool:
    return get_output().confirm(label, default)


def prompt_secret(label: str) -> str:
    return get_output().prompt_secret(label)


def warning(message: str) -> None:
    get_output().warning(message)


def error(message: str) -> None:
    get_output().error(message)
