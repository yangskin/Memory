"""设备自动注册（邀请凭据或开放项目）；凭据明文不经过服务端，不提供人工审批路径。"""
from datetime import UTC, datetime, timedelta
import hashlib
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from memory_hub.api.client_address import client_source
from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.db.models import AccessToken, DeviceEnrollment
from memory_hub.db.repositories import active_token

router = APIRouter()
DEVICE_SCOPES = ["events:write", "context:read"]


class EnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    device_name: str = Field(pattern=r"^[^\W_][\w.:-]{0,95}$")
    token_id: str = Field(pattern=r"^tok_[A-Za-z0-9_-]{24}$")
    token_secret_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    poll_secret_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def _now():
    return datetime.now(UTC)


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _view(item):
    state = item.status
    if state == "pending" and _aware(item.expires_at) <= _now():
        state = "expired"
    return dict(enrollment_id=item.enrollment_id, device_name=item.device_name,
                pairing_code=item.poll_secret_hash[:12].upper(), status=state,
                expires_at=_aware(item.expires_at).isoformat())


def _limit(request, category, limit, key="global"):
    if not request.app.state.rate_limiter.allow(key, category, limit):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})


def _source_key(request):
    # 限流键只保存来源地址的哈希，不在内存中保留明文 IP。
    return hashlib.sha256(client_source(request).encode()).hexdigest()


def _project(principal, project_id):
    if principal.project_id != project_id:
        raise HTTPException(403, "project access denied")


@router.post("/v1/projects/{project_id}/device-enrollments/auto")
def auto_enroll(project_id: str, payload: EnrollmentRequest, request: Request,
                principal: Principal = Depends(require_principal("devices:enroll"))):
    """A project invitation grants a separate device token, never admin scopes."""
    _project(principal, project_id)
    _limit(request, "auto_enroll", 10, principal.token_id)
    with request.app.state.session_factory() as session:
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                            {"key": "device-enrollment:" + project_id})
        old = session.get(DeviceEnrollment, payload.enrollment_id)
        if old:
            if (old.project_id != project_id or old.approved_by != principal.token_id
                    or any(getattr(old, key) != value for key, value in payload.model_dump().items())):
                raise HTTPException(409, "enrollment conflict")
            if old.status != "approved" or not active_token(session, old.token_id):
                raise HTTPException(409, "enrollment inactive")
            return _view(old)
        count = session.scalar(select(func.count()).select_from(DeviceEnrollment).where(
            DeviceEnrollment.project_id == project_id,
            DeviceEnrollment.created_at > _now() - timedelta(hours=1)))
        if count >= 50:
            raise HTTPException(429, "project enrollment quota exceeded")
        if session.get(AccessToken, payload.token_id):
            raise HTTPException(409, "enrollment conflict")
        item = DeviceEnrollment(**payload.model_dump(), project_id=project_id, status="approved",
                                approved_by=principal.token_id, created_at=_now(),
                                expires_at=_now() + timedelta(minutes=30))
        session.add(item)
        session.add(AccessToken(token_id=payload.token_id, token_secret_hash=payload.token_secret_hash,
                               token_prefix="mem_v1." + payload.token_id[:13],
                               user_id=principal.user_id, project_id=project_id, scopes=list(DEVICE_SCOPES)))
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "enrollment conflict") from None
        return _view(item)


@router.get("/v1/projects/{project_id}/connection")
def connection(project_id: str, principal: Principal = Depends(require_principal("context:read"))):
    """Read-only capability check; identity is always derived from the token."""
    _project(principal, project_id)
    return {"ok": True, "project_id": principal.project_id, "user_id": principal.user_id,
            "capabilities": {"board_read": True, "tasks_read": True, "project_brief": True,
                             "board_write": "events:write" in principal.scopes,
                             "tasks_write": "events:write" in principal.scopes}}


@router.post("/v1/projects/{project_id}/device-enrollments/open")
def open_enroll(project_id: str, payload: EnrollmentRequest, request: Request):
    """Public enrollment is enabled only for explicitly configured projects."""
    if project_id not in request.app.state.settings.open_enrollment_projects:
        raise HTTPException(404, "enrollment unavailable")
    # 先查来源额度：被来源额度拒绝的请求不能再消耗全局额度，否则单一来源即可挡住所有人。
    _limit(request, "open_enroll_source", 10, _source_key(request))
    _limit(request, "open_enroll_global", 60)
    with request.app.state.session_factory() as session:
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                            {"key": "device-enrollment:" + project_id})
        old = session.get(DeviceEnrollment, payload.enrollment_id)
        if old:
            if (old.project_id != project_id or old.approved_by != "open-registration"
                    or any(getattr(old, key) != value for key, value in payload.model_dump().items())):
                raise HTTPException(409, "enrollment conflict")
            if old.status != "approved" or not active_token(session, old.token_id):
                raise HTTPException(409, "enrollment inactive")
            return _view(old)
        known = session.scalar(select(AccessToken.token_id).where(
            AccessToken.project_id == project_id, AccessToken.revoked_at.is_(None),
            AccessToken.expires_at.is_(None) | (AccessToken.expires_at > _now())).limit(1))
        if not known:
            raise HTTPException(404, "enrollment unavailable")
        count = session.scalar(select(func.count()).select_from(DeviceEnrollment).where(
            DeviceEnrollment.project_id == project_id,
            DeviceEnrollment.created_at > _now() - timedelta(hours=1)))
        if count >= 50:
            raise HTTPException(429, "project enrollment quota exceeded")
        if session.get(AccessToken, payload.token_id):
            raise HTTPException(409, "enrollment conflict")
        item = DeviceEnrollment(**payload.model_dump(), project_id=project_id, status="approved",
                                approved_by="open-registration", created_at=_now(),
                                expires_at=_now() + timedelta(minutes=30))
        session.add(item)
        session.add(AccessToken(token_id=payload.token_id, token_secret_hash=payload.token_secret_hash,
                               token_prefix="mem_v1." + payload.token_id[:13],
                               user_id="open-device:" + uuid4().hex,
                               project_id=project_id, scopes=list(DEVICE_SCOPES)))
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "enrollment conflict") from None
        return _view(item)
