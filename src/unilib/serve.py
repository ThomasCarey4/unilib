"""Serve the MCP server over streamable HTTP, behind a bearer-token check and an idle exit.

Run under systemd socket activation, the server starts on the first connection and exits
once it has been idle for `UNILIB_IDLE_EXIT_S` seconds, so the models only hold memory
while the library is in use; systemd keeps the socket and starts it again on demand.
The transport is stateless, so a client never holds a session that an idle exit could strand.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import socket
import sys
import time
from pathlib import Path
from typing import Callable

DEFAULT_PORT = 8095
DEFAULT_IDLE_EXIT_S = 900.0
SD_LISTEN_FDS_START = 3  # first fd systemd passes (sd_listen_fds(3))


class Gate:
    """ASGI middleware: rejects HTTP requests without the bearer token (when one is set) and
    records activity, so `idle_for` is 0 while a request is in flight."""

    def __init__(self, app, token: str | None, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self._expected = f"Bearer {token}".encode() if token else None
        self._clock = clock
        self._last = clock()
        self._inflight = 0

    def idle_for(self) -> float:
        return 0.0 if self._inflight else self._clock() - self._last

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan: starts the MCP session manager
            return await self.app(scope, receive, send)
        if self._expected is not None:
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized\n"})
                return
        self._inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._inflight -= 1
            self._last = self._clock()


async def exit_when_idle(gate: Gate, server, idle_s: float, poll_s: float | None = None) -> None:
    """Set `server.should_exit` once `gate` has been idle for `idle_s` seconds."""
    poll_s = poll_s if poll_s is not None else min(30.0, idle_s / 4)
    while not server.should_exit:
        if gate.idle_for() >= idle_s:
            print(f"unilib-mcp: idle for {idle_s:.0f} s, exiting", file=sys.stderr)
            server.should_exit = True
            return
        await asyncio.sleep(poll_s)


def systemd_socket() -> socket.socket | None:
    """The listening socket passed by systemd socket activation, if any.

    LISTEN_PID may name the parent: `uv run` starts this interpreter as its child."""
    if (os.environ.get("LISTEN_PID") not in (str(os.getpid()), str(os.getppid()))
            or int(os.environ.get("LISTEN_FDS", "0")) < 1):
        return None
    return socket.socket(fileno=SD_LISTEN_FDS_START)  # family and type are read from the fd


def _is_loopback(sock: socket.socket) -> bool:
    try:
        return ipaddress.ip_address(sock.getsockname()[0]).is_loopback
    except (ValueError, IndexError, TypeError):
        return False


def read_token() -> str | None:
    path = os.environ.get("UNILIB_TOKEN_FILE")
    if not path:
        return None
    token = Path(path).expanduser().read_text().strip()
    if not token:
        raise SystemExit(f"unilib-mcp: {path} is empty")
    return token


def serve_http(mcp, host: str, port: int, idle_s: float | None) -> None:
    """Serve `mcp` on the systemd socket if there is one, else on host:port. idle_s=None never exits."""
    import uvicorn

    sock = systemd_socket()
    if sock is None:
        sock = socket.create_server((host, port), family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    token = read_token()
    if token is None and not _is_loopback(sock):
        raise SystemExit("unilib-mcp: set UNILIB_TOKEN_FILE to serve beyond loopback")
    # host="0.0.0.0": no DNS-rebinding Host check; the bearer token guards the endpoint instead.
    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  host="127.0.0.1" if token is None else "0.0.0.0")
    gate = Gate(app, token)
    server = uvicorn.Server(uvicorn.Config(gate, log_level="warning"))

    async def run() -> None:
        tasks = [asyncio.create_task(server.serve(sockets=[sock]))]
        if idle_s:
            tasks.append(asyncio.create_task(exit_when_idle(gate, server, idle_s)))
        await tasks[0]
        for t in tasks[1:]:
            t.cancel()

    print(f"unilib-mcp: serving on {sock.getsockname()[:2]}"
          f"{f', idle exit after {idle_s:.0f} s' if idle_s else ''}", file=sys.stderr)
    asyncio.run(run())
