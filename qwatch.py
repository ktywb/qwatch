#!/usr/bin/env python3
"""Terminal monitor for Quartus compilation progress.

The Quartus GUI Monitoring Mode reads qdb/.../runlog.db as SQLite.  This
program reads the same database and task qmsgdb files in read-only mode,
and augments them with /proc metrics and checkpoint/report file events.
It never starts quartus_sh and never derives task state from the build log.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import select
import sqlite3
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass
from pathlib import Path


ESC = "\x1b["
RESET = f"{ESC}0m"
BOLD = f"{ESC}1m"
BLUE = f"{ESC}34m"
GREEN = f"{ESC}32m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"
GRAY = f"{ESC}37m"
RED = f"{ESC}31m"
CLEAR_LINE = f"{ESC}K"
CLEAR_BELOW = f"{ESC}J"
HOME = f"{ESC}H"
CLEAR_SCREEN = f"{ESC}2J"
ALT_ENTER = f"{ESC}?1049h"
ALT_LEAVE = f"{ESC}?1049l"
CURSOR_HIDE = f"{ESC}?25l"
CURSOR_SHOW = f"{ESC}?25h"

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TASK_WEIGHTS = {
    "plan": 5,
    "place": 45,
    "route": 23,
    "fast": 5,
    "retime": 6,
    "finalize": 16,
}
TASK_LABELS = {
    "a_s": "Analysis & Synthesis",
    "elab": "Analysis & Elaboration",
    "syn": "Synthesis",
    "fitter": "Fitter",
    "plan": "Plan",
    "place": "Place",
    "route": "Route",
    "fast": "Fast Forward",
    "retime": "Retime",
    "finalize": "Fitter (Finalize)",
    "asm": "Assembler",
    "sta": "Timing Analysis (Finalize)",
}


@dataclass(frozen=True)
class Task:
    id: int
    name: str
    run_name: str
    percent: int
    status: str
    start_time: int
    last_updated: int
    end_time: int
    elapsed_time: int
    process_id: int
    parent_id: int
    checkpoint: int
    result: str
    success: int
    errors: int
    critical_warnings: int
    hostname: str


@dataclass(frozen=True)
class Checkpoint:
    stage: str
    mtime: int
    predecessor_stage: str
    predecessor_mtime: int


@dataclass
class ProcRow:
    pid: int
    ppid: int
    state: str
    cpu: float
    rss_bytes: int
    vsz_bytes: int
    read_rate: float
    write_rate: float
    threads: int
    elapsed: int
    name: str


@dataclass
class ProcSnapshot:
    rows: list[ProcRow]
    alive_pids: set[int]
    qsys_elapsed: int | None
    qmsg_paths: list[Path]
    project_tool_starts: tuple[int, ...]


def clip(text: str, columns: int) -> str:
    """Trim printable text to the terminal width without ANSI state."""
    clean = ANSI_RE.sub("", text).replace("\r", "")
    return clean[: max(1, columns - 1)]


def scaled_bytes(value: float) -> str:
    if value < 0:
        return "-"
    units = ("B", "K", "M", "G", "T")
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return "-"


def elapsed_text(seconds: int) -> str:
    if seconds < 0:
        return "--:--:--"
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def status_text(status: str) -> str:
    value = status.lower()
    if value == "running":
        return "RUNNING"
    if value == "done":
        return "DONE"
    if value == "scheduled":
        return "WAITING"
    if not value or value == "no_status":
        return "-"
    return value.upper()


def color_status(status: str) -> str:
    text = f"{status_text(status):<8}"
    color = {
        "running": CYAN,
        "done": GREEN,
        "scheduled": YELLOW,
        "error": RED,
        "failed": RED,
    }.get(status.lower(), GRAY)
    return f"{color}{text}{RESET}"


def progress_bar(percent: int | None, width: int, tick: int, indeterminate: bool) -> str:
    if width < 1:
        return "-"
    if indeterminate:
        block = min(6, max(3, width // 5))
        start = (tick % (width + block)) - block
        body = "".join("█" if start <= i < start + block else "░" for i in range(width))
        return f"|{body}|"
    value = max(0, min(100, percent or 0))
    filled = int(value * width / 100.0)
    return f"|{'█' * filled}{'░' * (width - filled)}|"


class RunlogReader:
    REQUIRED_COLUMNS = {
        "id", "name", "run_name", "percent", "status", "start_time",
        "last_updated", "end_time", "elapsed_time", "process_id",
        "parent_id", "checkpoint", "result", "success", "errors", "critical_warnings",
        "hostname",
    }

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path: Path | None = None
        self.identity: tuple[int, int] | None = None
        self.last_error = ""
        self.checkpoints: dict[str, Checkpoint] = {}

    def find(self) -> Path | None:
        matches = [Path(path) for path in glob.glob(
            str(self.root / "qdb" / "_compiler" / "**" / "legacy" / "*" / "runlog.db"),
            recursive=True,
        )]
        matches = [path for path in matches if path.is_file()]
        return max(matches, key=lambda path: path.stat().st_mtime) if matches else None

    def read(self) -> list[Task]:
        path = self.find()
        if path is None:
            self.path = None
            self.identity = None
            self.checkpoints = {}
            self.last_error = "runlog.db not found; start a Quartus compilation first"
            return []
        self.path = path
        try:
            stamp = path.stat()
            self.identity = (stamp.st_dev, stamp.st_ino)
        except OSError:
            self.identity = None
        uri = f"file:{path}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=0.2)
            connection.execute("PRAGMA busy_timeout = 200")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(status)")}
            missing = self.REQUIRED_COLUMNS - columns
            if missing:
                raise RuntimeError(f"unsupported runlog schema; missing {', '.join(sorted(missing))}")
            rows = connection.execute(
                "SELECT id,name,run_name,percent,status,start_time,last_updated,"
                "end_time,elapsed_time,process_id,parent_id,checkpoint,result,success,errors,"
                "critical_warnings,hostname FROM status ORDER BY id"
            ).fetchall()
            connection.close()
        except (sqlite3.Error, OSError, RuntimeError) as error:
            self.last_error = str(error)
            return []
        self.last_error = ""
        self.read_checkpoints()
        return [Task(*row) for row in rows]

    def read_checkpoints(self) -> None:
        self.checkpoints = {}
        if self.path is None:
            return
        version_dir = self.path.parents[2]
        raw: dict[str, tuple[int, str]] = {}
        for meta in version_dir.glob("*/[0-9]*/state.meta"):
            try:
                data = json.loads(meta.read_text())
                if data.get("validity") != "LEGAL_STATE":
                    continue
                mtime = int(meta.stat().st_mtime)
                predecessor_parts = str(data.get("predecessor", "")).rstrip("/").split("/")
                predecessor = predecessor_parts[-2] if len(predecessor_parts) >= 2 else ""
            except (OSError, ValueError, TypeError, IndexError, AttributeError):
                continue
            stage = meta.parent.parent.name
            if stage not in raw or mtime > raw[stage][0]:
                raw[stage] = (mtime, predecessor)
        for stage, (mtime, predecessor) in raw.items():
            predecessor_mtime = raw.get(predecessor, (mtime, ""))[0]
            self.checkpoints[stage] = Checkpoint(stage, mtime, predecessor, predecessor_mtime)

    @staticmethod
    def checkpoint_stage_for_task(name: str) -> str:
        return {
            "Analysis & Synthesis": "synthesized",
            "Analysis & Elaboration": "partitioned",
            "Synthesis": "synthesized",
            "Plan": "planned",
            "Place": "placed",
            "Route": "routed",
            "Fast Forward": "fastforward",
            "Retime": "retimed",
            "Fitter (Finalize)": "final",
        }.get(name, "")

    def task_has_checkpoint(self, task: Task, tolerance: int = 60) -> bool:
        stage = self.checkpoint_stage_for_task(task.name)
        checkpoint = self.checkpoints.get(stage)
        return bool(
            checkpoint and task.status == "done" and task.success and task.checkpoint
            and task.end_time > 0 and abs(checkpoint.mtime - task.end_time) <= tolerance
        )

    def synthetic_task(self, key: str, run_name: str) -> Task | None:
        stage = {
            "plan": "planned", "place": "placed", "route": "routed",
            "fast": "fastforward", "retime": "retimed", "finalize": "final",
        }.get(key, "")
        checkpoint = self.checkpoints.get(stage)
        if checkpoint is None:
            return None
        start = min(checkpoint.mtime, checkpoint.predecessor_mtime)
        return Task(
            -1, TASK_LABELS[key], run_name, 100, "done", start,
            checkpoint.mtime, checkpoint.mtime,
            max(0, checkpoint.mtime - start), 0, 0, 1,
            "done", 1, 0, 0, "",
        )

    def has_synthesis_checkpoint(self, producer: Task, maximum_gap: int) -> bool:
        if self.path is None or not producer.checkpoint:
            return False
        version_dir = self.path.parents[2]
        for meta in version_dir.glob("synthesized/[0-9]*/state.meta"):
            try:
                data = json.loads(meta.read_text())
                mtime = int(meta.stat().st_mtime)
            except (OSError, ValueError, TypeError):
                continue
            if (data.get("validity") == "LEGAL_STATE"
                    and producer.start_time <= mtime <= producer.end_time + maximum_gap):
                return True
        return False

    def quartus_session_start(self, rows: list[Task], root: Task, maximum_gap: int) -> int:
        """Join adjacent Quartus invocations, without using their launcher."""
        if root.name != "Flow":
            return root.start_time
        roots = sorted(
            (row for row in rows
             if row.run_name == root.run_name
             and row.name in {"Flow", TASK_LABELS["a_s"]}
             and row.start_time <= root.start_time),
            key=lambda row: (row.start_time, row.id),
        )
        boundary = root.start_time
        for previous in reversed(roots[:-1]):
            if previous.status != "done" or previous.end_time <= 0:
                break
            gap = boundary - previous.end_time
            if gap < 0 or gap > maximum_gap:
                break
            if (previous.name == TASK_LABELS["a_s"]
                    and not self.has_synthesis_checkpoint(previous, maximum_gap)):
                break
            boundary = previous.start_time
        return boundary

    def heartbeat_age(self, run_name: str) -> float | None:
        if self.path is None or not run_name:
            return None
        heartbeat = self.path.parent / f"flow.{run_name}.heartbeat"
        try:
            return max(0.0, time.time() - heartbeat.stat().st_mtime)
        except OSError:
            return None


class LogTail:
    def __init__(self, root: Path, requested: str | None, line_count: int) -> None:
        self.root = root
        self.requested = requested
        self.history: deque[str] = deque(maxlen=1000)
        self.partial = ""
        self.path: Path | None = None
        self.handle = None
        self.offset = 0
        self.view_offset = 0
        self.line_count = line_count

    @property
    def enabled(self) -> bool:
        return self.requested is not None

    def select_auto(self) -> Path | None:
        selected: Path | None = None
        selected_mtime = -1.0
        log_dir = self.root / "logs"
        for latest in log_dir.glob("*.latest.log"):
            try:
                mtime = latest.stat().st_mtime
            except OSError:
                continue
            if mtime >= selected_mtime:
                selected, selected_mtime = latest, mtime
        return selected

    def wanted_path(self) -> Path | None:
        if self.requested is None:
            return None
        if self.requested == "auto":
            return self.select_auto()
        path = Path(self.requested)
        return path if path.is_absolute() else self.root / path

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
        self.handle = None
        self.path = None
        self.offset = 0

    def refresh(self) -> None:
        wanted = self.wanted_path()
        if wanted is None:
            return
        try:
            resolved = wanted.resolve(strict=True)
        except OSError:
            return
        if self.path != resolved:
            self.close()
            self.path = resolved
            self.history.clear()
            self.partial = ""
            self.view_offset = 0
            try:
                self.handle = resolved.open("rb")
                size = resolved.stat().st_size
                if size > 131072:
                    self.handle.seek(size - 131072)
                    self.handle.readline()
                self.offset = self.handle.tell()
            except OSError:
                self.close()
                return
        if self.handle is None:
            return
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.handle.seek(0)
                self.offset = 0
                self.history.clear()
                self.partial = ""
            data = self.handle.read()
            self.offset = self.handle.tell()
        except OSError:
            return
        if not data:
            return
        pieces = (self.partial + data.decode("utf-8", errors="replace")).replace("\r", "").split("\n")
        self.partial = pieces.pop()
        self.history.extend(ANSI_RE.sub("", line) for line in pieces)

    def display_path(self) -> str:
        if self.path is None:
            return "(no log selected)"
        try:
            return str(self.path.relative_to(self.root))
        except ValueError:
            return str(self.path)

    def lines(self, count: int, columns: int) -> list[str]:
        lines = list(self.history)
        if self.partial:
            lines.append(self.partial)
        if not lines:
            return ["(empty)"]
        bottom = max(0, len(lines) - count)
        first = max(0, bottom - self.view_offset)
        return [clip(line, columns) for line in lines[first:first + count]]


class QMessageTail:
    """Merged incremental view of all task databases in this Quartus session."""

    REQUIRED_COLUMNS = {"sequence_id", "time", "source", "type", "text"}

    def __init__(self, root: Path, line_count: int) -> None:
        self.root = root
        self.line_count = line_count
        self.history: deque[str] = deque(maxlen=50000)
        self.path: Path | None = None
        self.paths: set[Path] = set()
        self.cursors: dict[Path, int] = {}
        self.validated: set[Path] = set()
        self.view_offset = 0
        self.last_error = ""
        self.session_start = 0
        self.database_dir: Path | None = None
        self.last_message_time: float | None = None

    def set_session(self, start_time: int, database_dir: Path | None) -> None:
        self.session_start = start_time
        self.database_dir = database_dir
        self.path = None
        self.paths.clear()
        self.cursors.clear()
        self.validated.clear()
        self.history.clear()
        self.view_offset = 0
        self.last_message_time = None

    def resolve_paths(self, paths: list[Path]) -> set[Path]:
        candidates: set[Path] = set()
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
                recent = resolved.stat().st_mtime >= self.session_start
            except OSError:
                continue
            if recent and not resolved.name.endswith(".header.qmsgdb"):
                candidates.add(resolved)
        return candidates

    def candidates(self, active_paths: list[Path]) -> set[Path]:
        directory = self.database_dir
        paths = list(directory.glob("*.qmsgdb")) if directory is not None else []
        paths.extend(active_paths)
        return self.resolve_paths(paths)

    @staticmethod
    def row_key(path: Path, row: tuple[object, ...]) -> tuple[str, str, int]:
        return str(row[1] or ""), path.name, int(row[0])

    def format_message(self, row: tuple[object, ...]) -> str:
        _, stamp, source, kind, message = row
        stamp_text = str(stamp or "")
        display_stamp = stamp_text[11:19] if len(stamp_text) >= 19 else stamp_text
        prefix = " ".join(part for part in (display_stamp, str(kind or ""), str(source or "")) if part)
        body = ANSI_RE.sub("", str(message or "")).replace("\r", " ").replace("\n", " ")
        return f"{prefix}: {body}" if prefix else body

    def update_last_message_time(self, rows: list[tuple[Path, tuple[object, ...]]]) -> None:
        for _, row in reversed(rows):
            try:
                self.last_message_time = time.mktime(
                    time.strptime(str(row[1] or ""), "%Y-%m-%d %H:%M:%S")
                )
                return
            except ValueError:
                continue

    def connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.2)
        connection.execute("PRAGMA busy_timeout = 200")
        if path not in self.validated:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            missing = self.REQUIRED_COLUMNS - columns
            if missing:
                connection.close()
                raise RuntimeError(f"unsupported qmsgdb schema; missing {', '.join(sorted(missing))}")
            self.validated.add(path)
        return connection

    def reload(self, paths: set[Path]) -> None:
        records: list[tuple[Path, tuple[object, ...]]] = []
        cursors: dict[Path, int] = {}
        for path in paths:
            connection = self.connect(path)
            rows = connection.execute(
                "SELECT sequence_id,time,source,type,text FROM "
                "(SELECT sequence_id,time,source,type,text FROM messages "
                "ORDER BY sequence_id DESC LIMIT 50000) ORDER BY sequence_id"
            ).fetchall()
            connection.close()
            records.extend((path, row) for row in rows)
            cursors[path] = max((int(row[0]) for row in rows), default=0)
        records.sort(key=lambda item: self.row_key(*item))
        self.history.clear()
        self.view_offset = 0
        self.history.extend(self.format_message(row) for _, row in records)
        self.update_last_message_time(records)
        self.paths = set(paths)
        self.cursors = cursors

    def refresh(self, active_paths: list[Path]) -> None:
        paths = self.candidates(active_paths)
        if not paths:
            return
        try:
            active = self.resolve_paths(active_paths)
            self.path = max(active or paths, key=lambda path: path.stat().st_mtime)
            if paths != self.paths:
                self.reload(paths)
                self.last_error = ""
                return
            poll_paths = active or {self.path}
            records: list[tuple[Path, tuple[object, ...]]] = []
            for path in poll_paths:
                connection = self.connect(path)
                maximum = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence_id), 0) FROM messages"
                ).fetchone()[0])
                if maximum < self.cursors.get(path, 0):
                    connection.close()
                    self.reload(paths)
                    return
                rows = connection.execute(
                    "SELECT sequence_id,time,source,type,text FROM messages "
                    "WHERE sequence_id > ? ORDER BY sequence_id",
                    (self.cursors.get(path, 0),)
                ).fetchall()
                connection.close()
                records.extend((path, row) for row in rows)
                self.cursors[path] = maximum
            records.sort(key=lambda item: self.row_key(*item))
            if records and self.view_offset:
                self.view_offset += len(records)
            self.history.extend(self.format_message(row) for _, row in records)
            self.update_last_message_time(records)
            self.last_error = ""
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
            self.last_error = str(error)

    def display_path(self) -> str:
        if self.path is None:
            return "(no qmsgdb in current Quartus session)"
        try:
            current = str(self.path.relative_to(self.root))
        except ValueError:
            current = str(self.path)
        extra = max(0, len(self.paths) - 1)
        return f"{current} (+{extra} task dbs)" if extra else current

    def lines(self, count: int, columns: int) -> list[str]:
        lines = list(self.history)
        if not lines:
            return ["(no task messages yet)"]
        bottom = max(0, len(lines) - count)
        first = max(0, bottom - self.view_offset)
        return [clip(line, columns) for line in lines[first:first + count]]

    def age_text(self) -> str:
        if self.last_message_time is None:
            return "no messages"
        seconds = max(0, int(time.time() - self.last_message_time))
        if seconds < 60:
            return f"last {seconds}s"
        return f"last {seconds // 60}m"


class EventWatcher:
    """Report stage/checkpoint file transitions without interpreting report contents."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.known: dict[Path, tuple[int, int]] = {}
        self.events: deque[str] = deque(maxlen=20)
        self.initialized = False

    def reset(self) -> None:
        self.known.clear()
        self.events.clear()
        self.initialized = False

    def refresh(self, runlog_path: Path | None) -> None:
        paths: list[Path] = []
        if runlog_path is not None and len(runlog_path.parents) >= 3:
            version_dir = runlog_path.parents[2]
            paths.extend(version_dir.glob("*/[0-9]*/state.meta"))
        paths.extend(self.root.glob("*.rpt"))
        current: dict[Path, tuple[int, int]] = {}
        for path in paths:
            try:
                stamp = path.stat()
            except OSError:
                continue
            state = (stamp.st_mtime_ns, stamp.st_size)
            current[path] = state
            if self.initialized and self.known.get(path) != state:
                when = time.strftime("%H:%M:%S", time.localtime(stamp.st_mtime))
                if path.name == "state.meta":
                    validity = ""
                    try:
                        validity = str(json.loads(path.read_text()).get("validity", ""))
                    except (OSError, ValueError, TypeError):
                        pass
                    stage = path.parent.parent.name.upper()
                    detail = f"checkpoint {stage}" + (f" {validity}" if validity else "")
                else:
                    detail = f"report {path.name}"
                self.events.append(f"{when}  {detail}")
        self.known = current
        self.initialized = True


class ProcSampler:
    def __init__(self) -> None:
        self.previous: dict[int, tuple[int, int, int, float]] = {}
        self.clk_tck = int(os.sysconf("SC_CLK_TCK"))

    @staticmethod
    def read_stat(pid: int) -> tuple[int, str, int, int, int, int, str] | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            opening = raw.find("(")
            closing = raw.rfind(") ")
            if opening < 0 or closing < opening:
                return None
            name = raw[opening + 1:closing]
            rest = raw[closing + 2:]
            fields = rest.split()
            # fields starts at proc(5) field 3, so 11/12 are utime/stime (14/15).
            return (int(fields[1]), fields[0], int(fields[11]), int(fields[12]),
                    int(fields[17]), int(fields[19]), name)
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def read_status(pid: int) -> tuple[int, int, int]:
        rss_kb = vsz_kb = threads = 0
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                key, _, value = line.partition(":")
                if key == "VmRSS":
                    rss_kb = int(value.split()[0])
                elif key == "VmSize":
                    vsz_kb = int(value.split()[0])
                elif key == "Threads":
                    threads = int(value.strip())
        except (OSError, ValueError, IndexError):
            pass
        return rss_kb * 1024, vsz_kb * 1024, threads

    @staticmethod
    def read_io(pid: int) -> tuple[int, int]:
        read_bytes = write_bytes = 0
        try:
            for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                key, _, value = line.partition(":")
                if key == "read_bytes":
                    read_bytes = int(value.strip())
                elif key == "write_bytes":
                    write_bytes = int(value.strip())
        except (OSError, ValueError):
            pass
        return read_bytes, write_bytes

    @staticmethod
    def read_cmdline(pid: int) -> str:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""

    @staticmethod
    def qmsg_fds(pid: int) -> list[Path]:
        paths: list[Path] = []
        try:
            entries = Path(f"/proc/{pid}/fd").iterdir()
            for entry in entries:
                try:
                    target = Path(os.readlink(entry))
                except OSError:
                    continue
                if target.suffix == ".qmsgdb" and not target.name.endswith(".header.qmsgdb"):
                    paths.append(target)
        except OSError:
            pass
        return paths

    def snapshot(self, active_pids: set[int], root: Path) -> ProcSnapshot:
        now = time.monotonic()
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        boot_time = time.time() - uptime
        stats: dict[int, tuple[int, str, int, int, int, int, str]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            stat = self.read_stat(int(entry.name))
            if stat is not None:
                stats[int(entry.name)] = stat
        included = set(active_pids)
        for pid in tuple(active_pids):
            parent = stats.get(pid, (0, "", 0, 0, 0, 0, ""))[0]
            while parent in stats and parent not in included:
                included.add(parent)
                parent = stats[parent][0]
        rows: list[ProcRow] = []
        current: dict[int, tuple[int, int, int, float]] = {}
        qsys_elapsed: int | None = None
        qmsg_paths: list[Path] = []
        project_tool_starts: list[int] = []
        for pid, stat in stats.items():
            name = stat[6]
            command = ""
            is_project_tool = False
            if "qsys" in name.lower():
                command = self.read_cmdline(pid)
                is_project_tool = "qsys-generate" in command and str(root) in command
            elif name.startswith("quartus"):
                try:
                    is_project_tool = Path(f"/proc/{pid}/cwd").resolve() == root
                except OSError:
                    is_project_tool = False
                if is_project_tool:
                    command = self.read_cmdline(pid)
                    # Worker processes are born throughout a task; only a
                    # project-level driver is a stable generation boundary.
                    if "--ipc_mode" in command or "--ipc_sh" in command:
                        is_project_tool = False
            if is_project_tool:
                project_tool_starts.append(int(boot_time + stat[5] / self.clk_tck))
            if "qsys" in name.lower() and is_project_tool:
                elapsed = max(0, int(uptime - stat[5] / self.clk_tck))
                qsys_elapsed = max(qsys_elapsed or 0, elapsed)
        for pid in included:
            stat = stats.get(pid)
            if stat is None:
                continue
            ppid, state, utime, stime, threads_from_stat, start_ticks, name = stat
            if not name.startswith("quartus"):
                continue
            ticks = utime + stime
            read_bytes, write_bytes = self.read_io(pid)
            previous = self.previous.get(pid)
            cpu = read_rate = write_rate = 0.0
            if previous is not None:
                old_ticks, old_read, old_write, old_time = previous
                elapsed = now - old_time
                if elapsed > 0:
                    cpu = (ticks - old_ticks) * 100.0 / (self.clk_tck * elapsed)
                    read_rate = max(0.0, (read_bytes - old_read) / elapsed)
                    write_rate = max(0.0, (write_bytes - old_write) / elapsed)
            current[pid] = (ticks, read_bytes, write_bytes, now)
            rss, vsz, threads = self.read_status(pid)
            qmsg_paths.extend(self.qmsg_fds(pid))
            rows.append(ProcRow(
                pid, ppid, state, max(0.0, cpu), rss, vsz, read_rate, write_rate,
                threads or threads_from_stat,
                max(0, int(uptime - start_ticks / self.clk_tck)), name,
            ))
        self.previous = current
        return ProcSnapshot(
            rows=sorted(rows, key=lambda row: row.cpu, reverse=True),
            alive_pids=set(stats), qsys_elapsed=qsys_elapsed,
            qmsg_paths=list(dict.fromkeys(qmsg_paths)),
            project_tool_starts=tuple(sorted(set(project_tool_starts))),
        )


class Terminal:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.enabled = sys.stdin.isatty() and sys.stdout.isatty()
        self.old_state = None

    def __enter__(self) -> "Terminal":
        if self.enabled:
            self.old_state = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            sys.stdout.write(f"{RESET}{ALT_ENTER}{CURSOR_HIDE}{CLEAR_SCREEN}{HOME}")
            sys.stdout.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self.enabled:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_state)
            sys.stdout.write(f"{RESET}{CURSOR_SHOW}{ALT_LEAVE}")
            sys.stdout.flush()

    def keys(self) -> bytes:
        if not self.enabled:
            return b""
        ready, _, _ = select.select([self.fd], [], [], 0)
        return os.read(self.fd, 64) if ready else b""

    @staticmethod
    def size() -> os.terminal_size:
        return os.get_terminal_size(sys.stdout.fileno()) if sys.stdout.isatty() else os.terminal_size((80, 40))


class QWatch:
    def __init__(self, root: Path, project: str | None, interval: float, log: str | None,
                 log_lines: int, stitch_gap: int = 60) -> None:
        self.root = root.resolve()
        qpf_files = sorted(self.root.glob("*.qpf"))
        self.project_override = project
        self.project = project or (qpf_files[0].stem if len(qpf_files) == 1 else self.root.name)
        self.interval = max(0.2, interval)
        self.stitch_gap = max(0, stitch_gap)
        self.reader = RunlogReader(self.root)
        self.tailer = LogTail(self.root, log, log_lines)
        self.messages = QMessageTail(self.root, log_lines)
        self.events = EventWatcher(self.root)
        self.procs = ProcSampler()
        self.rows: list[Task] = []
        self.proc_rows: list[ProcRow] = []
        self.session_start = 0
        self.invocation_start = 0
        self.invocation_root_name = ""
        self.message_start = 0
        self.session_run_name = ""
        self.fit_last_percent = -1
        self.paused = False
        self.message_mode = True
        self.tick = 0
        self.last_size: tuple[int, int] | None = None
        self.last_error = ""
        self.runlog_identity: tuple[int, int] | None = None
        self.pending_generation_start = 0
        self.pending_generation_reason = ""
        self.qsys_started: float | None = None
        self.qsys_last_seen = 0.0
        self.qsys_final = -1
        self.qsys_snapshot = ("", -1)
        self.alive_pids: set[int] = set()
        self.update_age: int | None = None
        self.heartbeat_age: int | None = None
        self.input_buffer = b""

    def task(self, key: str) -> Task | None:
        label = TASK_LABELS[key]
        current = self.session_rows()
        matches = [row for row in current if row.name == label]
        if key == "fitter":
            matches = [row for row in current if row.name in {"Fitter", "Fitter (Partial)"}]
        if not matches:
            return (self.reader.synthetic_task(key, self.session_run_name)
                    if self.invocation_root_name == "Flow" else None)
        return max(matches, key=lambda row: (row.start_time, row.last_updated, row.id))

    def session_rows(self) -> list[Task]:
        if self.pending_generation_start or not self.session_start:
            return []
        return [row for row in self.rows
                if row.run_name == self.session_run_name
                and (row.start_time >= self.invocation_start
                     or (row.status == "scheduled" and row.last_updated >= self.invocation_start)
                     or (self.invocation_root_name == "Flow"
                         and self.reader.task_has_checkpoint(row)))]

    @staticmethod
    def newest_activity(rows: list[Task]) -> int:
        return max((max(row.start_time, row.last_updated, row.end_time)
                    for row in rows), default=0)

    @staticmethod
    def latest_root(rows: list[Task]) -> Task | None:
        roots = [row for row in rows if row.name in {"Flow", TASK_LABELS["a_s"]}]
        return max(roots, key=lambda row: (row.start_time, row.id), default=None)

    def begin_generation(self, start_time: int, reason: str) -> None:
        start_time = max(1, int(start_time))
        if self.pending_generation_start == start_time:
            return
        self.pending_generation_start = start_time
        self.pending_generation_reason = reason
        self.rows = []
        self.session_start = 0
        self.invocation_start = 0
        self.invocation_root_name = ""
        self.message_start = 0
        self.session_run_name = ""
        self.fit_last_percent = -1
        self.update_age = None
        self.heartbeat_age = None
        self.qsys_started = None
        self.qsys_last_seen = 0.0
        self.qsys_final = -1
        self.qsys_snapshot = ("", -1)
        self.messages.set_session(start_time, None)
        self.events.reset()

    def adopt_rows(self, rows: list[Task], root: Task) -> None:
        invocation = (root.run_name, root.start_time)
        session_start = self.reader.quartus_session_start(rows, root, self.stitch_gap)
        if (root.start_time
                and (invocation != (self.session_run_name, self.invocation_start)
                     or session_start != self.session_start)):
            self.session_run_name = root.run_name
            self.invocation_start = root.start_time
            self.invocation_root_name = root.name
            self.session_start = session_start
            self.fit_last_percent = -1
        self.pending_generation_start = 0
        self.pending_generation_reason = ""
        self.rows = rows
        visible_starts = [row.start_time for row in self.session_rows() if row.start_time > 0]
        message_start = min(visible_starts, default=self.session_start)
        if message_start != self.message_start:
            self.message_start = message_start
            database_dir = self.reader.path.parent if self.reader.path is not None else None
            self.messages.set_session(self.message_start, database_dir)

    def refresh(self) -> None:
        previous_identity = self.runlog_identity
        rows = self.reader.read()
        self.last_error = self.reader.last_error
        self.tailer.refresh()

        # Sample both the newly read rows and the cached rows.  This lets a
        # new project-scoped Quartus driver invalidate a stale run before its
        # replacement runlog.db has been created.
        sampled_rows = rows or self.rows
        active = {row.process_id for row in sampled_rows
                  if row.status == "running" and row.process_id > 0}
        proc_snapshot = self.procs.snapshot(active, self.root)
        current_identity = self.reader.identity
        if self.project_override is None and self.reader.path is not None:
            try:
                compiler_relative = self.reader.path.relative_to(
                    self.root / "qdb" / "_compiler"
                )
                self.project = compiler_relative.parts[0]
            except (ValueError, IndexError):
                pass
        identity_lost = previous_identity is not None and current_identity is None
        identity_changed = (previous_identity is not None and current_identity is not None
                            and previous_identity != current_identity)
        running_ids = {row.process_id for row in sampled_rows
                       if row.status == "running" and row.process_id > 0}
        old_run_alive = bool(running_ids & proc_snapshot.alive_pids)
        activity = self.newest_activity(sampled_rows)
        new_tool_starts = [started for started in proc_snapshot.project_tool_starts
                           if started > activity + 1]
        new_tool_start = min(new_tool_starts, default=None)
        tool_started_new_generation = bool(new_tool_start is not None and not old_run_alive)
        # Quartus may briefly replace or hide runlog.db while a compiler
        # process is still alive.  Keep the cached session in that case;
        # disappearance is a generation boundary only after the old run dies.
        if identity_lost and not old_run_alive:
            self.begin_generation(new_tool_start or int(time.time()),
                                  "runlog reset")
        elif identity_changed:
            root = self.latest_root(rows)
            self.begin_generation(root.start_time if root is not None else int(time.time()),
                                  "new runlog")
        elif tool_started_new_generation:
            self.begin_generation(new_tool_start or int(time.time()),
                                  "new Quartus process")
        self.runlog_identity = current_identity

        root = self.latest_root(rows)
        if root is not None:
            live_runlog = any(
                row.status == "running" and row.process_id > 0
                and row.process_id in proc_snapshot.alive_pids
                for row in rows
            )
            new_enough = (not self.pending_generation_start
                          or root.start_time >= self.pending_generation_start - 10
                          or live_runlog)
            if new_enough:
                self.adopt_rows(rows, root)

        current = self.session_rows()
        self.proc_rows = proc_snapshot.rows
        self.alive_pids = proc_snapshot.alive_pids
        self.messages.refresh(proc_snapshot.qmsg_paths)
        self.events.refresh(self.reader.path)
        self.update_qsys_status(proc_snapshot.qsys_elapsed)
        updates = [row.last_updated for row in current if row.last_updated > 0]
        self.update_age = max(0, int(time.time()) - max(updates)) if updates else None
        heartbeat = self.reader.heartbeat_age(self.session_run_name)
        self.heartbeat_age = int(heartbeat) if heartbeat is not None else None
        self.tick += 1

    def task_status(self, key: str) -> tuple[str, int | None, int]:
        task = self.task(key)
        if task is None:
            return "", None, -1
        if task.status == "running" and task.start_time:
            # Fitter child rows report cumulative Fitter elapsed_time while
            # running.  Their own start_time is the stage-local boundary.
            elapsed = max(0, int(time.time()) - task.start_time)
        elif task.status == "done" and task.start_time and task.end_time >= task.start_time:
            elapsed = task.end_time - task.start_time
        elif task.status == "scheduled":
            elapsed = -1
        else:
            elapsed = task.elapsed_time if task.elapsed_time > 0 else -1
        return task.status, task.percent, elapsed

    def fit_status(self) -> tuple[str, int, int]:
        children = [self.task(key) for key in TASK_WEIGHTS]
        statuses = [task.status for task in children if task is not None]
        parent = self.task("fitter")
        if parent is not None and parent.status == "done":
            status = "done"
        elif "running" in statuses or (parent is not None and parent.status == "running"):
            status = "running"
        elif "done" in statuses:
            status = "scheduled"
        else:
            return "", 0, -1
        fast = self.task("fast")
        later = self.task("retime") or self.task("finalize")
        skipped_fast = fast is None and later is not None
        weight_total = 95 if skipped_fast else 100
        progress = 0.0
        elapsed = 0
        for key, weight in TASK_WEIGHTS.items():
            if skipped_fast and key == "fast":
                continue
            task = self.task(key)
            if task is None:
                continue
            if task.status == "done":
                progress += weight
            elif task.status == "running":
                progress += weight * max(0, min(100, task.percent)) / 100.0
            stage_elapsed = self.task_status(key)[2]
            if stage_elapsed > 0:
                elapsed += stage_elapsed
        percent = int(round(progress * 100.0 / weight_total))
        if status == "done":
            percent = 100
        elif percent >= 100:
            percent = 99
        percent = max(percent, self.fit_last_percent)
        self.fit_last_percent = percent
        child_starts = [task.start_time for task in children if task is not None and task.start_time > 0]
        if (parent is not None and parent.start_time
                and (not child_starts or parent.start_time <= min(child_starts))):
            elapsed = (int(time.time()) - parent.start_time
                       if parent.status == "running" else parent.elapsed_time)
        return status, percent, elapsed if elapsed else -1

    def total_elapsed(self) -> int:
        if not self.session_start:
            return -1
        current = self.session_rows()
        starts = [row.start_time for row in current if row.start_time > 0]
        start = min(starts, default=self.session_start)
        ends = [row.end_time for row in current if row.end_time > 0]
        end = max(ends, default=int(time.time()))
        if any(row.status == "running" for row in current):
            end = int(time.time())
        return max(0, end - start)

    def update_qsys_status(self, elapsed: int | None) -> None:
        now = time.time()
        if elapsed is not None:
            candidate_start = now - elapsed
            self.qsys_started = candidate_start if not self.qsys_last_seen else min(
                self.qsys_started, candidate_start
            )
            self.qsys_last_seen = now
            self.qsys_final = -1
            self.qsys_snapshot = ("running", max(0, int(now - self.qsys_started)))
            return
        if self.qsys_started is not None and self.qsys_last_seen:
            if now - self.qsys_last_seen <= 3:
                self.qsys_snapshot = ("running", max(0, int(now - self.qsys_started)))
                return
            self.qsys_final = max(0, int(self.qsys_last_seen - self.qsys_started))
            self.qsys_last_seen = 0.0
        if self.qsys_final >= 0:
            self.qsys_snapshot = ("done", self.qsys_final)
            return
        self.qsys_snapshot = ("", -1)

    @staticmethod
    def bar_width(columns: int) -> int:
        return max(8, min(30, columns - 44))

    def task_line(self, label: str, key: str, width: int, indent: str = "") -> str:
        status, percent, elapsed = self.task_status(key)
        if not status:
            return self.row(f"{indent}{label}", "-", "-", "", -1, width)
        indeterminate = key in {"fast", "finalize"} and status == "running" and not percent
        value = None if indeterminate else percent
        return self.row(f"{indent}{label}", progress_bar(value, width, self.tick, indeterminate),
                        "-" if indeterminate else f"{percent:3d}", status, elapsed, width)

    @staticmethod
    def row(stage: str, progress: str, percent: str, status: str, elapsed: int, width: int) -> str:
        return f"{stage:<12} {progress:<{width + 2}} {percent:>6}   {color_status(status)}  {elapsed_text(elapsed)}"

    def process_lines(self, columns: int, maximum: int) -> list[str]:
        wide = columns >= 105
        if wide:
            lines = ["PID     PPID    S   CPU%     RSS     VSZ    R/s    W/s  THR ELAPSED   PROCESS"]
        else:
            lines = ["PID     PPID    S   CPU%     RSS   IO/s  THR ELAPSED   PROCESS"]
        for row in self.proc_rows[:maximum]:
            if wide:
                line = (f"{row.pid:<7d} {row.ppid:<7d} {row.state:<1} {row.cpu:6.1f} "
                        f"{scaled_bytes(row.rss_bytes):>7} {scaled_bytes(row.vsz_bytes):>7} "
                        f"{scaled_bytes(row.read_rate):>6} {scaled_bytes(row.write_rate):>6} "
                        f"{row.threads:>4d} {elapsed_text(row.elapsed):>8}  {row.name}")
            else:
                io_rate = row.read_rate + row.write_rate
                line = (f"{row.pid:<7d} {row.ppid:<7d} {row.state:<1} {row.cpu:6.1f} "
                        f"{scaled_bytes(row.rss_bytes):>7} {scaled_bytes(io_rate):>6} "
                        f"{row.threads:>4d} {elapsed_text(row.elapsed):>8}  {row.name}")
            lines.append(clip(line, columns))
        if len(lines) == 1:
            lines.append("(no matching Quartus processes)")
        return lines

    def health_line(self, columns: int) -> str:
        if self.pending_generation_start:
            detail = self.pending_generation_reason or "waiting for runlog"
            return f"{CYAN}{clip(f'Flow: PREPARING  {detail}', columns)}{RESET}"
        running = [row for row in self.session_rows() if row.status == "running"]
        active = max(running, key=lambda row: (row.last_updated, row.start_time, row.id), default=None)
        if active is None:
            state = "IDLE" if self.session_start else "WAITING"
            return f"{GRAY}Flow: {state}  run={self.session_run_name or '-'}{RESET}"
        update = f"{self.update_age}s" if self.update_age is not None else "n/a"
        heartbeat = f"{self.heartbeat_age}s" if self.heartbeat_age is not None else "n/a"
        if active.process_id > 0:
            pid_ok = active.process_id in self.alive_pids
            pid = f"PID {active.process_id} {'OK' if pid_ok else 'DEAD'}"
        else:
            pid_ok = True
            pid = "PID n/a"
        unhealthy = not pid_ok and (self.heartbeat_age is None or self.heartbeat_age > 300)
        state = "STALE" if unhealthy else ("QUIET" if (self.update_age or 0) > 120 else "ACTIVE")
        color = RED if unhealthy else (YELLOW if state == "QUIET" else CYAN)
        current = self.session_rows()
        errors = max((int(row.errors or 0) for row in current), default=0)
        warnings = max((int(row.critical_warnings or 0) for row in current), default=0)
        text = (f"Active: {active.name} {active.percent}%  update {update}  {pid}  "
                f"heartbeat {heartbeat}  E{errors}/CW{warnings}  {state}")
        return f"{color}{clip(text, columns)}{RESET}"

    def render(self, terminal: Terminal) -> None:
        size = terminal.size()
        rows, columns = size.lines, size.columns
        compact = rows < 34
        process_max = 2 if compact else 5
        width = self.bar_width(columns)
        heartbeat = "■" if self.tick % 2 else "□"
        mode = "PAUSED" if self.paused else "LIVE"
        mode_color = YELLOW if self.paused else GREEN
        screen: list[str] = [f"{BOLD}{BLUE}{heartbeat} Quartus Monitor {mode_color}[{mode}]{RESET}  {self.project}  {self.interval:g}s"]
        screen.append(self.health_line(columns))
        if not compact:
            screen.extend(("", f"{BOLD}{BLUE}Processes{RESET}"))
        screen.extend(self.process_lines(columns, process_max))
        if not compact:
            screen.extend(("", f"{BOLD}{BLUE}Compilation{RESET}"))
        header = f"{'Stage':<12} {'Progress':<{width + 2}} {'%':>6}   {'Status':<8}  Elapsed"
        screen.append(f"{BOLD}{BLUE}{header}{RESET}")
        qsys_status, qsys_elapsed = self.qsys_snapshot
        screen.append(self.row("QSYS", "-", "-", qsys_status, qsys_elapsed, width))
        screen.append(self.task_line("A&S", "a_s", width))
        screen.append(self.task_line("ELAB", "elab", width, "  "))
        screen.append(self.task_line("SYN", "syn", width, "  "))
        fit_status, fit_percent, fit_elapsed = self.fit_status()
        if fit_status:
            screen.append(self.row("FIT (est.)", progress_bar(fit_percent, width, self.tick, False), f"{fit_percent:3d}", fit_status, fit_elapsed, width))
        else:
            screen.append(self.row("FIT", "-", "-", "", -1, width))
        for label, key in (("PLAN", "plan"), ("PLACE", "place"), ("ROUTE", "route"),
                           ("FAST-FWD", "fast"), ("RETIME", "retime"), ("FINALIZE", "finalize")):
            screen.append(self.task_line(label, key, width, "  "))
        screen.append(self.task_line("ASM", "asm", width))
        screen.append(self.task_line("STA", "sta", width))
        screen.append(f"{BOLD}{'Total : ' :>{35 + width}}{elapsed_text(self.total_elapsed())}{RESET}")
        if not compact:
            screen.extend(("", f"{BOLD}{BLUE}Events{RESET}"))
            event_lines = list(self.events.events)[-2:] or ["(no new checkpoint/report events)"]
            screen.extend(clip(line, columns) for line in event_lines)
        source = self.messages if self.message_mode else self.tailer
        source_label = (f"Messages ({self.messages.age_text()})"
                        if self.message_mode else "Log")
        source_path = source.display_path()
        if source.path is not None:
            screen.extend(("", f"{BOLD}{BLUE}{clip(source_label + ': ' + source_path, columns)}{RESET}"))
            count = max(1, min(source.line_count, rows - len(screen) - 2))
            screen.extend(source.lines(count, columns))
        elif source is self.messages:
            detail = self.messages.last_error or source_path
            screen.extend(("", f"{BOLD}{BLUE}Messages: {clip(detail, columns)}{RESET}"))
            screen.extend(self.messages.lines(1, columns))
        elif self.last_error:
            screen.extend(("", f"{RED}Status: {clip(self.last_error, columns)}{RESET}"))
        if not compact:
            screen.append("")
        footer = "q/Ctrl-C exit  Space pause  +/- rate  ↑/↓ scroll"
        if self.tailer.enabled:
            footer += "  m messages/log"
        screen.append(footer)
        clear = CLEAR_SCREEN if self.last_size != (rows, columns) else ""
        self.last_size = (rows, columns)
        rendered = "\n".join(f"{line}{CLEAR_LINE}" for line in screen)
        sys.stdout.write(f"{RESET}{clear}{HOME}{rendered}{CLEAR_BELOW}{RESET}")
        sys.stdout.flush()

    def handle_keys(self, keys: bytes) -> tuple[bool, bool]:
        """Return (keep_running, redraw_now), retaining split escape keys."""
        self.input_buffer += keys
        redraw = False
        while self.input_buffer:
            if self.input_buffer.startswith(b"\x1b"):
                if len(self.input_buffer) < 3:
                    break
                sequence, self.input_buffer = self.input_buffer[:3], self.input_buffer[3:]
                source = self.messages if self.message_mode else self.tailer
                available = len(source.history) + (bool(self.tailer.partial) if source is self.tailer else 0)
                if sequence == b"\x1b[A":
                    source.view_offset = min(max(0, available - 1), source.view_offset + 1)
                    redraw = True
                elif sequence == b"\x1b[B":
                    source.view_offset = max(0, source.view_offset - 1)
                    redraw = True
                continue
            key, self.input_buffer = self.input_buffer[:1], self.input_buffer[1:]
            if key in {b"q", b"Q", b"\x03"}:
                return False, redraw
            if key == b" ":
                self.paused = not self.paused
                redraw = True
            elif key == b"+":
                self.interval = max(0.2, self.interval - 0.2)
                redraw = True
            elif key == b"-":
                self.interval = min(10.0, self.interval + 0.2)
                redraw = True
            elif key in {b"m", b"M"}:
                if self.tailer.enabled:
                    self.message_mode = not self.message_mode
                    redraw = True
        return True, redraw

    def run(self) -> None:
        next_deadline = time.monotonic()
        with Terminal() as terminal:
            while True:
                if not self.paused:
                    self.refresh()
                self.render(terminal)
                next_deadline += self.interval
                now = time.monotonic()
                if next_deadline <= now:
                    next_deadline = now + self.interval
                while time.monotonic() < next_deadline:
                    keep_running, redraw = self.handle_keys(terminal.keys())
                    if not keep_running:
                        return
                    size = terminal.size()
                    resized = self.last_size != (size.lines, size.columns)
                    if redraw or resized:
                        # Input/resize redraws use only cached snapshots; SQLite,
                        # /proc sampling and log reads stay on the refresh
                        # cadence so interaction remains smooth.
                        self.render(terminal)
                    time.sleep(0.03)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root (default: cwd)")
    parser.add_argument("--project", help="display-name override (default: infer from Quartus project)")
    parser.add_argument("--interval", default=1.0, type=float, help="refresh interval in seconds (default: 1)")
    parser.add_argument("--log", default=None,
                        help="optional build-log path, or 'auto' for logs/*.latest.log")
    parser.add_argument("--lines", dest="log_lines", default=20, type=int,
                        help="maximum displayed message/log lines (default: 20)")
    parser.add_argument("--stitch-gap", type=int, default=60,
                        help="maximum seconds between adjacent Quartus invocations (default: 60)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval <= 0 or args.log_lines <= 0 or args.stitch_gap < 0:
        raise SystemExit("interval/log_lines must be positive and stitch-gap must be non-negative")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("qwatch requires an interactive terminal")
    try:
        QWatch(args.root, args.project, args.interval, args.log, args.log_lines, args.stitch_gap).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
