"""稳定身份规范化。

同一用户/代理在不同客户端里可能出现大小写、全半角和空白差异。身份一旦
进入目录名、task binding 或可见性过滤，就必须先规范化，否则会产生平行的
个人记忆分区。这里仅处理身份，不负责认证。
"""

from __future__ import annotations

import re
import unicodedata
import hashlib
import json
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .memory_locks import file_lock

_DASH_RE = re.compile(r"-+")
_PLACEHOLDER = "unknown"
_SESSION_LOCK = threading.Lock()
_SESSION_IDS: dict[str, str] = {}


@dataclass(frozen=True)
class CanonicalIdentity:
    raw: str
    canonical: str
    changed: bool
    alias_used: bool = False


@dataclass(frozen=True)
class RuntimeIdentity:
    agent_id: str
    agent_instance_id: str
    source_node_id: str
    source_node_name: str
    workspace_id: str
    session_id: str


def load_runtime_identity(repo_root: Path, args: Mapping[str, object] | None = None) -> RuntimeIdentity:
    """Return stable node/agent identity, creating only local metadata when absent."""
    args = args or {}
    repo_root = repo_root.resolve()
    path = repo_root / ".ai-memory" / "identity.json"
    with file_lock(repo_root, path):
        data: dict[str, object] = {}
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
        source_node_id = _bounded(data.get("source_node_id") or uuid.uuid4(), 256, "unknown-node")
        source_node_name = _bounded(args.get("source_node_name") or socket.gethostname(), 256, "unknown-host")
        agent_id = _bounded(args.get("agent_id") or os.getenv("MEMORY_AGENT_ID") or data.get("agent_id"), 256, "unknown-agent")
        agent_instance_id = _bounded(args.get("agent_instance_id") or os.getenv("MEMORY_AGENT_INSTANCE_ID") or data.get("agent_instance_id") or uuid.uuid4(), 256, "unknown-agent-instance")
        workspace_id = _bounded(data.get("workspace_id") or "sha256:" + hashlib.sha256(str(repo_root).encode()).hexdigest(), 256, "unknown-workspace")
        updated = {"agent_id": agent_id, "agent_instance_id": agent_instance_id, "source_node_id": source_node_id, "source_node_name": source_node_name, "workspace_id": workspace_id}
        if data != updated:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
    explicit_session_id = args.get("agent_session_id") or os.getenv("MEMORY_AGENT_SESSION_ID")
    if explicit_session_id:
        session_id = _bounded(explicit_session_id, 256, "unknown-session")
    else:
        workspace_key = str(repo_root)
        with _SESSION_LOCK:
            session_id = _SESSION_IDS.setdefault(workspace_key, str(uuid.uuid4()))
    return RuntimeIdentity(agent_id, agent_instance_id, source_node_id, source_node_name, workspace_id, session_id)


def _bounded(value: object, max_length: int, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    return text[:max_length]


def _base_canonical(value: object) -> str:
    if not isinstance(value, str):
        return _PLACEHOLDER
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized:
        return _PLACEHOLDER
    out: list[str] = []
    for char in normalized:
        if char.isalnum() or char in {".", "_", "@", "-"}:
            out.append(char)
        elif char.isspace() or char in {"/", "\\", ":"}:
            out.append("-")
        # 其它标点不进入路径身份；用分隔符保留词边界。
        else:
            out.append("-")
    canonical = _DASH_RE.sub("-", "".join(out)).strip("-._")
    return canonical or _PLACEHOLDER


def canonicalize_identity(
    value: object,
    *,
    aliases: Mapping[str, str] | None = None,
) -> CanonicalIdentity:
    raw = value if isinstance(value, str) else ""
    canonical = _base_canonical(value)
    alias_used = False
    if aliases:
        normalized_aliases = {
            _base_canonical(source): _base_canonical(target)
            for source, target in aliases.items()
            if _base_canonical(source) != _PLACEHOLDER and _base_canonical(target) != _PLACEHOLDER
        }
        replacement = normalized_aliases.get(canonical)
        if replacement:
            canonical = replacement
            alias_used = True
    return CanonicalIdentity(
        raw=raw,
        canonical=canonical,
        changed=canonical != raw.strip(),
        alias_used=alias_used,
    )


def canonical_identity(value: object, *, aliases: Mapping[str, str] | None = None) -> str:
    return canonicalize_identity(value, aliases=aliases).canonical


__all__ = ["CanonicalIdentity", "canonical_identity", "canonicalize_identity"]
