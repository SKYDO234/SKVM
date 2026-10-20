"""HTTP client for the SKVM node agent (``agent/skvm-node.py``).

Every request is signed with HMAC-SHA256 over a canonical string::

    METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)

The panel sends ``X-SKVM-Key``, ``X-SKVM-Timestamp``, ``X-SKVM-Nonce`` and
``X-SKVM-Signature``; the agent rejects anything older than 120 seconds or with
a replayed nonce.  Read endpoints long-poll for up to ``wait`` seconds so the
console feels instant without hammering the node.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import requests

log = logging.getLogger("skvm.nodeclient")

USER_AGENT = "skvm-panel/2.0"
MAX_CLOCK_SKEW = 120
DEFAULT_TIMEOUT = 30.0
READ_TIMEOUT = 45.0


class NodeError(RuntimeError):
    """Raised when a node agent refuses or fails a request."""

    def __init__(self, message: str, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    digest = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([method.upper(), path, timestamp, nonce, digest])


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    canonical = canonical_string(method, path, timestamp, nonce, body)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify_signature(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes,
                     signature: str) -> bool:
    """Shared by the agent; kept here so both sides use identical bytes."""
    expected = sign(secret, method, path, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature or "")


def fresh_timestamp() -> str:
    return str(int(time.time()))


def new_nonce() -> str:
    return secrets.token_hex(12)


class NodeClient:
    """Thin, synchronous wrapper around the agent HTTP API."""

    def __init__(
        self,
        endpoint: str,
        key_id: str,
        secret: str,
        *,
        verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not endpoint:
            raise NodeError("node endpoint is not configured")
        if not secret:
            raise NodeError("node secret is missing (was the encryption key rotated?)")
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.startswith(("http://", "https://")):
            self.endpoint = "http://" + self.endpoint
        self.key_id = key_id
        self.secret = secret
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # -- plumbing --------------------------------------------------------- #
    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.endpoint}{path}"
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        return url

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        raw: bool = False,
    ) -> Any:
        path = path if path.startswith("/") else "/" + path
        body = b""
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        timestamp = fresh_timestamp()
        nonce = new_nonce()
        headers = {
            "Content-Type": "application/json",
            "X-SKVM-Key": self.key_id,
            "X-SKVM-Timestamp": timestamp,
            "X-SKVM-Nonce": nonce,
            "X-SKVM-Signature": sign(self.secret, method, path, timestamp, nonce, body),
        }
        try:
            response = self.session.request(
                method.upper(),
                self.url(path, params),
                data=body or None,
                headers=headers,
                timeout=timeout or self.timeout,
                verify=self.verify,
            )
        except requests.Timeout as exc:
            raise NodeError(f"node did not answer within {(timeout or self.timeout):.0f}s") from exc
        except requests.RequestException as exc:
            raise NodeError(f"cannot reach node at {self.endpoint}: {exc}") from exc

        if raw:
            if response.status_code >= 400:
                raise NodeError(f"node returned HTTP {response.status_code}", response.status_code)
            return response.content

        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:400]}

        if response.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
            message = ""
            if isinstance(data, dict):
                message = str(data.get("error") or data.get("message") or "")
            raise NodeError(message or f"node returned HTTP {response.status_code}", response.status_code, data)
        return data

    # -- introspection ---------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        return self.request("GET", "/v1/health", timeout=8.0)

    def info(self) -> dict[str, Any]:
        return self.request("GET", "/v1/info", timeout=12.0)

    def images(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v1/images").get("images", [])

    def pull_image(self, reference: str) -> dict[str, Any]:
        return self.request("POST", "/v1/images/pull", {"image": reference}, timeout=600.0)

    # -- instances -------------------------------------------------------- #
    def create(self, **spec: Any) -> dict[str, Any]:
        return self.request("POST", "/v1/servers", spec, timeout=180.0)

    def inspect(self, instance_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/servers/{instance_id}")

    def list_servers(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v1/servers").get("servers", [])

    def remove(self, instance_id: str, force: bool = True) -> dict[str, Any]:
        return self.request("DELETE", f"/v1/servers/{instance_id}", params={"force": int(force)}, timeout=90.0)

    def start(self, instance_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/start", timeout=90.0)

    def stop(self, instance_id: str, timeout_s: int = 10) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/stop", {"timeout": timeout_s}, timeout=90.0)

    def restart(self, instance_id: str, timeout_s: int = 10) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/restart", {"timeout": timeout_s}, timeout=90.0)

    def suspend(self, instance_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/suspend", timeout=60.0)

    def resume(self, instance_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/resume", timeout=60.0)

    def stats(self, instance_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/servers/{instance_id}/stats", timeout=15.0)

    def host_stats(self) -> dict[str, Any]:
        return self.request("GET", "/v1/host/stats", timeout=12.0)

    def logs(self, instance_id: str, tail: int = 200) -> str:
        data = self.request("GET", f"/v1/servers/{instance_id}/logs", params={"tail": tail}, timeout=20.0)
        return data.get("logs", "")

    def exec_command(self, instance_id: str, command: list[str], timeout: int = 30) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/servers/{instance_id}/exec",
            {"command": command, "timeout": timeout},
            timeout=timeout + 15.0,
        )

    def snapshot(self, instance_id: str, name: str = "") -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/snapshot", {"name": name}, timeout=300.0)

    def restore(self, instance_id: str, reference: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/servers/{instance_id}/restore", {"reference": reference}, timeout=300.0)

    def delete_snapshot(self, reference: str) -> dict[str, Any]:
        return self.request("POST", "/v1/snapshots/delete", {"reference": reference}, timeout=90.0)

    def upload(self, instance_id: str, path: str, data: bytes, mode: int = 0o644) -> dict[str, Any]:
        import base64

        return self.request(
            "POST",
            f"/v1/servers/{instance_id}/files/write",
            {"path": path, "content": base64.b64encode(data).decode(), "mode": mode},
            timeout=120.0,
        )

    def download(self, instance_id: str, path: str) -> bytes:
        import base64

        data = self.request("GET", f"/v1/servers/{instance_id}/files/read", params={"path": path}, timeout=120.0)
        return base64.b64decode(data.get("content") or "")

    # -- PTY sessions ----------------------------------------------------- #
    def pty_open(self, instance_id: str, shell: str = "/bin/bash") -> dict[str, Any]:
        return self.request("POST", "/v1/pty/open", {"instance_id": instance_id, "shell": shell}, timeout=30.0)

    def pty_read(self, sid: str, wait: float = 0.2) -> str:
        data = self.request("GET", f"/v1/pty/{sid}/read", params={"wait": round(wait, 3)}, timeout=READ_TIMEOUT)
        return data.get("data", "")

    def pty_write(self, sid: str, data: str) -> None:
        self.request("POST", f"/v1/pty/{sid}/write", {"data": data}, timeout=20.0)

    def pty_resize(self, sid: str, rows: int, cols: int) -> None:
        self.request("POST", f"/v1/pty/{sid}/resize", {"rows": rows, "cols": cols}, timeout=20.0)

    def pty_close(self, sid: str) -> None:
        try:
            self.request("POST", f"/v1/pty/{sid}/close", timeout=15.0)
        except NodeError as exc:  # closing twice is fine
            log.debug("pty close: %s", exc)
