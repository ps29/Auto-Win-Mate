"""Tests for computer.py: parsing Fara-7B and UI-TARS replies, and
running the resulting actions on the real desktop through a scaled Screen.

The live part drives WinControlMCP's throwaway test window (tests/testwin.ps1);
WINCONTROL_ALLOWED_EXES is locked to powershell.exe, so no other app receives input.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\computer_test.py
"""
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["WINCONTROL_ALLOWED_EXES"] = "powershell.exe"  # must be set before importing the server
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pilot  # noqa: E402,F401  (sets up sys.path)
import computer  # noqa: E402
import server  # noqa: E402

SERVER_TESTS = HERE.parent.parent / "WinControlMCP" / "tests"
STATE = SERVER_TESTS / "teststate.txt"
TITLE = "WinControlTest"


def test_parsers():
    assert computer.normalize_keys("Return") == "enter"
    assert computer.normalize_keys(["Control", "a"]) == "ctrl+a"
    assert computer.normalize_keys("ctrl c") == "ctrl+c"
    assert computer.normalize_keys("super+ArrowUp") == "win+up"

    thought, acts = computer.parse_fara(
        'I will search.\n<tool_call>\n{"name": "computer_use", "arguments": {"action": "type", '
        '"text": "cats", "coordinate": [100, 50]}}\n</tool_call>')
    assert thought == "I will search." and [a["kind"] for a in acts] == ["click", "type"], acts
    assert acts[1]["text"] == "cats\n", acts  # Fara submits typed text by default
    _, acts = computer.parse_fara('<tool_call>{"name": "computer_use", "arguments": {"action": "scroll", '
                                  '"pixels": -300}}</tool_call>')
    assert acts[0]["direction"] == "down" and acts[0]["amount"] == 3, acts
    _, acts = computer.parse_fara('<tool_call>{"name": "computer_use", "arguments": {"action": "terminate", '
                                  '"status": "success"}}</tool_call>')
    assert acts[0]["kind"] == "finish"

    thought, acts = computer.parse_uitars("Thought: Click Save.\nAction: click(point='<point>120 340</point>')")
    assert thought == "Click Save." and acts[0]["point"] == [120, 340], acts
    _, acts = computer.parse_uitars("Thought: x\nAction: click(start_box='<|box_start|>(10,20)<|box_end|>')")
    assert acts[0]["point"] == [10, 20], acts
    _, acts = computer.parse_uitars("Thought: x\nAction: hotkey(key='ctrl s')")
    assert computer.normalize_keys(acts[0]["keys"]) == "ctrl+s"
    _, acts = computer.parse_uitars("Thought: x\nAction: type(content='it\\'s done\\n')")
    assert acts[0]["text"] == "it's done\n", acts
    _, acts = computer.parse_uitars("Thought: x\nAction: drag(start_point='<point>1 2</point>', "
                                    "end_point='<point>30 40</point>')")
    assert acts[0]["start"] == [1, 2] and acts[0]["point"] == [30, 40], acts
    _, acts = computer.parse_uitars("Thought: done\nAction: finished(content='All set')")
    assert acts[0] == {"kind": "finish", "text": "All set"}

    h, w = computer.smart_resize(1080, 1920)
    assert (h, w) == (756, 1344) and h % 28 == 0 and w % 28 == 0 and h * w <= 1350 * 28 * 28, (h, w)
    assert computer.fara_fit(2560, 1600) == (1428, 896)
    print("parsers ok")


def test_live():
    screen = computer.Screen(lambda w, h: (w // 2, h // 2))  # the model sees a half-size desktop
    x, y = screen.to_screen(*screen.to_model(800, 600))
    assert abs(x - 800) <= 2 and abs(y - 600) <= 2, (x, y)

    asyncio.run(server.mcp.call_tool("focus_window", {"window": TITLE}))  # screen clicks hit whatever is on top
    r = asyncio.run(server.mcp.call_tool("get_ui_elements", {"window": TITLE}))
    elements = r.content[0].text

    def model_point(pattern):
        m = re.search(pattern + r".*? at \((\d+), (\d+)\)", elements)
        return list(screen.to_model(int(m[1]), int(m[2])))

    button, empty = model_point(r'Button "Press me"'), model_point(r"Edit id=emptyBox")
    thumb, slider = model_point(r'Thumb "Position"'), model_point(r"Slider id=slider")

    for act in ({"kind": "click", "point": button, "button": "left", "clicks": 1},
                {"kind": "click", "point": button, "button": "left", "clicks": 2},
                {"kind": "click", "point": empty, "button": "left", "clicks": 1},
                {"kind": "type", "text": "typed by vision"},
                {"kind": "key", "keys": "shift+home", "repeat": 1},  # select what was typed...
                {"kind": "type", "text": "vision"},                  # ...and replace it
                {"kind": "drag", "start": thumb, "point": [2 * slider[0] - thumb[0], thumb[1]]}):
        assert computer.describe(act, screen)
        assert computer.perform(act, screen) == ("OK", None), act
    text, image = computer.perform({"kind": "screenshot"}, screen)
    assert image[0] == "image/jpeg" and len(image[1]) > 1000
    text, image = computer.perform({"kind": "zoom", "region": [button[0] - 20, button[1] - 10,
                                                               button[0] + 20, button[1] + 10]}, screen)
    assert image[0] == "image/png"
    text, _ = computer.perform({"kind": "cursor"}, screen)
    assert text.startswith("("), text

    try:  # the allowlist still applies to vision actions
        computer.perform({"kind": "click", "point": [0, screen.size[1] - 1], "button": "left", "clicks": 1}, screen)
        raise AssertionError("clicked outside the allowlist")
    except PermissionError:
        pass

    time.sleep(0.8)
    state = dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))
    assert state["clicks"] == "3", state
    assert state["empty"] == "vision", state
    assert int(state["track"]) >= 7, state
    print("live actions ok")


def main():
    test_parsers()
    STATE.unlink(missing_ok=True)
    window = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(SERVER_TESTS / "testwin.ps1")])
    try:
        for _ in range(40):
            if STATE.exists():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Test window did not start.")
        time.sleep(0.5)
        test_live()
        print("PASS")
    finally:
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
