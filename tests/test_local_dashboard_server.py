from __future__ import annotations

import unittest
import signal
import socket
from threading import Event, Thread, Timer
from unittest.mock import patch
from urllib.request import urlopen

from mb_market_data.local_dashboard_server import LocalDashboardServer


def simple_wsgi_application(environ, start_response):
    body = b"dashboard ready"
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


class TestLocalDashboardServer(unittest.TestCase):
    def test_console_error_still_closes_server_and_restores_handlers(self):
        server = LocalDashboardServer(simple_wsgi_application, host="127.0.0.1", port=0,
                                      stop_event=Event(), keyboard=False)
        before = signal.getsignal(signal.SIGINT)
        with patch("mb_market_data.local_dashboard_server.ConsoleShutdown.poll", side_effect=OSError("console unavailable")):
            with self.assertRaisesRegex(OSError, "console unavailable"):
                server.serve_until_stopped()
        self.assertEqual(signal.getsignal(signal.SIGINT), before)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", server.bound_port), timeout=5)

    def test_q_closes_actual_http_listener(self):
        server = LocalDashboardServer(simple_wsgi_application, host="127.0.0.1", port=0,
                                      stop_event=Event())
        keys = ["Q"]
        with patch("mb_market_data.console_shutdown._keyboard_reader", return_value=(lambda: bool(keys), lambda: keys.pop(0))):
            self.assertEqual(server.serve_until_stopped(), "Q key")
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", server.bound_port), timeout=5)

    def test_real_interrupt_closes_listener_and_restores_ignored_handler(self):
        stop_event = Event()
        server = LocalDashboardServer(simple_wsgi_application, host="127.0.0.1", port=0,
                                      stop_event=stop_event, keyboard=False)
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # raise_signal queues the interpreter's actual interrupt; it does not
        # send a console event to unrelated processes in the test runner's CMD.
        timer = Timer(0.1, lambda: signal.raise_signal(signal.SIGINT))
        fallback = Timer(3, stop_event.set)
        timer.start()
        fallback.start()
        try:
            self.assertEqual(server.serve_until_stopped(), "Ctrl+C")
        finally:
            timer.cancel()
            fallback.cancel()
            timer.join(2)
            fallback.join(2)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", server.bound_port), timeout=5)

    def test_serves_and_stops_cleanly_from_shared_event(self) -> None:
        stop_event = Event()
        server = LocalDashboardServer(
            simple_wsgi_application,
            host="127.0.0.1",
            port=0,
            stop_event=stop_event,
            poll_interval=0.01,
        )
        thread = Thread(target=server.serve_until_stopped)
        thread.start()
        self.addCleanup(stop_event.set)
        self.addCleanup(thread.join, 5.0)

        with urlopen(
            f"http://127.0.0.1:{server.bound_port}",
            timeout=2.0,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"dashboard ready")

        server.request_stop()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

    def test_rejects_nonpositive_poll_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            LocalDashboardServer(
                simple_wsgi_application,
                host="127.0.0.1",
                port=0,
                stop_event=Event(),
                poll_interval=0,
            )


if __name__ == "__main__":
    unittest.main()
