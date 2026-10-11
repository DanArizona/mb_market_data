"""Scoped console stop handling for an interactive local viewer."""

from __future__ import annotations

import os
import signal
import sys
from threading import current_thread, main_thread
from typing import Callable


_retained_native_handlers = []


def _console_error(action):
    import ctypes
    error = ctypes.WinError(ctypes.get_last_error())
    error.add_note(action)
    return error


class _WindowsConsoleControl:
    """Own one Win32 callback; perform shutdown work only in the main loop."""

    def __init__(self, kernel, on_control, *, restore_ignore, handler_type):
        self.kernel = kernel
        self.restore_ignore = restore_ignore
        self.active = False
        self.event_count = 0

        def control(kind):
            if kind not in (0, 1):
                return 0  # preserve normal close/logoff/shutdown processing
            self.event_count += 1
            on_control(kind)
            return 1

        # Keep the ctypes callback alive until Windows confirms its removal.
        self.callback = handler_type(control)

    def install(self):
        if not self.kernel.SetConsoleCtrlHandler(self.callback, True):
            raise _console_error("Registering the viewer console handler")
        self.active = True
        # Win32's inheritable ignore-Ctrl+C flag is independent of Python's
        # signal-handler setting. This interactive viewer enables its own
        # Ctrl+C processing; existing processes are not affected.
        if not self.kernel.SetConsoleCtrlHandler(None, False):
            raise _console_error("Enabling the viewer's Ctrl+C processing")

    def close(self):
        if not self.active:
            return
        errors = []
        if self.restore_ignore and not self.kernel.SetConsoleCtrlHandler(None, True):
            errors.append(_console_error("Restoring the prior Python ignore-Ctrl+C policy"))
        if not self.kernel.SetConsoleCtrlHandler(self.callback, False):
            # Dropping the callback while Windows still holds its address could
            # crash the process on a later event. Retain it and report the error.
            if self not in _retained_native_handlers:
                _retained_native_handlers.append(self)
            errors.append(_console_error("Removing the viewer console handler"))
        else:
            self.active = False
            if self in _retained_native_handlers:
                _retained_native_handlers.remove(self)
        if errors:
            raise errors[0]


def _windows_control(on_control, restore_ignore):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    processes = (wintypes.DWORD * 1)()
    count = kernel.GetConsoleProcessList(processes, len(processes))
    if count == 0:
        if ctypes.get_last_error() == 6:  # no attached console; browser stop still works
            return None
        raise _console_error("Inspecting this viewer's console attachment")
    kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
    return _WindowsConsoleControl(kernel, on_control, restore_ignore=restore_ignore,
        handler_type=ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD))


def _keyboard_reader() -> tuple[Callable[[], bool], Callable[[], str]] | None:
    if os.name != "nt" or not sys.stdin.isatty():
        return None
    import msvcrt
    return msvcrt.kbhit, msvcrt.getwch


class ConsoleShutdown:
    """Restore previous handlers on exit; never lock inside a signal handler."""

    def __init__(self, *, keyboard: bool = True) -> None:
        self.reason: str | None = None
        self._keyboard_enabled = keyboard
        self._keyboard = None
        self._handlers: dict[int, object] = {}
        self._extended_key = False
        self._native = None

    def _interrupt(self, number, frame) -> None:
        if self.reason is None:
            self.reason = "Ctrl+C" if number == signal.SIGINT else "Ctrl+Break"

    def _native_control(self, kind):
        if self.reason is None:
            self.reason = "Ctrl+C" if kind == 0 else "Ctrl+Break"

    def __enter__(self):
        # Background HTTP-server tests and embedded callers must not change
        # process-global handlers or consume somebody else's console input.
        if current_thread() is not main_thread():
            return self
        try:
            for name in ("SIGINT", "SIGBREAK"):
                number = getattr(signal, name, None)
                if number is not None:
                    self._handlers[number] = signal.getsignal(number)
                    signal.signal(number, self._interrupt)
            self._native = _windows_control(self._native_control,
                self._handlers.get(signal.SIGINT) == signal.SIG_IGN)
            if self._native is not None:
                self._native.install()
            if self._keyboard_enabled:
                self._keyboard = _keyboard_reader()
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def poll(self) -> str | None:
        if self.reason is not None or self._keyboard is None:
            return self.reason
        available, read = self._keyboard
        while available():
            key = read()
            if self._extended_key:
                self._extended_key = False
                continue
            if key in ("\x00", "\xe0"):
                # A scan code may arrive on the next poll. PgDn's scan code
                # is Q; it must never be interpreted as the Q stop key.
                self._extended_key = True
            elif key.lower() == "q":
                self.reason = "Q key"
                break
            elif key == "\x03":
                self.reason = "Ctrl+C"
                break
        return self.reason

    def __exit__(self, exc_type=None, error=None, traceback=None):
        cleanup_errors = []
        for number, previous in self._handlers.items():
            try:
                signal.signal(number, previous)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        self._handlers.clear()
        self._keyboard = None
        if self._native is not None:
            try:
                self._native.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            if error is not None:
                for cleanup_error in cleanup_errors:
                    error.add_note(f"Console cleanup also failed: {cleanup_error}")
            else:
                raise cleanup_errors[0]

