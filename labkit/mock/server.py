"""Serve the mock API over real HTTP.

In-process labs use the mock through an httpx2 MockTransport (no sockets).  Some things
run in *another process* and only accept a base URL - the Claude Agent SDK's bundled
CLI, a Dockerized service, curl.  For those, run:

    python -m labkit.mock.server --port 8765
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_API_KEY=mock-key

or, from Python, `with running_mock_server() as base_url: ...`.
"""

from __future__ import annotations

import argparse
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import httpx2

from .api import get_mock_api


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "labkit-mock/1.0"

    def _dispatch(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        url = f"http://{self.headers.get('host', 'localhost')}{self.path}"
        request = httpx2.Request(self.command, url, headers=dict(self.headers.items()), content=body)
        response = get_mock_api().handle(request)
        payload = response.content
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _dispatch

    def log_message(self, fmt: str, *args: object) -> None:   # keep test output quiet
        pass


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


@contextlib.contextmanager
def running_mock_server(host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Start the mock on a background thread; yields its base URL."""
    server = make_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the labkit mock Claude API over HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"labkit mock API listening on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
