"""Small HTTPS JSON client; failures are values, never write-path exceptions."""

from __future__ import annotations

import json
import ssl
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .memory_sync_config import SharedMemoryConfig

try:
    import certifi
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()
else:
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class MemoryHubClient:
    def __init__(self, config: SharedMemoryConfig) -> None:
        self.config = config

    def post(self, path: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        if not self.config.active:
            return 0, {"error": "shared_memory_disabled"}
        headers = {"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"}
        if self.config.user_id:
            headers["X-Memory-User-ID"] = self.config.user_id
        request = Request(f"{self.config.server_url}{path}", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds, context=_SSL_CONTEXT) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = self._http_error_body(exc)
            retry_after = self._retry_after_seconds(exc.headers)
            body.setdefault("error", f"http_{exc.code}")
            if retry_after is not None:
                body["retry_after_seconds"] = retry_after
            return exc.code, body
        except (URLError, TimeoutError, ValueError):
            return 0, {"error": "remote_unavailable"}

    @staticmethod
    def _http_error_body(exc: HTTPError) -> dict[str, Any]:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (AttributeError, OSError, UnicodeDecodeError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _retry_after_seconds(headers: Message | None) -> float | None:
        value = headers.get("Retry-After") if headers is not None else None
        try:
            return max(0.0, float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def upload(self, events: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        return self.post(f"/v1/projects/{self.config.project_id}/events/batch", {"events": events}, self.config.upload_timeout_seconds)

    def context(self, request: dict[str, Any], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        return self.post(f"/v1/projects/{self.config.project_id}/context", request, timeout_seconds)

    def graph(self, request: dict[str, Any], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        return self.post(f"/v1/projects/{self.config.project_id}/graph/query", request, timeout_seconds)