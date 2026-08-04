"""Safe process logging for Memory Hub.

Callers log identifiers and error codes only. Event bodies, bearer tokens,
database URLs, and provider credentials are never accepted as log fields.
"""

from __future__ import annotations

import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )