"""Housekeeping: keep logs and caches from growing without limit, and check the environment.

WinPilot calls clean() at startup and once a day; maintenance.cmd runs this file by hand:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe housekeeping.py [--keep-days N] [--deep]

What it does:
  * deletes activity logs (%APPDATA%\\WinPilot\\logs\\activity-YYYY-MM-DD.jsonl) older than N days
  * rotates WinControlMCP\\actions.log at 5 MB, keeping 3 old copies
  * removes leftovers: a stale test-window state file and the local-agent temp config
  * --deep: also clears __pycache__ folders and pip's download cache, and runs a health check
"""
import argparse
import datetime
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER_DIR = REPO / "WinControlMCP"
LOG_DIR = Path(os.environ.get("APPDATA", HERE)) / "WinPilot" / "logs"
ACTIONS_LOG = Path(os.environ.get("WINCONTROL_LOG", SERVER_DIR / "actions.log"))
ACTIONS_MAX_BYTES, ACTIONS_KEEP = 5 * 1024 * 1024, 3  # same limits as server._log
LOG_NAME = re.compile(r"activity-(\d{4}-\d{2}-\d{2})\.jsonl$")


def size_of(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def prune_activity_logs(folder=LOG_DIR, keep_days=30, today=None):
    """Delete daily activity logs older than keep_days. Returns (files removed, bytes freed)."""
    cutoff = (today or datetime.date.today()) - datetime.timedelta(days=keep_days)
    removed = freed = 0
    for f in Path(folder).glob("activity-*.jsonl"):
        m = LOG_NAME.search(f.name)
        if m and datetime.date.fromisoformat(m[1]) < cutoff:
            freed += f.stat().st_size
            f.unlink()
            removed += 1
    return removed, freed


def rotate(path=ACTIONS_LOG, max_bytes=ACTIONS_MAX_BYTES, keep=ACTIONS_KEEP):
    """If path is bigger than max_bytes, shift it to path.1 (path.1 -> path.2 ...), dropping the oldest."""
    path = Path(path)
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False
    oldest = path.with_name(f"{path.name}.{keep}")
    oldest.unlink(missing_ok=True)
    for i in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{i + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def clean(keep_days=30, deep=False):
    """Run the routine cleanup. Returns a list of human-readable lines."""
    lines = []
    removed, freed = prune_activity_logs(LOG_DIR, keep_days)
    lines.append(f"Activity logs: removed {removed} older than {keep_days} days ({human(freed)} freed); "
                 f"{human(size_of(LOG_DIR))} kept.")
    lines.append("Action log: rotated." if rotate() else
                 f"Action log: {human(size_of(ACTIONS_LOG))} (rotates at {human(ACTIONS_MAX_BYTES)}).")
    gone = []
    for p in (SERVER_DIR / "tests" / "teststate.txt", Path(tempfile.gettempdir()) / "wincontrol-mcp.json"):
        if p.exists() and time.time() - p.stat().st_mtime > 3600:  # an hour old: nothing is using it
            p.unlink()
            gone.append(p.name)
    if gone:
        lines.append(f"Removed leftovers: {', '.join(gone)}.")
    if deep:
        caches = [p for p in REPO.rglob("__pycache__") if ".venv" not in p.parts]
        freed = sum(size_of(p) for p in caches)
        for p in caches:
            shutil.rmtree(p, ignore_errors=True)
        lines.append(f"Python caches: removed {len(caches)} __pycache__ folders ({human(freed)}).")
        r = subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], capture_output=True, text=True)
        lines.append("pip download cache: " + ((r.stdout or r.stderr).strip().splitlines() or ["cleared"])[-1])
    return lines


def health():
    """Check the environment WinPilot and the server need. Returns (ok, lines)."""
    lines, ok = [], True
    for module, why in (("mcp", "MCP server"), ("pywinauto", "UI Automation"), ("mss", "screenshots"),
                        ("PIL", "images"), ("anthropic", "Claude API"), ("pystray", "tray icon"),
                        ("sv_ttk", "theme (optional)"), ("torch", "OmniParser (optional)")):
        found = importlib.util.find_spec(module) is not None
        optional = "optional" in why
        ok &= found or optional
        lines.append(f"  {'ok ' if found else ('-- ' if optional else 'MISSING')} {module:<10} {why}")
    r = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    ok &= r.returncode == 0
    lines.append("  pip check: " + (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr)
                 else "  pip check: ok")
    omni = SERVER_DIR / "omniparser"
    lines.append(f"  OmniParser weights: {human(size_of(omni)) if omni.exists() else 'not installed'}")
    lines.append(f"  Python environment: {human(size_of(SERVER_DIR / '.venv'))}")
    return ok, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-days", type=int, default=30, help="keep activity logs this many days (default 30)")
    ap.add_argument("--deep", action="store_true", help="also clear Python/pip caches and run a health check")
    args = ap.parse_args()
    for line in clean(args.keep_days, args.deep):
        print(line)
    if args.deep:
        ok, lines = health()
        print("Health check:", "OK" if ok else "PROBLEMS FOUND (re-run WinControlMCP\\setup.cmd)")
        print("\n".join(lines))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
