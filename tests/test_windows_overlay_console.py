"""Actual Windows console event test, scoped to a fresh owned child console."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen


CHILD = r'''
import json, signal, sys
from pathlib import Path
from threading import Event, Timer
from mb_market_data.local_dashboard_server import LocalDashboardServer

def app(environ, start):
    start("200 OK", [("Content-Length", "2")])
    return [b"ok"]

root = Path(sys.argv[1])
signal.signal(signal.SIGINT, signal.SIG_IGN)
stop = Event()
server = LocalDashboardServer(app, host="127.0.0.1", port=0, stop_event=stop, keyboard=False)
fallback = Timer(20, stop.set)
fallback.start()
with (root / "ready.json").open("x") as out:
    json.dump({"pid": __import__("os").getpid(), "port": server.bound_port}, out)
try:
    reason = server.serve_until_stopped()
    with (root / "stopped.json").open("x") as out:
        json.dump({"reason": reason, "prior_ignore_restored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN}, out)
finally:
    fallback.cancel()
    fallback.join(2)
'''

EMITTER = r'''
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.AttachConsole.argtypes = [wintypes.DWORD]
k.AttachConsole.restype = wintypes.BOOL
k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
k.SetConsoleCtrlHandler.restype = wintypes.BOOL
if not k.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    # Ignore the emitter's copy of the event. It is attached only to the new
    # child console; no event is broadcast in the user's production CMD.
    if not k.SetConsoleCtrlHandler(None, True):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.GenerateConsoleCtrlEvent(0, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    __import__("time").sleep(0.2)
finally:
    k.FreeConsole()
'''


@unittest.skipUnless(os.name == "nt", "requires a native Windows console")
class WindowsOverlayConsoleTests(unittest.TestCase):
    def test_actual_ctrl_c_event_closes_owned_viewer_and_releases_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = subprocess.Popen([sys.executable, "-c", CHILD, str(root)],
                                     creationflags=subprocess.CREATE_NEW_CONSOLE)
            try:
                deadline = time.monotonic() + 10
                ready = None
                while time.monotonic() < deadline and child.poll() is None:
                    try:
                        ready = json.loads((root / "ready.json").read_text())
                        break
                    except (OSError, ValueError):
                        time.sleep(0.05)
                self.assertIsNotNone(ready, "owned synthetic server never became ready")
                self.assertEqual(ready["pid"], child.pid)
                # An HTTP response proves serve_forever is running, after the
                # scoped interrupt handlers were installed. A TCP handshake
                # alone would succeed at construction before that point.
                with urlopen(f"http://127.0.0.1:{ready['port']}/", timeout=5) as response:
                    self.assertEqual(response.read(), b"ok")
                emitter = subprocess.run([sys.executable, "-c", EMITTER, str(child.pid)],
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, capture_output=True)
                self.assertEqual(emitter.returncode, 0, emitter.stderr.decode(errors="replace"))
                self.assertEqual(child.wait(timeout=8), 0)
                result = json.loads((root / "stopped.json").read_text())
                self.assertEqual(result["reason"], "Ctrl+C")
                self.assertTrue(result["prior_ignore_restored"])
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(("127.0.0.1", ready["port"]), timeout=5)
            finally:
                if child.poll() is None:
                    child.terminate()  # only the exact Popen child created here
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
