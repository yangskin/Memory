from dataclasses import replace
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from servers.memory_server import memory_hub_enrollment as enrollment
from servers.memory_server.memory_config import load_config, local_hub_credentials_path, ReloadableMemoryConfig
from servers.memory_server.memory_sync_worker import MemorySyncWorker


@pytest.fixture
def runtime(repo, monkeypatch):
    monkeypatch.delenv("MEMORY_HUB_TOKEN", raising=False)
    monkeypatch.setenv("MEMORY_HUB_ENROLLMENT_TOKEN", "mem_v1.tok_" + "a" * 24 + "." + "b" * 58)
    config = load_config(repo)
    data = json.loads(config.config_path.read_text(encoding="utf-8"))
    data["shared_memory"] = {"enabled": True, "server_url": "https://hub.example.com", "project_id": "project"}
    config.config_path.write_text(json.dumps(data), encoding="utf-8")
    return load_config(repo)


def transport(calls):
    def send(config, method, path, payload=None):
        calls.append((config, method, path, payload))
        if path in {"/device-enrollments/auto", "/device-enrollments/open"}:
            return {"status": "approved", "enrollment_id": payload["enrollment_id"],
                    "device_name": payload["device_name"], "pairing_code": payload["poll_secret_hash"][:12].upper()}
        if path == "/connection":
            return {"ok": True, "project_id": "project", "user_id": "owner",
                    "capabilities": {key: True for key in ["board_read", "board_write", "tasks_read", "tasks_write", "project_brief"]}}
        return {"freshness": {}, "project_brief": {"markdown": "Important project memory"}} if path == "/context" else {"items": []}
    return send


def test_enroll_probe_persist_and_reload_for_other_clients(runtime):
    calls = []
    provider = ReloadableMemoryConfig(runtime)
    result = enrollment.ensure_hub_connection(runtime, send=transport(calls))
    assert result["status"] == "ready" and result["registered"]
    assert result["project_brief_available"]
    assert provider.get().shared_memory.active
    token = provider.get().shared_memory.token
    assert token != calls[0][0].token  # invitation is never the business credential
    assert token not in str(result) + str(calls[0][3])
    assert [c[2] for c in calls] == ["/device-enrollments/auto", "/connection", "/board/query", "/tasks?limit=1", "/context"]
    calls.clear()
    enrollment.ensure_hub_connection(provider.get(), send=transport(calls))
    assert all(c[2] != "/device-enrollments/auto" for c in calls)


@pytest.mark.parametrize("fail_path", ["/device-enrollments/auto", "/connection", "/board/query", "/tasks?limit=1", "/context"])
def test_retry_preserves_request_and_does_not_publish_untested_token(runtime, fail_path):
    calls = []
    healthy = transport(calls)
    def send(config, method, path, payload=None):
        result = healthy(config, method, path, payload)
        if path == fail_path:
            raise enrollment.EnrollmentError("hub_unavailable")
        return result
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.ensure_hub_connection(runtime, send=send)
    credential = local_hub_credentials_path(runtime.repo_root)
    assert not credential.exists()
    first = calls[0][3]
    calls.clear()
    enrollment.ensure_hub_connection(runtime, send=healthy)
    assert calls[0][3] == first
    assert not (credential.parent / "hub-enrollment.json").exists()


def test_no_invitation_uses_open_registration_but_disabled_never_contacts_network(runtime, monkeypatch):
    monkeypatch.delenv("MEMORY_HUB_ENROLLMENT_TOKEN")
    calls = []
    assert enrollment.ensure_hub_connection(runtime, send=transport(calls))["status"] == "ready"
    assert calls[0][2] == "/device-enrollments/open"
    calls.clear()
    disabled = replace(runtime, shared_memory=replace(runtime.shared_memory, enabled=False))
    assert enrollment.ensure_hub_connection(disabled, send=transport(calls))["status"] == "disabled"
    assert calls == []


def test_rejected_existing_token_never_reenrolls(runtime):
    runtime = replace(runtime, shared_memory=replace(runtime.shared_memory, local_token="existing"))
    calls = []
    def denied(config, method, path, payload=None):
        calls.append(path)
        raise enrollment.EnrollmentError("hub_http_401")
    with pytest.raises(enrollment.EnrollmentError, match="hub_http_401"):
        enrollment.ensure_hub_connection(runtime, send=denied)
    assert calls == ["/connection"]


def test_concurrent_clients_do_not_create_two_requests(runtime):
    calls = []
    entered, release = threading.Event(), threading.Event()
    healthy = transport(calls)
    def paused(config, method, path, payload=None):
        if path == "/device-enrollments/auto":
            entered.set()
            assert release.wait(5)
        return healthy(config, method, path, payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(enrollment.ensure_hub_connection, runtime, send=paused)
        assert entered.wait(5)
        from servers.memory_server.memory_locks import LockTimeoutError
        try:
            with pytest.raises(LockTimeoutError):
                enrollment.ensure_hub_connection(runtime, send=healthy)
        finally:
            release.set()
        assert first.result()["registered"]
    assert len([c for c in calls if c[2] == "/device-enrollments/auto"]) == 1


def test_background_worker_reports_failure_and_rate_limits_retries(runtime, monkeypatch):
    attempts = []
    def failed(config):
        attempts.append(1)
        raise enrollment.EnrollmentError("hub_http_403")
    monkeypatch.setattr(enrollment, "ensure_hub_connection", failed)
    worker = MemorySyncWorker(lambda: runtime)
    worker.run_once()
    assert worker.connection_status == {"status": "enrollment_failed", "reason": "hub_http_403"}
    worker.run_once()
    assert len(attempts) == 1


@pytest.mark.parametrize("url", ["http://hub.example.com", "https://user:secret@hub.example.com", "https://hub.example.com/path", "https://hub.example.com?secret=x"])
def test_invalid_addresses_rejected_before_network(runtime, url):
    with pytest.raises(enrollment.EnrollmentError, match="invalid_hub_address"):
        enrollment._request(replace(runtime.shared_memory, server_url=url), "GET", "/connection")


def test_redirects_cannot_forward_credentials():
    assert enrollment._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.com") is None


def test_task_mutation_cannot_silently_create_local_task_before_hub_registration(runtime):
    from servers.memory_server.server_dispatch import _dispatch_memory_task_sync
    result = _dispatch_memory_task_sync(runtime, {"action": "create", "command_id": "test", "expected_version": 0,
                                                "task_id": "test", "title": "Must wait for Hub"})
    assert result["ok"] is False and result["error"] == "hub_not_ready"


def test_failed_readiness_shape_does_not_save_credentials(runtime):
    healthy = transport([])
    def send(config, method, path, payload=None):
        return {"error": "not_ready"} if path == "/tasks?limit=1" else healthy(config, method, path, payload)
    with pytest.raises(enrollment.EnrollmentError, match="invalid_hub_response"):
        enrollment.ensure_hub_connection(runtime, send=send)
    assert not local_hub_credentials_path(runtime.repo_root).exists()


def test_mcp_returns_registration_status_without_waiting_for_network(runtime, monkeypatch):
    import asyncio
    from mcp.types import CallToolRequest, CallToolRequestParams
    from servers.memory_server.server import create_server
    from servers.memory_server import memory_sync_worker
    worker = MemorySyncWorker(lambda: runtime)
    worker.connection_status = {"status": "enrollment_credential_required"}
    monkeypatch.setitem(memory_sync_worker._WORKERS, runtime.repo_root.resolve(), worker)
    server = create_server(runtime)
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(
        name="memory_task_sync", arguments={"action": "sync"}))
    response = asyncio.run(server.request_handlers[CallToolRequest](request))
    result = json.loads(response.root.content[0].text)
    assert result["error"] == "hub_not_ready"
    assert result["hub_connection"]["status"] == "enrollment_credential_required"


def test_ready_status_is_invalidated_when_effective_credentials_change(runtime, monkeypatch):
    from servers.memory_server import memory_sync_worker as sync
    original = enrollment.ensure_hub_connection
    def verify(config):
        if config.shared_memory.token == "different-unverified-token":
            raise enrollment.EnrollmentError("hub_http_401")
        return original(config, send=transport([]))
    monkeypatch.setattr(enrollment, "ensure_hub_connection", verify)
    provider = ReloadableMemoryConfig(runtime)
    worker = MemorySyncWorker(provider.get)
    monkeypatch.setitem(sync._WORKERS, runtime.repo_root.resolve(), worker)
    worker.run_once()
    assert worker.connection_status["status"] == "ready"
    credential = local_hub_credentials_path(runtime.repo_root)
    data = json.loads(credential.read_text(encoding="utf-8"))
    data["token"] = "different-unverified-token"
    credential.write_text(json.dumps(data), encoding="utf-8")
    assert sync.hub_connection_status(runtime.repo_root)["status"] == "configured"
    worker.run_once()
    assert worker.connection_status == {"status": "connection_failed", "reason": "hub_http_401"}


def test_existing_credential_is_probed_after_worker_restart(runtime, monkeypatch):
    original = enrollment.ensure_hub_connection
    monkeypatch.setattr(enrollment, "ensure_hub_connection", lambda config: original(config, send=transport([])))
    provider = ReloadableMemoryConfig(runtime)
    MemorySyncWorker(provider.get).run_once()
    restarted = MemorySyncWorker(provider.get)
    restarted.run_once()
    assert restarted.connection_status["status"] == "ready"
