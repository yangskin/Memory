"""稳定身份规范化。

同一用户/代理在不同客户端里可能出现大小写、全半角和空白差异。身份一旦
进入目录名、task binding 或可见性过滤，就必须先规范化，否则会产生平行的
个人记忆分区。这里仅处理身份，不负责认证。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

_DASH_RE = re.compile(r"-+")
_PLACEHOLDER = "unknown"


@dataclass(frozen=True)
class CanonicalIdentity:
    raw: str
    canonical: str
    changed: bool
    alias_used: bool = False


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
