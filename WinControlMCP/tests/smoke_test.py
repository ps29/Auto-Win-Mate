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
    _, shot = await call("screenshot", window=TITLE)
    assert [c.type for c in shot.content] == ["text", "image"], shot.content
    await call("wait", ms=800)

    state = read_state()
    assert state["filled"] == "existing user text", state
    assert state["empty"] == "typed by test", state
    assert state["clicks"] == "3", state


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
