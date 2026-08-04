"""Read-only shared-feed query contract.

The shared feed is a strictly project-visible view: it only ever returns
events whose ``scope`` is one of the shared/project scopes, plus the LLM
rendered project brief. It never returns any user's personal-scope events,
so a browser dashboard cannot leak private content.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SharedFeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_minutes: int = Field(default=720, ge=1, le=10080)
    max_items: int = Field(default=50, ge=1, le=200)
