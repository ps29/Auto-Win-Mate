"""WinControl - an MCP server that gives AI agents (any MCP client, including
local models served by Lemonade) tools to see and control the Windows desktop.

Mouse/keyboard/window handling is reused from ClickBot (..\\ClickBot\\clickbot.py).
Reading an app's buttons and fields uses UI Automation through pywinauto.

Safety:
  * Push the mouse into the top-left screen corner and every input tool refuses to act.
  * Set WINCONTROL_ALLOWED_EXES=notepad.exe,calc.exe to restrict which apps get input.
  * Every action is logged to actions.log next to this file.
"""
import asyncio
import collections
import concurrent.futures
import ctypes
import ctypes.wintypes as wt
import datetime
import functools
import io
import os
import sys
import time
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("WINCONTROL_CLICKBOT_DIR", str(HERE.parent / "ClickBot")))
import clickbot as cb  # noqa: E402  (importing it also makes this process DPI-aware)

import mss  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

FROZEN = getattr(sys, "frozen", False)  # packaged as WinControlMCP.exe / inside WinPilot.exe
LOG = Path(os.environ.get("WINCONTROL_LOG", Path(os.environ.get("LOCALAPPDATA", HERE)) / "WinPilot" / "actions.log"
                          if FROZEN else HERE / "actions.log"))
LOG_MAX_BYTES, LOG_KEEP = 5 * 1024 * 1024, 3  # actions.log rotates to actions.log.1 .. .3
# How fast simulated input is: "human" (default: curved mouse paths, typing at a person's pace),
# "careful" (slower still) or "instant".
cb.set_input_speed(os.environ.get("WINCONTROL_INPUT_SPEED", "human"))
ALLOWED = {e.strip().lower() for e in os.environ.get("WINCONTROL_ALLOWED_EXES", "").split(",") if e.strip()}
INTERACTIVE = {"Button", "Edit", "CheckBox", "ComboBox", "ListItem", "MenuItem", "TabItem",
               "Hyperlink", "RadioButton", "Document", "Slider", "TreeItem", "DataItem",
               "SplitButton", "Spinner"}

cb.user32.GetClientRect.restype = wt.BOOL
cb.user32.GetClientRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))


def _init_worker():
    # pywinauto creates its UI Automation COM object on the importing thread, so
    # import it on the single worker thread that runs every tool.
    import pywinauto  # noqa: F401


_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_init_worker)
_ELEMENTS = []  # (wrapper, top-level hwnd) from the latest get_ui_elements call

mcp = MCPServer(
    name="wincontrol",
    instructions=(
        "Tools to see and control the Windows desktop. Typical flow: list_windows -> "
        "focus_window or start_app -> get_ui_elements -> click_element / set_text -> "
        "get_ui_elements again to check the result. Element ids are only valid until the next "
        "get_ui_elements call. Use screenshot plus click(x, y, window=...) for apps that expose "
        "no UI elements (games, custom-drawn apps). move_mouse hovers, drag does drag-and-drop, and "
        "get_cursor shows where the pointer, keyboard focus and text cursor are. Use wait_for instead of "
        "guessing waits, read_page_text to read pages and documents, and get_ui_elements(web_only=True) "
        "inside browsers. "
        "Call wait after actions that open windows. "
        "Apps may restore previous sessions (e.g. Notepad reopens unsaved tabs): check what a "
        "window contains before editing it, and never delete or overwrite the user's content "
        "or close unsaved work without asking."
    ),
)


def tool(fn):
    """Register fn as an MCP tool that runs on the single worker thread."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await asyncio.wrap_future(_EXEC.submit(fn, *args, **kwargs))
        except Exception as e:
            # Only ToolError messages reach the model; others become a generic error.
            raise ToolError(str(e) or type(e).__name__) from e
    return mcp.tool()(wrapper)

# ---------------------------------------------------------------- helpers


def _log(msg):
    try:
        if LOG.stat().st_size > LOG_MAX_BYTES:
            for i in range(LOG_KEEP - 1, 0, -1):
                older = LOG.with_name(f"{LOG.name}.{i}")
                if older.exists():
                    older.replace(LOG.with_name(f"{LOG.name}.{i + 1}"))
            LOG.replace(LOG.with_name(f"{LOG.name}.1"))
    except OSError:
        pass  # no log yet, or another process is rotating it
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def _guard(hwnd=None):
    if cb.at_failsafe():
        raise RuntimeError("Failsafe is active (the mouse is in the top-left screen corner). "
                           "Move the mouse away to allow input.")
    if ALLOWED:
        hwnd = hwnd or cb.root_of(cb.user32.GetForegroundWindow())
        exe = cb.process_name(hwnd).lower() if hwnd else "?"
        if exe not in ALLOWED:
            raise PermissionError(f"'{exe}' is not in WINCONTROL_ALLOWED_EXES "
                                  f"({', '.join(sorted(ALLOWED))}).")


def _resolve(window):
    """hwnd number, exact title, title substring, or exe name -> top-level hwnd."""
    w = str(window).strip()
    if w.isdigit():
        if cb.user32.IsWindow(int(w)):
            return cb.root_of(int(w))
        raise ValueError(f"No window with hwnd {w}. Call list_windows.")
    windows, low = cb.list_windows(), w.lower()
    for match in (lambda t, e: t.lower() == low, lambda t, e: low in t.lower(), lambda t, e: low in e.lower()):
        hits = [h for h, t, e in windows if match(t, e)]
        if hits:
            return hits[0]
    raise ValueError(f"No window matching '{window}'. Call list_windows to see what's open.")


def _focus(hwnd):
    cb.activate(hwnd)
    if cb.root_of(cb.user32.GetForegroundWindow()) != hwnd:
        return " (warning: Windows did not bring the window to the front)"
    return ""


def _client_rect(hwnd):
    r = wt.RECT()
    cb.user32.GetClientRect(hwnd, ctypes.byref(r))
    left, top = cb.client_to_screen(hwnd, 0, 0)
    return left, top, r.right - r.left, r.bottom - r.top


def _element(element_id):
    if not 0 <= element_id < len(_ELEMENTS):
        raise ValueError(f"Unknown element id {element_id}. Call get_ui_elements first.")
    el, hwnd = _ELEMENTS[element_id]
    if not cb.user32.IsWindow(hwnd):
        raise ValueError("That element's window has closed. Call list_windows.")
    return el, hwnd


def _center(el):
    r = el.element_info.rectangle  # re-read: the window may have moved
    if r.width() <= 0 or r.height() <= 0:
        raise ValueError("Element has no on-screen area (hidden or scrolled away). "
                         "Call get_ui_elements again.")
    return r.mid_point().x, r.mid_point().y


def _value(el):
    try:
        return el.iface_value.CurrentValue or ""
    except Exception:
        pass
    try:
        return el.iface_text.DocumentRange.GetText(500) or ""
    except Exception:
        return ""


def _describe(el):
    info = el.element_info
    r = info.rectangle
    if r.width() <= 0 or r.height() <= 0:
        return None
    ctype, name, aid = info.control_type or "?", (info.name or "").strip(), info.automation_id or ""
    if not name and not aid and ctype not in INTERACTIVE:
        return None
    parts = [ctype]
    if name:
        parts.append(f'"{name[:80]}"')
    if aid and aid != name:
        parts.append(f"id={aid}")
    if ctype in ("Edit", "Document", "ComboBox"):
        value = _value(el).replace("\r", "").replace("\n", "\\n")
        if value:
            parts.append(f'value="{value[:80]}{"..." if len(value) > 80 else ""}"')
    parts.append(f"at ({r.mid_point().x}, {r.mid_point().y})")
    if not info.enabled:
        parts.append("disabled")
    return " ".join(parts)

# ---------------------------------------------------------------- tools


@tool
def list_windows() -> str:
    """List visible top-level windows as hwnd | title | exe."""
    fg = cb.root_of(cb.user32.GetForegroundWindow())
    lines = []
    for h, t, e in cb.list_windows():
        flags = [f for f, on in (("foreground", h == fg), ("minimized", cb.user32.IsIconic(h))) if on]
        lines.append(f"hwnd={h} | {t} | {e}" + (f" | {', '.join(flags)}" if flags else ""))
    return "\n".join(lines) or "No visible windows."


@tool
def focus_window(window: str | int) -> str:
    """Bring a window to the front. `window` is an hwnd, a title (or part of one), or an exe name."""
    hwnd = _resolve(window)
    note = _focus(hwnd)
    _log(f"focus_window {hwnd} {cb.window_title(hwnd)!r}")
    return f"Focused hwnd={hwnd}: {cb.window_title(hwnd)}{note}"


@tool
def start_app(command: str, arguments: str = "") -> str:
    """Launch a program, file, or URI, e.g. 'notepad', 'calc', 'ms-settings:', or a full path."""
    if cb.at_failsafe():
        raise RuntimeError("Failsafe is active (the mouse is in the top-left screen corner).")
    before = {h for h, _, _ in cb.list_windows()}
    os.startfile(command, arguments=arguments) if arguments else os.startfile(command)
    _log(f"start_app {command!r} {arguments!r}")
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.3)
        new = [(h, t, e) for h, t, e in cb.list_windows() if h not in before]
        if new:
            return "Started. New window(s):\n" + "\n".join(f"hwnd={h} | {t} | {e}" for h, t, e in new)
    return "Started, but no new window appeared within 8 s (it may have reused an existing one). Call list_windows."


def _root(hwnd):
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo
    return UIAWrapper(UIAElementInfo(hwnd))


def _walk(root, flt="", max_depth=12, max_items=250, seen=None):
    """Describable elements under root as (wrapper, depth, line); plus whether max_items cut it short.
    `seen`, if given, gets "n": how many describable elements were visited, filter or not."""
    found, flt = [], flt.lower()
    seen = seen if seen is not None else {}
    seen["n"] = 0

    def walk(el, depth):
        if depth > max_depth or len(found) >= max_items:
            return
        try:
            children = el.children()
        except Exception:
            return
        for child in children:
            if len(found) >= max_items:
                return
            line = _describe(child)
            if line:
                seen["n"] += 1
            if line and (not flt or flt in line.lower()):
                found.append((child, depth, line))
            walk(child, depth + 1)

    walk(root, 0)
    return found, len(found) >= max_items


def _web_document(hwnd):
    """The page content of a Chrome/Edge/Electron window (its first Document element), or None."""
    queue = collections.deque([(_root(hwnd), 0)])
    while queue:
        el, depth = queue.popleft()
        if el.element_info.control_type == "Document" and depth:
            return el
        if depth < 8:
            try:
                queue += [(c, depth + 1) for c in el.children()]
            except Exception:
                pass
    return None


@tool
def get_ui_elements(window: str | int, filter: str = "", max_depth: int = 12, max_items: int = 250,
                    web_only: bool = False) -> str:
    """List a window's UI elements (buttons, fields, menu items...) with numeric ids.

    Use the ids with click_element / set_text. `filter` keeps only lines containing that text.
    Positions are screen coordinates. Ids are valid until the next get_ui_elements call.
    web_only=True lists only the web page inside a browser (Chrome, Edge, Electron apps),
    skipping tabs and toolbars.
    """
    hwnd = _resolve(window)
    root = _root(hwnd)
    if web_only:
        root = _web_document(hwnd)
        if root is None:
            raise ValueError("No web page found in that window. web_only works with Chrome, Edge and "
                             "Electron apps; call get_ui_elements without it.")
        max_depth = max(max_depth, 30)  # web pages nest deeply
    seen = {}
    found, truncated = _walk(root, filter, max_depth, max_items, seen)
    if seen["n"] < 10:  # count before filtering, so a filter doesn't trigger the wait on a built tree
        # Chromium and Electron apps build their accessibility tree only once someone asks.
        time.sleep(0.6)
        found, truncated = _walk(root, filter, max_depth, max_items)
    _ELEMENTS[:] = [(el, hwnd) for el, _, _ in found]
    lines = [f"Window hwnd={hwnd}: {cb.window_title(hwnd)} [{cb.process_name(hwnd)}]"]
    lines += [("" if filter else "  " * depth) + f"[{i}] {line}" for i, (_, depth, line) in enumerate(found)]
    if not found:
        lines.append("No UI elements found. This app may not expose UI Automation; use screenshot instead.")
    if truncated:
        lines.append(f"(stopped at {max_items} elements; use `filter` to narrow down)")
    return "\n".join(lines)


@tool
def read_page_text(window: str | int, max_chars: int = 8000) -> str:
    """Read the text content of a window: a web page in a browser, a document in an editor, or
    otherwise the text of its labels. Use it to read information instead of screenshots."""
    hwnd = _resolve(window)
    doc = _web_document(hwnd)
    text = ""
    for el in (doc, _root(hwnd)):
        if el is None:
            continue
        try:
            text = el.iface_text.DocumentRange.GetText(max(1, max_chars)) or ""
        except Exception:
            text = ""
        if text.strip():
            break
    if not text.strip():  # no text pattern: collect the names of text-like elements
        found, _ = _walk(doc or _root(hwnd), max_depth=30, max_items=1500)
        names = [el.element_info.name.strip() for el, _, _ in found
                 if el.element_info.control_type in ("Text", "Hyperlink", "Document", "Edit", "ListItem", "Button", "CheckBox",
                                                  "RadioButton", "TabItem", "MenuItem", "DataItem", "Header")
                 and (el.element_info.name or "").strip()]
        text = "\n".join(dict.fromkeys(names))
    text = text.replace("\r", "").replace("￼", " ")  # U+FFFC marks embedded objects (images...)
    if not text.strip():
        return "No readable text found. Try screenshot."
    more = f"\n... ({len(text) - max_chars} more characters)" if len(text) > max_chars else ""
    return f"Text of {cb.window_title(hwnd)}:\n{text[:max_chars]}{more}"


@tool
def wait_for(window: str | int, text: str = "", timeout_ms: int = 10000) -> str:
    """Wait until a window exists (by title, part of a title, or exe), or, with `text`, until that
    text appears in the window's UI (a button, label, field value...). Better than guessing with wait."""
    deadline = time.time() + max(0, min(timeout_ms, 60000)) / 1000
    last_error = ""
    while True:
        try:
            hwnd = _resolve(window)
            if not text:
                return f"Window is there: hwnd={hwnd}: {cb.window_title(hwnd)}"
            if text.lower() in cb.window_title(hwnd).lower():
                return f"Found '{text}' in the title of hwnd={hwnd}: {cb.window_title(hwnd)}"
            found, _ = _walk(_root(hwnd), text, max_depth=30, max_items=5)
            if found:
                return f"Found in hwnd={hwnd}: {found[0][2]}"
            last_error = f"'{text}' not in {cb.window_title(hwnd)}"
        except ValueError as e:
            last_error = str(e)
        if time.time() >= deadline:
            raise TimeoutError(f"Timed out after {timeout_ms} ms: {last_error}")
        time.sleep(0.5)


@tool
def clipboard(action: Literal["get", "set"], text: str = "") -> str:
    """Read the clipboard's text (action='get') or put text on it (action='set'). Paste with
    press_keys('ctrl+v'); setting the clipboard and pasting is the fastest way to enter long text."""
    import win32clipboard
    for attempt in range(10):  # another app may hold the clipboard open for a moment
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("The clipboard is in use by another program. Try again.")
    try:
        if action == "set":
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            _log(f"clipboard set {len(text)} chars")
            return f"Put {len(text)} characters on the clipboard."
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return "The clipboard holds no text."
        value = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        _log("clipboard get")
        return f"Clipboard text ({len(value)} characters):\n{value[:8000]}"
    finally:
        win32clipboard.CloseClipboard()


cb.user32.MoveWindow.restype = wt.BOOL
cb.user32.MoveWindow.argtypes = (wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL)
cb.user32.GetWindowRect.restype = wt.BOOL
cb.user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))


@tool
def arrange_window(window: str | int, action: Literal["maximize", "minimize", "restore", "move"],
                   x: int | None = None, y: int | None = None,
                   width: int | None = None, height: int | None = None) -> str:
    """Maximize, minimize or restore a window, or move/resize it (action='move' with screen x, y and
    optionally width, height in pixels)."""
    hwnd = _resolve(window)
    if action == "move":
        if x is None or y is None:
            raise ValueError("action='move' needs x and y.")
        cb.user32.ShowWindow(hwnd, 9)  # restore first: a maximized window can't be moved
        r = wt.RECT()
        cb.user32.GetWindowRect(hwnd, ctypes.byref(r))
        cb.user32.MoveWindow(hwnd, x, y, width or r.right - r.left, height or r.bottom - r.top, True)
    else:
        cb.user32.ShowWindow(hwnd, {"maximize": 3, "minimize": 6, "restore": 9}[action])
    _log(f"arrange_window {hwnd} {action} {x} {y} {width} {height}")
    return f"{action.title()}d '{cb.window_title(hwnd)}'." if action != "move" else \
        f"Moved '{cb.window_title(hwnd)}' to ({x}, {y})."


@tool
def click_element(element_id: int, button: Literal["left", "right", "middle"] = "left",
                  double: bool = False) -> str:
    """Click the center of an element returned by get_ui_elements."""
    el, hwnd = _element(element_id)
    _guard(hwnd)
    note = _focus(hwnd)
    x, y = _center(el)
    cb.click((x, y), button, 2 if double else 1)
    name = el.element_info.name
    _log(f"click_element [{element_id}] {name!r} at ({x}, {y}) {button}{' double' if double else ''}")
    return f"Clicked [{element_id}] {el.element_info.control_type} \"{name}\" at ({x}, {y}).{note}"


@tool
def set_text(element_id: int, text: str, press_enter: bool = False, replace_existing: bool = False) -> str:
    """Set the text of an edit field / document returned by get_ui_elements.

    If the field already contains text, this refuses unless replace_existing=True, because the
    existing text would be lost. Ask the user before replacing content they wrote.
    """
    el, hwnd = _element(element_id)
    old = _value(el)
    if old and old != text and not replace_existing:
        preview = old[:100].replace("\r", "").replace("\n", "\\n")
        raise ValueError(f"[{element_id}] already contains {len(old)} characters "
                         f"(\"{preview}{'...' if len(old) > 100 else ''}\"), which would be lost. "
                         "Ask the user first, then call again with replace_existing=True; "
                         "or click into the field and use type_text to add text instead.")
    _guard(hwnd)
    note = _focus(hwnd)
    method = None
    try:
        el.iface_value.SetValue(text)
        if _value(el) == text:
            method = "UI Automation"
    except Exception:
        pass
    if not method:  # fall back to real typing: click in, select all, type over it
        cb.click(_center(el))
        cb.press_combo("ctrl+a")
        cb.type_text(text)
        method = "typing"
    if press_enter:
        cb.press_combo("enter")
    _log(f"set_text [{element_id}] {len(text)} chars via {method}{' + enter' if press_enter else ''}")
    return f"Set text of [{element_id}] via {method}{' and pressed Enter' if press_enter else ''}.{note}"


@tool
def click(x: int, y: int, button: Literal["left", "right", "middle"] = "left", clicks: int = 1,
          window: str | int | None = None) -> str:
    """Click at a point. Without `window`, x/y are screen coordinates; with `window`,
    they are relative to that window's client area (as reported by screenshot(window=...))."""
    note = ""
    if window is not None:
        hwnd = _resolve(window)
        _guard(hwnd)
        note = _focus(hwnd)
        sx, sy = cb.client_to_screen(hwnd, x, y)
    else:
        sx, sy = x, y
        _guard(cb.root_of(cb.user32.WindowFromPoint(wt.POINT(x, y))))
    cb.click((sx, sy), button, max(1, min(clicks, 3)))
    _log(f"click ({sx}, {sy}) {button} x{clicks}")
    return f"Clicked {button} x{clicks} at screen ({sx}, {sy}).{note}"


def _screen_point(x, y, window):
    """Client (x, y) of `window`, or screen (x, y) without one -> guarded screen point and focus note."""
    if window is not None:
        hwnd = _resolve(window)
        _guard(hwnd)
        return cb.client_to_screen(hwnd, x, y), _focus(hwnd)
    _guard(cb.root_of(cb.user32.WindowFromPoint(wt.POINT(x, y))))
    return (x, y), ""


@tool
def move_mouse(x: int, y: int, window: str | int | None = None) -> str:
    """Move the mouse pointer without clicking, e.g. to hover over a menu or show a tooltip.
    Coordinates work as in click()."""
    (sx, sy), note = _screen_point(x, y, window)
    cb.move_to((sx, sy))
    _log(f"move_mouse ({sx}, {sy})")
    return f"Moved the mouse to screen ({sx}, {sy}).{note}"


@tool
def drag(from_x: int, from_y: int, to_x: int, to_y: int, window: str | int | None = None,
         button: Literal["left", "right", "middle"] = "left") -> str:
    """Press the mouse button at (from_x, from_y), move to (to_x, to_y) and release: drag-and-drop,
    sliders, selecting text or resizing. Coordinates work as in click()."""
    start, note = _screen_point(from_x, from_y, window)
    end, _ = _screen_point(to_x, to_y, window)
    cb.drag(start, end, button)
    _log(f"drag {start} -> {end} {button}")
    return f"Dragged {button} from screen {start} to {end}.{note}"


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


cb.user32.GetGUIThreadInfo.restype = wt.BOOL
cb.user32.GetGUIThreadInfo.argtypes = (wt.DWORD, ctypes.POINTER(_GUITHREADINFO))


@tool
def get_cursor() -> str:
    """Where the mouse pointer is, which element has keyboard focus, and where the text cursor
    (caret) is. Check this before type_text / press_keys to know where the keys will land."""
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo

    x, y = cb.cursor_pos()
    under = cb.root_of(cb.user32.WindowFromPoint(wt.POINT(x, y)))
    lines = [f"Mouse at ({x}, {y}) over hwnd={under}: {cb.window_title(under)} [{cb.process_name(under)}]"]
    fg = cb.root_of(cb.user32.GetForegroundWindow())
    lines.append(f"Foreground window: hwnd={fg}: {cb.window_title(fg)} [{cb.process_name(fg)}]")
    try:
        el = UIAWrapper(UIAElementInfo(IUIA().iuia.GetFocusedElement()))
        info = el.element_info
        lines.append("Keyboard focus: " + (_describe(el) or f"{info.control_type} \"{info.name or ''}\""))
    except Exception:
        lines.append("Keyboard focus: unknown")
    gti = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
    if cb.user32.GetGUIThreadInfo(0, ctypes.byref(gti)) and gti.hwndCaret:
        r = gti.rcCaret
        cx, cy = cb.client_to_screen(gti.hwndCaret, r.left, r.top)
        lines.append(f"Text cursor (caret) at screen ({cx}, {cy}), height {r.bottom - r.top}")
    else:
        lines.append("Text cursor position unknown (this app doesn't report it); typing goes to the focused element.")
    return "\n".join(lines)


@tool
def type_text(text: str, window: str | int | None = None) -> str:
    """Type text at the text cursor of the focused control (or focus `window` first). Newlines press
    Enter. To place the text cursor, click into the field or move it with press_keys."""
    note = _focus(_resolve(window)) if window is not None else ""
    _guard()
    cb.type_text(text)
    _log(f"type_text {len(text)} chars")
    return f"Typed {len(text)} characters.{note}"


@tool
def press_keys(keys: str, window: str | int | None = None) -> str:
    """Press keys or combos, space-separated and in order: e.g. 'ctrl+a delete', 'alt+f4', 'enter'.
    Names: a-z, 0-9, f1-f24, enter, tab, esc, space, backspace, delete, home, end, pageup,
    pagedown, up, down, left, right, ctrl, shift, alt, win.
    Moving the text cursor: left/right/up/down, home/end (line), ctrl+home/ctrl+end (document),
    ctrl+left/ctrl+right (word); add shift to select, e.g. 'shift+end'."""
    combos = keys.split()
    for combo in combos:
        cb.parse_combo(combo)  # validate everything before pressing anything
    note = _focus(_resolve(window)) if window is not None else ""
    _guard()
    for combo in combos:
        cb.press_combo(combo)
        time.sleep(0.05)
    _log(f"press_keys {keys!r}")
    return f"Pressed {keys}.{note}"


@tool
def scroll(amount: int, x: int | None = None, y: int | None = None,
           window: str | int | None = None) -> str:
    """Scroll the mouse wheel `amount` notches (positive = up, negative = down).
    With `window`, x/y are client coordinates (default: the window's center); otherwise screen
    coordinates (default: the current mouse position)."""
    pos, note = None, ""
    if window is not None:
        hwnd = _resolve(window)
        _guard(hwnd)
        note = _focus(hwnd)
        if x is None or y is None:
            left, top, w, h = _client_rect(hwnd)
            pos = (left + w // 2, top + h // 2)
        else:
            pos = cb.client_to_screen(hwnd, x, y)
    else:
        if x is not None and y is not None:
            pos = (x, y)
        _guard(cb.root_of(cb.user32.WindowFromPoint(wt.POINT(*(pos or cb.cursor_pos())))))
    cb.scroll(pos, amount)
    _log(f"scroll {amount} at {pos}")
    return f"Scrolled {amount} notches.{note}"


def _capture(window):
    """Full-resolution image of a window's client area (brought to the front) or of the whole
    desktop -> (image, left, top, hwnd or None, note)."""
    note = ""
    if window is not None:
        hwnd = _resolve(window)
        if cb.root_of(cb.user32.GetForegroundWindow()) != hwnd:
            note = _focus(hwnd)
            time.sleep(0.2)
        left, top, w, h = _client_rect(hwnd)
    else:
        hwnd = None
        with mss.mss() as s:
            m = s.monitors[0]  # the whole virtual desktop
        left, top, w, h = m["left"], m["top"], m["width"], m["height"]
    if w <= 0 or h <= 0:
        raise ValueError("The window has no visible area (is it minimized?).")
    with mss.mss() as s:
        shot = s.grab({"left": left, "top": top, "width": w, "height": h})
    return PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), left, top, hwnd, note


def _shrink(img, max_width):
    scale = max(1.0, img.width / max(320, max_width))
    if scale > 1:
        img = img.resize((round(img.width / scale), round(img.height / scale)), PILImage.BILINEAR, reducing_gap=2.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return img, scale, Image(data=buf.getvalue(), format="jpeg")


@tool
def screenshot(window: str | int | None = None, max_width: int = 1280) -> list:
    """Capture the whole desktop, or one window's client area (brought to the front first).
    The reply explains how to turn image pixels into click() coordinates."""
    img, left, top, hwnd, note = _capture(window)
    img, scale, image = _shrink(img, max_width)
    if hwnd:
        how = (f"To click pixel (px, py) of this image: click(x=round(px*{scale:.4f}), "
               f"y=round(py*{scale:.4f}), window={hwnd}).")
    else:
        how = (f"To click pixel (px, py) of this image: click(x={left}+round(px*{scale:.4f}), "
               f"y={top}+round(py*{scale:.4f})).")
    _log(f"screenshot {'hwnd=' + str(hwnd) if hwnd else 'desktop'}")
    return [f"Screenshot {img.width}x{img.height}. {how}{note}", image]


def detect_elements(window: str | int | None = None, max_width: int = 1280, max_items: int = 120) -> list:
    """Find clickable things on screen by vision (OmniParser), for apps with no UI elements:
    games, custom-drawn apps, remote desktops. Returns a screenshot with numbered boxes and the
    click() point of each box. Needs setup_omniparser.cmd to have been run once."""
    from PIL import ImageDraw
    import omniparser

    img, left, top, hwnd, note = _capture(window)
    boxes = sorted(omniparser.detect(img)[:max_items], key=lambda b: (round(b[1] / 20), b[0]))  # reading order
    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    lines = []
    for i, (x0, y0, x1, y1, score) in enumerate(boxes):
        draw.rectangle((x0, y0, x1, y1), outline=(255, 0, 80), width=2)
        draw.rectangle((x0, y0, x0 + 9 * len(str(i)) + 6, y0 + 16), fill=(255, 0, 80))
        draw.text((x0 + 3, y0 + 2), str(i), fill="white")
        cx, cy = round((x0 + x1) / 2), round((y0 + y1) / 2)
        where = f"click(x={cx}, y={cy}, window={hwnd})" if hwnd else f"click(x={left + cx}, y={top + cy})"
        lines.append(f"[{i}] {round(x1 - x0)}x{round(y1 - y0)} -> {where}")
    _, _, image = _shrink(marked, max_width)
    _log(f"detect_elements {'hwnd=' + str(hwnd) if hwnd else 'desktop'}: {len(boxes)} boxes")
    head = f"Found {len(boxes)} clickable regions (numbered in the image). Click one with:{note}"
    return ["\n".join([head] + lines) if boxes else "No clickable regions found." + note, image]


if not FROZEN:  # the packaged exe leaves out torch (about 1 GB), so it has no vision detector
    detect_elements = tool(detect_elements)


@tool
def wait(ms: int) -> str:
    """Pause (max 30000 ms) to let the UI update, e.g. after opening a window."""
    time.sleep(max(0, min(ms, 30000)) / 1000)
    return f"Waited {ms} ms."


if __name__ == "__main__":
    mcp.run("stdio")
