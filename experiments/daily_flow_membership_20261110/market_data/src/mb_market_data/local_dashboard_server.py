"""Supervised lifecycle for a local WSGI dashboard server."""

from __future__ import annotations

from threading import Event, Thread
from typing import Any

from werkzeug.serving import BaseWSGIServer, make_server


class LocalDashboardServer:
    """Serve a local WSGI application until an explicit stop is requested.

    Werkzeug runs in a worker thread so the Python main thread remains free to
    receive ``Ctrl+C`` on Windows.  A shared event also lets the browser request
    the same orderly shutdown path.
    """

    def __init__(
        self,
        wsgi_application: Any,
        *,
        host: str,
        port: int,
        stop_event: Event,
        poll_interval: float = 0.1,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._stop_event = stop_event
        self._poll_interval = poll_interval
        self._server: BaseWSGIServer = make_server(
            host,
            port,
            wsgi_application,
            threaded=True,
        )

    @property
    def bound_port(self) -> int:
        """Return the actual bound port, including an OS-selected port."""

        return int(self._server.server_port)

    def request_stop(self) -> None:
        """Request an orderly stop from any thread."""

        self._stop_event.set()

    def serve_until_stopped(self) -> None:
        """Serve until the shared event is set or ``Ctrl+C`` is received."""

        server_error: list[BaseException] = []

        def serve() -> None:
            try:
                self._server.serve_forever()
            except BaseException as exc:  # pragma: no cover - defensive path
                server_error.append(exc)
                self._stop_event.set()

        thread = Thread(
            target=serve,
            name="masterbot-dashboard-server",
            daemon=True,
        )
        thread.start()
        try:
            while thread.is_alive() and not self._stop_event.wait(
                self._poll_interval
            ):
                pass
        except KeyboardInterrupt:
            self._stop_event.set()
        finally:
            self._server.shutdown()
            self._server.server_close()
            thread.join(timeout=5.0)

        if thread.is_alive():  # pragma: no cover - severe platform failure
            raise RuntimeError("Dashboard server did not stop within 5 seconds")
        if server_error:
            raise RuntimeError("Dashboard server failed") from server_error[0]
