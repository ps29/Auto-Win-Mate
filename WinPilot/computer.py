"""Screenshot-and-coordinates desktop control for vision models.

Runs the actions of two local computer-use models, Fara-7B and UI-TARS-1.5, on the real desktop. Each model sees the desktop
as a resized screenshot; a Screen maps the model's pixel coordinates back to the desktop.

Every model's output is first turned into one plain action dict (see Computer.perform), so
approval, logging, the failsafe and the allowlist work the same for all of them.
Import after `server` (it provides ClickBot as `server.cb`).
"""
import ast
import base64
import io
import json
import math
import os
import re
import time
import urllib.parse
import ctypes.wintypes as wt

import mss
from PIL import Image

import server

cb = server.cb

# ---------------------------------------------------------------- screen geometry


def fara_fit(w, h):
    return 1428, 896  # the resolution Fara-7B's prompt promises it


def smart_resize(height, width, factor=28, min_pixels=100 * 28 * 28, max_pixels=1350 * 28 * 28):
    """UI-TARS / Qwen2.5-VL resizing: multiples of 28, within a pixel budget (README_coordinates.md)."""
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = math.floor(height / beta / factor) * factor
        w_bar = math.floor(width / beta / factor) * factor
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def uitars_fit(w, h):
    h2, w2 = smart_resize(h, w)
    return w2, h2


class Screen:
    """The whole virtual desktop as one model sees it: a screenshot resized by fit(w, h)."""

    def __init__(self, fit):
        self.fit = fit
        self.measure()

    def measure(self):
        with mss.mss() as s:
            m = s.monitors[0]
        self.left, self.top, self.width, self.height = m["left"], m["top"], m["width"], m["height"]
        self.size = self.fit(self.width, self.height)
        self.sx, self.sy = self.width / self.size[0], self.height / self.size[1]

    def to_screen(self, x, y):
        return self.left + round(float(x) * self.sx), self.top + round(float(y) * self.sy)

    def to_model(self, x, y):
        return round((x - self.left) / self.sx), round((y - self.top) / self.sy)

    def grab(self):
        self.measure()
        with mss.mss() as s:
            shot = s.grab({"left": self.left, "top": self.top, "width": self.width, "height": self.height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def screenshot(self):
        """(mime, base64) of the desktop at the model's size."""
        img = self.grab().resize(self.size, Image.BILINEAR, reducing_gap=2.0)
        return encode(img, "JPEG")

    def zoom(self, region):
        """(mime, base64) of a region (model coordinates) at full screen resolution."""
        x0, y0, x1, y1 = (float(v) for v in region)
        (left, top), (right, bottom) = self.to_screen(min(x0, x1), min(y0, y1)), self.to_screen(max(x0, x1), max(y0, y1))
        img = self.grab().crop((left - self.left, top - self.top, max(right, left + 1) - self.left,
                                max(bottom, top + 1) - self.top))
        img.thumbnail((2576, 2576))
        return encode(img, "PNG")


def encode(img, fmt):
    buf = io.BytesIO()
    img.save(buf, fmt, **({"quality": 80} if fmt == "JPEG" else {}))
    return f"image/{fmt.lower()}", base64.b64encode(buf.getvalue()).decode()

# ---------------------------------------------------------------- key names

KEY_ALIASES = {"return": "enter", "kp_enter": "enter", "super": "win", "cmd": "win", "meta": "win",
               "windows": "win", "command": "win", "control": "ctrl", "option": "alt", "escape": "esc",
               "page_up": "pageup", "prior": "pageup", "page_down": "pagedown", "next": "pagedown",
               "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
               "back_space": "backspace", "del": "delete", "spacebar": "space", "ins": "insert"}


def normalize_keys(keys):
    """'ctrl+s', 'Return', 'ctrl c' or ['Control', 'a'] -> 'ctrl+s' style for clickbot."""
    if isinstance(keys, (list, tuple)):
        parts = [str(k) for k in keys]
    else:
        keys = str(keys).strip()
        parts = keys.split("+") if "+" in keys.strip("+") else keys.split()
    out = []
    for p in parts:
        p = p.strip()
        out.append(KEY_ALIASES.get(p.lower(), p.lower() if len(p) > 1 else p))
    return "+".join(p for p in out if p)

# ---------------------------------------------------------------- one action format for every model

INPUT_KINDS = {"click", "drag", "mouse_down", "mouse_up", "scroll", "type", "key", "hold", "open_url"}
# ---- Fara-7B (prompt from the Hugging Face model card, Qwen2.5-VL tool-call format)


FARA_SYSTEM = """You are a web automation agent that performs actions on websites to fulfill user requests by calling various tools.

You should stop execution at Critical Points. A Critical Point occurs in tasks like:
- Checkout
- Book
- Purchase
- Call
- Email
- Order

A Critical Point requires the user's permission or personal/sensitive information (name, email, credit card, address, payment information, resume, etc.) to complete a transaction (purchase, reservation, sign-up, etc.), or to communicate as a human would (call, email, apply to a job, etc.).

Guideline: Solve the task as far as possible up until a Critical Point.

Examples:
- If the task is to "call a restaurant to make a reservation," do not actually make the call. Instead, navigate to the restaurant's page and find the phone number.
- If the task is to "order new size 12 running shoes," do not place the order. Instead, search for the right shoes that meet the criteria and add them to the cart.

Some tasks, like answering questions, may not encounter a Critical Point at all.

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name": "computer_use", "description": "Use a mouse and keyboard to interact with a computer, and take screenshots.\\n* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must click on desktop icons to start applications.\\n* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions. E.g. if you click on Firefox and a window doesn't open, try wait and taking another screenshot.\\n* The screen's resolution is 1428x896.\\n* Whenever you intend to move the cursor to click on an element like an icon, you should consult a screenshot to determine the coordinates of the element before moving the cursor.\\n* If you tried clicking on a program or link but it failed to load, even after waiting, try adjusting your cursor position so that the tip of the cursor visually falls on the element that you want to click.\\n* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.\\n* When a separate scrollable container prominently overlays the webpage, if you want to scroll within it, you typically need to mouse_move() over it first and then scroll().\\n* If a popup window appears that you want to close, if left_click() on the 'X' or close button doesn't work, try key(keys=['Escape']) to close it.\\n* On some search bars, when you type(), you may need to press_enter=False and instead separately call left_click() on the search button to submit the search query. This is especially true of search bars that have auto-suggest popups for e.g. locations\\n* For calendar widgets, you usually need to left_click() on arrows to move between months and left_click() on dates to select them; type() is not typically used to input dates there.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:\\n* key: Performs key down presses on the arguments passed in order, then performs key releases in reverse order. Includes 'Enter', 'Alt', 'Shift', 'Tab', 'Control', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageDown', 'PageUp', 'Shift', etc.\\n* type: Type a string of text on the keyboard.\\n* mouse_move: Move the cursor to a specified (x, y) pixel coordinate on the screen.\\n* left_click: Click the left mouse button.\\n* scroll: Performs a scroll of the mouse scroll wheel.\\n* visit_url: Visit a specified URL.\\n* web_search: Perform a web search with a specified query.\\n* history_back: Go back to the previous page in the browser history.\\n* pause_and_memorize_fact: Pause and memorize a fact for future reference.\\n* wait: Wait specified seconds for the change to happen.\\n* terminate: Terminate the current task and report its completion status.", "enum": ["key", "type", "mouse_move", "left_click", "scroll", "visit_url", "web_search", "history_back", "pause_and_memorize_fact", "wait", "terminate"], "type": "string"}, "keys": {"description": "Required only by action=key.", "type": "array"}, "text": {"description": "Required only by action=type.", "type": "string"}, "coordinate": {"description": "(x, y) coordinates for mouse actions. Required only by action=left_click, action=mouse_move, and action=type.", "type": "array"}, "pixels": {"description": "Amount of scrolling. Positive = up, Negative = down. Required only by action=scroll.", "type": "number"}, "url": {"description": "The URL to visit. Required only by action=visit_url.", "type": "string"}, "query": {"description": "The query to search for. Required only by action=web_search.", "type": "string"}, "fact": {"description": "The fact to remember for the future. Required only by action=pause_and_memorize_fact.", "type": "string"}, "time": {"description": "Seconds to wait. Required only by action=wait.", "type": "number"}, "status": {"description": "Status of the task. Required only by action=terminate.", "type": "string", "enum": ["success", "failure"]}}, "required": ["action"], "type": "object"}}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>"""


def parse_fara(text):
    """Fara-7B output -> (thought, [action dicts]). Raises ValueError if there is no tool call."""
    calls = re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.S)
    if not calls:
        raise ValueError("No <tool_call> in the model's reply.")
    thought = text[:text.index("<tool_call>")].strip()
    actions = []
    for raw in calls:
        a = json.loads(raw).get("arguments", {})
        kind = a.get("action")
        if kind == "left_click":
            actions.append({"kind": "click", "point": a.get("coordinate"), "button": "left", "clicks": 1})
        elif kind == "mouse_move":
            actions.append({"kind": "move", "point": a["coordinate"]})
        elif kind == "type":
            if a.get("coordinate"):
                actions.append({"kind": "click", "point": a["coordinate"], "button": "left", "clicks": 1})
            enter = "\n" if a.get("press_enter", True) else ""  # Fara submits typed text unless told not to
            actions.append({"kind": "type", "text": str(a.get("text", "")) + enter})
        elif kind == "key":
            actions.append({"kind": "key", "keys": a.get("keys") or [], "repeat": 1})
        elif kind == "scroll":
            pixels = float(a.get("pixels") or 0)
            actions.append({"kind": "scroll", "point": a.get("coordinate"), "direction": "up" if pixels > 0 else "down",
                            "amount": max(1, round(abs(pixels) / 100))})
        elif kind == "visit_url":
            actions.append({"kind": "open_url", "url": a["url"]})
        elif kind == "web_search":
            actions.append({"kind": "open_url",
                            "url": "https://www.bing.com/search?q=" + urllib.parse.quote_plus(a["query"])})
        elif kind == "history_back":
            actions.append({"kind": "key", "keys": "alt+left", "repeat": 1})
        elif kind == "wait":
            actions.append({"kind": "wait", "duration": float(a.get("time") or 2)})
        elif kind == "pause_and_memorize_fact":
            actions.append({"kind": "note", "text": a.get("fact", "")})
        elif kind == "terminate":
            actions.append({"kind": "finish", "text": a.get("status", "success")})
        else:
            raise ValueError(f"Unknown Fara action '{kind}'.")
    return thought, actions

# ---- UI-TARS-1.5 (prompt from codes/ui_tars/prompt.py, Apache-2.0)


UITARS_PROMPT = r"""You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space

click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='ctrl c') # Split keys with a space and use lowercase. Also, do not use more than 3 keys in one hotkey action.
type(content='xxx') # Use escape characters \', \", and \n in content part to ensure we can parse the content in normal python string format. If you want to submit your input, use \n at the end of content.
scroll(point='<point>x1 y1</point>', direction='down or up or right or left') # Show more information on the `direction` side.
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished(content='xxx') # Use escape characters \', \", and \n in content part to ensure we can parse the content in normal python string format.


## Note
- Use English in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
"""


def _point(value):
    """'<point>x y</point>', '(x,y)', '<|box_start|>(x,y)<|box_end|>' or '[x1,y1,x2,y2]' -> [x, y]."""
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", value)]
    if len(nums) >= 4:
        return [(nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2]
    if len(nums) >= 2:
        return nums[:2]
    raise ValueError(f"Can't read a point from '{value}'.")


def parse_uitars(text):
    """UI-TARS output -> (thought, [action dict]). Raises ValueError if there is no action."""
    m = re.search(r"Action:\s*(\w+)\((.*)\)\s*$", text.strip(), re.S)
    if not m:
        raise ValueError("No 'Action: ...' line in the model's reply.")
    thought = re.sub(r"^Thought:\s*", "", text[:m.start()].strip())
    name, raw_args = m[1], m[2]
    args = {k: ast.literal_eval(v) for k, v in re.findall(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")",
                                                            raw_args, re.S)}
    point = args.get("point") or args.get("start_box") or args.get("start_point")
    if name in ("click", "left_single"):
        return thought, [{"kind": "click", "point": _point(point), "button": "left", "clicks": 1}]
    if name == "left_double":
        return thought, [{"kind": "click", "point": _point(point), "button": "left", "clicks": 2}]
    if name == "right_single":
        return thought, [{"kind": "click", "point": _point(point), "button": "right", "clicks": 1}]
    if name in ("drag", "select"):
        end = args.get("end_point") or args.get("end_box")
        return thought, [{"kind": "drag", "start": _point(point), "point": _point(end)}]
    if name == "hotkey":
        return thought, [{"kind": "key", "keys": args.get("key", ""), "repeat": 1}]
    if name == "type":
        return thought, [{"kind": "type", "text": args.get("content", "")}]
    if name == "scroll":
        return thought, [{"kind": "scroll", "point": _point(point) if point else None,
                          "direction": args.get("direction", "down"), "amount": 5}]
    if name == "wait":
        return thought, [{"kind": "wait", "duration": 5}]
    if name == "finished":
        return thought, [{"kind": "finish", "text": args.get("content", "")}]
    raise ValueError(f"Unknown UI-TARS action '{name}'.")

# ---------------------------------------------------------------- execution


def _window_at(pos):
    hwnd = cb.root_of(cb.user32.WindowFromPoint(wt.POINT(*pos)))
    return hwnd, (cb.window_title(hwnd) or cb.process_name(hwnd))


def describe(act, screen):
    """One line telling the user what an action will do, in screen coordinates."""
    k = act["kind"]

    def at(point):
        if point is None:
            return "at the mouse pointer"
        pos = screen.to_screen(*point)
        return f"at {pos} on '{_window_at(pos)[1]}'"

    mods = f" holding {normalize_keys(act['mods'])}" if act.get("mods") else ""
    if k == "click":
        what = {1: "click", 2: "double-click", 3: "triple-click"}[act.get("clicks", 1)]
        return f"{act.get('button', 'left').title()} {what} {at(act.get('point'))}{mods}"
    if k == "drag":
        return f"Drag from {screen.to_screen(*act['start'])} to {at(act['point'])}{mods}"
    if k == "move":
        return f"Move the mouse {at(act['point'])}"
    if k in ("mouse_down", "mouse_up"):
        return f"{'Press' if k == 'mouse_down' else 'Release'} the left mouse button {at(None)}"
    if k == "scroll":
        return f"Scroll {act['direction']} {act['amount']} {at(act.get('point'))}{mods}"
    if k == "type":
        return f'Type "{act["text"][:120]}"'
    if k == "key":
        times = f" {act['repeat']} times" if act.get("repeat", 1) > 1 else ""
        return f"Press {normalize_keys(act['keys'])}{times}"
    if k == "hold":
        return f"Hold {normalize_keys(act['keys'])} for {act['duration']} s"
    if k == "open_url":
        return f"Open {act['url']}"
    return {"screenshot": "Take a screenshot", "cursor": "Check the mouse position",
            "zoom": f"Zoom into {act.get('region')}", "wait": f"Wait {act.get('duration')} s",
            "note": f"Remember: {act.get('text', '')[:80]}", "finish": "Finish"}.get(k, k)


def perform(act, screen):
    """Run one action. Returns (text, image) where image is (mime, base64) or None.
    Raises on failure (failsafe, allowlist, blocked input, bad arguments)."""
    k = act["kind"]
    point = screen.to_screen(*act["point"]) if act.get("point") is not None else None
    if k in INPUT_KINDS or k == "move":
        if k in ("type", "key", "hold", "open_url"):
            server._guard()
        else:
            server._guard(_window_at(point or cb.cursor_pos())[0])
    if k == "screenshot":
        return "OK", screen.screenshot()
    if k == "zoom":
        return "OK", screen.zoom(act["region"])
    if k == "cursor":
        x, y = screen.to_model(*cb.cursor_pos())
        return f"({x}, {y})", None
    if k == "wait":
        time.sleep(max(0.0, min(float(act["duration"]), 30)))
        return "OK", None
    if k in ("note", "finish"):
        return "OK", None
    held = cb.keys_down(normalize_keys(act["mods"])) if act.get("mods") else []
    try:
        if k == "click":
            cb.click(point, act.get("button", "left"), act.get("clicks", 1))
        elif k == "drag":
            cb.drag(screen.to_screen(*act["start"]), point)
        elif k == "move":
            cb.move_to(point)
        elif k in ("mouse_down", "mouse_up"):
            cb.mouse_button("left", k == "mouse_down")
        elif k == "scroll":
            sign = 1 if act["direction"] in ("up", "right") else -1
            if act["direction"] in ("left", "right"):
                cb.hscroll(point, sign * act["amount"])
            else:
                cb.scroll(point, sign * act["amount"])
        elif k == "type":
            cb.type_text(act["text"])
        elif k == "key":
            combo = normalize_keys(act["keys"])
            for _ in range(max(1, min(int(act.get("repeat", 1)), 100))):
                cb.press_combo(combo)
                time.sleep(0.03)
        elif k == "hold":
            hold = cb.keys_down(normalize_keys(act["keys"]))
            try:
                time.sleep(max(0.0, min(float(act["duration"]), 30)))
            finally:
                cb.keys_up(hold)
        elif k == "open_url":
            if not re.match(r"https?://", act["url"]):
                raise ValueError("Only http(s) URLs can be opened.")
            os.startfile(act["url"])
        else:
            raise ValueError(f"Unknown action '{k}'.")
    finally:
        cb.keys_up(held)
    server._log(f"vision {k} {json.dumps({x: v for x, v in act.items() if x != 'kind'})[:200]}")
    return "OK", None
