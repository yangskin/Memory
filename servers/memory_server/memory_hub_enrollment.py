"""Client-neutral, background-only Hub enrollment and read-only readiness probes."""
from __future__ import annotations

import csv
from dataclasses import asdict, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, Request
from uuid import uuid4

from .memory_config import load_config, local_hub_credentials_path
from .memory_locks import file_lock
from .memory_sync_client import _SSL_CONTEXT


class EnrollmentError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(config, method, path, payload=None):
    """Never forward credentials to redirects or print remote error bodies."""
    url = urlsplit(config.server_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", config.project_id)):
        raise EnrollmentError("invalid_hub_address")
    req = Request(config.server_url.rstrip("/") + "/v1/projects/" + config.project_id + path,
                  data=json.dumps(payload).encode() if payload is not None else None,
                  headers={"Authorization": "Bearer " + config.token, "Content-Type": "application/json"},
                  method=method)
    try:
        with build_opener(_NoRedirect(), HTTPSHandler(context=_SSL_CONTEXT)).open(req, timeout=5) as response:
            data = response.read(262145)
            if len(data) > 262144:
                raise EnrollmentError("hub_response_too_large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise EnrollmentError("invalid_hub_response")
            return result
    except EnrollmentError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        raise EnrollmentError("hub_http_" + str(code) if isinstance(code, int) else "hub_unavailable") from None


def _save_private(path: Path, payload: dict):
    """Restrict access before writing secrets; replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hub-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name == "nt":
                who = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                                     capture_output=True, text=True, check=True)
                sid = next(csv.reader(who.stdout.strip().splitlines()))[1]
                if not re.fullmatch(r"S-1-[0-9-]+", sid):
                    raise EnrollmentError("credential_acl_failed")
                acl = subprocess.run(["icacls.exe", temporary, "/inheritance:r", "/grant:r", "*" + sid + ":(F)"],
                                     capture_output=True)
                if acl.returncode:
                    raise EnrollmentError("credential_acl_failed")
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _probe(config, send):
    info = send(config, "GET", "/connection")
    required = ("board_read", "board_write", "tasks_read", "tasks_write", "project_brief")
    if (info.get("ok") is not True or info.get("project_id") != config.project_id
            or not isinstance(info.get("user_id"), str) or not info["user_id"]
            or any(info.get("capabilities", {}).get(key) is not True for key in required)):
        raise EnrollmentError("hub_capabilities_incomplete")
    # Exercise actual read routes, not just a declaration of scopes.
    board = send(config, "POST", "/board/query", {"filter": "unresolved", "max_items": 1})
    tasks = send(config, "GET", "/tasks?limit=1")
    brief = send(config, "POST", "/context", {"agent_instance_id": "memory-mcp-enrollment",
                 "include": ["project_brief"], "max_items": 1})
    if (not isinstance(board.get("items"), list) or not isinstance(tasks.get("items"), list)
            or not isinstance(brief.get("freshness"), dict)):
        raise EnrollmentError("invalid_hub_response")
    snapshot = brief.get("project_brief")
    if snapshot is not None and not isinstance(snapshot, dict):
        raise EnrollmentError("invalid_hub_response")
    generated_at = (snapshot or {}).get("generated_at")
    return {"status": "ready", "checks": list(required), "checked_at": datetime.now(UTC).isoformat(),
            "project_brief_available": bool(snapshot),
            "project_brief_generated_at": generated_at if isinstance(generated_at, str) else None}


def ensure_hub_connection(runtime, *, send=_request):
    """One bounded background attempt, shared across all MCP clients on this workspace.

    Never rotate a rejected/revoked credential automatically. A persisted request
    survives lost responses, failed probes, and process restarts.
    """
    config = runtime.shared_memory
    if not config.enabled:
        return {"status": "disabled"}
    if not config.server_url or not config.project_id:
        return {"status": "configuration_required"}
    if config.token:
        return _probe(config, send)
    if os.getenv(config.token_env, "").strip() or config.local_token.strip():
        return {"status": "credential_rejected"}
    if not config.auto_enroll:
        return {"status": "enrollment_disabled"}
    invitation = os.getenv(config.enrollment_token_env, "").strip()
    if not invitation:
        return {"status": "enrollment_credential_required", "environment_variable": config.enrollment_token_env}
    if not re.fullmatch(r"mem_v1\.tok_[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]+", invitation):
        return {"status": "invalid_enrollment_credential"}
    credentials = local_hub_credentials_path(runtime.repo_root)
    state_path = credentials.parent / "hub-enrollment.json"
    with file_lock(runtime.repo_root, state_path, timeout=0.1):
        # Another client may already have completed enrollment while we waited.
        current = load_config(runtime.repo_root, runtime.config_path).shared_memory
        if current.token:
            return _probe(current, send)
        if current != config:
            return {"status": "configuration_changed"}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if (state["server_url"] != config.server_url or state["project_id"] != config.project_id
                        or not re.fullmatch(r"mem_v1\.tok_[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{58}", state["token"])
                        or not re.fullmatch(r"[a-f0-9]{32}", state["enrollment_id"])
                        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state["poll_secret"])
                        or not re.fullmatch(r"[^\W_][\w.:-]{0,95}", state["device_name"])):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise EnrollmentError("invalid_enrollment_state") from None
        else:
            device = socket.gethostname()
            if not re.fullmatch(r"[^\W_][\w.:-]{0,95}", device):
                raise EnrollmentError("invalid_device_name")
            state = {"server_url": config.server_url, "project_id": config.project_id,
                     "enrollment_id": uuid4().hex, "device_name": device,
                     "token": "mem_v1.tok_" + secrets.token_urlsafe(18) + "." + secrets.token_urlsafe(43),
                     "poll_secret": secrets.token_urlsafe(32)}
            _save_private(state_path, state)
        token = state["token"]
        payload = {"enrollment_id": state["enrollment_id"], "device_name": state["device_name"],
                   "token_id": token.split(".")[1],
                   "token_secret_hash": hashlib.sha256(token.split(".")[2].encode()).hexdigest(),
                   "poll_secret_hash": hashlib.sha256(state["poll_secret"].encode()).hexdigest()}
        invitation_config = replace(config, local_token=invitation, token_env="", legacy_token_allow_sha256="")
        result = send(invitation_config, "POST", "/device-enrollments/auto", payload)
        if (result.get("status") != "approved" or result.get("enrollment_id") != state["enrollment_id"]
                or result.get("device_name") != state["device_name"]
                or result.get("pairing_code") != payload["poll_secret_hash"][:12].upper()):
            raise EnrollmentError("enrollment_not_confirmed")
        device_config = replace(config, local_token=token, token_env="", legacy_token_allow_sha256="")
        ready = _probe(device_config, send)
        # Recheck administrator/local changes before publishing the credential.
        if load_config(runtime.repo_root, runtime.config_path).shared_memory != config:
            return {"status": "configuration_changed"}
        saved = asdict(config)
        saved.pop("local_token")
        saved.pop("user_id")
        saved.update(token=token, legacy_token_allow_sha256="")
        _save_private(credentials, saved)
        state_path.unlink()
        return {**ready, "registered": True}
