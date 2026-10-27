"""Verified TLS, proxy handling and replay boundaries for read-only Hub calls."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.error import URLError

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from servers.memory_server import memory_read_transport as transport
from servers.memory_server import memory_sync_client as client_module
from servers.memory_server.memory_sync_client import MemoryHubClient
from servers.memory_server.memory_sync_config import SharedMemoryConfig

TOKEN = "transport-test-only"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Deliberate fixture disconnects can interrupt the next request.

    def do_GET(self):
        self.reply()

    def do_POST(self):
        self.reply()

    def reply(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
        self.server.identity_headers.append((self.headers.get("X-Memory-User-ID"), self.headers.get("Proxy-Authorization")))
        if self.server.mode == "close_after_first" and len(self.server.requests) == 1:
            self.close_connection = True
        if self.server.mode == "drop" or self.server.mode == "drop_first" and len(self.server.requests) == 1:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        time.sleep(self.server.response_delay)
        status = self.server.mode if isinstance(self.server.mode, int) else 200
        data = b"invalid" if self.server.mode == "invalid" else b'{"items": [], "freshness": {"latest_event_seq": 1}}'
        try:
            self.send_response(status)
            if status == 302:
                self.send_header("Location", self.server.url + "/redirect-target")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            time.sleep(self.server.body_delay)
            if self.server.truncate_body or self.server.mode == "truncated" or self.server.mode == "truncated_first" and len(self.server.requests) == 1:
                self.wfile.write(data[:3])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.wfile.write(data)
        except OSError:
            pass  # Expected when a fixture deliberately exceeds the read timeout.


class TLSServer(ThreadingHTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        self.connections += 1
        try:
            if self.handshake_eofs:
                self.handshake_eofs -= 1
                connection.settimeout(2)
                # Consume the ClientHello TLS record before closing gracefully
                # at TCP level, so OpenSSL sees an unexpected TLS EOF, not RST.
                header = b""
                while len(header) < 5:
                    chunk = connection.recv(5 - len(header))
                    if not chunk:
                        raise OSError("fixture incomplete ClientHello")
                    header += chunk
                remaining = int.from_bytes(header[3:5], "big")
                while remaining:
                    chunk = connection.recv(remaining)
                    if not chunk:
                        raise OSError("fixture incomplete ClientHello")
                    remaining -= len(chunk)
                connection.shutdown(socket.SHUT_WR)
                connection.close()
                raise OSError("fixture abrupt TLS EOF")
            time.sleep(self.handshake_delay)
            return self.context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


@pytest.fixture
def tls(tmp_path, monkeypatch):
    # Scale down only the policy floor in timing fixtures. Dedicated tests below
    # exercise the production five-second floor and keep custom larger budgets.
    monkeypatch.setattr(client_module, "READ_RESPONSE_TIMEOUT_SECONDS", 0)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv("MEMORY_TEST_TLS_TOKEN", raising=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "read-transport-test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = TLSServer(("127.0.0.1", 0), Handler)
    server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.context.load_cert_chain(cert_path, key_path)
    server.url = f"https://127.0.0.1:{server.server_port}"
    server.connections = 0
    server.handshake_eofs = 0
    server.requests = []
    server.identity_headers = []
    server.handshake_delay = server.response_delay = server.body_delay = 0
    server.mode = None
    server.truncate_body = False
    context = ssl.create_default_context(cafile=str(cert_path))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    monkeypatch.setattr(client_module, "_SSL_CONTEXT", context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        transport._close_idle_connection()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def client(tls):
    return MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url, project_id="p",
        token_env="MEMORY_TEST_TLS_TOKEN", local_token=TOKEN))


def test_read_accepts_handshake_slower_than_response_budget(tls):
    tls.handshake_delay = 0.2
    assert client(tls).context({"include": ["project_brief"]}, 0.05)[0] == 200
    assert tls.connections == 1
    assert tls.requests[0][3] == "Bearer " + TOKEN


def test_read_retry_budget_is_bounded(tls, monkeypatch):
    monkeypatch.setattr(client_module, "READ_RETRY_TIMEOUT_SECONDS", 0.05)
    tls.response_delay = 0.2
    started = time.monotonic()
    assert client(tls).context({}, 0.05)[0] == 0
    assert time.monotonic() - started < 1.0
    assert len(tls.requests) == 2


def test_connect_attempts_are_bounded(tls, monkeypatch):
    tls.handshake_delay = 0.2
    monkeypatch.setattr(transport, "CONNECT_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(client_module, "READ_RETRY_TIMEOUT_SECONDS", 0.04)
    started = time.monotonic()
    assert client(tls).context({}, 0.04)[0] == 0
    assert time.monotonic() - started < 0.8
    assert tls.requests == []


def test_slow_read_recovers_with_the_separate_retry_budget(tls):
    tls.response_delay = 0.2
    assert client(tls).context({}, 0.05)[0] == 200
    assert len(tls.requests) == 2 and tls.requests[0] == tls.requests[1]


def test_slow_body_recovers_with_identical_query(tls):
    tls.body_delay = 0.2
    assert client(tls).context({"include": ["project_brief"]}, 0.05)[0] == 200
    assert len(tls.requests) == 2 and tls.requests[0] == tls.requests[1]


def test_partial_body_recovers_with_identical_query(tls):
    tls.mode = "truncated_first"
    assert client(tls).context({"include": ["project_brief"]}, 0.2)[0] == 200
    assert len(tls.requests) == 2 and tls.requests[0] == tls.requests[1]


def test_read_retries_disconnect_with_identical_query(tls):
    tls.mode = "drop_first"
    payload = {"filter": "all", "max_items": 1}
    assert client(tls).post("/v1/projects/p/board/query", payload, 0.2)[0] == 200
    assert len(tls.requests) == 2 and tls.requests[0] == tls.requests[1]
    assert json.loads(tls.requests[0][2]) == payload


def test_read_stops_after_one_retry(tls):
    tls.mode = "drop"
    assert client(tls).get("/v1/projects/p/tasks", {}, 0.1)[0] == 0
    assert len(tls.requests) == 2


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_decisions_are_not_retried_or_redirected(tls, status):
    tls.mode = status
    assert client(tls).context({}, 0.2)[0] == status
    assert len(tls.requests) == 1
    assert tls.requests[0][1] == "/v1/projects/p/context"


def test_invalid_json_is_not_retried(tls):
    tls.mode = "invalid"
    assert client(tls).context({}, 0.2)[0] == 0
    assert len(tls.requests) == 1


def test_real_tls_eof_retries_with_full_verification(tls, monkeypatch):
    tls.handshake_eofs = 1
    observed = []
    original = client_module.retryable_read_error
    def inspect_failure(error):
        observed.append(type(error.reason if isinstance(error, URLError) else error))
        return original(error)
    monkeypatch.setattr(client_module, "retryable_read_error", inspect_failure)
    assert client(tls).context({}, 0.2)[0] == 200
    assert observed == [ssl.SSLEOFError]
    assert tls.connections == 2 and len(tls.requests) == 1
    assert client_module._SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert client_module._SSL_CONTEXT.check_hostname


def test_permanent_tls_eof_stops_after_one_retry(tls):
    tls.handshake_eofs = 2
    assert client(tls).context({}, 0.2)[0] == 0
    assert tls.connections == 2 and tls.requests == []


def test_tls_eof_does_not_retry_writes(tls):
    tls.handshake_eofs = 2
    assert client(tls).post("/v1/projects/p/board/post", {}, 0.2)[0] == 0
    assert tls.connections == 1 and tls.requests == []


@pytest.mark.parametrize("error_type,expected", [(ssl.SSLEOFError, True),
    (ssl.SSLCertVerificationError, False), (ssl.SSLError, False)])
def test_direct_tls_body_error_is_classified_without_exception_escape(monkeypatch, error_type, expected):
    calls = []
    class BrokenBody:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            raise error_type("fixture")
    def open_response(*args, **kwargs):
        calls.append(1)
        return BrokenBody()
    monkeypatch.setattr(client_module, "read_urlopen", open_response)
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url="https://example.invalid", project_id="p",
        local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    assert value.context({}, 0.2) == (0, {"error": "remote_unavailable"})
    assert len(calls) == (2 if expected else 1)


def test_untrusted_certificate_is_rejected_without_retry(tls, monkeypatch):
    monkeypatch.setattr(client_module, "_SSL_CONTEXT", ssl.create_default_context())
    assert client(tls).context({}, 0.2)[0] == 0
    assert tls.connections == 1 and tls.requests == []


def test_wrong_hostname_is_rejected_without_retry(tls):
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url.replace("127.0.0.1", "localhost"),
        project_id="p", local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    assert value.context({}, 0.2)[0] == 0
    assert tls.connections == 1 and tls.requests == []


def test_plain_http_never_receives_credentials(tls):
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url.replace("https:", "http:"),
        project_id="p", local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    assert value.context({}, 0.2)[0] == 0
    assert tls.connections == 0 and tls.requests == []


@pytest.mark.parametrize("path", ["/v1/projects/p/events/batch", "/v1/projects/p/tasks/command",
    "/v1/projects/p/board/post", "/v1/projects/p/board/reply", "/v1/projects/p/board/resolve", "/other/context"])
@pytest.mark.parametrize("mode", ["drop", "truncated"])
def test_writes_never_use_the_read_retry(tls, path, mode):
    tls.mode = mode
    assert client(tls).post(path, {"event": "fixture"}, 0.2)[0] == 0
    assert len(tls.requests) == 1


def test_write_handshake_budget_is_unchanged(tls):
    tls.handshake_delay = 0.2
    assert client(tls).post("/v1/projects/p/events/batch", {}, 0.05)[0] == 0
    assert tls.connections == 1 and tls.requests == []


def test_each_retry_uses_a_new_unmodified_request(monkeypatch):
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url="https://example.invalid", project_id="p",
        local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    observed = []
    def fail(request, **kwargs):
        observed.append((request.full_url, request.host, request.get_header("Authorization"), kwargs["timeout"]))
        request.set_proxy("proxy.invalid:8080", "https")
        raise URLError(TimeoutError("fixture"))
    monkeypatch.setattr(client_module, "read_urlopen", fail)
    assert value.context({}, 0.2)[0] == 0
    assert len(observed) == 2 and observed[0][:3] == observed[1][:3]
    assert observed[0][3] == 5.0 and observed[1][3] == 10.0
    assert observed[0][1] == "example.invalid"


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        header = b""
        while b"\r\n\r\n" not in header and len(header) < 16384:
            data = self.request.recv(4096)
            if not data:
                return
            header += data
        first = header.split(b"\r\n", 1)[0]
        self.server.requests.append(header)
        if first != f"CONNECT 127.0.0.1:{self.server.target_port} HTTP/1.0".encode():
            return
        with socket.create_connection(("127.0.0.1", self.server.target_port), timeout=2) as upstream:
            self.request.sendall(b"HTTP/1.0 200 Connection established\r\n\r\n")
            sockets = [self.request, upstream]
            while True:
                ready, _, _ = select.select(sockets, [], [], 2)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


def test_configured_authenticated_proxy_is_preserved(tls, monkeypatch):
    assert client(tls).context({}, 0.2)[0] == 200  # Warm direct route first.
    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ProxyHandler)
    proxy.daemon_threads = True
    proxy.target_port = tls.server_port
    proxy.requests = []
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("https_proxy", f"http://fixture-user:fixture-password@127.0.0.1:{proxy.server_address[1]}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    tls.handshake_delay = 0.2
    try:
        assert client(tls).context({}, 0.05)[0] == 200
        assert client(tls).context({}, 0.05)[0] == 200
        assert len(proxy.requests) == 1 and len(tls.requests) == 3
        assert tls.connections == 2
        assert all(proxy_auth is None for _, proxy_auth in tls.identity_headers)
        assert b"Proxy-Authorization: Basic " in proxy.requests[0]
        assert TOKEN.encode() not in proxy.requests[0]
        assert tls.requests[0][3] == "Bearer " + TOKEN
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


def test_complete_read_reuses_one_verified_tls_connection(tls):
    tls.handshake_delay = 0.2
    for _ in range(3):
        assert client(tls).context({}, 0.05)[0] == 200
    assert tls.connections == 1 and len(tls.requests) == 3


def test_server_closed_idle_connection_reconnects_on_bounded_read_retry(tls):
    tls.mode = "close_after_first"
    assert client(tls).context({}, 0.2)[0] == 200
    assert client(tls).context({}, 0.2)[0] == 200
    assert tls.connections == 2 and len(tls.requests) == 2


def test_expired_idle_connection_is_not_reused(tls, monkeypatch):
    assert client(tls).context({}, 0.2)[0] == 200
    monkeypatch.setattr(transport, "KEEPALIVE_SECONDS", 0)
    assert client(tls).context({}, 0.2)[0] == 200
    assert tls.connections == 2


def test_credential_change_isolates_connection_and_sends_current_identity(tls):
    assert client(tls).context({}, 0.2)[0] == 200
    changed = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url, project_id="p",
        local_token="another-test-token", token_env="MEMORY_TEST_TLS_TOKEN"))
    assert changed.context({}, 0.2)[0] == 200
    assert tls.connections == 2
    assert tls.requests[-1][3] == "Bearer another-test-token"


def test_trust_context_change_does_not_reuse_authenticated_connection(tls, monkeypatch):
    assert client(tls).context({}, 0.2)[0] == 200
    monkeypatch.setattr(client_module, "_SSL_CONTEXT", ssl.create_default_context())
    assert client(tls).context({}, 0.2)[0] == 0
    assert tls.connections == 2 and len(tls.requests) == 1


def test_partial_consumption_discards_connection(tls):
    from urllib.request import Request
    request = Request(tls.url + "/v1/projects/p/context", headers={"Authorization": "Bearer " + TOKEN})
    with transport.read_urlopen(request, timeout=0.2, context=client_module._SSL_CONTEXT) as response:
        assert response.read(1)
    assert client(tls).context({}, 0.2)[0] == 200
    assert tls.connections == 2


def test_http_error_with_partial_body_is_not_retried_and_is_diagnostic(tls):
    tls.mode = 500
    tls.truncate_body = True
    assert client(tls).context({}, 0.2) == (500, {"error": "http_500"})
    assert tls.connections == 1 and len(tls.requests) == 1


def test_user_identity_change_isolates_idle_connection(tls):
    for user in ("first-test-user", "second-test-user"):
        value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url, project_id="p",
            user_id=user, local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
        assert value.context({}, 0.2)[0] == 200
    assert tls.connections == 2
    assert tls.identity_headers == [("first-test-user", None), ("second-test-user", None)]


def test_warm_connection_cannot_bypass_new_hostname_validation(tls):
    assert client(tls).context({}, 0.2)[0] == 200
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url=tls.url.replace("127.0.0.1", "localhost"),
        project_id="p", local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    assert value.context({}, 0.2)[0] == 0
    assert tls.connections == 2 and len(tls.requests) == 1


def test_concurrent_readers_use_separate_connections_without_mixing_payloads(tls):
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(2)
    def run(worker):
        barrier.wait(timeout=5)
        try:
            for index in range(3):
                assert client(tls).context({"worker": worker, "index": index}, 0.2)[0] == 200
        finally:
            transport._close_idle_connection()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, worker) for worker in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert tls.connections == 2 and len(tls.requests) == 6
    assert {(json.loads(r[2])["worker"], json.loads(r[2])["index"]) for r in tls.requests} == {
        (worker, index) for worker in range(2) for index in range(3)}


def test_response_floor_preserves_slow_healthy_connection(tls, monkeypatch):
    monkeypatch.setattr(client_module, "READ_RESPONSE_TIMEOUT_SECONDS", transport.READ_RESPONSE_TIMEOUT_SECONDS)
    tls.response_delay = tls.body_delay = 0.2
    for _ in range(2):
        assert client(tls).context({}, 0.05)[0] == 200
    assert tls.connections == 1 and len(tls.requests) == 2


def test_larger_caller_read_timeout_is_preserved(monkeypatch):
    observed = []
    def fail(request, **kwargs):
        observed.append(kwargs["timeout"])
        raise URLError(TimeoutError("fixture"))
    monkeypatch.setattr(client_module, "read_urlopen", fail)
    value = MemoryHubClient(SharedMemoryConfig(enabled=True, server_url="https://example.invalid", project_id="p",
        local_token=TOKEN, token_env="MEMORY_TEST_TLS_TOKEN"))
    assert value.context({}, 20)[0] == 0
    assert observed == [20, 20]
