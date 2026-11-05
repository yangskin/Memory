"""Single secret-material detector shared by every Hub write path.

Event ingest and the Board apply different policies on a match (redact the
body vs. reject the request), but they must agree on what counts as a secret.
"""

from __future__ import annotations

import re

SECRET_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s]+"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(?:ASIA|A3T)[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\"type\"\s*:\s*\"service_account\"",
    re.I,
)


def contains_secret(text: str | None) -> bool:
    return bool(text and SECRET_PATTERN.search(text))
