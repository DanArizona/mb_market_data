from __future__ import annotations

import json
import socket
import sys
import unittest
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, urlopen

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

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

    def test_real_dash_stop_callback_releases_http_listener(self) -> None:
        """Browser-confirmed Stop server must end WSGI and free the port.

        This is a loopback-only integration test, deliberately separate from
        verify_overlay_workspace.py (which forbids all network calls).
        It does not access Schwab, controller plans or quote journals.
        """
        from mb_market_data.quote_dashboard import create_quote_dashboard
        from test_quote_dashboard import single_event_controller

        stop_event = Event()
        app = create_quote_dashboard(
            single_event_controller(), request_server_stop=stop_event.set
        )
        server = LocalDashboardServer(
            app.server, host="127.0.0.1", port=0,
            stop_event=stop_event, poll_interval=0.01,
        )
        listener_port = server.bound_port
        thread = Thread(target=server.serve_until_stopped, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(stop_event.set)

        url = f"http://127.0.0.1:{listener_port}"
        with urlopen(url, timeout=3) as response:
            self.assertEqual(response.status, 200)

        request = Request(
            url + "/_dash-update-component",
            data=json.dumps({
                "output": "server-stop-status.children",
                "outputs": {
                    "id": "server-stop-status", "property": "children",
                },
                "changedPropIds": ["server-stop-confirm.submit_n_clicks"],
                "inputs": [{
                    "id": "server-stop-confirm", "property": "submit_n_clicks",
                    "value": 1,
                }],
                "state": [],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.load(response)["response"]["server-stop-status"]["children"],
                "Stopping…",
            )

        self.assertTrue(stop_event.wait(3.0))
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "Viewer WSGI thread is still running")
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", listener_port), timeout=0.25):
                pass

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
