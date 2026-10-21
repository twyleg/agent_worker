"""Resizable curses dashboard with an ASCII clock and htop-style meters."""

from __future__ import annotations

import curses
import datetime
import math
import os
import shutil
import textwrap
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

if __package__:
    from .codex_usage import read_usage
else:
    from codex_usage import read_usage as standalone_read_usage

    read_usage = standalone_read_usage

INTERVAL = 10
GREEN, RED, YELLOW, CYAN, MUTED = range(1, 6)
# Five-row block digits use thicker strokes than the old seven-segment font.
DIGITS = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ":": (" ", "#", " ", "#", " "),
}
# Simple, consistent strokes; 1 has a clear ascending hook and centered stem.
WIDE_DIGITS = {
    "0": ("#####", "#   #", "#   #", "#   #", "#####"),
    "1": ("  #  ", " ##  ", "  #  ", "  #  ", "#####"),
    "2": ("#####", "    #", "#####", "#    ", "#####"),
    "3": ("#####", "    #", "#####", "    #", "#####"),
    "4": ("#   #", "#   #", "#####", "    #", "    #"),
    "5": ("#####", "#    ", "#####", "    #", "#####"),
    "6": ("#####", "#    ", "#####", "#   #", "#####"),
    "7": ("#####", "    #", "    #", "    #", "    #"),
    "8": ("#####", "#   #", "#####", "#   #", "#####"),
    "9": ("#####", "#   #", "#####", "    #", "#####"),
    ":": DIGITS[":"],
}


class Screen(Protocol):
    def erase(self) -> None: ...
    def getmaxyx(self) -> tuple[int, int]: ...
    def addstr(self, row: int, column: int, text: str, attr: int) -> None: ...
    def refresh(self) -> None: ...


class InteractiveScreen(Screen, Protocol):
    def timeout(self, delay: int) -> None: ...
    def getch(self) -> int: ...


@dataclass(frozen=True)
class Meter:
    label: str
    percent: float | None
    detail: str = ""
    reset: str = ""
    color: int = GREEN
    # CPU user/system/steal percentages get separate htop-style colors.
    segments: tuple[tuple[float, int], ...] = ()


@dataclass(frozen=True)
class CpuSample:
    total: int
    idle: int
    user: int
    system: int
    steal: int


def cpu_sample() -> CpuSample:
    values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    # Guest counters are already included in user/nice; do not count them twice.
    return CpuSample(sum(values[:8]), values[3] + values[4], values[0] + values[1], values[2] + values[5] + values[6], values[7])


def system_meters(previous: CpuSample, current: CpuSample, disk_path: str = "/") -> list[Meter]:
    total = current.total - previous.total
    cpu = None if total <= 0 else max(0, 100 * (total - (current.idle - previous.idle)) / total)
    segments = (
        ()
        if total <= 0
        else (
            (max(0, 100 * (current.user - previous.user) / total), GREEN),
            (max(0, 100 * (current.system - previous.system) / total), RED),
            (max(0, 100 * (current.steal - previous.steal) / total), YELLOW),
        )
    )
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value, *_ = line.split()
        memory[name.rstrip(":")] = int(value)
    used = memory["MemTotal"] - memory["MemAvailable"]
    swap_used = memory["SwapTotal"] - memory["SwapFree"]
    disk = shutil.disk_usage(disk_path)
    return [
        Meter("CPU", cpu, color=GREEN, segments=segments),
        Meter("RAM", 100 * used / memory["MemTotal"], f"{used / 1048576:.1f}/{memory['MemTotal'] / 1048576:.1f}G"),
        Meter(
            "Swap",
            100 * swap_used / memory["SwapTotal"] if memory["SwapTotal"] else 0,
            f"{swap_used / 1048576:.1f}/{memory['SwapTotal'] / 1048576:.1f}G",
            color=YELLOW,
        ),
        Meter("Disk", 100 * disk.used / disk.total, f"{disk.used / 2**30:.1f}/{disk.total / 2**30:.1f}G", color=CYAN),
    ]


def quota_meters(result: dict[str, Any]) -> list[Meter]:
    buckets = result.get("rateLimitsByLimitId") or {}
    if not buckets and result.get("rateLimits"):
        buckets = {"codex": result["rateLimits"]}
    meters = []
    for bucket, snapshot in buckets.items():
        if not snapshot:
            continue
        for field in ("primary", "secondary"):
            window = snapshot.get(field) or {}
            label = "5h" if field == "primary" else "Week"
            duration = window.get("windowDurationMins")
            if duration is not None:
                label = {300: "5h", 10080: "Week"}.get(duration, f"{duration}m" if duration else label)
            if len(buckets) > 1:
                label = f"{label}/{snapshot.get('limitName') or bucket}"
            percent = window.get("usedPercent")
            if percent is not None:
                percent = float(percent)
                if not math.isfinite(percent):
                    percent = None
            reset = window.get("resetsAt")
            reset_text = ""
            if reset is not None:
                local = datetime.datetime.fromtimestamp(reset, datetime.timezone.utc).astimezone()
                reset_text = local.strftime("%m-%d %H:%M")
            color = RED if percent is not None and percent >= 90 else YELLOW if percent is not None and percent >= 70 else GREEN
            meters.append(Meter(label, percent, reset=reset_text, color=color))
    return meters


class UsagePoller:
    """Keep account queries off the rendering thread so the clock never stalls."""

    def __init__(self) -> None:
        self.meters: list[Meter] = []
        self.error = "Querying account..."
        self.updated: str | None = None
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                meters = quota_meters(read_usage(timeout=6))
                error = "" if meters else "No quota reported"
            except (RuntimeError, OSError, ValueError, KeyError, TypeError, OverflowError) as problem:
                meters = []
                error = f"Usage unavailable: {problem}"
            with self.lock:
                self.meters = meters
                self.error = error
                self.updated = datetime.datetime.now().strftime("%H:%M:%S")
            self.stop.wait(max(0, INTERVAL - (time.monotonic() - started)))

    def snapshot(self) -> tuple[list[Meter], str, str | None]:
        with self.lock:
            return list(self.meters), self.error, self.updated


def clock_lines(now: datetime.datetime, width: int) -> list[str]:
    # Prefer the wide, bold font; include seconds only when the whole time fits.
    for font in (WIDE_DIGITS, DIGITS):
        for pattern in ("%H:%M:%S", "%H:%M"):
            text = now.strftime(pattern)
            rows = [" ".join(font[char][row] for char in text) for row in range(5)]
            if len(rows[0]) <= width:
                return [row.center(width) for row in rows]
    return [now.strftime("%H:%M:%S")[:width].center(width)]


def meter_line(meter: Meter, width: int) -> list[tuple[str, int]]:
    """Return text/color spans sized to the pane, with unknown values as N/A."""
    label_width = max(1, min(9, width // 4))
    label = meter.label[:label_width].ljust(label_width)
    value_width = 14 if width >= 38 else 4
    percent = "N/A" if meter.percent is None else f"{meter.percent:.0f}%"
    suffix = f"{meter.detail} {percent}" if meter.detail and width >= 38 else percent
    suffix = suffix[:value_width].rjust(value_width)
    bar_width = width - label_width - value_width - 4
    if bar_width < 2:
        return [(f"{label} {suffix}"[:width], MUTED)]
    fill = 0 if meter.percent is None else round(bar_width * max(0, min(100, meter.percent)) / 100)
    spans = [(label + " [", MUTED)]
    if meter.segments and fill:
        remaining = fill
        for index, (amount, color) in enumerate(meter.segments):
            count = remaining if index == len(meter.segments) - 1 else min(remaining, max(0, round(bar_width * amount / 100)))
            spans.append(("|" * count, color))
            remaining -= count
    else:
        color = RED if meter.percent is not None and meter.percent >= 90 else meter.color
        spans.append(("|" * fill, color))
    spans.extend([("." * (bar_width - fill), MUTED), ("] ", MUTED), (suffix, meter.color)])
    return spans


def palette() -> dict[int, int]:
    colors = {}
    if curses.has_colors():
        curses.start_color()
        background = curses.COLOR_BLACK
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            pass
        for pair, color in (
            (GREEN, curses.COLOR_GREEN),
            (RED, curses.COLOR_RED),
            (YELLOW, curses.COLOR_YELLOW),
            (CYAN, curses.COLOR_CYAN),
            (MUTED, curses.COLOR_WHITE),
        ):
            try:
                curses.init_pair(pair, color, background)
                colors[pair] = curses.color_pair(pair)
            except curses.error:
                pass
    return colors


def draw(
    screen: Screen,
    metrics: Sequence[Meter],
    system_error: str,
    quotas: Sequence[Meter],
    quota_error: str,
    updated: str | None,
    colors: Mapping[int, int],
    scroll: int = 0,
) -> int:
    screen.erase()
    height, columns = screen.getmaxyx()
    width = max(0, columns - 1)
    if width == 0 or height < 2:
        return 0
    clock = clock_lines(datetime.datetime.now(), width)
    if height < 10:
        clock = [datetime.datetime.now().strftime("%H:%M:%S").center(width)]
    rows = []
    load = " ".join(f"{value:.1f}" for value in os.getloadavg())
    heading = f"SYSTEM | {os.cpu_count()} cores | load {load}"
    if len(heading) > width:
        heading = f"SYS {os.cpu_count()}c L {load}"
    rows.append([(heading, CYAN)])
    rows.extend(meter_line(meter, width) for meter in metrics)
    if system_error:
        rows.append([(system_error, YELLOW)])
    rows.append([(f"CODEX used | {updated or 'waiting'}", CYAN)])
    rows.extend(meter_line(meter, width) for meter in quotas)
    if width < 38:
        # Keep narrow bars aligned; quantities remain available in the data area.
        rows.extend([(f"{meter.label}: {meter.detail}", MUTED)] for meter in metrics if meter.detail)
    if quota_error:
        # Keep lengthy auth/network errors readable by scrolling the data area.
        rows.extend([(line, YELLOW)] for line in textwrap.wrap(quota_error, max(1, width)))
    # Put reset details after both quota bars so compact panes show both windows.
    rows.extend([(f"{meter.label} reset {meter.reset}", MUTED)] for meter in quotas if meter.reset)
    available = max(0, height - len(clock) - 1)
    max_scroll = max(0, len(rows) - available)
    scroll = max(0, min(scroll, max_scroll))

    def write(row: int, spans: Sequence[tuple[str, int]], bold: bool = False) -> None:
        column = 0
        for text, color in spans:
            text = "".join(char if char.isprintable() else " " for char in text).encode("ascii", "replace").decode()
            text = text[: max(0, width - column)]
            if text:
                try:
                    screen.addstr(row, column, text, colors.get(color, 0) | (curses.A_BOLD if bold else 0))
                except curses.error:
                    pass
            column += len(text)

    for row, text in enumerate(clock):
        write(row, [(text, YELLOW)], bold=True)
    for row, spans in enumerate(rows[scroll : scroll + available], start=len(clock)):
        write(row, spans)
    footer = f"10s poll | {os.cpu_count()} CPUs"
    if max_scroll:
        footer = f"Up/Down scroll {scroll + 1}/{max_scroll + 1}"
    write(height - 1, [(footer, MUTED)])
    screen.refresh()
    return scroll


def dashboard(screen: InteractiveScreen) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    colors = palette()
    screen.timeout(250)
    poller = UsagePoller()
    threading.Thread(target=poller.run, daemon=True).start()
    previous = cpu_sample()
    metrics = [Meter("CPU", None)]
    system_error = ""
    next_poll = time.monotonic() + 1
    scroll = 0
    try:
        while True:
            now = time.monotonic()
            if now >= next_poll:
                try:
                    current = cpu_sample()
                    metrics = system_meters(previous, current)
                    previous = current
                    system_error = ""
                except (OSError, ValueError, KeyError, ZeroDivisionError) as error:
                    system_error = f"System unavailable: {error}"
                next_poll = now + INTERVAL
            quotas, quota_error, updated = poller.snapshot()
            scroll = draw(screen, metrics, system_error, quotas, quota_error, updated, colors, scroll)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_NPAGE:
                scroll += max(1, screen.getmaxyx()[0] - 4)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - max(1, screen.getmaxyx()[0] - 4))
            elif key == curses.KEY_HOME:
                scroll = 0
    finally:
        poller.stop.set()


if __name__ == "__main__":
    try:
        curses.wrapper(dashboard)
    except KeyboardInterrupt:
        pass
