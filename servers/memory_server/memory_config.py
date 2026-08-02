from __future__ import annotations

import json
import hashlib
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ALLOWED_ROOTS = [".ai-context", "memory-bank"]
DEFAULT_EXCLUDED_DIRS = ["Binaries", "Intermediate", "DerivedDataCache", "Saved/Cooked"]

# Single source of truth for the built-in tag controlled vocabulary.
# memory_records imports this list so both the runtime validator and the
# default config stay in sync (previously they drifted as two parallel lists).
DEFAULT_ALLOWED_TAGS: list[str] = [
    "archive_candidate",
    "asset_pipeline",
    "build",
    "handoff_ready",
    "high_value",
    "material",
    "mcp",
    "needs_validation",
    "skill_possible",
    "texture",
    "ui",
    "validation",
    "workflow",
]

DEFAULT_CONFIG_CONTENT: dict[str, Any] = {
    "allowed_roots": DEFAULT_ALLOWED_ROOTS,
    "excluded_dirs": DEFAULT_EXCLUDED_DIRS,
    "max_file_size_bytes": 1_048_576,
    "skip_binary_files": True,
    "events_file": ".ai-memory/events.jsonl",
    "backups_dir": ".ai-memory/backups",
    "temp_dir": ".ai-memory/temp",
    "multi_user": {
        "user_scoped_paths": [
            "memory-bank/activeContext.md",
        ],
        "shared_paths_policy": {
            "memory-bank/teamContext.md": "append_only",
            "memory-bank/progress.md": "append_only",
            "memory-bank/techContext.md": "append_only",
            "memory-bank/systemPatterns.md": "append_only",
            "memory-bank/projectbrief.md": "append_only",
        },
    },
    "backup": {
        "max_total_bytes": 524_288,
        "max_file_bytes": 262_144,
        "max_batches": 5,
    },
    "guard": {
        "default_max_chars": 12_000,
        "default_max_tokens": 3_000,
        "total_max_chars": 60_000,
        "total_max_tokens": 15_000,
        "targets": [
            {"path": ".ai-context/current-task.md", "max_chars": 6_000, "policy": "hot_task", "role": "hot task context for current working session"},
            {"path": ".ai-context/latest-error.md", "max_chars": 4_000, "policy": "error_summary", "role": "latest valid error summary"},
            {"path": "memory-bank/activeContext.md", "max_chars": 8_000, "policy": "warm_context", "role": "current sprint focus, recent decisions, TODOs", "write_policy": "user_scoped"},
            {"path": "memory-bank/teamContext.md", "max_chars": 10_000, "policy": "warm_context", "role": "team-wide current focus and shared decisions", "write_policy": "append_only"},
            {"path": "memory-bank/progress.md", "max_chars": 12_000, "policy": "warm_context", "role": "feature completion status, milestones", "write_policy": "append_only"},
            {"path": "memory-bank/techContext.md", "max_chars": 10_000, "policy": "warm_context", "role": "tech stack, plugin matrix, architecture config", "write_policy": "append_only"},
            {"path": "memory-bank/systemPatterns.md", "max_chars": 10_000, "policy": "warm_context", "role": "architecture patterns, coding conventions, design decisions", "write_policy": "append_only"},
            {"path": "memory-bank/projectbrief.md", "max_chars": 8_000, "policy": "warm_context", "role": "project scope, core requirements, MVP goals", "write_policy": "append_only"},
        ],
    },
    "governance": {
        "min_confidence": 0.0,
        "require_source_refs_for": [],
        "publish_owners": [],
        "reviewers": [],
    },
    "tag_schema": {
        "allowed_tags": list(DEFAULT_ALLOWED_TAGS),
        "version": "v1",
    },
    "mcp": {
        "fsync_strict": False,
    },
    # 常驻 MCP 与后台任务共享同一运行时配置。worker 默认启用，但在没有
    # 队列任务时只写低频心跳，不会调用 LLM。
    "worker": {
        "enabled": True,
        "startup_grace_seconds": 2.0,
        "poll_seconds": 1.0,
        "max_jobs_per_tick": 4,
        "lease_seconds": 120,
        "max_attempts": 4,
        "retry_base_seconds": 2.0,
        "history_limit": 500,
    },
    # 项目级后台反思默认关闭；具体项目显式开启并配置 LLM capability 后
    # 才会消费 token。所有产物均为可替换的 distilled 记录，原始记录不变。
    "reflection": {
        "enabled": False,
        "trigger_phases": ["task_done", "test_failed"],
        "max_evidence_records": 256,
        "max_evidence_chars": 1_000_000,
        "max_candidates": 8,
        "min_confidence": 0.85,
        "auto_publish": True,
        "publish_min_confidence": 0.95,
        "publish_repeated_tasks": 2,
        "publish_with_validation_evidence": True,
        "curator_enabled": True,
        "curator_interval_hours": 24,
        "history_limit": 200,
        "archive_after_days": 90,
    },
    "record_packing": {
        "max_record_chars": 2_000,
        "max_pack_chars": 64_000,
        "archive_after_days": 90,
        "archive_pack_max_chars": 1_048_576,
        "max_active_pack_files": 500,
        "max_single_record_files": 2_000,
        "max_archive_pack_files": 2_000,
    },
    "key_documents": {
        # Mode for derived key documents: per-user activeContext plus shared
        # teamContext / progress / techContext / systemPatterns.
        # - "auto":     rebuild_key_documents is allowed and the writer
        #               will overwrite the in-place file with a generated body
        #               (manual edits archived first). [default]
        # - "manual":   rebuild_key_documents returns error=key_documents_manual_mode
        #               so legacy v0.6 hand-written workflow keeps working.
        # - "disabled": same as manual but also intended for environments
        #               where the four files are managed by an external tool.
        "mode": "auto",
        # Renderer preference order. The first available tier wins; lower
        # tiers are tried only as a fallback when the higher tier raises.
        # "embedding" is reserved for a future RAG-backed tier and is
        # currently equivalent to "deterministic".
        "renderers": {
            "prefer_order": ["llm", "deterministic"],
        },
        # Agent-first automatic memory settling. Uses deterministic rendering
        # by default; LLM is only an optional gate and never blocks writes.
        "auto_rebuild": {
            "enabled": True,
            "after_successful_writes": 5,
            "renderer": "deterministic",
            "targets": ["activeContext", "teamContext", "progress", "techContext", "systemPatterns"],
            "count_operations": ["record", "observation", "memory_write_record"],
            "phase_triggers": ["plan_confirmed", "test_passed", "stable_pattern_found", "task_done"],
            "llm_gate": "when_available",
            "async": True,
            "guard_prefer_llm": False,
        },
        # Promote high-signal personal/task records into a derived
        # project_shared record so shared key documents can settle team facts
        # without requiring every agent to remember scope=project_shared.
        "auto_team_settlement": {
            "enabled": True,
            "llm_gate": "when_available",
            "target_scope": "project_shared",
            "max_summary_chars": 1200,
        },
        "active_context_auto_archive": {
            "enabled": True,
            "archive_dir": "memory-bank/archive/activeContext",
            "policy": "warm_context",
        },
    },
    # P5 Phase 1 — local CPU-only RAG / vector supplement (see
    # MemorySystemDesignDocument.md §15.4).  All defaults are conservative:
    # the tier is OFF until the user explicitly opts in, and even when on
    # it never triggers a network call (provider=auto picks the best
    # locally-available CPU provider; missing models silently fall back).
    "embeddings": {
        "enabled": False,
        "provider": "auto",
        "model_path": ".ai-memory/models/bge-small-zh.onnx",
        "index_dir": ".ai-memory/vector_index",
        "max_batch": 32,
        "max_index_chunks": 100_000,
        "rebuild_on_provider_change": True,
    },
    # v0.10.0 — unified LLM capability defaults (see
    # MemorySystemDesignDocument.md §15.3 / memory_llm_runner.py).
    # All capabilities OFF by default — v0.10.0 only ships the *runner*;
    # flipping a capability to enabled is an explicit per-user decision so
    # the cost surface stays predictable.  Per-capability blocks override
    # the global "enabled" / "timeout" / "max_tokens" knobs.
    "llm_defaults": {
        "enabled": False,
        "timeout": None,
        "max_tokens": None,
        "capabilities": {
            # "distill_summary": {"enabled": True, "timeout": 60},
            # "summarize_recall": {"enabled": True},
            # "rebuild_key_document": {"enabled": True, "timeout": 90},
            # "query_rewrite": {"enabled": True, "timeout": 30, "max_tokens": 256},
            # "snapshot_narrative": {"enabled": True, "timeout": 60},
        },
    },
}


@dataclass(frozen=True)
class GuardTarget:
    path: str
    max_chars: int | None
    max_tokens: int | None
    policy: str | None
    suggestion: str | None
    role: str | None = None
    write_policy: str | None = None  # "append_only" | "user_scoped" | None


@dataclass(frozen=True)
class MultiUserConfig:
    """Always-on multi-user collaboration policy."""

    user_scoped_paths: list[str] | None = None  # 需要按用户分区的路径列表
    shared_paths_policy: dict[str, str] | None = None  # 共享路径 → 写入策略映射


@dataclass(frozen=True)
class KeyDocumentsAutoRebuildConfig:
    """Write-count based key-document rebuild trigger."""

    enabled: bool = True
    after_successful_writes: int = 5
    renderer: str = "deterministic"
    targets: list[str] | None = None
    count_operations: list[str] | None = None
    phase_triggers: list[str] | None = None
    llm_gate: str = "when_available"
    async_enabled: bool = True
    guard_prefer_llm: bool = False


@dataclass(frozen=True)
class KeyDocumentsAutoTeamSettlementConfig:
    """Automatic promotion of team-relevant personal records."""

    enabled: bool = True
    llm_gate: str = "when_available"
    target_scope: str = "project_shared"
    max_summary_chars: int = 1200


@dataclass(frozen=True)
class MemoryConfig:
    repo_root: Path
    config_path: Path
    config_hash: str
    config_source_hash: str
    config_mtime_ns: int
    config_size: int
    allowed_roots: list[Path]
    excluded_dirs: list[str]
    max_file_size_bytes: int
    skip_binary_files: bool
    events_file: Path
    backups_dir: Path
    temp_dir: Path
    guard_default_max_chars: int | None
    guard_default_max_tokens: int | None
    guard_targets: list[GuardTarget]
    guard_total_max_chars: int | None = None
    guard_total_max_tokens: int | None = None
    backup_max_file_bytes: int | None = None
    backup_max_total_bytes: int | None = None
    backup_max_batches: int | None = None
    multi_user: MultiUserConfig | None = None
    governance_min_confidence: float = 0.0
    governance_require_source_refs_for: list[str] | None = None
    governance_publish_owners: list[str] | None = None
    governance_reviewers: list[str] | None = None
    tag_allowed_tags: list[str] | None = None
    tag_schema_version: str = "v1"
    mcp_fsync_strict: bool = False
    mcp_allow_unknown_user: bool = False
    mcp_shared_overwrite_policy: str = "reject"  # "reject" | "downgrade"
    mcp_auto_maintenance: dict[str, Any] | None = None
    worker: dict[str, Any] | None = None
    reflection: dict[str, Any] | None = None
    key_documents_mode: str = "auto"
    key_documents_prefer_order: tuple[str, ...] = ("llm", "deterministic")
    key_documents_auto_rebuild: KeyDocumentsAutoRebuildConfig | None = None
    key_documents_auto_team_settlement: KeyDocumentsAutoTeamSettlementConfig | None = None
    key_documents_active_context_auto_archive: dict[str, Any] | None = None
    # P5 Phase 1 — see MemorySystemDesignDocument.md §15.4
    embeddings_enabled: bool = False
    embeddings_provider: str = "auto"
    embeddings_model_path: Path | None = None
    embeddings_index_dir: Path | None = None
    embeddings_max_batch: int = 32
    embeddings_max_index_chunks: int = 100_000
    embeddings_rebuild_on_provider_change: bool = True
    # v0.10.0 — unified LLM capability defaults; consumed by
    # :mod:`memory_llm_runner`. Stored as a free-form dict so adding new
    # knobs does not churn the dataclass.
    llm_defaults: dict[str, Any] | None = None
    record_packing_max_record_chars: int = 2_000
    record_packing_max_pack_chars: int = 64_000
    record_packing_archive_after_days: int = 90
    record_packing_archive_pack_max_chars: int = 1_048_576
    record_packing_max_active_pack_files: int = 500
    record_packing_max_single_record_files: int = 2_000
    record_packing_max_archive_pack_files: int = 2_000

    def repo_relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.repo_root).as_posix()


class MemoryConfigError(ValueError):
    """配置文件无效；常驻进程应保留最后一个已知良好版本。"""


class ReloadableMemoryConfig:
    """线程安全的运行时配置提供器。

    每次 MCP 请求和 worker tick 都会比较配置文件 stat。变更后加载完整新
    配置；若新文件损坏，则继续使用最后一个已知良好配置并暴露 reload_error，
    防止 CLI 与常驻 MCP 静默分叉或在半写 JSON 上回退到默认值。
    """

    def __init__(self, config: MemoryConfig):
        self._config = config
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._reload_count = 0

    def get(self) -> MemoryConfig:
        with self._lock:
            try:
                raw = self._config.config_path.read_bytes()
                source_hash = hashlib.sha256(raw).hexdigest()
            except OSError as exc:
                self._last_error = f"config read failed: {exc}"
                return self._config
            if source_hash == self._config.config_source_hash:
                # 文件被暂时删除/写坏后恢复为最后一个已知良好版本时，旧错误
                # 必须清除，否则诊断会永久误报配置仍处于降级状态。
                self._last_error = None
                return self._config
            try:
                refreshed = load_config(self._config.repo_root, self._config.config_path)
            except (OSError, MemoryConfigError, TypeError, ValueError, OverflowError) as exc:
                self._last_error = str(exc)
                return self._config
            self._config = refreshed
            self._last_error = None
            self._reload_count += 1
            return self._config

    def diagnostics(self) -> dict[str, Any]:
        config = self.get()
        return {
            "config_hash": config.config_hash,
            "config_source_hash": config.config_source_hash,
            "config_mtime_ns": config.config_mtime_ns,
            "config_size": config.config_size,
            "reload_count": self._reload_count,
            "reload_error": self._last_error,
        }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_runtime_blocks(config: dict[str, Any]) -> None:
    """拒绝会令常驻 worker 在运行期崩溃的语义错误配置。"""

    def require_object(name: str) -> dict[str, Any]:
        value = config.get(name)
        if not isinstance(value, dict):
            raise MemoryConfigError(f"config field {name} must be an object")
        return value

    def require_bool(block_name: str, block: dict[str, Any], key: str) -> None:
        if not isinstance(block.get(key), bool):
            raise MemoryConfigError(f"config field {block_name}.{key} must be a boolean")

    def require_number(
        block_name: str,
        block: dict[str, Any],
        key: str,
        *,
        minimum: float,
        integer: bool = False,
    ) -> None:
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise MemoryConfigError(f"config field {block_name}.{key} must be a finite number")
        if float(value) < minimum or (integer and int(value) != value):
            qualifier = f"an integer >= {int(minimum)}" if integer else f">= {minimum}"
            raise MemoryConfigError(f"config field {block_name}.{key} must be {qualifier}")

    worker = require_object("worker")
    require_bool("worker", worker, "enabled")
    for key, minimum, integer in (
        ("startup_grace_seconds", 0.0, False),
        ("poll_seconds", 0.05, False),
        ("max_jobs_per_tick", 1.0, True),
        ("lease_seconds", 1.0, False),
        ("max_attempts", 1.0, True),
        ("retry_base_seconds", 0.0, False),
        ("history_limit", 0.0, True),
    ):
        require_number("worker", worker, key, minimum=minimum, integer=integer)

    reflection = require_object("reflection")
    for key in (
        "enabled",
        "auto_publish",
        "publish_with_validation_evidence",
        "curator_enabled",
    ):
        require_bool("reflection", reflection, key)
    phases = reflection.get("trigger_phases")
    if not isinstance(phases, list) or not all(isinstance(item, str) and item.strip() for item in phases):
        raise MemoryConfigError("config field reflection.trigger_phases must be a list of non-empty strings")
    for key, minimum, integer in (
        ("max_evidence_records", 1.0, True),
        ("max_evidence_chars", 1000.0, True),
        ("max_candidates", 1.0, True),
        ("min_confidence", 0.0, False),
        ("publish_min_confidence", 0.0, False),
        ("publish_repeated_tasks", 2.0, True),
        ("curator_interval_hours", 0.0, False),
        ("history_limit", 0.0, True),
        ("archive_after_days", 0.0, True),
    ):
        require_number("reflection", reflection, key, minimum=minimum, integer=integer)
    for key in ("min_confidence", "publish_min_confidence"):
        if float(reflection[key]) > 1.0:
            raise MemoryConfigError(f"config field reflection.{key} must be <= 1.0")
    curator = reflection.get("curator")
    if curator is not None:
        if not isinstance(curator, dict):
            raise MemoryConfigError("config field reflection.curator must be an object")
        if "min_distinct_tasks" in curator:
            require_number("reflection.curator", curator, "min_distinct_tasks", minimum=2.0, integer=True)
        if "publish_confidence" in curator:
            require_number("reflection.curator", curator, "publish_confidence", minimum=0.0)
            if float(curator["publish_confidence"]) > 1.0:
                raise MemoryConfigError("config field reflection.curator.publish_confidence must be <= 1.0")


def _to_repo_path(repo_root: Path, value: str) -> Path:
    return (repo_root / value).resolve()


def _parse_guard_targets(raw_targets: Any) -> list[GuardTarget]:
    if not isinstance(raw_targets, list):
        return []
    parsed: list[GuardTarget] = []
    for item in raw_targets:
        if isinstance(item, str):
            parsed.append(GuardTarget(path=item, max_chars=None, max_tokens=None, policy=None, suggestion=None))
            continue
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        max_chars = item.get("max_chars")
        max_tokens = item.get("max_tokens")
        parsed.append(
            GuardTarget(
                path=path,
                max_chars=int(max_chars) if isinstance(max_chars, (int, float)) else None,
                max_tokens=int(max_tokens) if isinstance(max_tokens, (int, float)) else None,
                policy=str(item.get("policy")).strip() if item.get("policy") else None,
                suggestion=str(item.get("suggestion")).strip() if item.get("suggestion") else None,
                role=str(item.get("role")).strip() if item.get("role") else None,
                write_policy=str(item.get("write_policy")).strip() if item.get("write_policy") else None,
            )
        )
    return parsed


def _parse_multi_user(raw: Any) -> MultiUserConfig | None:
    """解析 multi_user 配置节。"""
    if not isinstance(raw, dict):
        return None
    user_scoped_paths = raw.get("user_scoped_paths")
    if isinstance(user_scoped_paths, list):
        user_scoped_paths = [str(p).strip() for p in user_scoped_paths if str(p).strip()]
    else:
        user_scoped_paths = None
    shared_paths_policy = raw.get("shared_paths_policy")
    if isinstance(shared_paths_policy, dict):
        shared_paths_policy = {str(k).strip(): str(v).strip() for k, v in shared_paths_policy.items() if str(k).strip()}
    else:
        shared_paths_policy = None
    return MultiUserConfig(
        user_scoped_paths=user_scoped_paths,
        shared_paths_policy=shared_paths_policy,
    )


def _parse_record_packing(raw: Any) -> dict[str, Any]:
    defaults = DEFAULT_CONFIG_CONTENT["record_packing"]
    if not isinstance(raw, dict):
        raw = {}

    def _positive_int(name: str) -> int:
        value = raw.get(name, defaults[name])
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        return int(defaults[name])

    max_record_chars = _positive_int("max_record_chars")
    max_pack_chars = max(_positive_int("max_pack_chars"), max_record_chars * 2)
    archive_pack_max_chars = max(_positive_int("archive_pack_max_chars"), max_pack_chars)
    return {
        "record_packing_max_record_chars": max_record_chars,
        "record_packing_max_pack_chars": max_pack_chars,
        "record_packing_archive_after_days": _positive_int("archive_after_days"),
        "record_packing_archive_pack_max_chars": archive_pack_max_chars,
        "record_packing_max_active_pack_files": _positive_int("max_active_pack_files"),
        "record_packing_max_single_record_files": _positive_int("max_single_record_files"),
        "record_packing_max_archive_pack_files": _positive_int("max_archive_pack_files"),
    }


_VALID_KEY_DOC_MODES = {"auto", "manual", "disabled"}
_VALID_KEY_DOC_RENDERERS = {"deterministic", "llm", "embedding"}
_VALID_KEY_DOC_AUTORUN_RENDERERS = {"auto", "deterministic", "llm", "embedding"}
_VALID_KEY_DOC_KEYS = {"activeContext", "teamContext", "progress", "techContext", "systemPatterns"}
_VALID_KEY_DOC_AUTORUN_OPS = {"file", "record", "observation", "memory_write_record"}
_VALID_KEY_DOC_PHASES = {
    "exploration",
    "plan_confirmed",
    "implementation",
    "test_failed",
    "test_passed",
    "stable_pattern_found",
    "task_done",
}
_VALID_KEY_DOC_LLM_GATE = {"off", "when_available", "always"}


def _parse_key_documents_mode(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "auto"
    mode = str(raw.get("mode") or "auto").strip().lower()
    if mode not in _VALID_KEY_DOC_MODES:
        return "auto"
    return mode


def _parse_key_documents_prefer_order(raw: Any) -> tuple[str, ...]:
    default = ("llm", "deterministic")
    if not isinstance(raw, dict):
        return default
    renderers = raw.get("renderers")
    if not isinstance(renderers, dict):
        return default
    order = renderers.get("prefer_order")
    if not isinstance(order, list):
        return default
    cleaned: list[str] = []
    for item in order:
        token = str(item).strip().lower()
        if token in _VALID_KEY_DOC_RENDERERS and token not in cleaned:
            cleaned.append(token)
    if "deterministic" not in cleaned:
        cleaned.append("deterministic")
    if not cleaned:
        return default
    return tuple(cleaned)


def _parse_key_documents_auto_rebuild(raw: Any) -> KeyDocumentsAutoRebuildConfig:
    default_targets = ["activeContext", "teamContext", "progress", "techContext", "systemPatterns"]
    default_ops = ["record", "observation", "memory_write_record"]
    default_phase_triggers = ["plan_confirmed", "test_passed", "stable_pattern_found", "task_done"]
    if not isinstance(raw, dict):
        return KeyDocumentsAutoRebuildConfig(
            targets=default_targets,
            count_operations=default_ops,
            phase_triggers=default_phase_triggers,
        )
    auto = raw.get("auto_rebuild")
    if not isinstance(auto, dict):
        return KeyDocumentsAutoRebuildConfig(
            targets=default_targets,
            count_operations=default_ops,
            phase_triggers=default_phase_triggers,
        )

    threshold_raw = auto.get("after_successful_writes", 5)
    try:
        threshold = int(threshold_raw)
    except (TypeError, ValueError):
        threshold = 5
    threshold = max(1, threshold)

    renderer = str(auto.get("renderer") or "deterministic").strip().lower()
    if renderer not in _VALID_KEY_DOC_AUTORUN_RENDERERS:
        renderer = "deterministic"

    raw_targets = auto.get("targets")
    targets = default_targets
    if isinstance(raw_targets, list):
        cleaned_targets = [
            str(item).strip()
            for item in raw_targets
            if str(item).strip() in _VALID_KEY_DOC_KEYS
        ]
        if cleaned_targets:
            targets = list(dict.fromkeys(cleaned_targets))

    raw_ops = auto.get("count_operations")
    count_operations = default_ops
    if isinstance(raw_ops, list):
        cleaned_ops = [
            str(item).strip()
            for item in raw_ops
            if str(item).strip() in _VALID_KEY_DOC_AUTORUN_OPS
        ]
        if cleaned_ops:
            count_operations = list(dict.fromkeys(cleaned_ops))

    raw_phases = auto.get("phase_triggers")
    phase_triggers = default_phase_triggers
    if isinstance(raw_phases, list):
        cleaned_phases = [
            str(item).strip()
            for item in raw_phases
            if str(item).strip() in _VALID_KEY_DOC_PHASES
        ]
        if cleaned_phases:
            phase_triggers = list(dict.fromkeys(cleaned_phases))

    llm_gate = str(auto.get("llm_gate") or "when_available").strip().lower()
    if llm_gate not in _VALID_KEY_DOC_LLM_GATE:
        llm_gate = "when_available"

    return KeyDocumentsAutoRebuildConfig(
        enabled=bool(auto.get("enabled", True)),
        after_successful_writes=threshold,
        renderer=renderer,
        targets=targets,
        count_operations=count_operations,
        phase_triggers=phase_triggers,
        llm_gate=llm_gate,
        async_enabled=bool(auto.get("async", auto.get("async_enabled", True))),
        guard_prefer_llm=bool(auto.get("guard_prefer_llm", False)),
    )


def _parse_auto_team_settlement(raw: Any) -> KeyDocumentsAutoTeamSettlementConfig:
    defaults = KeyDocumentsAutoTeamSettlementConfig()
    if not isinstance(raw, dict):
        return defaults
    section = raw.get("auto_team_settlement")
    if not isinstance(section, dict):
        return defaults

    llm_gate = str(section.get("llm_gate") or defaults.llm_gate).strip().lower()
    if llm_gate not in _VALID_KEY_DOC_LLM_GATE:
        llm_gate = defaults.llm_gate

    target_scope = str(section.get("target_scope") or defaults.target_scope).strip()
    if target_scope not in {"project_shared", "shared", "org_shared"}:
        target_scope = defaults.target_scope

    try:
        max_summary_chars = int(section.get("max_summary_chars", defaults.max_summary_chars))
    except (TypeError, ValueError):
        max_summary_chars = defaults.max_summary_chars
    max_summary_chars = min(max(240, max_summary_chars), 6000)

    return KeyDocumentsAutoTeamSettlementConfig(
        enabled=bool(section.get("enabled", defaults.enabled)),
        llm_gate=llm_gate,
        target_scope=target_scope,
        max_summary_chars=max_summary_chars,
    )


def _parse_active_context_auto_archive(raw: Any) -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "archive_dir": "memory-bank/archive/activeContext",
        "policy": "warm_context",
    }
    if not isinstance(raw, dict):
        return defaults
    section = raw.get("active_context_auto_archive")
    if not isinstance(section, dict):
        return defaults
    archive_dir = str(section.get("archive_dir") or defaults["archive_dir"]).replace("\\", "/").strip("/")
    if not archive_dir:
        archive_dir = defaults["archive_dir"]
    policy = str(section.get("policy") or defaults["policy"]).strip()
    if policy != "warm_context":
        policy = "warm_context"
    return {
        "enabled": bool(section.get("enabled", defaults["enabled"])),
        "archive_dir": archive_dir,
        "policy": policy,
    }


def _ensure_layout(repo_root: Path) -> None:
    ai_memory = repo_root / ".ai-memory"
    ai_memory.mkdir(parents=True, exist_ok=True)
    (ai_memory / "backups").mkdir(parents=True, exist_ok=True)
    (ai_memory / "temp").mkdir(parents=True, exist_ok=True)
    events_file = ai_memory / "events.jsonl"
    if not events_file.exists():
        events_file.touch()


def _ensure_config_file(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG_CONTENT, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(repo_root: str | Path, config_path: str | Path | None = None) -> MemoryConfig:
    root = Path(repo_root).resolve()
    _ensure_layout(root)

    resolved_config_path = (Path(config_path).resolve() if config_path else (root / ".ai-memory/config.json").resolve())
    _ensure_config_file(resolved_config_path)

    loaded: dict[str, Any] = {}
    try:
        config_bytes = resolved_config_path.read_bytes()
        loaded = json.loads(config_bytes.decode("utf-8", errors="strict"))
    except FileNotFoundError as exc:
        raise MemoryConfigError(f"config file disappeared during load: {resolved_config_path}") from exc
    except UnicodeDecodeError as exc:
        raise MemoryConfigError(f"config file is not valid UTF-8: {resolved_config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MemoryConfigError(f"config file is not valid JSON: {resolved_config_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MemoryConfigError(f"config root must be a JSON object: {resolved_config_path}")

    merged = _deep_merge(DEFAULT_CONFIG_CONTENT, loaded)
    _validate_runtime_blocks(merged)
    config_hash = hashlib.sha256(
        json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    config_source_hash = hashlib.sha256(config_bytes).hexdigest()
    config_stat = resolved_config_path.stat()
    guard = merged.get("guard", {}) if isinstance(merged.get("guard"), dict) else {}

    allowed_roots_raw = merged.get("allowed_roots", DEFAULT_ALLOWED_ROOTS)
    if not isinstance(allowed_roots_raw, list) or not allowed_roots_raw:
        allowed_roots_raw = DEFAULT_ALLOWED_ROOTS
    allowed_roots = [_to_repo_path(root, str(item)) for item in allowed_roots_raw]

    excluded_dirs_raw = merged.get("excluded_dirs", DEFAULT_EXCLUDED_DIRS)
    if not isinstance(excluded_dirs_raw, list):
        excluded_dirs_raw = DEFAULT_EXCLUDED_DIRS

    events_file = _to_repo_path(root, str(merged.get("events_file", ".ai-memory/events.jsonl")))
    backups_dir = _to_repo_path(root, str(merged.get("backups_dir", ".ai-memory/backups")))
    temp_dir = _to_repo_path(root, str(merged.get("temp_dir", ".ai-memory/temp")))
    events_file.parent.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    if not events_file.exists():
        events_file.touch()

    backup_cfg = merged.get("backup", {}) if isinstance(merged.get("backup"), dict) else {}
    governance_cfg = merged.get("governance", {}) if isinstance(merged.get("governance"), dict) else {}
    tag_schema_cfg = merged.get("tag_schema", {}) if isinstance(merged.get("tag_schema"), dict) else {}
    mcp_cfg = merged.get("mcp", {}) if isinstance(merged.get("mcp"), dict) else {}

    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    return MemoryConfig(
        repo_root=root,
        config_path=resolved_config_path,
        config_hash=config_hash,
        config_source_hash=config_source_hash,
        config_mtime_ns=int(config_stat.st_mtime_ns),
        config_size=int(config_stat.st_size),
        allowed_roots=allowed_roots,
        excluded_dirs=[str(item).replace("\\", "/").strip("/") for item in excluded_dirs_raw if str(item).strip()],
        max_file_size_bytes=int(merged.get("max_file_size_bytes", 1_048_576)),
        skip_binary_files=bool(merged.get("skip_binary_files", True)),
        events_file=events_file,
        backups_dir=backups_dir,
        temp_dir=temp_dir,
        guard_default_max_chars=(
            int(guard.get("default_max_chars")) if isinstance(guard.get("default_max_chars"), (int, float)) else None
        ),
        guard_default_max_tokens=(
            int(guard.get("default_max_tokens")) if isinstance(guard.get("default_max_tokens"), (int, float)) else None
        ),
        guard_targets=_parse_guard_targets(guard.get("targets", [])),
        guard_total_max_chars=(
            int(guard.get("total_max_chars")) if isinstance(guard.get("total_max_chars"), (int, float)) else None
        ),
        guard_total_max_tokens=(
            int(guard.get("total_max_tokens")) if isinstance(guard.get("total_max_tokens"), (int, float)) else None
        ),
        backup_max_file_bytes=(
            int(backup_cfg.get("max_file_bytes")) if isinstance(backup_cfg.get("max_file_bytes"), (int, float)) else None
        ),
        backup_max_total_bytes=(
            int(backup_cfg.get("max_total_bytes")) if isinstance(backup_cfg.get("max_total_bytes"), (int, float)) else None
        ),
        backup_max_batches=(
            int(backup_cfg.get("max_batches")) if isinstance(backup_cfg.get("max_batches"), (int, float)) else None
        ),
        multi_user=_parse_multi_user(merged.get("multi_user")),
        governance_min_confidence=(
            float(governance_cfg.get("min_confidence"))
            if isinstance(governance_cfg.get("min_confidence"), (int, float))
            else 0.0
        ),
        governance_require_source_refs_for=_string_list(governance_cfg.get("require_source_refs_for")),
        governance_publish_owners=_string_list(governance_cfg.get("publish_owners")),
        governance_reviewers=_string_list(governance_cfg.get("reviewers")),
        tag_allowed_tags=_string_list(tag_schema_cfg.get("allowed_tags")),
        tag_schema_version=str(tag_schema_cfg.get("version", "v1")),
        mcp_fsync_strict=bool(mcp_cfg.get("fsync_strict", False)),
        mcp_allow_unknown_user=bool(mcp_cfg.get("allow_unknown_user", False)),
        mcp_shared_overwrite_policy=(
            str(mcp_cfg.get("shared_overwrite_policy", "reject")).strip().lower()
            if mcp_cfg.get("shared_overwrite_policy") in ("reject", "downgrade")
            else "reject"
        ),
        mcp_auto_maintenance=(
            dict(mcp_cfg.get("auto_maintenance")) if isinstance(mcp_cfg.get("auto_maintenance"), dict) else None
        ),
        worker=(dict(merged.get("worker")) if isinstance(merged.get("worker"), dict) else None),
        reflection=(dict(merged.get("reflection")) if isinstance(merged.get("reflection"), dict) else None),
        key_documents_mode=_parse_key_documents_mode(merged.get("key_documents")),
        key_documents_prefer_order=_parse_key_documents_prefer_order(merged.get("key_documents")),
        key_documents_auto_rebuild=_parse_key_documents_auto_rebuild(merged.get("key_documents")),
        key_documents_auto_team_settlement=_parse_auto_team_settlement(merged.get("key_documents")),
        key_documents_active_context_auto_archive=_parse_active_context_auto_archive(merged.get("key_documents")),
        **_parse_embeddings(root, merged.get("embeddings")),
        llm_defaults=_parse_llm_defaults(merged.get("llm_defaults")),
        **_parse_record_packing(merged.get("record_packing")),
    )


def _parse_llm_defaults(raw: Any) -> dict[str, Any] | None:
    """Parse the optional ``llm_defaults`` config block (v0.10.0).

    Returns ``None`` when the block is absent or empty so :mod:`memory_llm_runner`
    can fall back to its built-in :data:`DEFAULT_CAPABILITY_PROFILES` without
    extra branching.  Unknown keys are preserved verbatim so future capabilities
    can ship config without a config-loader change.
    """

    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key in ("enabled", "timeout", "max_tokens"):
        if key in raw and raw[key] is not None:
            cleaned[key] = raw[key]
    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict):
        cleaned_caps: dict[str, Any] = {}
        for cap_name, cap_overrides in capabilities.items():
            if not isinstance(cap_overrides, dict):
                continue
            inner: dict[str, Any] = {}
            for inner_key in ("enabled", "timeout", "max_tokens"):
                if inner_key in cap_overrides and cap_overrides[inner_key] is not None:
                    inner[inner_key] = cap_overrides[inner_key]
            if inner:
                cleaned_caps[str(cap_name).strip()] = inner
        if cleaned_caps:
            cleaned["capabilities"] = cleaned_caps
    return cleaned or None


_VALID_EMBEDDING_PROVIDERS = {"auto", "deterministic-hash", "local-onnx"}


def _parse_embeddings(repo_root: Path, raw: Any) -> dict[str, Any]:
    """Parse the optional "embeddings" config block.

    Returns kwargs ready to splat into :class:`MemoryConfig`.  Missing or
    invalid fields fall back to the defaults so the vector tier always
    stays OFF unless the user explicitly enables it (see §15.4 "可选 + 可降级").
    """

    defaults = DEFAULT_CONFIG_CONTENT["embeddings"]
    if not isinstance(raw, dict):
        raw = {}

    provider = str(raw.get("provider", defaults["provider"])).strip().lower() or "auto"
    if provider not in _VALID_EMBEDDING_PROVIDERS:
        provider = "auto"

    model_path_raw = raw.get("model_path", defaults["model_path"])
    model_path = (
        _to_repo_path(repo_root, str(model_path_raw))
        if isinstance(model_path_raw, str) and model_path_raw.strip()
        else None
    )

    index_dir_raw = raw.get("index_dir", defaults["index_dir"])
    index_dir = (
        _to_repo_path(repo_root, str(index_dir_raw))
        if isinstance(index_dir_raw, str) and index_dir_raw.strip()
        else _to_repo_path(repo_root, str(defaults["index_dir"]))
    )

    def _pos_int(value: Any, fallback: int) -> int:
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        return fallback

    return {
        "embeddings_enabled": bool(raw.get("enabled", defaults["enabled"])),
        "embeddings_provider": provider,
        "embeddings_model_path": model_path,
        "embeddings_index_dir": index_dir,
        "embeddings_max_batch": _pos_int(raw.get("max_batch"), int(defaults["max_batch"])),
        "embeddings_max_index_chunks": _pos_int(
            raw.get("max_index_chunks"), int(defaults["max_index_chunks"])
        ),
        "embeddings_rebuild_on_provider_change": bool(
            raw.get("rebuild_on_provider_change", defaults["rebuild_on_provider_change"])
        ),
    }
