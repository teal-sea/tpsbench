"""Terminal output: colour, boxed tables, bars, heatmaps, progress. No dependencies.

Everything degrades gracefully: if stdout is not a TTY, or NO_COLOR is set, or
--no-color is passed, the same layout is emitted without ANSI codes. In --json
mode nothing here prints except `emit`. That matters because the output is read
by pipes and agents, not just eyes. FORCE_COLOR=1 forces colour (used to render
the README screenshots from real output).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GREY = "\033[90m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_CYAN = "\033[96m"
BADGES = {
    "info": "\033[1;30;46m",  # bold black on cyan
    "warn": "\033[1;30;43m",  # bold black on yellow
    "ok": "\033[1;30;42m",  # bold black on green
    "muted": "\033[1;30;47m",  # bold black on grey
    "plan": "\033[1;30;45m",  # bold black on magenta: measured on a subscription
}

# one colour per fingerprint cluster, cycled
GROUP_COLOURS = [BRIGHT_CYAN, MAGENTA, YELLOW, BRIGHT_GREEN, BLUE, RED]

EIGHTHS = " ▏▎▍▌▋▊▉"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _detect_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR", "0") not in ("", "0"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return (stream or sys.stdout).isatty()


def _stream_width(stream, fallback: int) -> int:
    if os.environ.get("COLUMNS", "").isdigit():
        return int(os.environ["COLUMNS"])
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return fallback


def bar(value: float | None, peak: float | None, width: int = 24) -> str:
    """Bar with 1/8-character resolution; empty for missing values."""
    if not value or not peak or value <= 0:
        return ""
    eighths = max(1, round(width * 8 * min(value / peak, 1.0)))
    full, part = divmod(eighths, 8)
    return "█" * full + (EIGHTHS[part] if part else "")


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{seconds // 60:.0f}m{seconds % 60:02.0f}s"


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class Col:
    header: str
    align: str = "r"  # "l" | "r"
    flex: bool = False  # may shrink to fit the terminal (bars)
    drop: int = 0  # >0: may be hidden on narrow terminals, highest first


@dataclass
class Bar:
    """A bar cell, drawn at whatever width its column ends up with."""

    value: float | None
    peak: float | None
    width: int = 24
    style: Callable[[str], str] | None = None


# a cell: plain text, (text, styling function applied after padding), or a Bar
Cell = str | tuple[str, Callable[[str], str] | None] | Bar


class UI:
    """Small façade so command code never touches ANSI codes directly."""

    def __init__(
        self,
        json_mode: bool = False,
        color: bool | None = None,
        quiet: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self._no_color = color is False
        self.color = _detect_color() if color is None else color
        self.width = shutil.get_terminal_size((100, 24)).columns

    @property
    def silent(self) -> bool:
        return self.json_mode or self.quiet

    # -- styling ---------------------------------------------------------
    def style(self, text: str, *codes: str) -> str:
        if not self.color or self.json_mode or not codes or not text:
            return text
        return "".join(codes) + text + RESET

    def bold(self, text: str) -> str:
        return self.style(text, BOLD)

    def dim(self, text: str) -> str:
        return self.style(text, DIM)

    def green(self, text: str) -> str:
        return self.style(text, GREEN)

    def red(self, text: str) -> str:
        return self.style(text, RED)

    def yellow(self, text: str) -> str:
        return self.style(text, YELLOW)

    def cyan(self, text: str) -> str:
        return self.style(text, CYAN)

    def magenta(self, text: str) -> str:
        return self.style(text, MAGENTA)

    def grey(self, text: str) -> str:
        return self.style(text, GREY)

    def best(self, text: str) -> str:
        return self.style(text, BOLD, BRIGHT_GREEN)

    def badge(self, text: str, tone: str = "info") -> str:
        """Inverse-video label; `[TEXT]` without colour."""
        if not self.color or self.json_mode:
            return f"[{text}]"
        return self.style(f" {text} ", BADGES[tone])

    def group(self, index: int) -> Callable[[str], str]:
        colour = GROUP_COLOURS[index % len(GROUP_COLOURS)]
        return lambda text: self.style(text, BOLD, colour)

    # -- lines -----------------------------------------------------------
    def say(self, text: str = "") -> None:
        if not self.silent:
            print(text)

    def header(
        self, command: str, detail: str = "", mock: bool = False, plan: bool = False
    ) -> None:
        """` tps  run ` badge line that opens every command's output."""
        if self.silent:
            return
        badge = self.badge("tps") if self.color else "tps"
        line = f"{badge} {self.bold(command)}"
        if mock:
            line += "  " + self.badge("MOCK DATA", "warn")
        if plan:
            line += "  " + self.badge("PLAN", "plan")
        if detail:
            line += "  " + self.grey(detail)
        print()
        print(line)

    def heading(self, text: str) -> None:
        if not self.silent:
            print()
            print(self.bold(text))

    def hint(self, text: str) -> None:
        if not self.silent:
            print(self.grey(text))

    def kv(self, key: str, value: str, width: int = 12) -> str:
        return f"{self.grey(key.ljust(width))}{value}"

    def ok(self, text: str) -> None:
        self.say(f"  {self.green('✓')} {text}")

    def fail(self, text: str) -> None:
        self.say(f"  {self.red('✗')} {text}")

    def warn(self, text: str) -> None:
        self.say(f"  {self.yellow('!')} {text}")

    def error(self, text: str, hint: str | None = None) -> None:
        colour = not self._no_color and _detect_color(sys.stderr)
        label = f"{RED}{BOLD}error{RESET}" if colour else "error"
        print(f"{label}: {text}", file=sys.stderr)
        if hint:
            print(f"  {hint}", file=sys.stderr)

    # -- tables ----------------------------------------------------------
    def table(
        self,
        cols: Sequence[Col],
        rows: Sequence[Sequence[Cell]],
        title: str = "",
        footer: str = "",
        min_inner: int = 0,
        measure: bool = False,
    ) -> int:
        """Rounded box, title in the top border, columns padded on plain text.

        Styling is applied after padding, so colour never disturbs alignment.
        Returns the inner width; `measure` returns it without printing, so
        boxes shown together can share one width (`min_inner`).
        """
        if self.silent and not measure:
            return 0

        def plain(cell: Cell) -> str:
            if isinstance(cell, Bar):
                return " " * cell.width
            return cell[0] if isinstance(cell, tuple) else str(cell)

        widths = [len(c.header) for c in cols]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(plain(cell)))

        gap = 2
        inner = sum(widths) + gap * (len(cols) - 1)
        overflow = inner + 4 - self.width
        for i, col in enumerate(cols):
            if overflow > 0 and col.flex:
                shrink = min(overflow, max(0, widths[i] - 8))
                widths[i] -= shrink
                overflow -= shrink
        inner = sum(widths) + gap * (len(cols) - 1)
        # still too wide: hide optional columns, least important first
        keep = list(range(len(cols)))
        for i in sorted((i for i, c in enumerate(cols) if c.drop), key=lambda i: -cols[i].drop):
            if inner + 4 <= self.width:
                break
            keep.remove(i)
            inner -= widths[i] + gap
        if len(keep) < len(cols):
            cols = [cols[i] for i in keep]
            widths = [widths[i] for i in keep]
            rows = [[row[i] if i < len(row) else "" for i in keep] for row in rows]
        inner = max(inner, len(title) + 3, min(len(footer), self.width - 4))
        inner = max(inner, min(min_inner, self.width - 4))
        if measure:
            return inner

        def fit(text: str, width: int) -> str:
            return text if len(text) <= width else text[: max(0, width - 1)] + "…"

        def render(cells: Sequence[Cell], head: bool = False) -> str:
            parts = []
            for i, col in enumerate(cols):
                cell = cells[i] if i < len(cells) else ""
                if isinstance(cell, Bar):
                    cell = (bar(cell.value, cell.peak, widths[i]), cell.style)
                text, fn = cell if isinstance(cell, tuple) else (str(cell), None)
                text = fit(text, widths[i])
                padded = text.ljust(widths[i]) if col.align == "l" else text.rjust(widths[i])
                if head:
                    padded = self.dim(padded)
                elif fn is not None and text:
                    # style only the visible text, keep the padding plain
                    lead = len(padded) - len(padded.lstrip(" "))
                    core = padded.strip(" ")
                    padded = padded[:lead] + fn(core) + padded[lead + len(core) :]
                parts.append(padded)
            body = (" " * gap).join(parts)
            pad = inner - sum(widths) - gap * (len(cols) - 1)
            return f"{self.grey('│')} {body}{' ' * pad} {self.grey('│')}"

        if title:
            label = f" {title} "
            top_line = (
                self.grey("╭─") + self.bold(label) + self.grey("─" * (inner + 1 - len(label)) + "╮")
            )
        else:
            top_line = self.grey("╭" + "─" * (inner + 2) + "╮")
        print(top_line)
        print(render([c.header for c in cols], head=True))
        print(self.grey("├" + "─" * (inner + 2) + "┤"))
        for row in rows:
            print(render(row))
        if footer:
            print(self.grey("├" + "─" * (inner + 2) + "┤"))
            print(f"{self.grey('│')} {self.grey(fit(footer, inner).ljust(inner))} {self.grey('│')}")
        print(self.grey("╰" + "─" * (inner + 2) + "╯"))
        return inner

    # -- heatmap ---------------------------------------------------------
    def heat(self, value: float | None, diagonal: bool = False) -> str:
        """A 5-wide agreement cell: 256-colour background tier when colour is on."""
        if value is None:
            return self.dim("   · ")
        text = f"{value * 100:4.0f} "
        if not self.color or self.json_mode:
            return text
        if diagonal:
            code = "\033[1;38;5;255;48;5;238m"
        elif value >= 0.99:
            code = "\033[1;38;5;255;48;5;28m"
        elif value >= 0.90:
            code = "\033[38;5;232;48;5;178m"
        elif value >= 0.50:
            code = "\033[38;5;255;48;5;130m"
        else:
            code = "\033[38;5;255;48;5;88m"
        return code + text + RESET

    # -- machine output --------------------------------------------------
    def emit(self, payload: dict[str, Any]) -> None:
        """The only thing on stdout in --json mode."""
        print(json.dumps(payload, indent=2, default=str))


class Progress:
    """Animated status line on stderr: spinner, bar, counts, ETA.

    Silent unless stderr is a TTY (and not --json / -q), so logs and pipes stay
    clean. Redraws from a daemon thread so the spinner moves while requests are
    in flight; `update` only records state.
    """

    def __init__(self, ui: UI, total: int, label: str = "") -> None:
        self.ui = ui
        self.total = max(1, total)
        self.done = 0
        self.label = label
        self.started = time.monotonic()
        self.enabled = not ui.silent and sys.stderr.isatty()
        self.colour = not ui._no_color and _detect_color(sys.stderr)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Progress:
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def update(self, done: int | None = None, label: str | None = None) -> None:
        with self._lock:
            if done is not None:
                self.done = min(done, self.total)
            if label is not None:
                self.label = label

    def close(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1)
            self._thread = None
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()

    def _paint(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    def _loop(self) -> None:
        frame = 0
        while not self._stop.wait(0.1):
            with self._lock:
                done, label = self.done, self.label
            elapsed = time.monotonic() - self.started
            eta = elapsed / done * (self.total - done) if done else None
            # sized to stderr (the stream it draws on) and never wider than the
            # terminal: a wrapped line can't be cleared with \r and piles up
            cols = _stream_width(sys.stderr, self.ui.width)
            count = f"{done:>4}/{self.total:<4}"
            timing = clock(elapsed) + (f" · eta {clock(eta)}" if eta is not None else "")
            width = max(10, min(32, cols - 60))
            room = cols - 1 - (width + len(count) + len(timing) + 7)
            if room < 0:
                width = max(4, width + room)
                room = 0
            if width + len(count) + len(timing) + 7 > cols - 1:
                timing = ""
            filled = round(width * done / self.total)
            track = self._paint(CYAN, "━" * filled) + self._paint(GREY, "━" * (width - filled))
            spin = self._paint(CYAN, SPINNER[frame % len(SPINNER)])
            line = f"  {spin} {track} {count} {label[:room]:<{room}} {self._paint(GREY, timing)}"
            sys.stderr.write("\r\033[2K" + line)
            sys.stderr.flush()
            frame += 1
