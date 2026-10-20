"""End-to-end test of the Claude Code CLI path without using Claude: tests/fake_claude.py stands in
for `claude`, connects to WinPilot's tool gateway the way Claude Code does, and calls tools.

Checks: the gateway rejects requests without its token; tool calls from the CLI go through the
Agent (approval, activity log); follow-up tasks resume the same session; Stop kills the CLI.
Drives WinControlMCP's throwaway test window; WINCONTROL_ALLOWED_EXES is locked to powershell.exe.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\cli_bridge_test.py
"""
import os
import queue
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ["WINCONTROL_ALLOWED_EXES"] = "powershell.exe"  # must be set before importing the server
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pilot  # noqa: E402
import activity  # noqa: E402
import cli_bridge  # noqa: E402

SERVER_TESTS = HERE.parent.parent / "WinControlMCP" / "tests"
STATE = SERVER_TESTS / "teststate.txt"


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
    cli_bridge.find_claude = lambda configured="": [sys.executable, str(HERE / "fake_claude.py")]
    pilot.CONFIG = Path(tempfile.mkdtemp()) / "settings.json"  # the CLI's work folder goes next to it
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
        cfg = dict(pilot.DEFAULTS, backend="claude_cli")
        log = []

        def run_task(text, stop_after=None):
            agent.tasks.put((text, cfg))
            started = time.time()
            while True:
                try:
                    kind, data = app.events.get(timeout=1)
                except queue.Empty:
                    if stop_after and time.time() - started > stop_after:
                        agent.stop()
                        stop_after = None
                    if time.time() - started > 90:
                        raise AssertionError(f"task hung; events so far: {log[-5:]}")
                    continue
                log.append((kind, data))
                if kind == "approve":
                    agent.decide("allow")
                elif kind == "done":
                    return time.time() - started

        run_task("press the button")
        said = [d for k, d in log if k == "assistant"]
        assert "click: ok" in said, log
        assert any(s.startswith("log: ") and "Click" in s for s in said), said
        assert any(k == "approve" and "Press me" in d for k, d in log), log
        assert not [d for k, d in log if k == "error"], log
        first_session = agent.backend.session_id
        assert first_session

        # The gateway only answers requests that carry its token.
        try:
            urllib.request.urlopen(urllib.request.Request(agent.gateway.url, data=b"{}", method="POST"), timeout=5)
            raise AssertionError("gateway accepted a request without the token")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code

        app.log.add("you", "shortcut", "pressed ctrl+s")
        run_task("anything else?")
        said = [d for k, d in log if k == "assistant"]
        assert any(s.startswith("Resumed session") and "Task: anything else?" in s for s in said), said
        assert agent.backend.session_id == first_session

        took = run_task("sleep please", stop_after=2)
        assert took < 15, took  # Stop killed the CLI instead of waiting out its 60 s sleep
        assert ("info", "Stopped.") in log, log[-4:]

        details = [e["detail"] for e in app.log.query(last=0, source="agent")]
        assert any(d.startswith("Click Button") for d in details), details
        time.sleep(0.8)
        state = dict(part.split("=", 1) for part in STATE.read_text().strip().split("|"))
        assert state["clicks"] == "1", state
        print("PASS")
    finally:
        window.kill()
        STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
