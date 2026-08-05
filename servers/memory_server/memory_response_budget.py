"""Bound MCP responses while preserving valid, useful JSON."""

from __future__ import annotations

import json
from typing import Any


DEFAULT_RESPONSE_MAX_CHARS = 12_000
DEFAULT_RESPONSE_MAX_TOKENS = 3_000
_PRIORITY_KEYS = (
    "ok",
    "error",
    "message",
    "operation",
    "status",
    "context_token",
    "task_id",
    "id",
    "path",
    "content",
    "brief_markdown",
    "shared_context",
    "graph",
    "items",
    "results",
    "context_items",
)
_PROFILES = (
    (100, 50, 4096, 12),
    (60, 20, 2048, 10),
    (30, 10, 1024, 8),
    (16, 5, 512, 6),
    (8, 3, 256, 5),
)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def _bounded_value(
    value: Any,
    *,
    max_dict_items: int,
    max_list_items: int,
    max_string_chars: int,
    max_depth: int,
    depth: int = 0,
) -> Any:
    if depth >= max_depth:
        if isinstance(value, (dict, list)):
            return {"truncated": True}
        if isinstance(value, str):
            return value[:max_string_chars]
        return value
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value
        return value[:max_string_chars] + "...[truncated]"
    if isinstance(value, list):
        items = [
            _bounded_value(
                item,
                max_dict_items=max_dict_items,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for item in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            items.append({"truncated_items": len(value) - max_list_items})
        return items
    if isinstance(value, dict):
        ordered_keys = [key for key in _PRIORITY_KEYS if key in value]
        ordered_keys.extend(key for key in value if key not in ordered_keys)
        selected = ordered_keys[:max_dict_items]
        result = {
            key: _bounded_value(
                value[key],
                max_dict_items=max_dict_items,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for key in selected
        }
        if len(value) > max_dict_items:
            result["truncated_fields"] = len(value) - max_dict_items
        return result
    return value


def finalize_mcp_response(
    result: Any,
    *,
    max_chars: int = DEFAULT_RESPONSE_MAX_CHARS,
    max_tokens: int = DEFAULT_RESPONSE_MAX_TOKENS,
) -> dict[str, Any]:
    """Return a JSON-safe response within MCP character and estimated-token budgets."""
    original = result if isinstance(result, dict) else {"ok": True, "result": result}
    original_json = _compact_json(original)
    original_chars = len(original_json)
    original_tokens = _estimate_tokens(original_json)
    if original_chars <= max_chars and original_tokens <= max_tokens:
        return original

    for max_dict_items, max_list_items, max_string_chars, max_depth in _PROFILES:
        bounded = _bounded_value(
            original,
            max_dict_items=max_dict_items,
            max_list_items=max_list_items,
            max_string_chars=max_string_chars,
            max_depth=max_depth,
        )
        bounded["response_truncated"] = True
        bounded["response_budget"] = {
            "max_chars": max_chars,
            "max_tokens": max_tokens,
            "original_chars": original_chars,
            "original_estimated_tokens": original_tokens,
        }
        bounded_json = _compact_json(bounded)
        if len(bounded_json) <= max_chars and _estimate_tokens(bounded_json) <= max_tokens:
            return bounded

    minimal = {
        key: original[key]
        for key in ("ok", "error", "message", "operation", "status", "shared_context")
        if key in original
    }
    if "shared_context" in minimal:
        minimal["shared_context"] = _bounded_value(
            minimal["shared_context"],
            max_dict_items=8,
            max_list_items=1,
            max_string_chars=80,
            max_depth=4,
        )
    minimal.update(
        {
            "response_truncated": True,
            "response_budget": {
                "max_chars": max_chars,
                "max_tokens": max_tokens,
                "original_chars": original_chars,
                "original_estimated_tokens": original_tokens,
            },
        }
    )
    return minimal