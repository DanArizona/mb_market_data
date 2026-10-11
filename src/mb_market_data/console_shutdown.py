"""Scoped console stop handling for an interactive local viewer."""

from __future__ import annotations

import os
import signal
import sys
from threading import current_thread, main_thread
from typing import Callable


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

    def _interrupt(self, number, frame) -> None:
        if self.reason is None:
            self.reason = "Ctrl+C" if number == signal.SIGINT else "Ctrl+Break"

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
            if self._keyboard_enabled:
                self._keyboard = _keyboard_reader()
        except BaseException:
            self.__exit__(None, None, None)
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

    def __exit__(self, *args):
        for number, previous in self._handlers.items():
            signal.signal(number, previous)
        self._handlers.clear()
        self._keyboard = None

