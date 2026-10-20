"""End-to-end test of WinPilot's agent loop with a scripted model (no LLM or API key needed).

Drives WinControlMCP's throwaway test window (tests/testwin.ps1) through the real tools,
and checks the approval flow: allow, deny, and "allow all for this task".
WINCONTROL_ALLOWED_EXES is locked to powershell.exe, so no other app receives input.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\agent_test.py
"""
import os
import queue
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["WINCONTROL_ALLOWED_EXES"] = "powershell.exe"  # must be set before importing the server
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pilot  # noqa: E402

SERVER_TESTS = HERE.parent.parent / "WinControlMCP" / "tests"
STATE = SERVER_TESTS / "teststate.txt"
TITLE = "WinControlTest"


class ScriptedBackend:
    """Stands in for the LLM: replays a fixed plan, reading element ids from tool results."""

    def __init__(self, cfg, tools):
        assert {t.name for t in tools} >= {"get_ui_elements", "set_text", "click_element"}
        self.results, self.turn = [], 0

    def mark(self):
        return 0

    def rollback(self, mark):
        pass

    def add_user(self, text):
        pass

    def add_results(self, results):
        self.results.append(results)

    def step(self):
        self.turn += 1
        if self.turn == 1:
            return "Looking at the window.", [("c1", "get_ui_elements", {"window": TITLE})], False
        elements = self.results[0][0]["text"]
        empty = int(re.search(r"\[(\d+)\] Edit id=emptyBox", elements)[1])
        button = int(re.search(r'\[(\d+)\] Button "Press me"', elements)[1])
        plan = {2: ("set_text", {"element_id": empty, "text": "typed by pilot"}),
                3: ("click_element", {"element_id": button}),   # denied
                4: ("click_element", {"element_id": button}),   # "allow all"
                5: ("click_element", {"element_id": button})}   # runs without asking
        if self.turn in plan:
            name, args = plan[self.turn]
            return "", [(f"c{self.turn}", name, args)], False
        return "Done.", [], False


class StubApp:
    hwnd, last_external = -1, 0

    def __init__(self):
        self.events = queue.Queue()

    def hide_for_action(self):
        return False

    def restore_target_focus(self):
        pass


def main():
    pilot.ClaudeBackend = ScriptedBackend
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

        app = StubApp()
        agent = pilot.Agent(app)
        agent.start()
        agent.tasks.put(("fill the form", dict(pilot.DEFAULTS)))
        verdicts, asked, log = ["allow", "deny", "all"], [], []
        while True:
            kind, data = app.events.get(timeout=60)
            log.append((kind, data))
            if kind == "approve":
                asked.append(data)
                agent.decide(verdicts[len(asked) - 1])
            elif kind == "done":
                break
        time.sleep(0.8)  # let the test window write its state

        state = dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))
        errors = [d for k, d in log if k == "error"]
        assert not errors, errors
        assert len(asked) == 3, asked
        assert asked[0].startswith('Type "typed by pilot" into Edit id=emptyBox'), asked[0]
        assert 'Button "Press me"' in asked[1], asked[1]
        assert ("info", "Denied.") in log, log
        assert ("assistant", "Done.") in log, log
        assert state["empty"] == "typed by pilot", state
        assert state["filled"] == "existing user text", state
        assert state["clicks"] == "2", state
        print("PASS")
    finally:
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
