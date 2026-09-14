from __future__ import annotations

import unittest
from threading import Event, Thread
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
