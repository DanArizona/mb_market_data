from __future__ import annotations

import ctypes
from ctypes import wintypes
import signal
from threading import Thread
import unittest
from unittest.mock import Mock, call, patch

from mb_market_data.console_shutdown import (
    ConsoleShutdown, _WindowsConsoleControl, _retained_native_handlers, _windows_control,
)


class ConsoleShutdownTests(unittest.TestCase):
    def setUp(self):
        # Signal/keyboard unit checks do not change native console attributes.
        native = patch("mb_market_data.console_shutdown._windows_control", return_value=None)
        native.start()
        self.addCleanup(native.stop)

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
        with patch("mb_market_data.console_shutdown.signal.signal") as install, patch("mb_market_data.console_shutdown._keyboard_reader") as keyboard, \
             patch("mb_market_data.console_shutdown._windows_control") as native:
            thread = Thread(target=run)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            install.assert_not_called()
            keyboard.assert_not_called()
            native.assert_not_called()
        self.assertEqual(result, [None])

    def test_native_control_works_without_keyboard_and_restores_python_handler(self):
        old = signal.getsignal(signal.SIGINT)
        kernel = Mock()
        kernel.SetConsoleCtrlHandler.return_value = 1
        native = None
        def factory(on_control, restore_ignore):
            nonlocal native
            native = _WindowsConsoleControl(kernel, on_control, restore_ignore=restore_ignore,
                                           handler_type=lambda callback: callback)
            return native
        with patch("mb_market_data.console_shutdown._windows_control", side_effect=factory):
            with ConsoleShutdown(keyboard=False) as stop:
                self.assertEqual(native.callback(0), 1)
                self.assertEqual(stop.poll(), "Ctrl+C")
        self.assertEqual(signal.getsignal(signal.SIGINT), old)
        self.assertFalse(native.active)

    def test_failed_native_enable_uninstalls_callback_and_restores_handlers(self):
        old = signal.getsignal(signal.SIGINT)
        kernel = Mock()
        kernel.SetConsoleCtrlHandler.side_effect = [1, 0, 1, 1]
        native = _WindowsConsoleControl(kernel, lambda kind: None,
                                       restore_ignore=False, handler_type=lambda callback: callback)
        with patch("mb_market_data.console_shutdown._windows_control", return_value=native), \
             patch("mb_market_data.console_shutdown._console_error", side_effect=lambda action: OSError(action)):
            with self.assertRaisesRegex(OSError, "Enabling"):
                with ConsoleShutdown(keyboard=False):
                    self.fail("failed setup entered its body")
        self.assertEqual(signal.getsignal(signal.SIGINT), old)
        self.assertFalse(native.active)
        kernel.SetConsoleCtrlHandler.assert_has_calls([
            call(native.callback, True), call(None, False), call(native.callback, False)])

    def test_cleanup_failure_does_not_mask_original_body_error(self):
        native = Mock()
        native.close.side_effect = OSError("synthetic cleanup failure")
        original = ValueError("synthetic viewer failure")
        with patch("mb_market_data.console_shutdown._windows_control", return_value=native):
            with self.assertRaises(ValueError) as raised:
                with ConsoleShutdown(keyboard=False):
                    raise original
        self.assertIs(raised.exception, original)
        self.assertIn("synthetic cleanup failure", " ".join(original.__notes__))
        native.close.assert_called_once()


class WindowsControlProtocolTests(unittest.TestCase):
    def setUp(self):
        errors = patch("mb_market_data.console_shutdown._console_error",
                       side_effect=lambda action: OSError(action))
        errors.start()
        self.addCleanup(errors.stop)

    def control(self, *, restore_ignore=False):
        kernel = Mock()
        kernel.SetConsoleCtrlHandler.return_value = 1
        observed = []
        native = _WindowsConsoleControl(kernel, observed.append, restore_ignore=restore_ignore,
                                       handler_type=lambda callback: callback)
        return kernel, observed, native

    def test_registers_before_enabling_and_unregisters_the_same_callback(self):
        kernel, observed, native = self.control()
        native.install()
        self.assertTrue(native.active)
        native.close()
        self.assertFalse(native.active)
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_args_list, [
            call(native.callback, True), call(None, False), call(native.callback, False)])
        native.close()
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_count, 3)

    def test_only_interrupts_are_consumed_without_doing_cleanup_in_callback(self):
        kernel, observed, native = self.control()
        native.install()
        for kind in (0, 1):
            self.assertEqual(native.callback(kind), 1)
        for kind in (2, 5, 6, 99):
            self.assertEqual(native.callback(kind), 0)
        self.assertEqual(observed, [0, 1])
        self.assertEqual(native.event_count, 2)
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_count, 2)
        native.close()

    def test_known_ignore_policy_is_restored_before_callback_is_removed(self):
        kernel, observed, native = self.control(restore_ignore=True)
        native.install()
        native.close()
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_args_list[-2:], [
            call(None, True), call(native.callback, False)])

    def test_registration_failure_does_not_enable_processing_or_remove_another_handler(self):
        kernel, observed, native = self.control()
        kernel.SetConsoleCtrlHandler.return_value = 0
        with self.assertRaisesRegex(OSError, "Registering"):
            native.install()
        native.close()
        kernel.SetConsoleCtrlHandler.assert_called_once_with(native.callback, True)
        self.assertFalse(native.active)

    def test_failed_ignore_restoration_still_removes_registered_callback(self):
        kernel, observed, native = self.control(restore_ignore=True)
        native.install()
        kernel.SetConsoleCtrlHandler.side_effect = [0, 1]
        with self.assertRaisesRegex(OSError, "Restoring"):
            native.close()
        self.assertFalse(native.active)
        self.assertEqual(kernel.SetConsoleCtrlHandler.call_args_list[-2:], [
            call(None, True), call(native.callback, False)])

    def test_failed_removal_retains_live_callback_until_successful_retry(self):
        kernel, observed, native = self.control()
        self.addCleanup(lambda: _retained_native_handlers.remove(native)
                        if native in _retained_native_handlers else None)
        native.install()
        kernel.SetConsoleCtrlHandler.return_value = 0
        with self.assertRaisesRegex(OSError, "Removing"):
            native.close()
        self.assertIn(native, _retained_native_handlers)
        self.assertTrue(native.active)
        self.assertEqual(native.callback(0), 1)
        self.assertEqual(observed, [0])
        kernel.SetConsoleCtrlHandler.return_value = 1
        native.close()
        self.assertNotIn(native, _retained_native_handlers)
        self.assertFalse(native.active)

    def native_factory(self, count, error=0):
        kernel = Mock()
        kernel.GetConsoleProcessList.return_value = count
        with patch("mb_market_data.console_shutdown.os.name", "nt"), \
             patch("ctypes.WinDLL", return_value=kernel, create=True), \
             patch("ctypes.get_last_error", return_value=error, create=True), \
             patch("ctypes.WINFUNCTYPE", return_value=lambda callback: callback, create=True):
            return kernel, _windows_control(lambda kind: None, False)

    def test_no_attached_console_preserves_browser_shutdown(self):
        kernel, native = self.native_factory(0, 6)
        self.assertIsNone(native)
        kernel.SetConsoleCtrlHandler.assert_not_called()

    def test_unknown_console_inspection_error_is_reported(self):
        with self.assertRaisesRegex(OSError, "Inspecting"):
            self.native_factory(0, 5)

    def test_attached_console_installs_even_when_stdin_is_not_a_terminal(self):
        with patch("mb_market_data.console_shutdown.sys.stdin.isatty", return_value=False):
            kernel, native = self.native_factory(2)
        self.assertIsInstance(native, _WindowsConsoleControl)
        kernel.SetConsoleCtrlHandler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
