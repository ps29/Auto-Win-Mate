"""Scheduled tasks: run a WinPilot prompt at a set time, once or on a repeat.

Tasks live in %APPDATA%\\WinPilot\\schedule.json and run while WinPilot is running (it can start
with Windows). A task that comes due while WinPilot isn't running, or the PC is asleep, still runs
if it is at most GRACE late; otherwise it is recorded as missed instead of running at an odd time.
"""
import datetime
import json
import re
import threading
import uuid
from pathlib import Path

REPEATS = {"once": "Once", "daily": "Every day", "weekdays": "Weekdays (Mon-Fri)", "weekly": "Weekly"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GRACE = datetime.timedelta(minutes=15)


def parse_time(text):
    """'23:00', '11 pm', '11:30pm', '7am', '7.15 PM', 'noon', 'midnight' -> 'HH:MM'. Raises ValueError."""
    t = str(text).strip().lower().replace(".", ":")
    special = {"noon": "12:00", "midday": "12:00", "midnight": "00:00"}
    if t in special:
        return special[t]
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", t)
    if not m:
        raise ValueError(f"Can't read the time '{text}'. Try 23:00 or 11 pm.")
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"'{text}': with am/pm the hour must be 1-12.")
        hour = hour % 12 + (12 if half.startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' isn't a valid time.")
    return f"{hour:02d}:{minute:02d}"


def parse_days(value):
    """'mon,wed,fri', ['Mon', 'Fri'] or [0, 4] -> sorted weekday numbers (0 = Monday)."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;/]+", str(value or "").strip())
    days = set()
    for item in items:
        if item == "" or item is None:
            continue
        if isinstance(item, int) or str(item).isdigit():
            days.add(int(item) % 7)
            continue
        name = str(item).strip().lower()[:3]
        if name not in [d.lower() for d in DAY_NAMES]:
            raise ValueError(f"Unknown day '{item}'. Use Mon, Tue, Wed, Thu, Fri, Sat, Sun.")
        days.add([d.lower() for d in DAY_NAMES].index(name))
    return sorted(days)


def next_run(entry, after):
    """The first time this task should run strictly after `after` (a datetime), or None."""
    hour, minute = map(int, entry["time"].split(":"))
    repeat = entry["repeat"]
    if repeat == "once":
        when = datetime.datetime.combine(datetime.date.fromisoformat(entry["date"]), datetime.time(hour, minute))
        return when if when > after else None
    allowed = {"daily": range(7), "weekdays": range(5), "weekly": entry.get("days") or []}[repeat]
    for offset in range(8):
        day = after.date() + datetime.timedelta(days=offset)
        when = datetime.datetime.combine(day, datetime.time(hour, minute))
        if when > after and day.weekday() in allowed:
            return when
    return None


def describe(entry):
    """'Every day at 23:00', 'Mon, Wed at 07:30', 'Once on 2026-10-21 at 23:00'."""
    if entry["repeat"] == "once":
        return f"Once on {entry['date']} at {entry['time']}"
    if entry["repeat"] == "weekly":
        return f"{', '.join(DAY_NAMES[d] for d in entry.get('days', []))} at {entry['time']}"
    return f"{REPEATS[entry['repeat']]} at {entry['time']}"


class Schedule:
    """The list of scheduled tasks, saved as JSON. Thread-safe."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _build(self, prompt, time, repeat, date, days, now):
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("The task needs a prompt: what should WinPilot do?")
        if repeat not in REPEATS:
            raise ValueError(f"Repeat must be one of: {', '.join(REPEATS)}.")
        entry = {"prompt": prompt, "time": parse_time(time), "repeat": repeat}
        if repeat == "once":
            if date:
                entry["date"] = datetime.date.fromisoformat(str(date).strip()).isoformat()
            else:  # today if that time is still ahead, otherwise tomorrow
                h, m = map(int, entry["time"].split(":"))
                today = datetime.datetime.combine(now.date(), datetime.time(h, m))
                entry["date"] = (now.date() + datetime.timedelta(days=0 if today > now else 1)).isoformat()
            if next_run(entry, now) is None:
                raise ValueError(f"{entry['date']} {entry['time']} is already in the past.")
        if repeat == "weekly":
            entry["days"] = parse_days(days)
            if not entry["days"]:
                raise ValueError("A weekly task needs at least one day.")
        return entry

    def add(self, prompt, time, repeat="once", date="", days=None, now=None):
        now = now or datetime.datetime.now()
        entry = self._build(prompt, time, repeat, date, days, now)
        entry.update(id=uuid.uuid4().hex[:8], enabled=True, anchor=now.isoformat(timespec="seconds"), last_run=None)
        with self._lock:
            self.entries.append(entry)
            self.save()
        return entry

    def update(self, entry_id, prompt, time, repeat="once", date="", days=None, now=None):
        now = now or datetime.datetime.now()
        new = self._build(prompt, time, repeat, date, days, now)
        with self._lock:
            entry = self.get(entry_id)
            for key in ("date", "days"):
                entry.pop(key, None)
            entry.update(new, enabled=True, anchor=now.isoformat(timespec="seconds"))
            self.save()
        return entry

    def get(self, entry_id):
        for entry in self.entries:
            if entry["id"] == entry_id:
                return entry
        raise KeyError(f"No scheduled task with id '{entry_id}'.")

    def remove(self, entry_id):
        with self._lock:
            entry = self.get(entry_id)
            self.entries.remove(entry)
            self.save()
        return entry

    def set_enabled(self, entry_id, enabled, now=None):
        with self._lock:
            entry = self.get(entry_id)
            entry["enabled"] = enabled
            if enabled:  # don't fire occurrences that passed while it was off
                entry["anchor"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
            self.save()

    def next_time(self, entry, now=None):
        if not entry.get("enabled"):
            return None
        return next_run(entry, max(now or datetime.datetime.now(),
                                   datetime.datetime.fromisoformat(entry["anchor"])))

    def due(self, now=None):
        """Occurrences that came due since each task was last handled: [(entry, "run" | "missed")].
        Several missed occurrences of one task collapse into one."""
        now = now or datetime.datetime.now()
        result = []
        with self._lock:
            for entry in self.entries:
                if not entry.get("enabled"):
                    continue
                when = next_run(entry, datetime.datetime.fromisoformat(entry["anchor"]))
                if when is None or when > now:
                    continue
                while True:  # judge the latest occurrence, so today's on-time run isn't lost to older missed ones
                    later = next_run(entry, when)
                    if later is None or later > now:
                        break
                    when = later
                status = "run" if now - when <= GRACE else "missed"
                entry["anchor"] = now.isoformat(timespec="seconds")
                if status == "run":
                    entry["last_run"] = now.isoformat(timespec="seconds")
                if entry["repeat"] == "once":
                    entry["enabled"] = False
                result.append((dict(entry), status))
            if result:
                self.save()
        return result


class Scheduler(threading.Thread):
    """Checks the schedule every `interval` seconds and calls on_due(entry, status) for each due task."""

    def __init__(self, schedule, on_due, interval=15):
        super().__init__(daemon=True)
        self.schedule, self.on_due, self.interval = schedule, on_due, interval
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(self.interval):
            for entry, status in self.schedule.due():
                self.on_due(entry, status)
