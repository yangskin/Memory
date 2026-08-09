"""Unified LLM capability runner — single point for opt-in / timeout / fallback.

Before this module each LLM call site (distill, recall summary, key-document
LLM tier, …) re-implemented its own ``_build_llm_client`` + ad-hoc try/except
+ default-disabled posture. v0.10.0 collapses that into a single, declarative
helper so callers can answer three questions in one call:

1. **Is this capability enabled?** Per-capability override > global default.
   Any capability MUST be registered in :mod:`memory_llm_policy`'s capability
   matrix as ``llm`` or ``hybrid``; deterministic capabilities are rejected
   loudly so we never accidentally route around the deterministic path.
2. **What is the per-call timeout?** Capability override > global default >
   :class:`~memory_llm.LLMConfig` default. We honour the smallest *positive*
   value so a capability cannot quietly raise the cap.
3. **What happens on failure?** ``run_llm_capability`` always returns a
   structured envelope ``{ok, status, ...}`` — callers never see a raw
   exception escape. ``status`` ∈ {``ok``, ``disabled``, ``unavailable``,
   ``timeout``, ``budget_exceeded``, ``failed``}. Optional ``fallback`` is
   invoked when the LLM cannot produce a result so the deterministic path
   continues to provide a value.

All knobs are read from :class:`~memory_config.MemoryConfig.llm_defaults`,
which is loaded from the ``llm_defaults`` block in ``.ai-memory/config.json``.
The block is fully optional — when absent every capability defaults to
``enabled=False`` (current opt-in posture). v0.10.0 does NOT flip the global
default; flipping requires an explicit user decision per capability so the
cost surface stays predictable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .memory_config import MemoryConfig
from .memory_llm_policy import capability_owner

logger = logging.getLogger(__name__)


# ── Capability defaults ───────────────────────────────────────────────


# Default per-capability knobs.  Each capability that wants to be runnable
# via :func:`run_llm_capability` MUST appear here.  Adding a new capability:
#   1. Register it in ``memory_llm_policy.LLM_CAPABILITY_MATRIX`` as
#      ``llm`` or ``hybrid``.
#   2. Add an entry below with ``enabled=False`` (opt-in by default) and a
#      conservative ``timeout`` / ``max_tokens`` budget.
@dataclass(frozen=True)
class CapabilityProfile:
    enabled: bool = False
    timeout: float | None = None  # seconds; None → use LLMConfig.timeout
    max_tokens: int | None = None  # per-call output cap; None → use LLMConfig
    description: str = ""


DEFAULT_CAPABILITY_PROFILES: dict[str, CapabilityProfile] = {
    "distill_summary": CapabilityProfile(
        enabled=False,
        timeout=60.0,
        max_tokens=1024,
        description="Persisted distilled_summary derived from a freshly written raw record.",
    ),
    "summarize_recall": CapabilityProfile(
        enabled=False,
        timeout=45.0,
        max_tokens=768,
        description="Read-only map-reduce summary attached to retrieve_context results.",
    ),
    "rebuild_key_document": CapabilityProfile(
        enabled=False,
        timeout=90.0,
        max_tokens=2048,
        description="LLM tier of memory_context.rebuild_key_documents (P4-C).",
    ),
    "guard_compaction": CapabilityProfile(
        enabled=False,
        timeout=45.0,
        max_tokens=1500,
        description="LLM-assisted compression of guard-overflowing memory files.",
    ),
    "auto_memory_gate": CapabilityProfile(
        enabled=False,
        timeout=20.0,
        max_tokens=384,
        description="Optional gate for agent-first automatic memory settling.",
    ),
    "query_rewrite": CapabilityProfile(
        enabled=False,
        timeout=30.0,
        max_tokens=256,
        description="Expand a natural-language query into FTS-friendly variants for recall (v0.10.0).",
    ),
    "snapshot_narrative": CapabilityProfile(
        enabled=False,
        timeout=60.0,
        max_tokens=1024,
        description="LLM-generated executive summary attached to weekly/monthly snapshots (v0.10.0).",
    ),
    "generate_task_brief": CapabilityProfile(
        enabled=False,
        timeout=20.0,
        max_tokens=1024,
        description="Evidence-bounded task briefing with deterministic fallback.",
    ),
    "project_reflection": CapabilityProfile(
        enabled=False,
        timeout=90.0,
        max_tokens=2048,
        description="Two-pass project-global reflection with deterministic evidence and publication gates.",
    ),
}


# ── Result envelope ───────────────────────────────────────────────────


# Statuses that callers can switch on without parsing free-text errors.
STATUS_OK = "ok"
STATUS_DISABLED = "disabled"          # capability turned off in config
STATUS_UNAVAILABLE = "unavailable"    # LLM client could not be built
STATUS_TIMEOUT = "timeout"            # upstream timeout
STATUS_BUDGET = "budget_exceeded"     # token / cost budget exhausted
STATUS_FAILED = "failed"              # other LLM error (network, parse, …)
STATUS_INVALID = "invalid_capability" # caller bug — capability not registered


@dataclass
class LLMRunResult:
    """Structured outcome of :func:`run_llm_capability`.

    ``ok`` mirrors ``status == STATUS_OK``. ``value`` carries the payload
    the callable produced (or the fallback's). ``fallback_used`` is True
    when the LLM path was skipped/failed and the fallback supplied the
    value. ``meta`` is a free-form dict for diagnostics (model, usage,
    elapsed, …) — never required for correctness.
    """

    ok: bool
    status: str
    capability: str
    value: Any = None
    error: str | None = None
    fallback_used: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": bool(self.ok),
            "status": self.status,
            "capability": self.capability,
            "fallback_used": bool(self.fallback_used),
        }
        if self.value is not None:
            out["value"] = self.value
        if self.error:
            out["error"] = self.error
        if self.meta:
            out["meta"] = dict(self.meta)
        return out


# ── Config helpers ────────────────────────────────────────────────────


def _llm_defaults_block(config: MemoryConfig) -> Mapping[str, Any]:
    """Pull the ``llm_defaults`` block from MemoryConfig safely.

    Stored as a free-form dict on the config (see
    :func:`memory_config.load_config`) so we don't churn the dataclass
    every time a new knob lands.
    """

    block = getattr(config, "llm_defaults", None)
    if isinstance(block, Mapping):
        return block
    return {}


def resolve_capability_profile(
    config: MemoryConfig,
    capability: str,
) -> CapabilityProfile:
    """Merge built-in defaults with the user's ``llm_defaults`` overrides.

    Precedence (highest first):

    1. ``llm_defaults.capabilities.<capability>.<field>``
    2. ``llm_defaults.<field>`` (global default: ``enabled``, ``timeout``,
       ``max_tokens``)
    3. :data:`DEFAULT_CAPABILITY_PROFILES` entry
    4. Fallback ``CapabilityProfile()`` (everything off / None)

    Unknown capabilities return an empty profile — they are still rejected
    by :func:`run_llm_capability` because they cannot be in the policy
    matrix, but returning a profile keeps the function pure.
    """

    base = DEFAULT_CAPABILITY_PROFILES.get(capability, CapabilityProfile())

    block = _llm_defaults_block(config)
    global_enabled = block.get("enabled")
    global_timeout = block.get("timeout")
    global_max_tokens = block.get("max_tokens")

    capabilities_raw = block.get("capabilities")
    cap_overrides: Mapping[str, Any] = {}
    if isinstance(capabilities_raw, Mapping):
        candidate = capabilities_raw.get(capability)
        if isinstance(candidate, Mapping):
            cap_overrides = candidate

    def _resolve_bool(*candidates: Any, default: bool) -> bool:
        for cand in candidates:
            if cand is None:
                continue
            if isinstance(cand, bool):
                return cand
            if isinstance(cand, str):
                low = cand.strip().lower()
                if low in {"1", "true", "yes", "on"}:
                    return True
                if low in {"0", "false", "no", "off"}:
                    return False
            if isinstance(cand, (int, float)):
                return bool(cand)
        return default

    def _resolve_pos_float(*candidates: Any, default: float | None) -> float | None:
        for cand in candidates:
            if cand is None:
                continue
            try:
                value = float(cand)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return default

    def _resolve_pos_int(*candidates: Any, default: int | None) -> int | None:
        for cand in candidates:
            if cand is None:
                continue
            try:
                value = int(cand)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return default

    enabled = _resolve_bool(
        cap_overrides.get("enabled"),
        global_enabled,
        default=base.enabled,
    )
    timeout = _resolve_pos_float(
        cap_overrides.get("timeout"),
        global_timeout,
        default=base.timeout,
    )
    max_tokens = _resolve_pos_int(
        cap_overrides.get("max_tokens"),
        global_max_tokens,
        default=base.max_tokens,
    )
    return CapabilityProfile(
        enabled=enabled,
        timeout=timeout,
        max_tokens=max_tokens,
        description=base.description,
    )


def is_capability_enabled(config: MemoryConfig, capability: str) -> bool:
    """Convenience predicate; prefer :func:`run_llm_capability` when running."""
    return resolve_capability_profile(config, capability).enabled


# ── Client factory (mockable for tests) ────────────────────────────────


ClientFactory = Callable[[CapabilityProfile], Any]


def _default_client_factory(profile: CapabilityProfile) -> Any:
    """Build a real :class:`LLMClient` honouring the per-capability timeout.

    Imports are local so the runner stays usable in environments where the
    optional ``memory_llm`` deps are missing.
    """

    from dataclasses import replace as _replace
    from .memory_llm import LLMClient, LLMConfigError, load_llm_config

    base_config = load_llm_config()
    if profile.timeout is not None and profile.timeout > 0:
        # Honour the smaller of capability vs base; we never raise the cap.
        new_timeout = min(float(profile.timeout), float(base_config.timeout))
        if new_timeout != base_config.timeout:
            base_config = _replace(base_config, timeout=new_timeout)
    if profile.max_tokens is not None and profile.max_tokens > 0:
        new_max = min(int(profile.max_tokens), int(base_config.max_output_tokens_per_call))
        if new_max != base_config.max_output_tokens_per_call:
            base_config = _replace(base_config, max_output_tokens_per_call=new_max)
    try:
        return LLMClient(config=base_config)
    except LLMConfigError:
        raise


# ── Runner ────────────────────────────────────────────────────────────


def run_llm_capability(
    config: MemoryConfig,
    capability: str,
    callable_: Callable[[Any, CapabilityProfile], Any],
    *,
    fallback: Callable[[], Any] | None = None,
    client_factory: ClientFactory | None = None,
    force_enabled: bool = False,
) -> LLMRunResult:
    """Execute ``callable_`` under the unified LLM policy envelope.

    Wraps :func:`_run_llm_capability_inner` so every invocation — success,
    failure, fallback, disabled — emits a single ``llm_capability_invoked``
    event line to ``events.jsonl`` with capability/status/latency_ms/usage.
    Event-emission errors are swallowed so audit-log issues never break
    the LLM path.
    """

    import time as _time

    start = _time.perf_counter()
    try:
        result = _run_llm_capability_inner(
            config,
            capability,
            callable_,
            fallback=fallback,
            client_factory=client_factory,
            force_enabled=force_enabled,
        )
    finally:
        latency_ms = int((_time.perf_counter() - start) * 1000)
    _emit_llm_event(config, capability, locals().get("result"), latency_ms)
    return result


def _emit_llm_event(
    config: MemoryConfig,
    capability: str,
    result: LLMRunResult | None,
    latency_ms: int,
) -> None:
    """Best-effort: append one ``llm_capability_invoked`` event row.

    Failures (missing repo_root, locked file, etc.) are logged and
    swallowed — the LLM call has already completed and must not fail
    just because we cannot persist an audit row.
    """

    try:
        from .memory_events import append_event
    except Exception:  # pragma: no cover — defensive
        return
    if result is None:
        return
    payload: dict[str, Any] = {
        "capability": capability,
        "status": result.status,
        "fallback_used": bool(result.fallback_used),
        "latency_ms": int(latency_ms),
    }
    if result.error:
        payload["error"] = str(result.error)[:240]
    usage = (result.meta or {}).get("usage")
    if isinstance(usage, dict):
        # Compact the usage dict to the few fields we actually look at
        # in dashboards; full usage stays in the in-memory result.
        payload["call_count"] = int(usage.get("call_count", 0) or 0)
        payload["total_tokens"] = int(usage.get("total_tokens", 0) or 0)
        if "retry_count" in usage:
            payload["retry_count"] = int(usage.get("retry_count", 0) or 0)
        if "total_estimated_cost_cny" in usage:
            payload["cost_cny"] = float(usage.get("total_estimated_cost_cny", 0.0) or 0.0)
    event_status = "ok" if result.ok else "error"
    try:
        append_event(config, "llm_capability_invoked", payload, status=event_status)
    except Exception as exc:  # pragma: no cover — audit must not fail callers
        logger.debug("llm_capability_invoked event suppressed: %s", exc)


def _run_llm_capability_inner(
    config: MemoryConfig,
    capability: str,
    callable_: Callable[[Any, CapabilityProfile], Any],
    *,
    fallback: Callable[[], Any] | None = None,
    client_factory: ClientFactory | None = None,
    force_enabled: bool = False,
) -> LLMRunResult:
    """Internal: original :func:`run_llm_capability` body, no telemetry."""

    # Step 0: capability sanity check (fail fast on caller bugs).
    try:
        owner = capability_owner(capability)
    except Exception as exc:
        logger.warning("run_llm_capability: %s", exc)
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_INVALID,
                capability=capability,
                error=str(exc),
            ),
            fallback,
        )
    if owner == "non_llm":
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_INVALID,
                capability=capability,
                error=f"capability {capability!r} is owned by deterministic layer",
            ),
            fallback,
        )

    profile = resolve_capability_profile(config, capability)
    if not profile.enabled and not force_enabled:
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_DISABLED,
                capability=capability,
                error="capability disabled in config",
            ),
            fallback,
        )

    # Step 1: build client (errors → STATUS_UNAVAILABLE).
    factory = client_factory or _default_client_factory
    try:
        client = factory(profile)
    except Exception as exc:
        # Local import so tests do not need the optional dep.
        try:
            from .memory_llm import LLMConfigError
        except Exception:  # pragma: no cover — defensive
            LLMConfigError = ()  # type: ignore[assignment]
        if isinstance(exc, LLMConfigError) or LLMConfigError == ():
            return _maybe_fallback(
                LLMRunResult(
                    ok=False,
                    status=STATUS_UNAVAILABLE,
                    capability=capability,
                    error=str(exc),
                ),
                fallback,
            )
        logger.warning("run_llm_capability(%s): unexpected client build failure: %s", capability, exc)
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_UNAVAILABLE,
                capability=capability,
                error=f"failed to build LLM client: {exc}",
            ),
            fallback,
        )

    # Step 2: invoke. Translate known LLM error subclasses into structured
    # statuses so callers never have to know the exception hierarchy.
    try:
        from .memory_llm import (
            LLMBudgetExceeded,
            LLMError,
            LLMRequestError,
        )
    except Exception:  # pragma: no cover — defensive
        LLMError = LLMRequestError = LLMBudgetExceeded = Exception  # type: ignore[assignment]

    try:
        value = callable_(client, profile)
    except LLMBudgetExceeded as exc:
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_BUDGET,
                capability=capability,
                error=str(exc),
                meta=_safe_usage(client),
            ),
            fallback,
        )
    except LLMRequestError as exc:
        # Best-effort timeout classification: the underlying client wraps
        # ``socket.timeout`` / ``urllib.error.URLError`` instances into
        # LLMRequestError with ``status=None`` and a "timeout" message.
        msg = str(exc).lower()
        is_timeout = "timeout" in msg or "timed out" in msg
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_TIMEOUT if is_timeout else STATUS_FAILED,
                capability=capability,
                error=str(exc),
                meta=_safe_usage(client),
            ),
            fallback,
        )
    except LLMError as exc:
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_FAILED,
                capability=capability,
                error=str(exc),
                meta=_safe_usage(client),
            ),
            fallback,
        )
    except Exception as exc:  # pragma: no cover — unexpected
        logger.exception("run_llm_capability(%s): unexpected callable failure", capability)
        return _maybe_fallback(
            LLMRunResult(
                ok=False,
                status=STATUS_FAILED,
                capability=capability,
                error=f"unexpected: {exc}",
                meta=_safe_usage(client),
            ),
            fallback,
        )

    return LLMRunResult(
        ok=True,
        status=STATUS_OK,
        capability=capability,
        value=value,
        meta={
            "profile": {
                "enabled": profile.enabled,
                "timeout": profile.timeout,
                "max_tokens": profile.max_tokens,
            },
            **_safe_usage(client),
        },
    )


def _safe_usage(client: Any) -> dict[str, Any]:
    """Best-effort ``usage_snapshot`` collector — never raises."""
    snap = getattr(client, "usage_snapshot", None)
    if not callable(snap):
        return {}
    try:
        usage = snap()
    except Exception:  # pragma: no cover — defensive
        return {}
    if isinstance(usage, dict):
        return {"usage": usage}
    return {}


def _maybe_fallback(result: LLMRunResult, fallback: Callable[[], Any] | None) -> LLMRunResult:
    """Run the optional fallback when the LLM path could not produce a value."""
    if fallback is None or result.ok:
        return result
    try:
        value = fallback()
    except Exception as exc:  # pragma: no cover — fallback bugs surface here
        logger.exception("run_llm_capability(%s): fallback raised", result.capability)
        result.meta["fallback_error"] = str(exc)
        return result
    result.value = value
    result.fallback_used = True
    # Preserve the original ``status`` so callers can tell *why* we fell back
    # while still treating the overall run as "successful enough" via ``ok``.
    result.ok = True
    return result


__all__ = [
    "STATUS_OK",
    "STATUS_DISABLED",
    "STATUS_UNAVAILABLE",
    "STATUS_TIMEOUT",
    "STATUS_BUDGET",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "CapabilityProfile",
    "DEFAULT_CAPABILITY_PROFILES",
    "LLMRunResult",
    "is_capability_enabled",
    "resolve_capability_profile",
    "run_llm_capability",
]
