r"""Unit test for the click-coordinate checks and the mouse glide clamp. Moves no mouse and sends no input.

Run from the WinControlMCP folder:
    .venv\Scripts\python.exe tests\bounds_test.py
"""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

cb = server.cb
SENT = []  # input that reached the (stubbed) input layer; a refused point must never get here
cb.click = lambda *a, **k: SENT.append(("click", a))
cb.drag = lambda *a, **k: SENT.append(("drag", a))
cb.move_to = lambda *a, **k: SENT.append(("move", a))
server._focus = lambda hwnd: ""  # don't change the user's focus either


def expect_error(call, *words):
    try:
        call()
        raise AssertionError(f"the point was accepted and input was sent: {SENT}")
    except ValueError as e:
        for word in words:
            assert word in str(e), (word, str(e))
        return
    raise AssertionError("expected a ValueError")


def test_off_screen_points_are_refused():
    click, move, drag = server.click.__wrapped__, server.move_mouse.__wrapped__, server.drag.__wrapped__
    expect_error(lambda: click(3548, 46490), "off the screen", "screenshot")
    expect_error(lambda: click(-50000, 10), "off the screen")
    expect_error(lambda: move(10 ** 6, 10), "off the screen")
    expect_error(lambda: drag(10, 10, 10 ** 6, 10), "off the screen")


def test_points_outside_a_window_are_refused():
    vx, vw = cb.user32.GetSystemMetrics(76), cb.user32.GetSystemMetrics(78)
    for hwnd, _, _ in cb.list_windows():
        r = ctypes.wintypes.RECT()
        cb.user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < 50 or r.left < vx + 150 or r.right > vx + vw - 150:
            continue  # need a window with room on both sides, so "just outside" is still on the screen
        width = r.right - r.left
        expect_error(lambda: server.click.__wrapped__(width + 10, 5, window=hwnd), "outside that window")
        expect_error(lambda: server.move_mouse.__wrapped__(-100, 5, window=hwnd), "outside that window")
        server._check_point(r.left + 5, r.top + 5, hwnd)  # inside: accepted
        return
    raise AssertionError("no suitable window open to test with")


def test_glide_path_stays_on_screen():
    for point in ((10 ** 6, 10 ** 6), (-10 ** 6, -10 ** 6), (100, 100)):
        x, y = cb.clamp_to_screen(*point)
        left, top = cb.user32.GetSystemMetrics(76), cb.user32.GetSystemMetrics(77)
        width, height = cb.user32.GetSystemMetrics(78), cb.user32.GetSystemMetrics(79)
        assert left <= x < left + width and top <= y < top + height, (point, x, y)
    assert cb.clamp_to_screen(100, 100) == (100, 100)


if __name__ == "__main__":
    import ctypes.wintypes  # noqa: F401  (ctypes.wintypes.RECT above)
    test_off_screen_points_are_refused()
    test_points_outside_a_window_are_refused()
    test_glide_path_stays_on_screen()
    print("PASS")
