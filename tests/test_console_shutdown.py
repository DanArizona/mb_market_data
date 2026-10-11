from __future__ import annotations

import signal
from threading import Thread
import unittest
from unittest.mock import patch

from mb_market_data.console_shutdown import ConsoleShutdown


class ConsoleShutdownTests(unittest.TestCase):
    def test_ignored_interrupt_is_overridden_and_restored(self):
        old = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with ConsoleShutdown(keyboard=False) as stop:
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertEqual(stop.poll(), "Ctrl+C")
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)

    def test_q_and_raw_ctrl_c_have_distinct_stop_reasons(self):
        for key, expected in (("Q", "Q key"), ("\x03", "Ctrl+C")):
            with self.subTest(key=key):
                keys = [key]
                with patch("mb_market_data.console_shutdown._keyboard_reader", return_value=(lambda: bool(keys), lambda: keys.pop(0))):
                    with ConsoleShutdown() as stop:
                        self.assertEqual(stop.poll(), expected)

    def test_extended_page_down_scan_code_is_not_q_even_across_polls(self):
        keys = ["\xe0"]
        with patch("mb_market_data.console_shutdown._keyboard_reader", return_value=(lambda: bool(keys), lambda: keys.pop(0))):
            with ConsoleShutdown() as stop:
                self.assertIsNone(stop.poll())
                keys.append("Q")
                self.assertIsNone(stop.poll())
                keys.append("q")
                self.assertEqual(stop.poll(), "Q key")

    def test_background_thread_never_changes_handlers_or_reads_keyboard(self):
        result = []
        def run():
            with ConsoleShutdown() as stop:
                result.append(stop.poll())
        with patch("mb_market_data.console_shutdown.signal.signal") as install, patch("mb_market_data.console_shutdown._keyboard_reader") as keyboard:
            thread = Thread(target=run)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            install.assert_not_called()
            keyboard.assert_not_called()
        self.assertEqual(result, [None])


if __name__ == "__main__":
    unittest.main()
