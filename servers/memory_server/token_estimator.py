from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]+")


def estimate_tokens_from_chars(char_count: int) -> int:
    """Rough token estimate from raw character count (ASCII-biased, legacy)."""
    if char_count <= 0:
        return 0
    return (char_count + 3) // 4


def estimate_tokens(text: str) -> int:
    """Estimate token count with CJK-aware heuristic.

    - CJK characters: ~0.6 tokens per character (most CJK chars → 1-2 tokens).
    - ASCII / other: ~0.25 tokens per character (≈ chars / 4).
    """
    if not text:
        return 0
    cjk_chars = sum(len(m.group()) for m in _CJK_RE.finditer(text))
    ascii_chars = len(text) - cjk_chars
    cjk_tokens = int(cjk_chars * 0.6 + 0.5)
    ascii_tokens = (ascii_chars + 3) // 4
    return cjk_tokens + ascii_tokens
