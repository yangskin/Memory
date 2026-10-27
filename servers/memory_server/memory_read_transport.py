"""Verified HTTPS for read-only Hub calls, with one idle connection per thread.

Keep urllib's configured proxy routing and TLS verification. Reuse only fully
consumed responses, within one target/proxy/TLS/credential identity. The caller
owns the bounded read retry; this module never replays a request itself.
"""
from __future__ import annotations

import hashlib
import http.client
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener


CONNECT_TIMEOUT_SECONDS = 5.0
READ_RESPONSE_TIMEOUT_SECONDS = 5.0
READ_RETRY_TIMEOUT_SECONDS = 10.0
KEEPALIVE_SECONDS = 30.0
_IDLE = threading.local()


def _close_idle_connection():
    idle = getattr(_IDLE, "connection", None)
    _IDLE.connection = None
    if idle is not None:
        idle[1].close()


class _ReadHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        response_timeout = self.timeout
        self.timeout = max(float(response_timeout), CONNECT_TIMEOUT_SECONDS)
        try:
            # Includes TCP, the configured proxy CONNECT, and verified TLS.
            super().connect()
        finally:
            self.timeout = response_timeout
        if self.sock is not None:
            self.sock.settimeout(response_timeout)


class _ReadResponse:
    def __init__(self, response, connection, key):
        self._response, self._connection, self._key = response, connection, key

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _finish(self, reusable):
        connection, self._connection = self._connection, None
        if connection is None:
            return
        if reusable and not self._response.will_close and connection.sock is not None:
            _close_idle_connection()
            _IDLE.connection = (self._key, connection, time.monotonic())
        else:
            connection.close()

    def read(self, amount=None):
        try:
            data = self._response.read(amount)
        except BaseException:
            self._finish(False)
            raise
        # Only a successful complete body may return its connection to the pool.
        if amount is None:
            self._finish(True)
        return data

    def close(self):
        try:
            self._response.close()
        finally:
            self._finish(False)


class _ReadHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        if not request.host:
            raise URLError("missing_read_host")
        headers = dict(request.unredirected_hdrs)
        headers.update({k: v for k, v in request.headers.items() if k not in headers})
        headers = {name.title(): value for name, value in headers.items()}
        # Credentials are only an isolation digest, never retained in pool keys.
        identity = tuple(headers.get(name, "") for name in
                         ("Authorization", "X-Memory-User-Id", "Proxy-Authorization"))
        identity_hash = hashlib.sha256(repr(identity).encode("utf-8")).digest()
        key = (request.host, request._tunnel_host, self._context, self._check_hostname, identity_hash)
        idle = getattr(_IDLE, "connection", None)
        _IDLE.connection = None
        if idle is not None and idle[0] == key and time.monotonic() - idle[2] < KEEPALIVE_SECONDS and idle[1].sock is not None:
            connection = idle[1]
        else:
            if idle is not None:
                idle[1].close()
            connection = _ReadHTTPSConnection(request.host, timeout=request.timeout,
                                              context=self._context, check_hostname=self._check_hostname)
            if request._tunnel_host:
                tunnel_headers = {}
                if "Proxy-Authorization" in headers:
                    tunnel_headers["Proxy-Authorization"] = headers["Proxy-Authorization"]
                connection.set_tunnel(request._tunnel_host, headers=tunnel_headers)
        # Proxy authentication must never enter the encrypted origin request.
        if request._tunnel_host:
            headers.pop("Proxy-Authorization", None)
        headers.setdefault("Connection", "keep-alive")
        try:
            connection.timeout = request.timeout
            if connection.sock is not None:
                connection.sock.settimeout(request.timeout)
            connection.request(request.get_method(), request.selector, request.data, headers,
                               encode_chunked=request.has_header("Transfer-encoding"))
            response = connection.getresponse()
        except OSError as error:
            connection.close()
            raise URLError(error) from error
        except BaseException:
            connection.close()
            raise
        response.url = request.get_full_url()
        response.msg = response.reason
        return _ReadResponse(response, connection, key)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Hub Authorization header to a redirected origin.
        return None


def read_urlopen(request, *, timeout, context):
    if request.type != "https":
        raise URLError("hub_read_requires_https")
    opener = build_opener(_ReadHTTPSHandler(context=context), _NoRedirect())
    return opener.open(request, timeout=timeout)


def retryable_read_error(error):
    """Only transient transport errors; not HTTP decisions or invalid TLS."""
    if isinstance(error, HTTPError):
        return False
    reason = error.reason if isinstance(error, URLError) else error
    # An abrupt TLS EOF is a transport disconnect, not a failed certificate
    # check. A fresh read connection still performs all TLS verification.
    if isinstance(reason, ssl.SSLEOFError):
        return True
    if isinstance(reason, ssl.SSLError):
        return False
    return isinstance(reason, (TimeoutError, ConnectionError, http.client.HTTPException))
