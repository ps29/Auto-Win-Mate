r"""Unit test for lemonade.py (starting, loading, restarting and stopping Lemonade Server) against a fake
server. No real Lemonade or model needed.

Run from the WinPilot folder:
    ..\WinControlMCP\.venv\Scripts\python.exe tests\lemonade_test.py
"""
import ctypes
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lemonade  # noqa: E402


class Fake:
    """A stand-in Lemonade Server on a free local port."""

    def __init__(self):
        self.loaded, self.loads, self.httpd, self.port = None, [], None, None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/v1/health":
                    self._send(200, {"status": "ok", "model_loaded": fake.loaded})
                else:
                    self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/v1/load" and body.get("model_name") != "missing":
                    fake.loaded = body["model_name"]
                    fake.loads.append(fake.loaded)
                    self._send(200, {"status": "success"})
                else:
                    self._send(404, {"error": "no such model"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port or 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def kill(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.loaded = None


class FakeProc:
    pid, returncode = 999999, None

    def __init__(self, exits_at_once=False):
        self.returncode = 1 if exits_at_once else None

    def poll(self):
        return self.returncode


def manager(fake, exe="C:/fake/LemonadeServer.exe", exits_at_once=False):
    started = []

    def popen(cmd, **kw):
        started.append(cmd)
        if not exits_at_once:
            fake.start()
        return FakeProc(exits_at_once)

    lemonade._kill_with_parent = lambda proc: None
    m = lemonade.LemonadeServer(f"http://127.0.0.1:{fake.port}/api/v1", popen=popen, exe_finder=lambda: exe)
    return m, started


def reserve_port():
    f = Fake()
    f.start()
    port = f.port
    f.kill()
    return port


def test_starts_and_loads():
    fake = Fake()
    fake.port = reserve_port()
    m, started = manager(fake)
    seen = []
    assert not m.alive()
    m.ensure("model-a", seen.append, timeout=10)
    assert started == [["C:/fake/LemonadeServer.exe"]], started
    assert fake.loads == ["model-a"] and m.alive()
    assert seen == ["Starting Lemonade server...", "Loading model-a..."], seen
    m.ensure("model-a", seen.append)  # already up and loaded: nothing more to do
    assert len(started) == 1 and fake.loads == ["model-a"]
    m.ensure("model-b")  # a different model gets loaded
    assert fake.loads == ["model-a", "model-b"]
    killed = []
    real_run, lemonade.subprocess.run = lemonade.subprocess.run, lambda cmd, **kw: killed.append(cmd)
    try:
        m.stop()
    finally:
        lemonade.subprocess.run = real_run
    assert killed and killed[0][:3] == ["taskkill", "/T", "/F"], killed  # it started the server, so it stops it
    fake.kill()


def test_existing_server_is_left_alone():
    fake = Fake()
    fake.start()
    m, started = manager(fake)
    m.ensure("model-a")
    assert started == [] and fake.loads == ["model-a"]
    killed = []
    real_run, lemonade.subprocess.run = lemonade.subprocess.run, lambda cmd, **kw: killed.append(cmd)
    try:
        m.stop()
    finally:
        lemonade.subprocess.run = real_run
    assert killed == [] and m.alive()  # not ours: still running
    fake.kill()


def test_errors_are_actionable():
    fake = Fake()
    fake.port = reserve_port()
    m, _ = manager(fake, exe=None)
    try:
        m.ensure("model-a")
        raise AssertionError("no exe should fail")
    except RuntimeError as e:
        assert "LEMONADE_SERVER_EXE" in str(e), e
    m, _ = manager(fake, exits_at_once=True)
    try:
        m.ensure("model-a", timeout=5)
        raise AssertionError("an exiting server should fail")
    except RuntimeError as e:
        assert "exited right after starting" in str(e), e
    remote = lemonade.LemonadeServer("http://192.0.2.1:13305/api/v1", exe_finder=lambda: "x")
    try:
        remote.ensure("model-a")
        raise AssertionError("a remote server can't be started")
    except RuntimeError as e:
        assert "another machine" in str(e), e
    fake2 = Fake()
    fake2.start()
    m, _ = manager(fake2)
    try:
        m.ensure("missing")
        raise AssertionError("loading an unknown model should fail")
    except RuntimeError as e:
        assert "couldn't load 'missing'" in str(e) and "HTTP 404" in str(e), e
    fake2.kill()


def test_watchdog_restarts_a_dead_server():
    fake = Fake()
    fake.port = reserve_port()
    m, started = manager(fake)
    m.watch("model-a", interval=0.2)
    time.sleep(0.6)
    assert started == []  # nothing has asked for the server yet: leave it off
    m.ensure("model-a", timeout=10)
    assert len(started) == 1
    fake.kill()  # the server dies on its own
    deadline = time.time() + 10
    while len(started) < 2 and time.time() < deadline:
        time.sleep(0.2)
    assert len(started) == 2 and m.alive() and fake.loaded == "model-a", (started, fake.loaded)
    m.wanted.clear()
    fake.kill()


def test_job_object_ends_the_server_with_winpilot():
    k32 = ctypes.windll.kernel32
    import importlib
    importlib.reload(lemonade)  # undo the test stubs
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = lemonade._kill_with_parent(proc)
        assert job, "job object wasn't created"
        assert proc.poll() is None
        k32.CloseHandle(ctypes.c_void_p(job))  # what happens when WinPilot exits
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            raise AssertionError("the server outlived its job object")


if __name__ == "__main__":
    test_starts_and_loads()
    test_existing_server_is_left_alone()
    test_errors_are_actionable()
    test_watchdog_restarts_a_dead_server()
    test_job_object_ends_the_server_with_winpilot()
    print("PASS")
