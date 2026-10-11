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
from unittest.mock import Mock, patch


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
import ctypes, os, sys
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.AttachConsole.argtypes = [wintypes.DWORD]
k.AttachConsole.restype = wintypes.BOOL
k.FreeConsole.argtypes = []
k.FreeConsole.restype = wintypes.BOOL
k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
k.GetConsoleProcessList.restype = wintypes.DWORD
k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
k.SetConsoleCtrlHandler.restype = wintypes.BOOL
target_pid = int(sys.argv[1])
own_pid = os.getpid()
if target_pid <= 0 or target_pid == own_pid:
    raise ValueError("Expected the positive PID of the owned test child")
# CREATE_NO_WINDOW can still leave this helper attached to a console. Detach
# only this helper before attaching; never detach the test runner or pollers.
if not k.FreeConsole():
    raise ctypes.WinError(ctypes.get_last_error())
if not k.AttachConsole(target_pid):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    processes = (wintypes.DWORD * 16)()
    count = k.GetConsoleProcessList(processes, len(processes))
    if count == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if count > len(processes) or set(processes[:count]) != {target_pid, own_pid}:
        raise RuntimeError("Refusing Ctrl+C: console is not exclusively the owned test child and helper")
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


class ConsoleEmitterHarnessTests(unittest.TestCase):
    """Exercise the actual helper script's API order and refusal paths."""

    def kernel(self, members=(101, 202)):
        kernel = Mock()
        for name in ("FreeConsole", "AttachConsole", "SetConsoleCtrlHandler", "GenerateConsoleCtrlEvent"):
            getattr(kernel, name).return_value = 1
        def processes(buffer, size):
            for index, pid in enumerate(members[:size]):
                buffer[index] = pid
            return len(members)
        kernel.GetConsoleProcessList.side_effect = processes
        return kernel

    def execute(self, kernel, target="101"):
        with patch("ctypes.WinDLL", return_value=kernel, create=True), \
             patch("ctypes.WinError", side_effect=lambda code: OSError(f"synthetic WinError {code}"), create=True), \
             patch("ctypes.get_last_error", return_value=5, create=True), \
             patch("os.getpid", return_value=202), \
             patch("sys.argv", ["owned-console-emitter", target]), \
             patch("time.sleep"):
            exec(compile(EMITTER, "owned-console-emitter", "exec"), {})

    def test_detaches_before_attachment_and_sends_only_after_membership_check(self):
        kernel = self.kernel()
        self.execute(kernel)
        calls = [call[0] for call in kernel.mock_calls]
        self.assertEqual(calls, ["FreeConsole", "AttachConsole", "GetConsoleProcessList",
                                 "SetConsoleCtrlHandler", "GenerateConsoleCtrlEvent", "FreeConsole"])
        kernel.AttachConsole.assert_called_once_with(101)
        kernel.GenerateConsoleCtrlEvent.assert_called_once_with(0, 0)

    def test_detach_failure_prevents_attachment_and_event(self):
        kernel = self.kernel()
        kernel.FreeConsole.return_value = 0
        with self.assertRaisesRegex(OSError, "synthetic WinError"):
            self.execute(kernel)
        kernel.AttachConsole.assert_not_called()
        kernel.GenerateConsoleCtrlEvent.assert_not_called()

    def test_attachment_failure_prevents_event(self):
        kernel = self.kernel()
        kernel.AttachConsole.return_value = 0
        with self.assertRaisesRegex(OSError, "synthetic WinError"):
            self.execute(kernel)
        kernel.GenerateConsoleCtrlEvent.assert_not_called()

    def test_foreign_console_process_refuses_event_and_detaches_helper(self):
        kernel = self.kernel((101, 202, 303))
        with self.assertRaisesRegex(RuntimeError, "not exclusively"):
            self.execute(kernel)
        kernel.GenerateConsoleCtrlEvent.assert_not_called()
        self.assertEqual(kernel.FreeConsole.call_count, 2)

    def test_oversized_process_list_refuses_event(self):
        kernel = self.kernel(tuple(range(32)))
        with self.assertRaisesRegex(RuntimeError, "not exclusively"):
            self.execute(kernel)
        kernel.GenerateConsoleCtrlEvent.assert_not_called()
        self.assertEqual(kernel.FreeConsole.call_count, 2)

    def test_process_list_read_failure_refuses_event(self):
        kernel = self.kernel()
        kernel.GetConsoleProcessList.side_effect = None
        kernel.GetConsoleProcessList.return_value = 0
        with self.assertRaisesRegex(OSError, "synthetic WinError"):
            self.execute(kernel)
        kernel.GenerateConsoleCtrlEvent.assert_not_called()
        self.assertEqual(kernel.FreeConsole.call_count, 2)

    def test_ignore_handler_failure_refuses_event(self):
        kernel = self.kernel()
        kernel.SetConsoleCtrlHandler.return_value = 0
        with self.assertRaisesRegex(OSError, "synthetic WinError"):
            self.execute(kernel)
        kernel.GenerateConsoleCtrlEvent.assert_not_called()
        self.assertEqual(kernel.FreeConsole.call_count, 2)

    def test_parent_console_alias_and_own_pid_are_refused_before_detach(self):
        for target in ("-1", "0", "202"):
            with self.subTest(target=target):
                kernel = self.kernel()
                with self.assertRaisesRegex(ValueError, "positive PID"):
                    self.execute(kernel, target=target)
                kernel.FreeConsole.assert_not_called()
                kernel.AttachConsole.assert_not_called()
                kernel.GenerateConsoleCtrlEvent.assert_not_called()


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
