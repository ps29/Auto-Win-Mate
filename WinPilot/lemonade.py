"""Keeps a local Lemonade Server running for WinPilot: starts it when a task needs it, loads the model,
restarts it if it dies, and stops it when WinPilot exits (only if WinPilot was the one that started it).

A server the user already runs is left alone: it is used, never stopped.
"""
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP = 0x08000000, 0x00000200
JOB_KILL_ON_CLOSE = 0x2000


def _request(url, body=None, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def find_server_exe():
    """LemonadeServer.exe: $LEMONADE_SERVER_EXE, then PATH, then the usual install folders."""
    candidates = [os.environ.get("LEMONADE_SERVER_EXE"), shutil.which("LemonadeServer")]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(str(Path(base) / "lemonade_server" / "bin" / "LemonadeServer.exe"))
            candidates.append(str(Path(base) / "Lemonade Server" / "bin" / "LemonadeServer.exe"))
    return next((c for c in candidates if c and Path(c).is_file()), None)


def _kill_with_parent(proc):
    """Put the server in a job object that dies with WinPilot, so a crash can't leave it running."""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.OpenProcess.restype = ctypes.c_void_p
        job = k32.CreateJobObjectW(None, None)

        class Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, only LimitFlags is used
            _fields_ = [("pad1", ctypes.c_byte * 16), ("LimitFlags", ctypes.c_uint32), ("pad2", ctypes.c_byte * 124)]
        limits = Limits()
        limits.LimitFlags = JOB_KILL_ON_CLOSE
        if not k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            return None
        handle = k32.OpenProcess(0x1F0FFF, False, proc.pid)
        k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return job  # keep it referenced: closing the handle (when WinPilot exits) ends the server
    except Exception:
        return None


class LemonadeServer:
    def __init__(self, url, popen=subprocess.Popen, exe_finder=find_server_exe):
        """`url` is the API base, e.g. http://localhost:13305/api/v1."""
        self.base = url.rstrip("/")
        self.host = (urlparse(self.base).hostname or "").lower()
        self.popen, self.exe_finder = popen, exe_finder
        self.proc, self.job = None, None
        self.lock = threading.Lock()
        self.wanted = threading.Event()  # set once a task has needed the server: keep it alive from then on

    @property
    def local(self):
        return self.host in ("localhost", "127.0.0.1", "::1")

    def health(self):
        """The server's /health answer, or None if it isn't responding."""
        try:
            return _request(self.base + "/health", timeout=3)
        except (OSError, ValueError):
            return None

    def alive(self):
        return self.health() is not None

    def ensure(self, model, status=lambda text: None, timeout=90):
        """Make sure the server answers and `model` is loaded. Raises RuntimeError with something actionable."""
        with self.lock:
            self.wanted.set()
            if not self.alive():
                if not self.local:
                    raise RuntimeError(f"Can't reach Lemonade at {self.base}. It's on another machine, so "
                                       "WinPilot can't start it. Is Lemonade Server running there?")
                self._start(status, timeout)
            self._load(model, status)

    def _start(self, status, timeout):
        exe = self.exe_finder()
        if not exe:
            raise RuntimeError("Lemonade Server isn't running and WinPilot couldn't find LemonadeServer.exe to "
                               "start it. Install Lemonade, or set LEMONADE_SERVER_EXE to its path.")
        status("Starting Lemonade server...")
        self.proc = self.popen([exe], creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=str(Path(exe).parent))
        self.job = _kill_with_parent(self.proc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.alive():
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"Lemonade Server exited right after starting (code {self.proc.returncode}). "
                                   "Another program may be using its port.")
            time.sleep(0.5)
        raise RuntimeError(f"Lemonade Server didn't answer within {timeout} s.")

    def _load(self, model, status):
        info = self.health() or {}
        if info.get("model_loaded") == model:
            return
        status(f"Loading {model}...")
        try:
            _request(self.base + "/load", {"model_name": model}, timeout=600)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Lemonade couldn't load '{model}' (HTTP {e.code}): {detail}") from e
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Lemonade couldn't load '{model}': {e}") from e

    def watch(self, model, interval=15):
        """Background thread: once a task has used the server, restart it if it dies. Returns the thread."""
        def run():
            while True:
                time.sleep(interval)
                if self.wanted.is_set() and not self.lock.locked() and not self.alive():
                    try:
                        self.ensure(model() if callable(model) else model)
                    except Exception:
                        pass  # the next task reports the problem
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def stop(self):
        """Stop the server if WinPilot started it. A server that was already running is left alone."""
        proc, self.proc = self.proc, None
        self.wanted.clear()
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
