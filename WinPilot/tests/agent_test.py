"""End-to-end test of WinPilot's agent loop with a scripted model (no LLM or API key needed).

Drives WinControlMCP's throwaway test window (tests/testwin.ps1) through the real tools,
and checks the approval flow (allow, deny, "allow all for this task"), the activity log the
agent writes and reads, the activity summary added to the next task, and a batch of screen
actions (as from a local computer-use model) that stops at the first failure.
WINCONTROL_ALLOWED_EXES is locked to powershell.exe, so no other app receives input.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\agent_test.py
"""
import os
import queue
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["WINCONTROL_ALLOWED_EXES"] = "powershell.exe"  # must be set before importing the server
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pilot  # noqa: E402
import activity  # noqa: E402
import computer  # noqa: E402

SERVER_TESTS = HERE.parent.parent / "WinControlMCP" / "tests"
STATE = SERVER_TESTS / "teststate.txt"
TITLE = "WinControlTest"


class ScriptedBackend:
    """Stands in for the LLM: replays a fixed plan, reading element ids from tool results."""

    def __init__(self, cfg, tools):
        assert {t.name for t in tools} >= {"get_ui_elements", "set_text", "click_element", "drag",
                                           "move_mouse", "get_cursor", "read_activity_log"}
        self.results, self.turn, self.prompts = [], 0, []
        self.screen = computer.Screen(lambda w, h: (w // 2, h // 2))
        ScriptedBackend.instance = self

    def mark(self):
        return 0

    def rollback(self, mark):
        pass

    def add_user(self, text):
        self.prompts.append(text)

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
                5: ("click_element", {"element_id": button}),   # runs without asking
                6: ("read_activity_log", {"source": "agent"})}
        if self.turn in plan:
            name, args = plan[self.turn]
            return "", [(f"c{self.turn}", name, args)], False
        if self.turn == 7:  # screen actions: a click, a bad action, then one that must not run
            m = re.search(r'Button "Press me".*? at \((\d+), (\d+)\)', elements)
            point = list(self.screen.to_model(int(m[1]), int(m[2])))
            click = {"kind": "click", "point": point, "button": "left", "clicks": 1}
            return "", [("t1", "vision", click, "vision"),
                        ("t2", "vision", {"kind": "teleport"}, "vision"),
                        ("t3", "vision", click, "vision")], False
        if self.turn == 8:
            return "Done.", [], False
        return "Second task done.", [], False


class StubApp:
    hwnd, last_external = -1, 0

    def __init__(self, folder):
        self.events = queue.Queue()
        self.log = activity.ActivityLog(folder)

    def hide_for_action(self):
        return False

    def restore_target_focus(self):
        pass


def main():
    pilot.LemonadeBackend = ScriptedBackend
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

        app = StubApp(Path(tempfile.mkdtemp()))
        agent = pilot.Agent(app)
        agent.start()
        verdicts, asked, log = ["allow", "deny", "all"], [], []

        def run_task(text):
            agent.tasks.put((text, dict(pilot.DEFAULTS, ask_before_input=True)))  # this test checks approvals
            while True:
                kind, data = app.events.get(timeout=60)
                log.append((kind, data))
                if kind == "approve":
                    asked.append(data)
                    agent.decide(verdicts[len(asked) - 1])
                elif kind == "done":
                    return

        run_task("fill the form")
        app.log.add("you", "shortcut", "pressed ctrl+s")  # something the user does between tasks
        run_task("anything else?")
        time.sleep(0.8)  # let the test window write its state

        state = dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))
        errors = [d for k, d in log if k == "error"]
        assert not errors, errors
        assert len(asked) == 3, asked
        assert asked[0].startswith('Type "typed by pilot" into Edit id=emptyBox'), asked[0]
        assert 'Button "Press me"' in asked[1], asked[1]
        assert ("info", "Denied.") in log, log
        assert ("assistant", "Done.") in log, log

        details = [e["detail"] for e in app.log.query(last=0, source="agent")]
        assert details[0] == 'started task: "fill the form"', details
        assert details[1].startswith("Read the controls of"), details
        assert details[2].startswith('Type "typed by pilot"'), details
        assert details[3].startswith("you denied: Click"), details
        assert details[4:6] == [details[4]] * 2 and details[4].startswith("Click"), details
        assert details[6].startswith("Left click at"), details  # the screen-action click
        assert details[7].startswith("teleport - failed"), details  # the bad action is logged as a failure
        assert details[8] == "finished task", details
        batch = ScriptedBackend.instance.results[6]
        assert [r["error"] for r in batch] == [False, True, True], batch
        assert batch[2]["text"] == "Not executed: an earlier computer action in this turn failed.", batch
        log_result = [d for k, d in log if k == "result"][-1]
        assert "agent  you denied: Click" in log_result, log_result
        prompts = ScriptedBackend.instance.prompts
        assert prompts[0] == "fill the form", prompts  # nothing from the user yet
        assert prompts[1].startswith("What happened on the desktop since your last task"), prompts[1]
        assert "you    pressed ctrl+s" in prompts[1] and prompts[1].endswith("Task: anything else?"), prompts[1]
        assert state["empty"] == "typed by pilot", state
        assert state["filled"] == "existing user text", state
        assert state["clicks"] == "3", state  # two click_element + one screen-action click
        print("PASS")
    finally:
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
