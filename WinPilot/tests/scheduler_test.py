"""Unit test for scheduled tasks (scheduler.py) and the agent's scheduling tools. No windows or input.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\scheduler_test.py
"""
import datetime as dt
import queue
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pilot  # noqa: E402
import activity  # noqa: E402
import scheduler  # noqa: E402

D = dt.datetime


def test_parsing():
    for text, want in (("23:00", "23:00"), ("11 pm", "23:00"), ("11:30pm", "23:30"), ("7am", "07:00"),
                       ("12 am", "00:00"), ("12 pm", "12:00"), ("7.15 PM", "19:15"), ("noon", "12:00"), ("9", "09:00")):
        assert scheduler.parse_time(text) == want, (text, scheduler.parse_time(text))
    for bad in ("25:00", "13 pm", "soon", "11:75"):
        try:
            scheduler.parse_time(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert scheduler.parse_days("mon, wed,Fri") == [0, 2, 4]
    assert scheduler.parse_days(["Sunday", 0]) == [0, 6]


def test_next_run():
    wed = D(2026, 10, 21, 22, 0)  # a Wednesday
    assert wed.weekday() == 2
    daily = {"time": "23:00", "repeat": "daily"}
    assert scheduler.next_run(daily, wed) == D(2026, 10, 21, 23, 0)
    assert scheduler.next_run(daily, D(2026, 10, 21, 23, 0)) == D(2026, 10, 22, 23, 0)  # strictly after
    fri_night = D(2026, 10, 23, 23, 30)
    assert scheduler.next_run({"time": "08:00", "repeat": "weekdays"}, fri_night) == D(2026, 10, 26, 8, 0)  # Monday
    weekly = {"time": "09:00", "repeat": "weekly", "days": [6]}
    assert scheduler.next_run(weekly, wed) == D(2026, 10, 25, 9, 0)  # Sunday
    once = {"time": "23:00", "repeat": "once", "date": "2026-10-21"}
    assert scheduler.next_run(once, wed) == D(2026, 10, 21, 23, 0) and scheduler.next_run(once, D(2026, 10, 22)) is None


def test_schedule(tmp):
    path = tmp / "schedule.json"
    s = scheduler.Schedule(path)
    now = D(2026, 10, 21, 22, 0)
    once = s.add("Open Firefox and go to https://example.com", "11 pm", now=now)
    assert once["date"] == "2026-10-21" and once["time"] == "23:00"
    late = s.add("later today has passed, so tomorrow", "9 pm", now=now)
    assert late["date"] == "2026-10-22"
    daily = s.add("Check mail", "07:30", "daily", now=now)
    try:
        s.add("in the past", "8 pm", "once", "2026-10-21", now=now)
        raise AssertionError("accepted a time in the past")
    except ValueError:
        pass
    try:
        s.add("", "8 pm", now=now)
        raise AssertionError("accepted an empty prompt")
    except ValueError:
        pass

    assert s.due(D(2026, 10, 21, 22, 59)) == []
    due = s.due(D(2026, 10, 21, 23, 0, 20))
    assert [(e["id"], st) for e, st in due] == [(once["id"], "run")], due
    assert s.due(D(2026, 10, 21, 23, 1)) == []  # not again
    assert not s.get(once["id"])["enabled"]  # a one-off switches itself off

    # Away for two days and back 5 minutes after today's 07:30: the old ones are skipped, today's runs.
    due = s.due(D(2026, 10, 24, 7, 35))
    assert [(e["id"], st) for e, st in due if e["id"] == daily["id"]] == [(daily["id"], "run")], due
    # Back much too late: recorded as missed, not run.
    due = s.due(D(2026, 10, 25, 9, 0))
    assert [(e["id"], st) for e, st in due if e["id"] == daily["id"]] == [(daily["id"], "missed")], due

    s.set_enabled(daily["id"], False)
    assert s.next_time(s.get(daily["id"])) is None
    reloaded = scheduler.Schedule(path)  # saved to disk
    assert [e["id"] for e in reloaded.entries] == [e["id"] for e in s.entries]
    s.remove(late["id"])
    assert late["id"] not in [e["id"] for e in scheduler.Schedule(path).entries]
    assert scheduler.describe(daily) == "Every day at 07:30"


def test_agent_tools(tmp):
    class StubApp:
        hwnd, last_external = -1, 0

        def __init__(self):
            self.events, self.log = queue.Queue(), activity.ActivityLog(tmp / "logs")
            self.schedule = scheduler.Schedule(tmp / "agent-schedule.json")

    app = StubApp()
    agent = pilot.Agent(app)
    agent.cfg = dict(pilot.DEFAULTS)
    names = {t.name for t in pilot.SCHEDULE_TOOLS}
    assert names == {"schedule_task", "list_scheduled_tasks", "cancel_scheduled_task"}
    r = agent._run_tool("c1", "schedule_task", {"prompt": "Open Firefox and go to https://example.com",
                                                "time": "11 pm", "repeat": "once"})
    assert not r["error"] and r["text"].startswith("Scheduled (id "), r
    task_id = app.schedule.entries[0]["id"]
    r = agent._run_tool("c2", "list_scheduled_tasks", {})
    assert task_id in r["text"] and "example.com" in r["text"], r
    r = agent._run_tool("c3", "schedule_task", {"prompt": "x", "time": "soon"})
    assert r["error"] and "Can't read the time" in r["text"], r
    r = agent._run_tool("c4", "cancel_scheduled_task", {"task_id": task_id})
    assert not r["error"] and app.schedule.entries == [], r
    assert any(e["detail"].startswith("scheduled ") for e in app.log.query(source="agent"))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_parsing()
        test_next_run()
        test_schedule(Path(tmp))
        test_agent_tools(Path(tmp))
    print("PASS")


if __name__ == "__main__":
    main()
