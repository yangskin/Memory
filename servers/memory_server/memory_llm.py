"""LLM client for Memory MCP — OpenAI-compatible chat completion API.

Compatible with any OpenAI-style endpoint (DeepSeek, OpenAI, Moonshot, ...).
The default profile targets DeepSeek (`https://api.deepseek.com`).

Configuration sources (highest priority first):

1. Explicit ``LLMConfig`` passed to :class:`LLMClient`.
2. Environment variables:
   - ``MEMORY_LLM_API_KEY``  (fallbacks: ``DEEPSEEK_API_KEY``, ``OPENAI_API_KEY``)
   - ``MEMORY_LLM_BASE_URL``
   - ``MEMORY_LLM_MODEL``
   - ``MEMORY_LLM_TIMEOUT``
3. Local config file at the plugin root: ``MCP/Memory/llm_config.local.json``.
   This file is gitignored. A template ``llm_config.example.json`` is shipped
   alongside it.

The client is intentionally dependency-free (uses ``urllib`` from stdlib) so it
can be used from the MCP server without adding install-time requirements.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    # Local heuristic token estimator (CJK-aware).
    from .token_estimator import estimate_tokens as _estimate_tokens
except ImportError:  # pragma: no cover — direct script execution path
    from token_estimator import estimate_tokens as _estimate_tokens  # type: ignore

# Plugin root = parent of `servers/` (i.e. MCP/Memory).
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG_FILENAME = "llm_config.local.json"
EXAMPLE_CONFIG_FILENAME = "llm_config.example.json"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60.0


class LLMError(RuntimeError):
    """Base class for LLM errors."""


class LLMConfigError(LLMError):
    """Raised when configuration is missing or invalid (e.g. no API key)."""


class LLMRequestError(LLMError):
    """Raised when the upstream HTTP call fails or returns a non-2xx status."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# Valid values for `reasoning_effort` per DeepSeek/OpenAI reasoning spec.
VALID_REASONING_EFFORT = {"low", "medium", "high"}

# Default per-call output cap. Conservative for cost (~0.0001~0.001 元 per call
# at deepseek-v4-flash pricing). Override via config or per-call.
DEFAULT_MAX_OUTPUT_TOKENS = 1024
# Default per-process cumulative output cap (safety net to prevent runaway loops).
DEFAULT_MAX_TOTAL_OUTPUT_TOKENS = 200_000
# Default per-call input cap. Bounds prompt size to control input-side billing.
# Estimated via :func:`token_estimator.estimate_tokens`. Set to 0 to disable.
DEFAULT_MAX_INPUT_TOKENS_PER_CALL = 32_000
# Default pricing (CNY per 1M tokens) — deepseek-v4-flash. Override via config
# when switching to deepseek-v4-pro (12 / 24) or other providers.
DEFAULT_INPUT_CNY_PER_MTOK = 1.0
DEFAULT_OUTPUT_CNY_PER_MTOK = 2.0
# Default per-process cumulative cost cap in CNY. 0 disables the check.
DEFAULT_MAX_TOTAL_COST_CNY = 0.0


class LLMBudgetExceeded(LLMError):
    """Raised when a planned or observed token usage exceeds the configured budget."""


class LLMInputTooLarge(LLMBudgetExceeded):
    """Raised when the estimated prompt token count exceeds the per-call input cap."""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Cost / safety controls --------------------------------------------------
    # Hard cap on `max_tokens` per single chat call. If a caller passes a higher
    # value (or omits one), it is clamped to this value.
    max_output_tokens_per_call: int = DEFAULT_MAX_OUTPUT_TOKENS
    # Soft per-process budget across all calls of an `LLMClient` instance.
    # When exceeded the next call raises ``LLMBudgetExceeded`` instead of hitting
    # the network. Set to 0 to disable.
    max_total_output_tokens: int = DEFAULT_MAX_TOTAL_OUTPUT_TOKENS
    # Hard cap on estimated prompt tokens per single chat call. If the
    # estimated prompt size exceeds this value the client raises
    # ``LLMInputTooLarge`` before hitting the network. Set to 0 to disable.
    max_input_tokens_per_call: int = DEFAULT_MAX_INPUT_TOKENS_PER_CALL
    # Pricing (CNY per 1M tokens) used for cost accounting in `usage_snapshot`.
    input_cny_per_mtok: float = DEFAULT_INPUT_CNY_PER_MTOK
    output_cny_per_mtok: float = DEFAULT_OUTPUT_CNY_PER_MTOK
    # Soft per-process cumulative cost cap in CNY. When estimated cumulative
    # cost exceeds this, the next call raises ``LLMBudgetExceeded``. 0 disables.
    max_total_cost_cny: float = DEFAULT_MAX_TOTAL_COST_CNY
    # Default thinking mode for the client; per-call override always wins.
    default_thinking: bool = False
    default_reasoning_effort: str | None = None
    # Retry policy for transient upstream errors (5xx + network).  Set
    # ``max_retries=0`` to disable.  Backoff is exponential: ``backoff *
    # 2**attempt`` seconds, capped at ``retry_backoff_max_seconds``.  We
    # never retry 4xx (auth/budget) or :class:`LLMBudgetExceeded` /
    # :class:`LLMInputTooLarge` (deterministic refusals).
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    retry_backoff_max_seconds: float = 4.0

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _local_config_path(plugin_root: Path | None = None) -> Path:
    return (plugin_root or PLUGIN_ROOT) / LOCAL_CONFIG_FILENAME


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LLMConfigError(f"failed to read llm config {path}: {exc}") from exc
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMConfigError(f"llm config {path} must be a JSON object")
    return data


def load_llm_config(
    *,
    plugin_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> LLMConfig:
    """Load an :class:`LLMConfig` by merging file → env → overrides.

    Raises :class:`LLMConfigError` when no API key can be resolved.
    """
    env_map = env if env is not None else os.environ
    file_data = _read_json_file(_local_config_path(plugin_root))

    api_key = (
        (overrides or {}).get("api_key")
        or env_map.get("MEMORY_LLM_API_KEY")
        or env_map.get("DEEPSEEK_API_KEY")
        or env_map.get("OPENAI_API_KEY")
        or file_data.get("api_key")
    )
    if not api_key or not str(api_key).strip():
        raise LLMConfigError(
            "LLM API key not found. Set MEMORY_LLM_API_KEY (or DEEPSEEK_API_KEY) "
            f"or create {_local_config_path(plugin_root)}"
        )

    base_url = (
        (overrides or {}).get("base_url")
        or env_map.get("MEMORY_LLM_BASE_URL")
        or file_data.get("base_url")
        or DEFAULT_BASE_URL
    )
    model = (
        (overrides or {}).get("model")
        or env_map.get("MEMORY_LLM_MODEL")
        or file_data.get("model")
        or DEFAULT_MODEL
    )

    timeout_raw = (
        (overrides or {}).get("timeout")
        or env_map.get("MEMORY_LLM_TIMEOUT")
        or file_data.get("timeout")
        or DEFAULT_TIMEOUT
    )
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"invalid timeout value: {timeout_raw!r}") from exc

    extra_headers_raw = (overrides or {}).get("extra_headers") or file_data.get("extra_headers") or {}
    if not isinstance(extra_headers_raw, dict):
        raise LLMConfigError("extra_headers must be a JSON object")
    extra_headers = {str(k): str(v) for k, v in extra_headers_raw.items()}

    def _int_setting(key: str, env_key: str, default: int) -> int:
        raw = (
            (overrides or {}).get(key)
            if (overrides or {}).get(key) is not None
            else env_map.get(env_key) or file_data.get(key) or default
        )
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise LLMConfigError(f"invalid integer for {key}: {raw!r}") from exc

    max_output_tokens_per_call = _int_setting(
        "max_output_tokens_per_call", "MEMORY_LLM_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
    )
    max_total_output_tokens = _int_setting(
        "max_total_output_tokens", "MEMORY_LLM_MAX_TOTAL_OUTPUT_TOKENS", DEFAULT_MAX_TOTAL_OUTPUT_TOKENS
    )
    max_input_tokens_per_call = _int_setting(
        "max_input_tokens_per_call",
        "MEMORY_LLM_MAX_INPUT_TOKENS",
        DEFAULT_MAX_INPUT_TOKENS_PER_CALL,
    )
    if max_output_tokens_per_call <= 0:
        raise LLMConfigError("max_output_tokens_per_call must be positive")
    if max_total_output_tokens < 0:
        raise LLMConfigError("max_total_output_tokens must be >= 0")
    if max_input_tokens_per_call < 0:
        raise LLMConfigError("max_input_tokens_per_call must be >= 0")

    def _float_setting(key: str, env_key: str, default: float) -> float:
        raw = (
            (overrides or {}).get(key)
            if (overrides or {}).get(key) is not None
            else env_map.get(env_key) or file_data.get(key) or default
        )
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise LLMConfigError(f"invalid float for {key}: {raw!r}") from exc

    input_cny_per_mtok = _float_setting(
        "input_cny_per_mtok", "MEMORY_LLM_INPUT_CNY_PER_MTOK", DEFAULT_INPUT_CNY_PER_MTOK
    )
    output_cny_per_mtok = _float_setting(
        "output_cny_per_mtok", "MEMORY_LLM_OUTPUT_CNY_PER_MTOK", DEFAULT_OUTPUT_CNY_PER_MTOK
    )
    max_total_cost_cny = _float_setting(
        "max_total_cost_cny", "MEMORY_LLM_MAX_TOTAL_COST_CNY", DEFAULT_MAX_TOTAL_COST_CNY
    )
    if input_cny_per_mtok < 0 or output_cny_per_mtok < 0:
        raise LLMConfigError("pricing values must be >= 0")
    if max_total_cost_cny < 0:
        raise LLMConfigError("max_total_cost_cny must be >= 0")

    default_thinking_raw = (overrides or {}).get("default_thinking")
    if default_thinking_raw is None:
        default_thinking_raw = env_map.get("MEMORY_LLM_DEFAULT_THINKING")
    if default_thinking_raw is None:
        default_thinking_raw = file_data.get("default_thinking", False)
    default_thinking = str(default_thinking_raw).strip().lower() in {"1", "true", "yes", "on"} if isinstance(
        default_thinking_raw, str
    ) else bool(default_thinking_raw)

    default_reasoning_effort = (
        (overrides or {}).get("default_reasoning_effort")
        or env_map.get("MEMORY_LLM_DEFAULT_REASONING_EFFORT")
        or file_data.get("default_reasoning_effort")
    )
    if default_reasoning_effort is not None:
        default_reasoning_effort = str(default_reasoning_effort).strip().lower()
        if default_reasoning_effort not in VALID_REASONING_EFFORT:
            raise LLMConfigError(
                f"default_reasoning_effort must be one of {sorted(VALID_REASONING_EFFORT)}"
            )

    return LLMConfig(
        api_key=str(api_key).strip(),
        base_url=str(base_url).strip(),
        model=str(model).strip(),
        timeout=timeout,
        extra_headers=extra_headers,
        max_output_tokens_per_call=max_output_tokens_per_call,
        max_total_output_tokens=max_total_output_tokens,
        max_input_tokens_per_call=max_input_tokens_per_call,
        input_cny_per_mtok=input_cny_per_mtok,
        output_cny_per_mtok=output_cny_per_mtok,
        max_total_cost_cny=max_total_cost_cny,
        default_thinking=default_thinking,
        default_reasoning_effort=default_reasoning_effort,
    )



def build_chat_payload(
    messages: Iterable[dict[str, Any]],
    *,
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    thinking: bool = False,
    reasoning_effort: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible chat completion payload.

    ``thinking`` controls DeepSeek-style reasoning mode. **Defaults to False**
    so callers do not pay reasoning-token cost unless they explicitly opt in.
    When ``thinking=True`` the payload sends ``thinking={"type":"enabled"}``
    (DeepSeek extension) and ``reasoning_effort`` (defaults to ``"medium"``
    when enabled). When ``thinking=False`` the payload sends
    ``thinking={"type":"disabled"}`` so providers that default-on (e.g.
    deepseek-v4-flash) are explicitly turned off.

    Caller-supplied ``extra`` keys win over auto-injected reasoning fields,
    so power users can fully customise the request.
    """
    msg_list = list(messages)
    if not msg_list:
        raise LLMConfigError("messages must not be empty")
    for msg in msg_list:
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            raise LLMConfigError("each message must be a dict with 'role' and 'content'")

    if reasoning_effort is not None and str(reasoning_effort) not in VALID_REASONING_EFFORT:
        raise LLMConfigError(
            f"reasoning_effort must be one of {sorted(VALID_REASONING_EFFORT)}, got {reasoning_effort!r}"
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": msg_list,
        "stream": bool(stream),
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    if thinking:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = reasoning_effort or "medium"
    else:
        payload["thinking"] = {"type": "disabled"}

    if extra:
        # Caller-supplied params win, but we keep `model`/`messages` authoritative.
        for key, value in extra.items():
            if key in {"model", "messages"}:
                continue
            payload[key] = value
    return payload


def extract_text(response: dict[str, Any]) -> str:
    """Extract assistant text from an OpenAI-style chat completion response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMRequestError("response missing choices", body=json.dumps(response, ensure_ascii=False))
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        raise LLMRequestError("response missing message", body=json.dumps(response, ensure_ascii=False))
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, list):
        # Some providers return a list of content parts (OpenAI vision-style).
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


class LLMClient:
    """Thin OpenAI-compatible chat completion client (non-streaming).

    The client tracks cumulative token usage across all calls
    (``total_prompt_tokens`` / ``total_completion_tokens`` / ``call_count``) and
    enforces ``LLMConfig.max_total_output_tokens``. Per-call ``max_tokens`` is
    clamped to ``LLMConfig.max_output_tokens_per_call``.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        plugin_root: Path | None = None,
        transport: "callable | None" = None,
    ) -> None:
        self.config = config or load_llm_config(plugin_root=plugin_root)
        # `transport(url, headers, body, timeout) -> (status, body_text)` for tests.
        self._transport = transport
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.call_count: int = 0
        self.total_estimated_cost_cny: float = 0.0
        self.retry_count: int = 0
        self.last_retry_reason: str | None = None

    def reset_usage(self) -> None:
        """Reset cumulative usage counters."""
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0
        self.total_estimated_cost_cny = 0.0
        self.retry_count = 0
        self.last_retry_reason = None

    def usage_snapshot(self) -> dict[str, Any]:
        return {
            "call_count": self.call_count,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "total_estimated_cost_cny": round(self.total_estimated_cost_cny, 6),
            "retry_count": self.retry_count,
        }

    @staticmethod
    def estimate_prompt_tokens(messages: Iterable[dict[str, Any]]) -> int:
        """Rough estimate of input tokens for a chat ``messages`` list.

        Used by the per-call input-side gate and by callers that want to
        chunk before issuing a request. Each message contributes its
        ``content`` plus a small per-message overhead (~4 tokens) to mirror
        the OpenAI counting convention.
        """
        total = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text") or ""
                    else:
                        text = str(part)
                    total += _estimate_tokens(str(text))
            elif content is not None:
                total += _estimate_tokens(str(content))
            total += 4  # per-message overhead
        return total

    def _compute_cost_cny(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.config.input_cny_per_mtok / 1_000_000.0
            + completion_tokens * self.config.output_cny_per_mtok / 1_000_000.0
        )

    def chat(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request and return the parsed JSON response.

        ``thinking`` defaults to ``LLMConfig.default_thinking`` (False unless
        configured otherwise). Pass ``thinking=True`` to force-enable reasoning
        mode for this call, or ``thinking=False`` to force-disable it.

        ``max_tokens`` is always clamped to
        ``LLMConfig.max_output_tokens_per_call``. If omitted the cap value is
        used directly. The cumulative output budget
        ``LLMConfig.max_total_output_tokens`` is enforced before the call is
        sent (raises :class:`LLMBudgetExceeded`).
        """
        # Budget pre-check based on already observed completion tokens.
        if (
            self.config.max_total_output_tokens > 0
            and self.total_completion_tokens >= self.config.max_total_output_tokens
        ):
            raise LLMBudgetExceeded(
                f"cumulative completion tokens {self.total_completion_tokens} "
                f"exceeded budget {self.config.max_total_output_tokens}"
            )
        # Cumulative cost pre-check.
        if (
            self.config.max_total_cost_cny > 0
            and self.total_estimated_cost_cny >= self.config.max_total_cost_cny
        ):
            raise LLMBudgetExceeded(
                f"cumulative estimated cost {self.total_estimated_cost_cny:.4f} CNY "
                f"exceeded budget {self.config.max_total_cost_cny:.4f} CNY"
            )

        # Materialise messages once so estimation and payload share the same list.
        msg_list = list(messages)

        # Input-side pre-check: estimate prompt tokens to bound input billing.
        if self.config.max_input_tokens_per_call > 0:
            estimated_prompt = self.estimate_prompt_tokens(msg_list)
            if estimated_prompt > self.config.max_input_tokens_per_call:
                raise LLMInputTooLarge(
                    f"estimated prompt tokens {estimated_prompt} exceed per-call "
                    f"input cap {self.config.max_input_tokens_per_call}; chunk the "
                    "input or raise max_input_tokens_per_call"
                )

        # Clamp per-call output cap.
        cap = max(1, int(self.config.max_output_tokens_per_call))
        effective_max_tokens = cap if max_tokens is None else min(int(max_tokens), cap)

        # Resolve defaults for thinking mode.
        if thinking is None:
            thinking = bool(self.config.default_thinking)
        if reasoning_effort is None and thinking:
            reasoning_effort = self.config.default_reasoning_effort

        payload = build_chat_payload(
            msg_list,
            model=model or self.config.model,
            temperature=temperature,
            max_tokens=effective_max_tokens,
            stream=False,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            extra=extra,
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        headers.update(self.config.extra_headers)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self.config.chat_completions_url()
        effective_timeout = float(timeout) if timeout is not None else self.config.timeout

        if self._transport is not None:
            transport_call = lambda: self._transport(url, headers, body_bytes, effective_timeout)
        else:
            transport_call = lambda: _http_post(url, headers, body_bytes, effective_timeout)

        # Retry loop: 5xx + network errors are transient; 4xx are not.
        max_retries = max(0, int(self.config.max_retries))
        backoff = max(0.0, float(self.config.retry_backoff_seconds))
        backoff_cap = max(backoff, float(self.config.retry_backoff_max_seconds))
        attempt = 0
        while True:
            try:
                status, body_text = transport_call()
                network_error: LLMRequestError | None = None
            except LLMRequestError as exc:
                status, body_text = 0, ""
                network_error = exc
            should_retry = attempt < max_retries and (
                network_error is not None or (status >= 500 and status != 501)
            )
            if not should_retry:
                if network_error is not None:
                    raise network_error
                break
            attempt += 1
            self.retry_count += 1
            sleep_for = min(backoff_cap, backoff * (2 ** (attempt - 1))) if backoff > 0 else 0.0
            self.last_retry_reason = (
                f"network: {network_error}" if network_error is not None else f"http {status}"
            )
            if sleep_for > 0:
                import time as _time
                _time.sleep(sleep_for)

        if status >= 400:
            raise LLMRequestError(
                f"LLM request failed with HTTP {status}",
                status=status,
                body=body_text,
            )
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise LLMRequestError(f"invalid JSON response: {exc}", status=status, body=body_text) from exc

        # Record usage for cumulative budget tracking. ``usage.completion_tokens``
        # already includes reasoning tokens on DeepSeek, so this is a sound
        # billing-side measurement.
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_estimated_cost_cny += self._compute_cost_cny(
                prompt_tokens, completion_tokens
            )
        self.call_count += 1
        return parsed

    def complete_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Convenience: send a single user prompt and return the assistant text."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self.chat(messages, **kwargs)
        return extract_text(response)


# ── Autonomous-distillation primitives (raw-immutable / derived-replaceable) ─

# `provenance` values used on records produced by the LLM pipeline.
PROVENANCE_HUMAN = "human"
PROVENANCE_RAW = "raw_capture"   # captured verbatim from a tool/sensor/log
PROVENANCE_LLM = "llm"           # produced by an LLM pass


class RawImmutableError(LLMError):
    """Raised when code tries to modify a record marked as raw / immutable."""


def make_raw_record(
    *,
    record_id: str,
    content: str,
    source: str,
    captured_at: str,
    author: str = "system",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a raw, write-once record dict.

    Raw records are the **only** authoritative truth source. Any LLM
    distillation later runs on top of them via :func:`make_distilled_record`,
    and can be regenerated freely without losing information because the raw
    record stays untouched.

    Returned dict carries:
    - ``provenance = "raw_capture"``
    - ``immutable = True``
    - ``status = "raw"`` (no candidate/validated/published gate)
    - ``authoritative = True``
    """
    if not record_id or not record_id.strip():
        raise LLMConfigError("raw record requires a non-empty record_id")
    if content is None:
        raise LLMConfigError("raw record requires content")
    if not source or not source.strip():
        raise LLMConfigError("raw record requires a source")
    if not captured_at or not captured_at.strip():
        raise LLMConfigError("raw record requires captured_at")

    record: dict[str, Any] = {
        "id": str(record_id).strip(),
        "provenance": PROVENANCE_RAW,
        "immutable": True,
        "authoritative": True,
        "status": "raw",
        "author": str(author).strip() or "system",
        "source": str(source).strip(),
        "captured_at": str(captured_at).strip(),
        "content": str(content),
    }
    if extra_meta:
        for key, value in extra_meta.items():
            # Reserved keys protect the immutability invariants.
            if key in {"provenance", "immutable", "authoritative", "status", "id"}:
                raise RawImmutableError(
                    f"extra_meta cannot override reserved raw field: {key}"
                )
            record[key] = value
    return record


def make_distilled_record(
    *,
    record_id: str,
    content: str,
    derived_from: list[str],
    model: str,
    distilled_at: str,
    kind: str = "summary",
    confidence: float | None = None,
    tags: list[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a distilled (LLM-derived) record dict.

    Distilled records are NOT authoritative: they are cheap, replaceable
    summaries of raw records and may be regenerated by any LLM. They carry an
    explicit ``derived_from`` list so any later pass can reproduce them from
    the immutable raw layer.

    Returned dict carries:
    - ``provenance = "llm"``
    - ``immutable = False``  (any LLM may overwrite)
    - ``authoritative = False``
    - ``status = "distilled"`` (does NOT require human validate/publish)
    - ``replaceable = True``
    """
    if not derived_from:
        raise LLMConfigError(
            "distilled record requires non-empty derived_from (list of raw record ids); "
            "distillation must always trace back to immutable raw layer"
        )
    if not record_id or not record_id.strip():
        raise LLMConfigError("distilled record requires a non-empty record_id")
    if not model or not model.strip():
        raise LLMConfigError("distilled record requires a model identifier")

    record: dict[str, Any] = {
        "id": str(record_id).strip(),
        "provenance": PROVENANCE_LLM,
        "immutable": False,
        "authoritative": False,
        "replaceable": True,
        "status": "distilled",
        "kind": str(kind).strip() or "summary",
        "model": str(model).strip(),
        "distilled_at": str(distilled_at).strip(),
        "derived_from": [str(rid).strip() for rid in derived_from if str(rid).strip()],
        "content": str(content),
    }
    if confidence is not None:
        try:
            record["confidence"] = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError) as exc:
            raise LLMConfigError(f"invalid confidence: {confidence!r}") from exc
    if tags:
        record["tags"] = sorted({str(t).strip() for t in tags if str(t).strip()})
    if extra_meta:
        for key, value in extra_meta.items():
            if key in {"provenance", "immutable", "authoritative", "replaceable",
                       "status", "id", "derived_from"}:
                raise RawImmutableError(
                    f"extra_meta cannot override reserved distilled field: {key}"
                )
            record[key] = value
    if not record["derived_from"]:
        raise LLMConfigError("derived_from must contain at least one raw record id")
    return record


def assert_raw_writable(existing_record: dict[str, Any] | None) -> None:
    """Raise :class:`RawImmutableError` if ``existing_record`` is raw/immutable.

    Use this guard before any in-place update to enforce the
    "raw is write-once" invariant. Distilled records (``immutable=False``)
    pass through silently.
    """
    if existing_record is None:
        return
    if not isinstance(existing_record, dict):
        return
    if existing_record.get("immutable") is True or existing_record.get("provenance") == PROVENANCE_RAW:
        raise RawImmutableError(
            f"record {existing_record.get('id', '?')!r} is raw/immutable and "
            "cannot be modified; create a new distilled record that "
            "supersedes it via derived_from instead"
        )


def supersede_distilled(
    *,
    previous: dict[str, Any],
    new_content: str,
    model: str,
    distilled_at: str,
    confidence: float | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Replace a distilled record with a fresh LLM pass.

    The new record reuses the previous ``derived_from`` chain (which always
    points at the immutable raw layer), so the substitution is auditable and
    can be redone with a different LLM without losing provenance.

    Refuses to supersede raw records — call :func:`make_raw_record` for new
    raw captures and let distillations regenerate from there.
    """
    assert_raw_writable(previous)
    derived_from = list(previous.get("derived_from") or [])
    if not derived_from:
        raise LLMConfigError(
            "previous distilled record has empty derived_from; cannot supersede "
            "without a raw lineage"
        )
    new_id = str(previous.get("id") or "").strip()
    if not new_id:
        raise LLMConfigError("previous distilled record missing id")
    record = make_distilled_record(
        record_id=new_id,
        content=new_content,
        derived_from=derived_from,
        model=model,
        distilled_at=distilled_at,
        kind=str(previous.get("kind") or "summary"),
        confidence=confidence,
        tags=tags,
    )
    record["supersedes"] = previous.get("supersedes_id") or previous.get("id")
    return record


# ── Auto-distill bridge: raw records → distilled record via LLM ──────────


DEFAULT_DISTILL_SYSTEM_PROMPT = (
    "You are a memory distiller. Read the RAW captured records below and "
    "produce a concise, faithful summary in the same language as the raw "
    "content. Do NOT invent facts. Do NOT modify or contradict the raw "
    "records — they are the only authoritative source. Output the summary "
    "directly, with no preamble."
)


def _format_raw_records_for_prompt(raw_records: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for rec in raw_records:
        rid = rec.get("id", "?")
        src = rec.get("source", "?")
        captured = rec.get("captured_at", "?")
        content = rec.get("content", "")
        parts.append(
            f"--- raw id={rid} source={src} captured_at={captured} ---\n{content}"
        )
    return "\n\n".join(parts)


def distill_raw_records(
    client: "LLMClient",
    raw_records: list[dict[str, Any]],
    *,
    record_id: str,
    distilled_at: str,
    system_prompt: str | None = None,
    user_instruction: str | None = None,
    model: str | None = None,
    kind: str = "summary",
    tags: list[str] | None = None,
    confidence: float | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Run an LLM pass over ``raw_records`` and return a distilled record.

    This is the **bridge** between the immutable raw layer and the
    replaceable distilled layer. It enforces:

    - Every input record must be raw (``provenance == raw_capture`` or
      ``immutable is True``). The function refuses to distil already-distilled
      content — re-distill is done by re-running this on the source raw set
      and using :func:`supersede_distilled` if you want to chain ids.
    - The returned distilled record's ``derived_from`` is exactly the list of
      input raw record ids, in input order.
    - Cost / input-size gates of ``client`` apply normally (so a too-large
      raw set raises :class:`LLMInputTooLarge` before hitting the network).

    No human gate; per the project's autonomous-distillation contract any LLM
    is free to overwrite the resulting record later.
    """
    if not raw_records:
        raise LLMConfigError("distill_raw_records requires at least one raw record")

    derived_from: list[str] = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            raise LLMConfigError("each raw record must be a dict")
        if rec.get("provenance") != PROVENANCE_RAW and rec.get("immutable") is not True:
            raise LLMConfigError(
                f"distill_raw_records only accepts raw records; got "
                f"id={rec.get('id', '?')!r} provenance={rec.get('provenance')!r}"
            )
        rid = str(rec.get("id") or "").strip()
        if not rid:
            raise LLMConfigError("raw record missing id")
        derived_from.append(rid)

    body = _format_raw_records_for_prompt(raw_records)
    user_msg = body if not user_instruction else f"{user_instruction}\n\n{body}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt or DEFAULT_DISTILL_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    response = client.chat(
        messages,
        model=model,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )
    summary = extract_text(response).strip()
    if not summary:
        raise LLMRequestError("LLM returned empty distillation", body=json.dumps(response, ensure_ascii=False))

    return make_distilled_record(
        record_id=record_id,
        content=summary,
        derived_from=derived_from,
        model=model or client.config.model,
        distilled_at=distilled_at,
        kind=kind,
        confidence=confidence,
        tags=tags,
    )


def _http_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 — controlled url
            status = resp.getcode() or 0
            text = resp.read().decode("utf-8", errors="replace")
            return int(status), text
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            pass
        return int(exc.code), body_text
    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        http.client.HTTPException,
        ConnectionError,
    ) as exc:
        raise LLMRequestError(f"network error contacting LLM: {exc}") from exc


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_TOTAL_OUTPUT_TOKENS",
    "DEFAULT_MAX_INPUT_TOKENS_PER_CALL",
    "DEFAULT_INPUT_CNY_PER_MTOK",
    "DEFAULT_OUTPUT_CNY_PER_MTOK",
    "DEFAULT_MAX_TOTAL_COST_CNY",
    "DEFAULT_DISTILL_SYSTEM_PROMPT",
    "LOCAL_CONFIG_FILENAME",
    "EXAMPLE_CONFIG_FILENAME",
    "VALID_REASONING_EFFORT",
    "PROVENANCE_HUMAN",
    "PROVENANCE_RAW",
    "PROVENANCE_LLM",
    "LLMBudgetExceeded",
    "LLMInputTooLarge",
    "LLMClient",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "LLMRequestError",
    "RawImmutableError",
    "assert_raw_writable",
    "build_chat_payload",
    "distill_raw_records",
    "extract_text",
    "load_llm_config",
    "make_distilled_record",
    "make_raw_record",
    "supersede_distilled",
]
