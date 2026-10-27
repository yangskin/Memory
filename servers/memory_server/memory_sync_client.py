"""Small HTTPS JSON client; failures are values, never write-path exceptions."""

from __future__ import annotations

import json
import http.client
import ssl
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .memory_sync_config import SharedMemoryConfig
from .memory_read_transport import READ_RESPONSE_TIMEOUT_SECONDS, READ_RETRY_TIMEOUT_SECONDS, read_urlopen, retryable_read_error

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
        return self._request("POST", path, payload, timeout_seconds)

    def get(self, path: str, params: dict[str, Any], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        query = urlencode(params)
        return self._request("GET", f"{path}?{query}" if query else path, None, timeout_seconds)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        if not self.config.active:
            return 0, {"error": "shared_memory_disabled"}
        headers = {"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"}
        if self.config.user_id:
            headers["X-Memory-User-ID"] = self.config.user_id
        # Only GET and these two query endpoints can replay. Board/task/event
        # mutations keep their existing single-attempt transport and budgets.
        read_only = method == "GET" or (method == "POST" and path in {
            f"/v1/projects/{self.config.project_id}/context",
            f"/v1/projects/{self.config.project_id}/board/query",
        })
        open_request = read_urlopen if read_only else urlopen
        for attempt in range(2 if read_only else 1):
            # urllib applies proxy state to Request; use a fresh request on retry.
            request = Request(f"{self.config.server_url}{path}", data=json.dumps(payload).encode("utf-8") if payload is not None else None, headers=headers, method=method)
            request_timeout = timeout_seconds
            if read_only:
                request_timeout = max(timeout_seconds, READ_RETRY_TIMEOUT_SECONDS if attempt else READ_RESPONSE_TIMEOUT_SECONDS)
            try:
                with open_request(request, timeout=request_timeout, context=_SSL_CONTEXT) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    if not isinstance(body, dict):
                        return 0, {"error": "invalid_json_object"}
                    return response.status, body
            except HTTPError as exc:
                body = self._http_error_body(exc)
                retry_after = self._retry_after_seconds(exc.headers)
                body.setdefault("error", f"http_{exc.code}")
                if retry_after is not None:
                    body["retry_after_seconds"] = retry_after
                return exc.code, body
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError) as exc:
                if read_only and attempt == 0 and retryable_read_error(exc):
                    continue
                return 0, {"error": "remote_unavailable"}
            except ValueError:
                return 0, {"error": "remote_unavailable"}

    @staticmethod
    def _http_error_body(exc: HTTPError) -> dict[str, Any]:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (AttributeError, OSError, UnicodeDecodeError, ValueError, http.client.HTTPException):
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