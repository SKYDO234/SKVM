"""A dependency-free RFC 6455 WebSocket server implementation.

SKVM streams the interactive console and live metrics over WebSockets.  Rather
than pulling in an eventlet/gevent stack (which fights with the Docker SDK
threads), the panel speaks the protocol directly on top of the Werkzeug socket:

* :class:`WebSocket` — frame reader/writer with ping/pong, close and fragmentation
* :func:`websocket_route` — negotiates the upgrade inside a normal Flask route

The implementation is deliberately small: no permessage-deflate, no extensions,
text + binary frames, 16 MiB payload ceiling.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import select
import socket
import struct
import threading
from typing import Any

from flask import request

log = logging.getLogger("skvm.ws")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
MAX_PAYLOAD = 16 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """Minimal blocking WebSocket connection wrapper."""

    def __init__(self, sock: socket.socket, environ: dict[str, Any] | None = None) -> None:
        self.sock = sock
        self.environ = environ or {}
        self.closed = False
        self._send_lock = threading.Lock()
        self._buffer = b""

    # -- handshake -------------------------------------------------------- #
    @classmethod
    def is_upgrade(cls, environ: dict[str, Any] | None = None) -> bool:
        environ = environ or request.environ
        return (
            environ.get("HTTP_UPGRADE", "").lower() == "websocket"
            and "upgrade" in environ.get("HTTP_CONNECTION", "").lower()
        )

    @classmethod
    def from_request(cls) -> "WebSocket | None":
        environ = request.environ
        if not cls.is_upgrade(environ):
            return None
        key = environ.get("HTTP_SEC_WEBSOCKET_KEY")
        if not key:
            return None
        sock = environ.get("werkzeug.socket") or environ.get("gunicorn.socket")
        if sock is None:
            raise WebSocketError(
                "No socket available for the upgrade — run SKVM with a server that exposes "
                "the raw socket (Werkzeug, Gunicorn gthread, or the built-in werkzeug dev server)."
            )
        sock = cls._take_over(sock)
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(response.encode())
        return cls(sock, environ)

    @classmethod
    def accept(cls) -> "WebSocket":
        ws = cls.from_request()
        if ws is None:
            raise WebSocketError("Client did not request a WebSocket upgrade")
        return ws

    # -- socket ownership -------------------------------------------------- #
    @staticmethod
    def _take_over(sock: socket.socket) -> socket.socket:
        """Detach the raw socket so the WSGI server cannot write a reply.

        After a 101 response, the WSGI server would still emit its own HTTP
        response for the view's return value, corrupting the stream.  We dup the
        real file descriptor for our own use and point the original fd at
        ``/dev/null``, which makes the server's writes harmless while keeping the
        descriptor number valid (no fd-reuse hazards).
        """
        if os.environ.get("SKVM_WS_DETACH", "1") not in ("1", "true", "yes", "on"):
            return sock
        try:
            fd = sock.fileno()
            duplicate = os.dup(fd)
            os.set_inheritable(duplicate, True)
            detached = socket.socket(fileno=duplicate)
            detached.settimeout(None)
            null_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null_fd, fd)
            finally:
                os.close(null_fd)
            return detached
        except (OSError, AttributeError, ValueError) as exc:  # pragma: no cover - exotic servers
            log.warning("could not detach socket for the websocket upgrade: %s", exc)
            return sock

    # -- low level IO ----------------------------------------------------- #
    def _recv_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining > 0:
            if self._buffer:
                take = self._buffer[:remaining]
                self._buffer = self._buffer[len(take):]
                chunks.append(take)
                remaining -= len(take)
                continue
            try:
                data = self.sock.recv(min(65536, remaining))
            except (TimeoutError, socket.timeout):
                continue
            except OSError as exc:
                raise WebSocketError(str(exc)) from exc
            if not data:
                raise WebSocketError("connection closed")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def send(self, payload: bytes | str, opcode: int = OP_TEXT) -> None:
        if self.closed:
            return
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        length = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        with self._send_lock:
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError as exc:
                self.closed = True
                raise WebSocketError(str(exc)) from exc

    def send_text(self, text: str) -> None:
        self.send(text, OP_TEXT)

    def send_json(self, payload: Any) -> None:
        import json

        self.send(json.dumps(payload, default=str), OP_TEXT)

    def send_binary(self, data: bytes) -> None:
        self.send(data, OP_BINARY)

    def ping(self, payload: bytes = b"skvm") -> None:
        self.send(payload, OP_PING)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self.send(struct.pack("!H", code) + reason.encode(), OP_CLOSE)
        except WebSocketError:
            pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def set_timeout(self, seconds: float | None) -> None:
        try:
            self.sock.settimeout(seconds)
        except OSError:  # pragma: no cover
            pass

    # -- frame reading ---------------------------------------------------- #
    def receive(self, timeout: float = 0.5) -> tuple[int, bytes] | None:
        """Read one logical message; returns ``None`` when the socket is idle."""
        self.set_timeout(timeout)
        try:
            first = self._recv_exact(2)
        except WebSocketError as exc:
            if "timed out" in str(exc).lower():
                return None
            self.closed = True
            return None
        except (TimeoutError, socket.timeout):
            return None

        fin = bool(first[0] & 0x80)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_PAYLOAD:
            self.close(1009, "payload too large")
            return None
        mask = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length) if length else b""
        if masked:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))

        if opcode == OP_CLOSE:
            self.close(1000, "bye")
            return OP_CLOSE, data
        if opcode == OP_PING:
            self.send(data, OP_PONG)
            return OP_PING, data
        if opcode == OP_PONG:
            return OP_PONG, data
        if opcode == OP_CONTINUATION:
            return self.receive(timeout)
        if not fin:
            more = self.receive(timeout)
            if more is not None and more[0] == opcode:
                return opcode, data + more[1]
        return opcode, data

    def read_text(self, timeout: float = 0.5) -> str | None:
        message = self.receive(timeout)
        if message is None:
            return None
        opcode, data = message
        if opcode in (OP_TEXT, OP_BINARY):
            return data.decode("utf-8", "replace")
        return None

    def readable(self, timeout: float = 0.0) -> bool:
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            return bool(ready)
        except (OSError, ValueError):  # pragma: no cover
            return False


def console_origin_allowed() -> bool:
    """Same-origin check for WebSocket handshakes (CSRF protection for WS)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients (curl, tests)
    host = request.host
    return origin.split("://")[-1] == host or origin.endswith(f"//{host}")
