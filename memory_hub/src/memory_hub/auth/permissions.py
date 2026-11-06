"""Token-derived request identity and scope checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    token_id: str
    user_id: str
    project_id: str
    scopes: frozenset[str]
    # 凭据绑定的设备名；任务执行者身份只从这里推导，未绑定为 None。
    device_name: str | None = None


def allows(principal: Principal, scope: str) -> bool:
    return scope in principal.scopes


def author_auth_id(project_id: str, token_id: str) -> str:
    """公开不可逆的发布者标识；不返回令牌ID、凭据或其秘密部分。"""
    return hashlib.sha256(("memory.board-author/1:" + project_id + ":" + token_id).encode()).hexdigest()