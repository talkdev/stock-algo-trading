"""
Human-readable console reporting.

Every decision the engine makes is printed with the numbers that produced it, so a
reader can audit the reasoning without opening the database.
"""
from __future__ import annotations

import os
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
    # A legacy Windows console renders \x1b[32m as literal garbage. Ask conhost for
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


_TTY = _ansi_ok()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def bold(s):  return _c("1", s)
def dim(s):   return _c("2", s)
def red(s):   return _c("31", s)
def green(s): return _c("32", s)
def amber(s): return _c("33", s)
def cyan(s):  return _c("36", s)
def grey(s):  return _c("90", s)


def rule(ch: str = "-") -> str:
    return ch * W


def hr(title: str = "", ch: str = "=") -> None:
    if title:
        pad = max(0, W - len(title) - 2)
        print(bold(f"{ch} {title} {ch * pad}"))
    else:
        print(dim(rule(ch)))


def section(title: str) -> None:
    print()
    print(bold(cyan(f"{'─' * 2} {title} " + "─" * max(0, W - len(title) - 5))))


def kv_line(label: str, value: str, indent: int = 2) -> None:
    print(" " * indent + f"{dim(label + ':'):<34}{value}")


def verdict(ok: bool | None, width: int = 6) -> str:
    if ok is None:
        return amber("SKIP".ljust(width))
    return green("PASS".ljust(width)) if ok else red("FAIL".ljust(width))


def badge(tag: str) -> str:
    tag = tag.upper()
    if tag in ("GREEN", "PASS", "SELECTED", "OK", "FILLED", "ARMED"):
        return green(tag)
    if tag in ("AMBER", "DEFERRED", "PENDING", "PENDING_ENTRY", "OPEN", "T1_HIT"):
        return amber(tag)
    if tag in ("RED", "CRISIS", "FAIL", "REJECTED", "CANCELLED", "CLOSED"):
        return red(tag)
    return tag


def gate_line(gate: str, ok: bool | None, headline: str, why: str = "") -> None:
    """One gate result with its justification."""
    print(f"  {verdict(ok)} {bold(gate):<16} {headline}")
    if why:
        for i, chunk in enumerate(_wrap(why, W - 30)):
            print(" " * 25 + grey(chunk) if i else " " * 25 + grey(chunk))


def _fmt(text: str, indent) -> tuple[str, int]:
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
        print(" " * indent + (amber("! " + chunk) if i == 0 else amber(chunk)))


def table(headers: list[str], rows: list[list[str]], aligns: str | None = None) -> None:
    """Simple fixed-width table."""
    if not rows:
        print("  " + grey("(no rows)"))
        return
    aligns = aligns or "".join("<" for _ in headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(_plain(str(cell))))
    line = "  " + "  ".join(h.ljust(widths[i]) if aligns[i] == "<" else h.rjust(widths[i])
                             for i, h in enumerate(headers))
    print(bold(line))
    print("  " + dim("-" * (len(_plain(line)) - 2)))
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            s = str(cell)
            pad = widths[i] - len(_plain(s))
            cells.append(s + " " * pad if aligns[i] == "<" else " " * pad + s)
        print("  " + "  ".join(cells))


def banner(lines: list[str]) -> None:
    print(rule("="))
    for ln in lines:
        print(bold(f"  {ln}"))
    print(rule("="))


def money(x: float | None, symbol: str = "Rs ") -> str:
    if x is None:
        return "n/a"
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e7:
        return f"{sign}{symbol}{a/1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}{symbol}{a/1e5:.2f} L"
    if a >= 1e3:
        return f"{sign}{symbol}{a/1e3:.2f} K"
    return f"{sign}{symbol}{a:,.0f}"


def pct(x: float | None, dp: int = 2) -> str:
    return "n/a" if x is None else f"{x*100:.{dp}f}%"


def num(x: float | None, dp: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{dp}f}"


def ts(dt: datetime | None = None) -> str:
    from .clock import now_ist
    return (dt or now_ist()).strftime("%Y-%m-%d %H:%M:%S")


def _plain(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, max(20, width)) or [""]


# ============================================================================
# High-level helpers used by engine.py. They build on the primitives above and
# accept a GateResult (anything with .gate/.passed/.headline/.justification) so a
# gate can be printed straight from the object the gates module returned.
# ============================================================================
def head(title: str) -> None:
    section(title)


def info(label: str, detail: str = "") -> None:
    print(f"  {cyan('-')} {bold(label):<26}{grey(detail)}")


def ok(label: str, detail: str = "") -> None:
    print(f"  {green('+')} {bold(label):<26}{detail}")


def wrap(text: str, indent: int = 2) -> None:
    note(text or "", indent)


def show_gate(res, prefix: str = "") -> None:
    """Print a GateResult: verdict badge, headline, then the wrapped justification."""
    tag = res.verdict or ("PASS" if res.passed else "FAIL")
    line = f"  {badge(tag):<10} {bold((prefix + res.gate).strip()):<28} {res.headline}"
    print(line)
    if res.justification:
        for chunk in _wrap(res.justification, W - 32):
            print(" " * 32 + grey(chunk))


def verdict_block(title: str, detail: str = "") -> None:
    print()
    print(rule("="))
    print(bold(f"  {title}"))
    for chunk in _wrap(detail, W - 4):
        print("  " + chunk)
    print(rule("="))


def make_table(data: list[list]) -> None:
    """table() that takes a single list whose first row is the header."""
    if not data:
        print("  " + grey("(no rows)"))
        return
    table([str(x) for x in data[0]], [[str(c) for c in r] for r in data[1:]])
