"""End-to-end smoke test for the WinControl tools.

Opens its own throwaway Windows Forms window (testwin.ps1) and only sends input to
that window. WINCONTROL_ALLOWED_EXES is locked to powershell.exe, so the tools
refuse to type into or click any other application.

Run from the WinControlMCP folder:
    .venv\\Scripts\\python.exe tests\\smoke_test.py
"""
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["WINCONTROL_ALLOWED_EXES"] = "powershell.exe"  # must be set before importing server
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import server  # noqa: E402

STATE = HERE / "teststate.txt"
TITLE = "WinControlTest"


async def call(name, **kwargs):
    result = await server.mcp.call_tool(name, kwargs)
    return "\n".join(c.text for c in result.content if c.type == "text"), result


def read_state():
    return dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))


async def run():
    elements, _ = await call("get_ui_elements", window=TITLE)
    filled = int(re.search(r"\[(\d+)\] Edit id=filledBox", elements).group(1))
    empty = int(re.search(r"\[(\d+)\] Edit id=emptyBox", elements).group(1))
    button = int(re.search(r'\[(\d+)\] Button "Press me"', elements).group(1))

    try:
        await call("set_text", element_id=filled, text="overwrite!")
        raise AssertionError("set_text overwrote existing text without replace_existing=True")
    except AssertionError:
        raise
    except Exception as e:
        assert "already contains" in str(e), e

    await call("set_text", element_id=empty, text="typed by test")
    await call("click_element", element_id=button)
    await call("click_element", element_id=button, double=True)
    # Cursor tools: hover, focus/caret readout, drag.
    pos = re.search(r'\[\d+\] Button "Press me".*? at \((\d+), (\d+)\)', elements)
    bx, by = int(pos[1]), int(pos[2])
    await call("move_mouse", x=bx, y=by)
    cursor, _ = await call("get_cursor")
    assert f"Mouse at ({bx}, {by})" in cursor and TITLE in cursor, cursor
    await call("click_element", element_id=empty)
    cursor, _ = await call("get_cursor")
    assert "id=emptyBox" in cursor and "Text cursor (caret) at" in cursor, cursor
    slider = re.search(r'\[\d+\] Slider id=slider at \((\d+), (\d+)\)', elements)
    thumb = re.search(r'\[\d+\] Thumb "Position" at \((\d+), (\d+)\)', elements)
    assert slider and thumb, elements
    # From the thumb (value 0) to the right end of the track.
    right = 2 * int(slider[1]) - int(thumb[1])
    await call("drag", from_x=int(thumb[1]), from_y=int(thumb[2]), to_x=right, to_y=int(thumb[2]))

    # Waiting, reading, clipboard and window placement.
    found, _ = await call("wait_for", window=TITLE, text="clicks: 3", timeout_ms=5000)
    assert "clicks: 3" in found, found
    try:
        await call("wait_for", window="NoSuchWindow-xyz", timeout_ms=600)
        raise AssertionError("wait_for did not time out")
    except AssertionError:
        raise
    except Exception as e:
        assert "Timed out" in str(e), e
    page, _ = await call("read_page_text", window=TITLE)
    assert "clicks: 3" in page and "Press me" in page, page
    saved, _ = await call("clipboard", action="get")
    await call("clipboard", action="set", text="winpilot clipboard test")
    got, _ = await call("clipboard", action="get")
    assert got.endswith("winpilot clipboard test"), got
    if saved.startswith("Clipboard text"):  # put the user's clipboard back
        await call("clipboard", action="set", text=saved.split(":\n", 1)[1])
    await call("arrange_window", window=TITLE, action="move", x=340, y=320)
    r = server.wt.RECT()
    server.cb.user32.GetWindowRect(server._resolve(TITLE), server.ctypes.byref(r))
    assert (r.left, r.top) == (340, 320), (r.left, r.top)

    import omniparser
    if omniparser.available():  # optional vision detector (setup_omniparser.cmd)
        found, result = await call("detect_elements", window=TITLE)
        assert [c.type for c in result.content] == ["text", "image"], result.content
        moved, _ = await call("get_ui_elements", window=TITLE, filter="Press me")  # the window was moved above
        bx, by = map(int, re.search(r"at \((\d+), (\d+)\)", moved).groups())
        left, top = server.cb.client_to_screen(server._resolve(TITLE), 0, 0)
        points = [(int(m[1]), int(m[2])) for m in re.finditer(r"click\(x=(\d+), y=(\d+)", found)]
        assert any(abs(left + x - bx) < 25 and abs(top + y - by) < 15 for x, y in points), (found, bx, by)
    else:
        print("(OmniParser not installed: skipping detect_elements)")

    _, shot = await call("screenshot", window=TITLE)
    assert [c.type for c in shot.content] == ["text", "image"], shot.content
    await call("wait", ms=800)

    state = read_state()
    assert state["filled"] == "existing user text", state
    assert state["empty"] == "typed by test", state
    assert state["clicks"] == "3", state
    assert int(state["track"]) >= 7, state


def main():
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
        asyncio.run(run())
        print("PASS")
    finally:
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
