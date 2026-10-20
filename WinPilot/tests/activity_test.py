"""Unit test for the activity log's keyboard handling: shortcuts and key names are logged,
typed text is only counted, never stored. No hooks or windows involved.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\activity_test.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pilot  # noqa: E402,F401  (sets up sys.path for clickbot)
import activity  # noqa: E402

VK_CTRL, VK_SHIFT, VK_ENTER = 0x11, 0x10, 0x0D


def main():
    with tempfile.TemporaryDirectory() as tmp:
        log = activity.ActivityLog(Path(tmp))
        keys = activity.KeyAggregator(log, idle=2.0)

        def press(vk, t, hwnd=0):
            keys.key(vk, True, hwnd, t)
            keys.key(vk, False, hwnd, t)

        for i, ch in enumerate("secret pw"):  # typing: counted only
            press(ord(ch.upper()) if ch != " " else 0x20, 1.0 + i * 0.1)
        keys.key(VK_CTRL, True, 0, 2.0)  # ctrl+s flushes the count, then logs the shortcut
        press(ord("S"), 2.0)
        keys.key(VK_CTRL, False, 0, 2.0)
        keys.key(VK_SHIFT, True, 0, 3.0)  # shift+letter is still typing
        press(ord("A"), 3.0)
        keys.key(VK_SHIFT, False, 0, 3.0)
        keys.tick(4.0)  # not idle long enough yet
        assert [e["detail"] for e in log.query()] == ["typed 9 characters", "pressed ctrl+s"], log.query()
        keys.tick(5.1)
        press(VK_ENTER, 6.0)

        details = [e["detail"] for e in log.query()]
        assert details == ["typed 9 characters", "pressed ctrl+s", "typed 1 character", "pressed enter"], details
        assert all(e["source"] == "you" for e in log.query())

        log.paused = True
        assert log.add("you", "click", "ignored while paused") is None
        assert log.add("agent", "action", "agent actions are always kept")
        assert [e["detail"] for e in log.query(source="agent")] == ["agent actions are always kept"]
        assert len(log.query(last=2)) == 2 and log.query(after_id=4)[0]["id"] == 5

        written = "".join(f.read_text(encoding="utf-8") for f in Path(tmp).glob("activity-*.jsonl"))
        assert "secret" not in written.lower() and len(written.splitlines()) == 5, written
        assert json.loads(written.splitlines()[0])["detail"] == "typed 9 characters"
        print(activity.format_entry(log.query()[0]))
    print("PASS")


if __name__ == "__main__":
    main()
