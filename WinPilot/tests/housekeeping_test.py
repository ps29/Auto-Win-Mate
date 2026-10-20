"""Unit test for housekeeping: old activity logs are pruned by date, the action log rotates.
No windows or input involved.

Run from the WinPilot folder:
    ..\\WinControlMCP\\.venv\\Scripts\\python.exe tests\\housekeeping_test.py
"""
import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import housekeeping  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        today = datetime.date(2026, 10, 21)
        for days_old in (0, 29, 30, 31, 90):
            day = today - datetime.timedelta(days=days_old)
            (tmp / f"activity-{day}.jsonl").write_text("x" * 10)
        (tmp / "notes.txt").write_text("not a log")  # unrelated files are left alone
        removed, freed = housekeeping.prune_activity_logs(tmp, keep_days=30, today=today)
        left = sorted(f.name for f in tmp.iterdir())
        assert (removed, freed) == (2, 20), (removed, freed)
        assert left == ["activity-2026-09-21.jsonl", "activity-2026-09-22.jsonl", "activity-2026-10-21.jsonl",
                        "notes.txt"], left

        log = tmp / "actions.log"
        log.write_text("small")
        assert not housekeeping.rotate(log, max_bytes=100, keep=2)
        for generation in "abc":
            log.write_text(generation * 200)
            assert housekeeping.rotate(log, max_bytes=100, keep=2)
        assert not log.exists()
        assert (tmp / "actions.log.1").read_text()[0] == "c" and (tmp / "actions.log.2").read_text()[0] == "b"
        assert not (tmp / "actions.log.3").exists()  # the oldest generation was dropped
    assert housekeeping.human(5 * 1024 * 1024) == "5.0 MB"
    print("PASS")


if __name__ == "__main__":
    main()
