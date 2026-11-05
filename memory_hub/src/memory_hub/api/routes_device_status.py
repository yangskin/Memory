"""设备状态独立模块：每台设备每个通道一行覆盖更新，不使用 Board 留言板。

状态只是 advisory 可见性信息：不表示设备在线，不承载任务生命周期，也不作为执行授权。
发布方身份由 Token 决定；同一凭据下的设备名数量有上限，表大小只随凭据与设备数增长。
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from memory_hub.api.dependencies import effective_user_id, require_principal
from memory_hub.auth.permissions import Principal, author_auth_id
from memory_hub.db.models import DeviceStatus
from memory_hub.services.secret_scan import contains_secret

router = APIRouter()

# 已知的状态通道；未知通道一律拒绝，避免任意凭据无界创建新通道。
DEVICE_STATUS_CHANNELS = ("hub-health", "codex-execution")
# 旧的手工共享凭据可能被多台设备共用，上限留足余量；过期很久的行由 worker 清理，不长期占名额。
MAX_DEVICES_PER_CREDENTIAL = 64
STALE_RETENTION = timedelta(days=7)
MAX_READ_ITEMS = 20
MAX_CONTENT_CHARS = 65536
_DEVICE_NAME = r"^[^\W_][\w.:-]{0,95}$"


def purge_stale_device_status(session, *, now: datetime | None = None, batch: int = 500) -> int:
    """删除过期超过保留期的状态行（设备改名、换凭据或停用后遗留），保持表只随在用设备增长。"""
    cutoff = (now or datetime.now(UTC)) - STALE_RETENTION
    victims = list(session.execute(
        select(DeviceStatus.project_id, DeviceStatus.channel, DeviceStatus.author_auth_id, DeviceStatus.device_name)
        .where(DeviceStatus.expires_at < cutoff).limit(max(1, batch))))
    for key in victims:
        session.delete(session.get(DeviceStatus, tuple(key)))
    if victims:
        session.commit()
    return len(victims)


class DeviceStatusWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str = Field(pattern=_DEVICE_NAME)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _assert_project(principal: Principal, project_id: str) -> None:
    if principal.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project access denied")


def _assert_channel(channel: str) -> None:
    if channel not in DEVICE_STATUS_CHANNELS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device status channel")


def _enforce_rate_limit(request: Request, principal: Principal, category: str, limit: int) -> None:
    decision = request.app.state.rate_limiter.check(principal.token_id, category, limit)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(max(1, math.ceil(decision.retry_after_seconds)))},
        )


def _item(row: DeviceStatus) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "channel": row.channel,
        "device_name": row.device_name,
        "author_user_id": row.author_user_id,
        "author_auth_id": row.author_auth_id,
        "content": row.content,
        "updated_at": _aware(row.updated_at).isoformat(),
        "expires_at": _aware(row.expires_at).isoformat(),
    }


def _apply(row: DeviceStatus, payload: DeviceStatusWrite, user_id: str, now: datetime) -> None:
    row.author_user_id = user_id
    row.content = payload.content
    row.updated_at = now
    row.expires_at = now + timedelta(seconds=payload.ttl_seconds)


@router.post("/v1/projects/{project_id}/device-status/{channel}")
def put_device_status(project_id: str, channel: str, payload: DeviceStatusWrite, request: Request,
                      principal: Principal = Depends(require_principal("events:write"))) -> dict[str, object]:
    """覆盖写入本凭据下该设备在该通道的唯一状态行；重复发送天然幂等。"""
    _assert_project(principal, project_id)
    _assert_channel(channel)
    if contains_secret(payload.content):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "device status appears to include secret material")
    _enforce_rate_limit(request, principal, "device_status_write", 60)
    author = author_auth_id(project_id, principal.token_id)
    user_id = effective_user_id(request, principal)
    key = (project_id, channel, author, payload.device_name)
    factory = request.app.state.session_factory
    with factory() as session:
        for attempt in (0, 1):
            now = datetime.now(UTC)
            row = session.get(DeviceStatus, key)
            if row is None:
                known = session.scalar(select(func.count()).select_from(DeviceStatus).where(
                    DeviceStatus.project_id == project_id, DeviceStatus.channel == channel,
                    DeviceStatus.author_auth_id == author, DeviceStatus.expires_at >= now - STALE_RETENTION))
                if known >= MAX_DEVICES_PER_CREDENTIAL:
                    raise HTTPException(status.HTTP_409_CONFLICT, "too many devices for this credential")
                row = DeviceStatus(project_id=project_id, channel=channel, author_auth_id=author,
                                   device_name=payload.device_name)
                session.add(row)
            _apply(row, payload, user_id, now)
            try:
                session.commit()
            except IntegrityError:
                # 同一行被并发首次写入：回滚后按已存在行覆盖一次。
                session.rollback()
                if attempt:
                    raise HTTPException(status.HTTP_409_CONFLICT, "device status write conflict") from None
                continue
            return {"ok": True, "status": _item(row)}
    raise HTTPException(status.HTTP_409_CONFLICT, "device status write conflict")


@router.get("/v1/projects/{project_id}/device-status")
def list_device_status(project_id: str, request: Request, channel: str = Query(...),
                       device_name: str | None = Query(None),
                       principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    """返回该通道的状态行（含已过期行，由调用方判定新鲜度）；同名设备多行即身份冲突。"""
    _assert_project(principal, project_id)
    _assert_channel(channel)
    if device_name is not None and not re.fullmatch(_DEVICE_NAME, device_name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid device name")
    _enforce_rate_limit(request, principal, "device_status_read", 120)
    query = select(DeviceStatus).where(DeviceStatus.project_id == project_id, DeviceStatus.channel == channel)
    if device_name is not None:
        query = query.where(DeviceStatus.device_name == device_name)
    query = query.order_by(DeviceStatus.updated_at.desc(), DeviceStatus.device_name).limit(MAX_READ_ITEMS)
    with request.app.state.session_factory() as session:
        return {"ok": True, "items": [_item(row) for row in session.scalars(query)]}


@router.get("/v1/shared-device-status")
def shared_device_status(request: Request,
                         principal: Principal = Depends(require_principal("context:read"))) -> dict[str, object]:
    """共享面板只读视图：项目取自 Token，只返回仍在有效期内的状态。"""
    _enforce_rate_limit(request, principal, "device_status_read", 120)
    now = datetime.now(UTC)
    query = (select(DeviceStatus)
             .where(DeviceStatus.project_id == principal.project_id, DeviceStatus.expires_at > now)
             .order_by(DeviceStatus.updated_at.desc(), DeviceStatus.device_name)
             .limit(MAX_READ_ITEMS))
    with request.app.state.session_factory() as session:
        return {"project_id": principal.project_id, "generated_at": now.isoformat(),
                "items": [_item(row) for row in session.scalars(query)]}
