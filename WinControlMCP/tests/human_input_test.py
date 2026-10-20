"""Test for human-like input (clickbot.set_input_speed): curved, eased mouse paths at a human
pace, typing with natural timing, double-clicks that still register, and a glide that stops when
the mouse is moved by hand.

Opens its own throwaway window (testwin.ps1) and only sends input to it.
Run from the WinControlMCP folder:
    .venv\\Scripts\\python.exe tests\\human_input_test.py
"""
import asyncio
import math
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import server  # noqa: E402

cb = server.cb
STATE = HERE / "teststate.txt"
TITLE = "WinControlTest"


def read_state():
    return dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))


def test_path():
    start, end = (100, 100), (900, 500)
    pts = cb.human_path(start, end, random.Random(7))
    assert pts[-1] == end and 40 <= len(pts) <= 90, (len(pts), pts[-1])
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    off_line = max(abs((p[0] - start[0]) * dy - (p[1] - start[1]) * dx) / length for p in pts)
    assert off_line > 5, f"path is a straight line ({off_line:.1f} px off it)"
    progress = [((p[0] - start[0]) * dx + (p[1] - start[1]) * dy) / length ** 2 for p in pts]
    assert all(b >= a - 0.01 for a, b in zip(progress, progress[1:])), "path goes backwards"
    steps = [b - a for a, b in zip(progress, progress[1:])]
    assert steps[len(steps) // 2] > steps[0] and steps[len(steps) // 2] > steps[-1], "no ease-in/ease-out"
    assert cb.human_path((5, 5), (6, 5)) == [(6, 5)]
    print("path ok")


def test_live(elements):
    def point(pattern):
        m = re.search(pattern + r".*? at \((\d+), (\d+)\)", elements)
        return int(m[1]), int(m[2])

    button, empty = point(r'Button "Press me"'), point(r"Edit id=emptyBox")
    cb.set_input_speed("human")

    # A glide visits many points in between and takes a human amount of time.
    cb._set_cursor(button[0] - 400, button[1] - 150)
    seen, done = set(), threading.Event()

    def sample():
        while not done.is_set():
            seen.add(cb.cursor_pos())
            time.sleep(0.004)
    threading.Thread(target=sample, daemon=True).start()
    t = time.perf_counter()
    cb.move_to(button)
    took = time.perf_counter() - t
    done.set()
    assert 0.15 <= took <= 1.5, took
    assert len(seen) >= 10, len(seen)

    t = time.perf_counter()
    cb.click(button, clicks=2)  # a human double-click must still count as two clicks
    assert time.perf_counter() - t < 1.0
    cb.click(empty)
    t = time.perf_counter()
    cb.type_text("hello world")
    took = time.perf_counter() - t
    assert 0.6 <= took <= 5.0, f"typing 11 characters took {took:.2f} s"
    cb.press_combo("shift+home")  # select what was typed (WinForms text boxes ignore ctrl+a)
    cb.type_text("human")

    # Grabbing the mouse mid-glide stops the movement instead of fighting the user.
    cb._set_cursor(button[0] - 500, button[1])

    def grab():
        time.sleep(0.08)
        cb.user32.SetCursorPos(button[0] - 450, button[1] + 300)  # not through _set_cursor: "the user"
    threading.Thread(target=grab, daemon=True).start()
    try:
        cb.move_to(button)
        raise AssertionError("the glide ignored the user moving the mouse")
    except cb.MovedByUser:
        pass

    cb.set_input_speed("instant")
    t = time.perf_counter()
    cb.move_to(empty)
    assert time.perf_counter() - t < 0.1
    time.sleep(0.8)
    state = read_state()
    assert state["clicks"] == "2", state
    assert state["empty"] == "human", state
    print("live ok")


def main():
    test_path()
    STATE.unlink(missing_ok=True)
    window = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(HERE / "testwin.ps1")])
    try:
        for _ in range(40):
            if STATE.exists():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Test window did not start.")
        asyncio.run(server.mcp.call_tool("focus_window", {"window": TITLE}))
        r = asyncio.run(server.mcp.call_tool("get_ui_elements", {"window": TITLE}))
        test_live(r.content[0].text)
        print("PASS")
    finally:
        cb.set_input_speed("human")
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
