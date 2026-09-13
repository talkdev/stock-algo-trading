#!/usr/bin/env python3
"""
patch_v1.py -- self-contained repair for the JF-OU / NSE 2026 repository
==============================================================================

What it fixes
-------------
A.  Layout / path bugs (the reason the engine does not start at all)
    A1. The package `jfou/` does not exist: every module sits at the project root and
        uses relative imports (`from .config import CFG`), while `main.py` imports
        `jfou.*`.  Result: `ModuleNotFoundError: No module named 'jfou'` before a
        single line of strategy code runs, and `import config` from the root fails on
        its relative imports.
    A2. `tests/` does not exist: `run_tests.py`, `main.py test` and the test files
        themselves all address `<root>/tests/test_*.py`, so every suite reports
        MISSING and the suites' own `sys.path` bootstrap points one level too high.
    A3. `config.BASE_DIR` was `Path(__file__).parent.parent`, which in the broken
        layout resolved OUTSIDE the project (and, on a fresh run, made sqlite open a
        database in the parent directory of the checkout).
    A4. Data/log/universe paths were resolved from the current working directory or
        from a fixed depth of `__file__`, so the engine only worked when launched from
        one specific directory with one specific layout.

B.  Windows / path-independence
    B1. `zoneinfo.ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` on Windows
        (no IANA tz database ships with the OS), killing `clock.py` at import time.
    B2. `%JFOU_HOME%`/`JFOU_DB` handling: an unset variable on Windows expands to the
        empty string, and `Path("") / "data"` is a drive-relative path -- the DB
        silently landed on `C:\\data` instead of inside the project.  Quoted values and
        `~` were not handled either.
    B3. `Path.read_text()` used the locale encoding (cp1252/cp437 on Windows) for the
        universe JSON, and captured subprocess output used the same default.
    B4. The console writes box-drawing characters; on a Windows console that raises
        `UnicodeEncodeError` inside `print()`.  ANSI colour codes also need an explicit
        virtual-terminal opt-in on Windows, and must be off when the target is a
        redirect, a GUI launch (pythonw) or a terminal with no columns.
    B5. `sqlite3` PRAGMAs were unguarded: WAL mode is refused on SMB/OneDrive/Google
        Drive folders, which are extremely common Windows homes, and the refusal took
        down every command.
    B6. Git `core.autocrlf` on Windows can rewrite `data/jfou.sqlite3`; no
        `.gitattributes` existed to mark it binary.

C.  Real execution errors found while tracing the engine
    C1. `console.warn()`/`console.note()` declare `(text, indent=2)`, but `engine.py`
        calls them as `con.warn(label, detail)`.  On the gap-cancel and GFD-expiry
        paths that is `" " * <str>` -> `TypeError`, i.e. the reporting helper crashed
        the position-management pass.
    C2. `main.cmd_load_universe` raised a bare `FileNotFoundError` when the universe
        file was missing, instead of reporting the path it looked at.
    C3. `main.py` inserted its own directory into `sys.path` unconditionally (path
        duplicates on repeated imports) and could not find `jfou` if the layout had
        not been repaired -- now it fails with instructions instead of a traceback.
    C4. A bare `python main.py status` on an interpreter without the scientific stack
        raised ModuleNotFoundError from inside pandas. It now prints the venv + pip
        command for the host OS. requirements.txt also gained `tzdata`, which is what
        B1 needs on Windows (and is inert on Linux/macOS).

How it works
------------
Every fix is an anchored find/replace against the file's real contents, so:

  * it is idempotent -- a second run reports "already applied" and changes nothing;
  * it never rewrites a file it does not understand -- a missing anchor is reported as
    a failure rather than guessed at;
  * it works on the broken flat layout and on the repaired layout, from any working
    directory, and on any OS (POSIX or Windows separators, `~`, %VAR%, quoted values);
  * only the standard library is used, so it runs before `pip install -r
    requirements.txt` has happened.

Usage
-----
    python patch_v1.py                     # patch the repository holding this file
    python patch_v1.py C:\\work\\stock-algo-trading
    python patch_v1.py --check             # report only, change nothing
    python patch_v1.py --no-verify
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "1"
ENC = "utf-8-sig"          # tolerates the BOM a Windows editor may have left behind

# Modules that belong to the `jfou` package (they all use relative imports).
PACKAGE_MODULES = [
    "backtest.py", "clock.py", "config.py", "console.py", "dataclient.py", "db.py",
    "engine.py", "execution.py", "gates.py", "indicators.py", "sizing.py",
]
# Scripts that belong in `tests/` (their own path logic assumes <root>/tests/x.py).
TEST_SCRIPTS = ["test_indicators.py", "test_lifecycle.py", "test_pipeline.py"]

APPLIED: list[str] = []
ALREADY: list[str] = []
SKIPPED: list[str] = []
FAILED: list[str] = []


# -------------------------------------------------------------------- io helpers
def read_text(path: Path) -> str:
    # newline="" keeps the file's own line endings intact while we search for an
    # anchor: every anchor below is written with \n, so CRLF is normalised on read.
    with open(path, "r", encoding=ENC, newline="") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF for source files on every platform: Python reads them identically, and it
    # removes CRLF churn from a Windows working tree for good.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.replace("\r\n", "\n").replace("\r", "\n"))


def norm(path: str) -> Path:
    """Expand %VAR%, ~ and `~`-quoted forms, then normalise separators for this OS."""
    raw = os.path.expandvars(str(path)).strip().strip('"').strip("'")
    return Path(os.path.normpath(os.path.expanduser(raw)))


def find_root(start: Path) -> Path | None:
    """Walk up from `start` looking for the project root."""
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / "main.py").is_file() and (
                (cand / "jfou").is_dir() or (cand / "config.py").is_file()):
            return cand
    return None


def locate(root: Path, name: str) -> Path | None:
    """Where a file lives in either layout (package first, then tests, then root)."""
    for cand in (root / "jfou" / name, root / "tests" / name, root / name):
        if cand.is_file():
            return cand
    return None


# ------------------------------------------------------------------- edit engine
class Edit:
    """One anchored replacement in one file."""

    def __init__(self, tag: str, filename: str, old: str, new: str,
                 marker: str | None = None, must: bool = True):
        self.tag, self.filename, self.old, self.new = tag, filename, old, new
        self.marker = marker or new
        self.must = must

    def apply(self, root: Path, check_only: bool) -> None:
        path = locate(root, self.filename)
        if path is None:
            (FAILED if self.must else SKIPPED).append(f"{self.tag}: {self.filename} not found")
            return
        text = read_text(path)
        if self.marker in text:
            ALREADY.append(f"{self.tag}: {path.relative_to(root)}")
            return
        if self.old not in text:
            (FAILED if self.must else SKIPPED).append(
                f"{self.tag}: anchor not found in {path.relative_to(root)}")
            return
        if not check_only:
            write_text(path, text.replace(self.old, self.new, 1))
        APPLIED.append(f"{self.tag}: {path.relative_to(root)}")


class Append:
    """Add a block to the end of a file (idempotent via a marker)."""

    def __init__(self, tag: str, filename: str, block: str, marker: str,
                 after: str | None = None):
        self.tag, self.filename, self.block = tag, filename, block
        self.marker, self.after = marker, after

    def apply(self, root: Path, check_only: bool) -> None:
        path = locate(root, self.filename)
        if path is None:
            FAILED.append(f"{self.tag}: {self.filename} not found")
            return
        text = read_text(path)
        if self.marker in text:
            ALREADY.append(f"{self.tag}: {path.relative_to(root)}")
            return
        if self.after and self.after in text:
            new = text.replace(self.after, self.after + "\n" + self.block, 1)
        else:
            new = text.rstrip("\n") + "\n\n" + self.block.rstrip("\n") + "\n"
        if not check_only:
            write_text(path, new)
        APPLIED.append(f"{self.tag}: {path.relative_to(root)}")


class Write:
    """Create or replace a whole file (never clobbers a hand-written one)."""

    def __init__(self, tag: str, relpath: str, content: str, marker: str,
                 keep_if_different: bool = False):
        self.tag, self.relpath, self.content = tag, relpath, content
        self.marker, self.keep_if_different = marker, keep_if_different

    def apply(self, root: Path, check_only: bool) -> None:
        path = root / self.relpath
        if path.is_file():
            text = read_text(path)
            if self.marker in text:
                ALREADY.append(f"{self.tag}: {self.relpath}")
                return
            if self.keep_if_different:
                SKIPPED.append(f"{self.tag}: {self.relpath} exists and differs -- left alone")
                return
        if not check_only:
            write_text(path, self.content)
        APPLIED.append(f"{self.tag}: {self.relpath}")


# --------------------------------------------------------------------- restructure
def restructure(root: Path, check_only: bool) -> None:
    """Move the package modules into jfou/ and the suites into tests/.

    Safe to run twice: a file is only moved when its destination is free, and a
    leftover root-level copy that is byte-identical to the moved one is removed (a
    stale duplicate at the root would otherwise be importable as a top-level module
    and shadow nothing but the reader's confidence).
    """
    for name in PACKAGE_MODULES + TEST_SCRIPTS:
        src = root / name
        dst = (root / "jfou" / name) if name in PACKAGE_MODULES else (root / "tests" / name)
        if dst.exists() and not src.exists():
            ALREADY.append(f"layout: {dst.relative_to(root)}")
            continue
        if src.exists() and dst.exists():
            same = src.read_bytes() == dst.read_bytes()
            if same and not check_only:
                src.unlink()
                APPLIED.append(f"layout: removed stale duplicate {name}")
            elif same:
                APPLIED.append(f"layout: {name} duplicated (identical) at root")
            else:
                FAILED.append(f"layout: {name} and {dst.relative_to(root)} differ -- "
                              f"refusing to overwrite")
            continue
        if not src.exists():
            if dst.exists():
                ALREADY.append(f"layout: {dst.relative_to(root)}")
            elif name in PACKAGE_MODULES:
                FAILED.append(f"layout: {name} is missing from the repository")
            else:
                SKIPPED.append(f"layout: {name} not found")
            continue
        if not check_only:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        APPLIED.append(f"layout: {name} -> {dst.relative_to(root)}")

    # Stale bytecode from the flat layout must go: a .pyc whose source no longer sits
    # beside it is dead weight that debuggers and editors still happily list, and on
    # Windows a locked/one-drive-synced copy can hold the rename open. Only those dead
    # entries are removed -- a cache belonging to a module that is still there is left
    # untouched.
    for cache in sorted(root.glob("__pycache__")) + sorted(
            d / "__pycache__" for d in (root / "jfou", root / "tests") if d.is_dir()):
        if not cache.is_dir():
            continue
        dead = [pyc for pyc in sorted(cache.glob("*.pyc"))
                if not (cache.parent / (pyc.name.split(".")[0] + ".py")).exists()]
        if not dead:
            continue
        if not check_only:
            for pyc in dead:
                try:
                    pyc.unlink()
                except OSError:
                    pass
            if not any(cache.iterdir()):
                cache.rmdir()
        APPLIED.append(f"layout: dropped {len(dead)} stale .pyc from {cache.relative_to(root)}")


INIT_PY = '''"""
JF-OU / NSE 2026 -- Jump-Filtered Ornstein-Uhlenbeck mean-reversion engine.

Package layout (the layout every path in this repository assumes):

    <root>/main.py            CLI entry point -- imports `jfou.*`
    <root>/run_tests.py       suite runner -- imports nothing, spawns tests/
    <root>/universe_nifty100.json
    <root>/data/jfou.sqlite3
    <root>/tests/test_*.py
    <root>/jfou/*.py          this package; modules use relative imports

Submodules are imported lazily on purpose: `import jfou` must not require numpy,
pandas or scipy, so a bare install can still read the config and the clock.
"""
from __future__ import annotations

__version__ = "1.0.1"

__all__ = [
    "backtest", "clock", "config", "console", "dataclient", "db", "engine",
    "execution", "gates", "indicators", "sizing",
]
'''


# ------------------------------------------------------------------------- edits
def build_edits() -> list:
    e: list = []

    # ----------------------------------------------------- A3/B1/B2 config.py
    e.append(Edit(
        "cfg.paths", "config.py",
        '''import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

LAKH = 100_000
CRORE = 10_000_000

BASE_DIR = Path(os.environ.get("JFOU_HOME", Path(__file__).resolve().parent.parent))
DB_PATH = Path(os.environ.get("JFOU_DB", BASE_DIR / "data" / "jfou.sqlite3"))
LOG_DIR = BASE_DIR / "logs"''',
        '''import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

LAKH = 100_000
CRORE = 10_000_000

# --------------------------------------------------------------------- locations
# Nothing here is derived from the working directory and nothing assumes a fixed
# nesting depth. PKG_DIR is this file; ROOT_DIR is its parent. That holds on
# /home/me/stock-algo-trading, on C:\\Users\\me\\stock-algo-trading, on a mapped drive,
# and inside a zip-imported copy, and both slash directions resolve to the same place
# because pathlib joins with the host separator.
PKG_DIR = Path(__file__).resolve().parent          # <root>/jfou
ROOT_DIR = PKG_DIR.parent                            # <root>


def _path_from(raw, default: Path) -> Path:
    """Resolve a user-supplied path, falling back to `default`.

    Set-but-empty must behave like unset. On Windows an undefined %JFOU_HOME%
    expands to "", and Path("") / "data" is drive-relative, which is how the state
    database ends up next to the drive root instead of inside the project. Quoted
    values (a path containing spaces) and ~ are accepted, and the result is
    normalised for the host OS, so both slash directions work.
    """
    if raw is None or not str(raw).strip():
        return Path(default)
    p = Path(os.path.normpath(os.path.expandvars(str(raw)).strip().strip('"')))
    p = p.expanduser()
    if not p.is_absolute():
        p = Path(os.path.normpath(str(Path.cwd() / p)))
    return p


BASE_DIR = _path_from(os.environ.get("JFOU_HOME"), ROOT_DIR)
DB_PATH = _path_from(os.environ.get("JFOU_DB"), BASE_DIR / "data" / "jfou.sqlite3")
LOG_DIR = _path_from(os.environ.get("JFOU_LOG_DIR"), BASE_DIR / "logs")''',
        marker="def _path_from("))

    e.append(Append(
        "cfg.locators", "config.py",
        '''
# --------------------------------------------------------------------- locators
def resolve_universe_file(name_or_path: str | None = None,
                          roots: tuple = ()) -> Path:
    """Locate the universe JSON without assuming a working directory.

    An absolute path is honoured exactly as given, so a Windows drive path, a POSIX
    `/srv/jfou/universe.json` and a `~`-relative path all work. A bare file name is
    searched in the project root, then under data/, then beside the package, then in
    the current directory -- the first hit wins. When nothing matches, the primary
    candidate is returned so the caller can report the exact path it wanted.
    """
    raw = str(name_or_path or CFG.universe_file)
    # Not _path_from(): that helper makes a relative path absolute against the cwd,
    # which would defeat the whole point of searching the roots below.
    p = Path(os.path.normpath(os.path.expandvars(raw).strip().strip('"'))).expanduser()
    if p.is_absolute():
        return p
    bases: list = []
    for b in tuple(roots) + (ROOT_DIR, BASE_DIR, PKG_DIR, Path.cwd()):
        b = Path(b)
        if b not in bases:
            bases.append(b)
    for b in bases:
        for cand in (b / p, b / "data" / p, b / "jfou" / p, b / "tests" / p):
            if cand.is_file():
                return cand
    return bases[0] / p


def ensure_dirs() -> None:
    """Create data/ and logs/ for the resolved config.

    mkdir(parents=True, exist_ok=True) is the race-safe form: on Windows another
    process (an editor, a second scan, a scheduled task) may create the directory
    between the existence check and the call.
    """
    for d in (DB_PATH.parent, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:            # read-only media: the DB may still open fine
            pass
''',
        marker="def resolve_universe_file(", after="CFG = Config()"))

    # ------------------------------------------------------------- B1 clock.py
    e.append(Edit(
        "clock.tz", "clock.py",
        '''from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")''',
        '''from datetime import datetime, time, timedelta, date, timezone


def _ist_zone():
    """Asia/Kolkata, with a fallback that cannot fail at import time.

    Windows ships no IANA tz database, so a plain `ZoneInfo("Asia/Kolkata")` raises
    ZoneInfoNotFoundError there and the whole program dies while importing this
    module -- before a single line of strategy code runs. zoneinfo does consult the
    `tzdata` wheel when it is installed (requirements.txt now asks for it), and if
    neither source exists we fall back to the fixed +05:30 offset. India has had no
    DST since 1945, so the fallback is exact rather than approximate and every
    session-phase rule keeps its meaning.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:                       # Python < 3.9
        return timezone(timedelta(hours=5, minutes=30), "IST")
    try:
        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone(timedelta(hours=5, minutes=30), "IST")


IST = _ist_zone()''',
        marker="def _ist_zone("))

    # --------------------------------------------------------------- B3/D1 db.py
    e.append(Edit(
        "db.imports", "db.py",
        '''import json
import sqlite3
import threading''',
        '''import json
import os
import sqlite3
import threading''',
        marker="import os\nimport sqlite3"))

    e.append(Edit(
        "db.path+pragmas", "db.py",
        '''    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)''',
        '''    def __init__(self, path: str | Path):
        # str or Path, POSIX or Windows separators, ~, %VAR%, quoted values. sqlite3
        # is then handed a plain absolute str, because "unable to open database file"
        # -- the most common first-run failure on a fresh checkout -- is almost always
        # a relative path, a missing parent directory or a drive-relative one.
        self.path = resolve_db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)''',
        marker="self.path = resolve_db_path(path)"))

    e.append(Edit(
        "db.pragma-guard", "db.py",
        '''            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")''',
        '''            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            self.journal_mode = "delete"
            for pragma, name in (("PRAGMA journal_mode=WAL", "wal"),
                                 ("PRAGMA synchronous=NORMAL", None),
                                 ("PRAGMA foreign_keys=ON", None),
                                 ("PRAGMA busy_timeout=30000", None)):
                try:
                    c.execute(pragma)
                except sqlite3.OperationalError:
                    # WAL needs the directory to support the -shm/-wal lock files.
                    # SMB, OneDrive and Google Drive folders -- the usual Windows
                    # home for a checkout -- refuse it, and a refusal used to abort
                    # every command. Degrade to the rollback journal instead: slower,
                    # still crash-safe, still single-writer consistent.
                    if name == "wal":
                        try:
                            c.execute("PRAGMA journal_mode=DELETE")
                        except sqlite3.OperationalError:
                            pass
                    continue''',
        marker="for pragma, name in ((\"PRAGMA journal_mode=WAL\", \"wal\"),"))

    e.append(Edit(
        "db.now", "db.py",
        '''def _now() -> str:
    from .clock import now_ist
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")''',
        '''def resolve_db_path(path: "str | Path") -> Path:
    """Absolute, OS-normalised, `~`/`%VAR%`-expanded view of a database path."""
    raw = os.path.expandvars(str(path)).strip().strip('"')
    p = Path(os.path.normpath(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = Path(os.path.normpath(str(Path.cwd() / p)))
    return p


def _now() -> str:
    from .clock import now_ist
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")''',
        marker="def resolve_db_path("))

    # --------------------------------------------------------- B4/C1 console.py
    e.append(Edit(
        "con.width+ansi", "console.py",
        '''import shutil
import sys
from datetime import datetime

W = max(78, min(shutil.get_terminal_size((100, 24)).columns - 2, 118))

_TTY = sys.stdout.isatty()''',
        '''import os
import shutil
import sys
from datetime import datetime


def _width() -> int:
    """A report width that survives a redirect, a service and CI.

    A detached or absent console reports 0 columns; a negative width then makes every
    ljust()/wrap() raise inside print(), far from the code that caused it.
    """
    cols = 0
    try:
        env = os.environ.get("COLUMNS")
        cols = int(env) if env else int(shutil.get_terminal_size((100, 24)).columns)
    except Exception:
        cols = 0
    if cols < 40:
        cols = 100
    return max(78, min(cols - 2, 118))


W = _width()


def _ansi_ok() -> bool:
    """True only when writing escape sequences to this stream is both possible and wanted."""
    stream = getattr(sys, "stdout", None)
    if stream is None or not hasattr(stream, "isatty"):
        return False                    # pythonw.exe / GUI launch: no stdout at all
    try:
        if not stream.isatty():
            return False                # redirected to a file or a pipe
    except Exception:
        return False
    if os.environ.get("NO_COLOR") or os.environ.get("JFOU_COLOR") == "0":
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if os.name != "nt":
        return True
    # A legacy Windows console renders \\x1b[32m as literal garbage. Ask conhost for
    # virtual-terminal processing and stay monochrome if it refuses.
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


_TTY = _ansi_ok()''',
        marker="def _ansi_ok("))

    e.append(Edit(
        "con.warn-detail", "console.py",
        '''def note(text: str, indent: int = 2) -> None:
    for chunk in _wrap(text, W - indent - 2):
        print(" " * indent + grey(chunk))


def warn(text: str, indent: int = 2) -> None:
    for i, chunk in enumerate(_wrap(text, W - indent - 2)):
        print(" " * indent + (amber("! " + chunk) if i == 0 else amber(chunk)))''',
        '''def _fmt(text: str, indent) -> tuple[str, int]:
    """Accept warn("label", "detail") as well as warn(text, indent=4).

    engine.py calls these helpers as `con.warn(f"{sym} CANCEL_GAP", why)`, passing a
    detail string in the indent slot. `" " * <str>` raised TypeError, so a
    gap-cancel or a GFD expiry -- the two events an operator most needs reported --
    killed the whole position-management pass. A str second argument is now appended
    as detail; anything non-numeric falls back to the default indent.
    """
    if isinstance(indent, str):
        detail = indent.strip()
        if detail:
            text = f"{text}  |  {detail}" if text else detail
        indent = 2
    try:
        indent = max(0, int(indent))
    except (TypeError, ValueError):
        indent = 2
    return text, indent


def note(text: str, indent: int = 2) -> None:
    text, indent = _fmt(text, indent)
    for chunk in _wrap(text, W - indent - 2):
        print(" " * indent + grey(chunk))


def warn(text: str, indent: int = 2) -> None:
    text, indent = _fmt(text, indent)
    for i, chunk in enumerate(_wrap(text, W - indent - 2)):
        print(" " * indent + (amber("! " + chunk) if i == 0 else amber(chunk)))''',
        marker="def _fmt(text: str, indent)"))

    # ----------------------------------------------------------------- main.py
    e.append(Edit(
        "main.utf8+imports", "main.py",
        '''BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from jfou import console as con                                   # noqa: E402
from jfou.clock import now_ist, resolve_phase, session_date_for    # noqa: E402
from jfou.config import CFG, DB_PATH, LAKH, CRORE                  # noqa: E402
from jfou.dataclient import MockClient, UpstoxClient, UpstoxError  # noqa: E402
from jfou.db import Database                                       # noqa: E402
from jfou.engine import Engine                                     # noqa: E402''',
        '''BASE = Path(os.path.normpath(str(Path(__file__).resolve().parent)))
if str(BASE) not in sys.path:                 # no duplicate entries on re-import
    sys.path.insert(0, str(BASE))


def _force_utf8_stdio() -> None:
    """The reports print box-drawing characters; make sure the stream can take them.

    On Windows the default stdio encoding is the active ANSI codepage (cp1252, cp437,
    ...), where "─" raises UnicodeEncodeError from inside print(). Redirecting to a
    file has the same default. Reconfigure to UTF-8 with replacement, so the worst
    case is a question mark, never a traceback from a cosmetic character.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:                  # pythonw.exe has no console at all
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except (AttributeError, ValueError, OSError):
            pass
        try:
            import io
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name, io.TextIOWrapper(
                    buf, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


_force_utf8_stdio()

try:
    from jfou import console as con                                   # noqa: E402
    from jfou.clock import now_ist, resolve_phase, session_date_for   # noqa: E402
    from jfou.config import (CFG, DB_PATH, LAKH, CRORE,               # noqa: E402
                             ensure_dirs, resolve_universe_file)
    from jfou.dataclient import MockClient, UpstoxClient, UpstoxError  # noqa: E402
    from jfou.db import Database                                      # noqa: E402
    from jfou.engine import Engine                                    # noqa: E402
except ModuleNotFoundError as exc:                                    # noqa: E402
    if (exc.name or "").split(".")[0] in {"numpy", "pandas", "scipy", "statsmodels",
                                          "arch", "requests"}:
        # The usual first run on a fresh Windows box: the interpreter is the system
        # one and no venv was activated. Say so instead of showing a traceback from
        # deep inside pandas.
        for _line in (
                f"JF-OU needs the scientific stack (missing: {exc.name}).",
                "  python -m venv .venv",
                r"  Windows:  .venv\\Scripts\\python -m pip install -r requirements.txt",
                r"  Linux:    .venv/bin/python    -m pip install -r requirements.txt"):
            print(_line, file=sys.stderr)
        raise SystemExit(2)
    raise ModuleNotFoundError(
        f"{exc}. The `jfou` package must sit next to main.py -- looked for at "
        f"{BASE / 'jfou'}. Run `python patch_v1.py` in the repository root to "
        f"repair the layout."
    ) from None''',
        marker="def _force_utf8_stdio("))

    e.append(Edit(
        "main.universe-path", "main.py",
        '''def _file_symbols() -> list[str]:
    p = BASE / CFG.universe_file
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return [m["symbol"] for m in data.get("members", [])]''',
        '''def _universe_path() -> Path:
    """Absolute path of the universe file, resolved independently of the cwd."""
    return resolve_universe_file(CFG.universe_file, roots=(BASE,))


def _read_json(path: Path) -> dict:
    # Explicit encoding: the platform default on Windows is cp1252/cp437, where a
    # UTF-8 BOM or a rupee sign in the JSON is a UnicodeDecodeError. utf-8-sig
    # swallows the BOM if an editor left one behind.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_symbols() -> list[str]:
    p = _universe_path()
    if not p.is_file():
        return []
    try:
        data = _read_json(p)
    except (OSError, ValueError) as exc:
        con.warn(f"could not read the universe file {p}: {exc}")
        return []
    return [m["symbol"] for m in data.get("members", [])]''',
        marker="def _universe_path() -> Path:"))

    e.append(Edit(
        "main.load-universe", "main.py",
        '''    p = BASE / CFG.universe_file
    data = json.loads(p.read_text())
    members = data.get("members", [])''',
        '''    p = _universe_path()
    if not p.is_file():
        con.warn(f"universe file not found: {p}. Place universe_nifty100.json in the "
                 f"repository root, or point CFG.universe_file at an absolute path.")
        return 2
    data = _read_json(p)
    members = data.get("members", [])''',
        marker="universe file not found:"))

    e.append(Edit(
        "main.test-command", "main.py",
        '''def cmd_test(db: Database, args) -> int:
    import subprocess
    return subprocess.call([sys.executable, str(BASE / "tests" / "test_indicators.py")])''',
        '''def _suite(name: str) -> Path | None:
    """tests/ is the documented home; the project root is accepted too."""
    for cand in (BASE / "tests" / name, BASE / name, BASE / "jfou" / "tests" / name):
        if cand.is_file():
            return cand
    return None


def cmd_test(db: Database, args) -> int:
    import subprocess
    path = _suite("test_indicators.py")
    if path is None:
        con.warn(f"test_indicators.py not found under {BASE / 'tests'} or {BASE}")
        return 1
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(BASE)}
    return subprocess.call([sys.executable, str(path)], cwd=str(BASE), env=env)''',
        marker="def _suite(name: str)"))

    e.append(Edit(
        "main.db-init", "main.py",
        '''    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = Database(DB_PATH)''',
        '''    ensure_dirs()                      # data/ and logs/, from the resolved config
    db = Database(DB_PATH)''',
        marker="    ensure_dirs()"))

    # -------------------------------------------------------------- run_tests.py
    e.append(Edit(
        "rt.imports", "run_tests.py",
        '''import subprocess
import sys
import time
from pathlib import Path''',
        '''import os
import subprocess
import sys
import time
from pathlib import Path''',
        marker="import os\nimport subprocess"))

    e.append(Edit(
        "rt.locate", "run_tests.py",
        '''BASE = Path(__file__).resolve().parent
SUITES = [
    ("indicators", BASE / "tests" / "test_indicators.py",
     "numerical calibration: Hurst, Kalman, OU half-life, ADF, BNS, Kelly"),
    ("lifecycle", BASE / "tests" / "test_lifecycle.py",
     "arm -> fill -> exit ladder -> report -> restart, through the real code path"),
    ("pipeline", BASE / "tests" / "test_pipeline.py",
     "end-to-end scan against the offline mock: gates, capture log, restart safety"),
]''',
        '''BASE = Path(os.path.normpath(str(Path(__file__).resolve().parent)))

# A suite is addressed by file name, never by a hard-coded depth: tests/ is the
# documented home, the project root is accepted as well, and the paths come from
# __file__ rather than the working directory, so `python run_tests.py` works from
# wherever the shell happens to be.
SUITES = [
    ("indicators", "test_indicators.py",
     "numerical calibration: Hurst, Kalman, OU half-life, ADF, BNS, Kelly"),
    ("lifecycle", "test_lifecycle.py",
     "arm -> fill -> exit ladder -> report -> restart, through the real code path"),
    ("pipeline", "test_pipeline.py",
     "end-to-end scan against the offline mock: gates, capture log, restart safety"),
]


def locate(filename: str) -> Path | None:
    for cand in (BASE / "tests" / filename, BASE / filename,
                 BASE / "jfou" / "tests" / filename):
        if cand.is_file():
            return cand
    return None


def child_env() -> dict:
    """UTF-8 for the children.

    The suites print box-drawing characters, and on Windows both the child's stdout
    and the parent's decoding of it default to the ANSI codepage. Setting PYTHONUTF8
    makes the two agree; PYTHONPATH lets a suite import `jfou` without a .pth file.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONPATH", str(BASE))
    return env''',
        marker="def locate(filename: str)"))

    e.append(Edit(
        "rt.run", "run_tests.py",
        '''    for name, path, what in SUITES:
        if not path.exists():
            print(f"\\n### {name}: MISSING ({path})")
            results.append((name, 1, 0.0))
            continue
        print(f"\\n### {name} -- {what}")
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(path)])''',
        '''    for name, filename, what in SUITES:
        path = locate(filename)
        if path is None:
            print(f"\\n### {name}: MISSING ({BASE / 'tests' / filename})")
            results.append((name, 1, 0.0))
            continue
        print(f"\\n### {name} -- {what}")
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(path)], cwd=str(BASE),
                             env=child_env())''',
        marker="rc = subprocess.call([sys.executable, str(path)], cwd=str(BASE),"))

    # ------------------------------------------------------------- tests/*.py
    bootstrap = '''def _bootstrap_path() -> str:
    """Directory to import `jfou` from, found by walking up from __file__.

    A hard-coded parent-of-parent only happens to be right for one layout; this works
    from tests/, from the project root, from a symlinked checkout and from any working
    directory on either OS.
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "jfou").is_dir() or (cand / "main.py").is_file():
            return str(cand)
    return str(here.parent)


_ROOT = _bootstrap_path()
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)'''

    e.append(Edit(
        "test.indicators.path", "test_indicators.py",
        '''import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))''',
        '''import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

''' + bootstrap,
        marker="def _bootstrap_path()"))

    e.append(Edit(
        "test.lifecycle.path", "test_lifecycle.py",
        '''import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))''',
        '''import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

''' + bootstrap,
        marker="def _bootstrap_path()"))

    e.append(Edit(
        "test.pipeline.path", "test_pipeline.py",
        '''BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))''',
        '''def _project_root() -> Path:
    """First ancestor that holds main.py -- the root, whatever the layout depth is."""
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "main.py").is_file():
            return cand
    return here.parent.parent


BASE = _project_root()
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))''',
        marker="def _project_root()"))

    e.append(Edit(
        "test.pipeline.run", "test_pipeline.py",
        '''def run(home: str, *args: str, timeout: int = 1200) -> tuple[int, str]:
    env = {**os.environ, "JFOU_HOME": home,
           "JFOU_DB": str(Path(home) / "data" / "jfou.sqlite3")}
    p = subprocess.run([sys.executable, str(BASE / "main.py"), *args],
                       capture_output=True, text=True, env=env, cwd=str(BASE),
                       timeout=timeout)
    return p.returncode, p.stdout + p.stderr''',
        '''def run(home: str, *args: str, timeout: int = 1200) -> tuple[int, str]:
    # JFOU_HOME is resolved to an absolute, canonical path: on Windows
    # tempfile.mkdtemp() may hand back an 8.3 short path, and a short path plus a
    # relative DB path is how two runs end up writing two different databases.
    # text=True without an explicit encoding decodes the child with the ANSI
    # codepage on Windows -- the reports use U+2500 and would raise there.
    home_path = Path(home).expanduser().resolve()
    env = dict(os.environ)
    env.update({"JFOU_HOME": str(home_path),
                "JFOU_DB": str(home_path / "data" / "jfou.sqlite3"),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": str(BASE)})
    p = subprocess.run([sys.executable, str(BASE / "main.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=str(BASE), timeout=timeout)
    return p.returncode, p.stdout + p.stderr''',
        marker="home_path = Path(home).expanduser().resolve()"))

    # -------------------------------------------------------------- package init
    e.append(Write("pkg.init", "jfou/__init__.py", INIT_PY,
                   marker="__all__", keep_if_different=True))

    # -------------------------------------------------------------- gitattributes
    e.append(Write(
        "git.attributes", ".gitattributes",
        '''# Windows checkouts: git must not rewrite bytes of the state database.
# core.autocrlf=true turns data/jfou.sqlite3 into a corrupt file the moment it is
# checked out, and text=auto would keep re-diffing every .py file on each commit.
* text=auto

*.py        text eol=lf
*.json      text eol=lf
*.md        text eol=lf
*.txt       text eol=lf
*.sqlite3   binary
*.sqlite    binary
*.db        binary
*.gz        binary
*.png       binary
''',
        marker="*.sqlite3   binary", keep_if_different=True))

    # ------------------------------------------------------------- requirements
    e.append(Edit(
        "req.tzdata", "requirements.txt",
        '''arch>=6.0              # GARCH(1,1) conditional variance (confluence S1)''',
        '''arch>=6.0              # GARCH(1,1) conditional variance (confluence S1)

# Windows has no IANA time-zone database of its own. Without tzdata,
# zoneinfo.ZoneInfo("Asia/Kolkata") raises ZoneInfoNotFoundError at import time and
# the session clock -- and with it every command -- is unavailable. clock.py degrades
# to a fixed +05:30 offset if it is missing, but install it for the real thing.
tzdata>=2023.3         # IANA tz database for Windows (a no-op on Linux/macOS)''',
        marker="tzdata>=2023.3"))

    return e


EDITS = build_edits()


# ----------------------------------------------------------------------- verify
def verify(root: Path) -> tuple[int, list[str]]:
    """Syntax-check every module and smoke-import what does not need third-party deps."""
    problems: list[str] = []
    n = 0
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", "__pycache__", "build", "dist"} for part in path.parts):
            continue
        n += 1
        try:
            source = path.read_text(encoding=ENC)
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"unreadable: {path.relative_to(root)}: {exc}")
            continue
        try:
            # compile() only -- no .pyc written, so the check works on read-only media
            # and on a Windows box where NUL redirection is not an option.
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            problems.append(f"syntax: {path.relative_to(root)}: line {exc.lineno}: {exc.msg}")
    print(f"  syntax-checked {n} python file(s)")

    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import jfou, jfou.config as c, "
         "jfou.clock as k; print('BASE_DIR', c.BASE_DIR); print('DB_PATH', c.DB_PATH); "
         "print('IST', k.IST); print('universe', "
         "c.resolve_universe_file('universe_nifty100.json').is_file())" % str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(root))
    sys.stdout.write("".join("  " + ln + "\n" for ln in probe.stdout.splitlines()))
    if probe.returncode != 0:
        problems.append("import: " + (probe.stderr or "").strip().splitlines()[-1])
    return n, problems


def smoke(root: Path) -> None:
    """Run the engine end to end if the scientific stack is installed.

    Uses a throwaway JFOU_HOME so the repository's data/jfou.sqlite3 is never
    touched by a repair script.
    """
    has = subprocess.run([sys.executable, "-c", "import numpy, pandas, scipy"],
                         capture_output=True, text=True)
    if has.returncode != 0:
        print("  smoke test skipped: numpy/pandas/scipy are not installed yet "
              "(python -m pip install -r requirements.txt)")
        return
    import tempfile
    home = Path(tempfile.mkdtemp(prefix=f"jfou_patch{VERSION}_"))
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "JFOU_HOME": str(home),
           "JFOU_DB": str(home / "data" / "jfou.sqlite3")}
    try:
        for cmd in (["main.py", "status"], ["main.py", "load-universe"],
                    ["main.py", "test"]):
            p = subprocess.run([sys.executable, *cmd], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env,
                               cwd=str(root), timeout=1800)
            out = (p.stdout + p.stderr).strip().splitlines()
            print(f"  python {' '.join(cmd)} -> rc={p.returncode} | "
                  + " / ".join(out[-2:]))
    except subprocess.TimeoutExpired:
        print("  smoke test timed out (the suites are slow on a cold database)")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog=f"patch_v{VERSION}",
        description=f"Path-independence and execution-error repair, patch v{VERSION}")
    ap.add_argument("target", nargs="?", default=str(Path(__file__).resolve().parent),
                    help="repository root (default: the directory holding this script)")
    ap.add_argument("--check", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--no-verify", action="store_true", help="skip the verification step")
    args = ap.parse_args(argv)

    start = norm(args.target)
    root = find_root(start) or (start if (start / "main.py").is_file() else None)
    if root is None:
        print(f"patch_v{VERSION}: no JF-OU repository at {start} "
              f"(main.py not found). Pass the repository root as the first argument.",
              file=sys.stderr)
        return 2

    print("=" * 78)
    print(f"patch_v{VERSION} -- JF-OU / NSE 2026 path + execution repair"
          + ("   [dry run]" if args.check else ""))
    print(f"repository: {root}")
    print("=" * 78)

    restructure(root, args.check)
    for edit in EDITS:
        edit.apply(root, args.check)

    print(f"\nlayout + fixes applied : {len(APPLIED)}")
    for line in APPLIED:
        print(f"   + {line}")
    print(f"already correct        : {len(ALREADY)}")
    for line in ALREADY:
        print(f"   = {line}")
    if SKIPPED:
        print(f"skipped (optional)     : {len(SKIPPED)}")
        for line in SKIPPED:
            print(f"   - {line}")
    if FAILED:
        print(f"FAILED                 : {len(FAILED)}")
        for line in FAILED:
            print(f"   ! {line}")

    if not args.check:
        # Bytecode from the old flat layout is stale the moment files move.
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)

    problems: list[str] = []
    if args.check:
        print("\nverification skipped (--check)")
    elif args.no_verify:
        print("\nverification skipped (--no-verify)")
    else:
        print("\nverification")
        _, problems = verify(root)
        if not FAILED and not problems:
            smoke(root)
        for line in problems:
            print(f"   ! {line}")

    if FAILED or problems:
        print(f"\npatch_v{VERSION}: NOT complete -- fix the entries above and re-run.")
        return 1
    print(f"\npatch_v{VERSION}: {'checked' if args.check else 'applied'} with no errors."
          + ("" if args.check else "\n  next: python main.py load-universe && python run_tests.py"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
