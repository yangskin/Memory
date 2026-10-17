"""设备自行申请、项目管理员核对配对码批准；凭据明文不经过服务端。"""
from datetime import UTC, datetime, timedelta
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from memory_hub.api.dependencies import require_principal
from memory_hub.auth.permissions import Principal
from memory_hub.auth.tokens import verify_secret
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


class EnrollmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(approve|reject)$")
    pairing_code: str = Field(pattern=r"^[A-F0-9]{12}$")


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


def _project(principal, project_id):
    if principal.project_id != project_id:
        raise HTTPException(403, "project access denied")


@router.post("/v1/projects/{project_id}/device-enrollments")
def enroll(project_id: str, payload: EnrollmentRequest, request: Request):
    # 不信任代理传入的 X-Forwarded-For；全局限流与数据库项目限额共同约束匿名申请。
    _limit(request, "enroll_global", 60)
    client = request.client.host if request.client else "unknown"
    _limit(request, "enroll_source", 10, hashlib.sha256(client.encode()).hexdigest())
    with request.app.state.session_factory() as session:
        # 相同项目的并发申请串行，避免用并发绕过数据库持久配额。
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "device-enrollment:" + project_id})
        old = session.get(DeviceEnrollment, payload.enrollment_id)
        if old:
            if old.project_id != project_id or any(getattr(old, key) != value for key, value in payload.model_dump().items()):
                raise HTTPException(409, "enrollment conflict")
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
        item = DeviceEnrollment(**payload.model_dump(), project_id=project_id, status="pending",
                                created_at=_now(), expires_at=_now() + timedelta(minutes=30))
        session.add(item)
        try:
            session.commit()
        except IntegrityError:
            raise HTTPException(409, "enrollment conflict") from None
        return _view(item)


@router.get("/v1/projects/{project_id}/device-enrollments/{enrollment_id}/status")
def enrollment_status(project_id: str, enrollment_id: str, request: Request):
    _limit(request, "enroll_status", 240)
    secret = request.headers.get("X-Memory-Enrollment-Secret", "")
    if len(secret) != 43:
        raise HTTPException(401, "invalid enrollment authentication")
    with request.app.state.session_factory() as session:
        item = session.get(DeviceEnrollment, enrollment_id)
        if not item or item.project_id != project_id or not verify_secret(secret, item.poll_secret_hash):
            raise HTTPException(401, "invalid enrollment authentication")
        result = _view(item)
        if item.status == "approved" and not active_token(session, item.token_id):
            result["status"] = "revoked"
        return result


@router.get("/v1/projects/{project_id}/device-enrollments")
def pending_enrollments(project_id: str, request: Request,
                        principal: Principal = Depends(require_principal("devices:approve"))):
    _project(principal, project_id)
    with request.app.state.session_factory() as session:
        items = session.scalars(select(DeviceEnrollment).where(
            DeviceEnrollment.project_id == project_id, DeviceEnrollment.status == "pending",
            DeviceEnrollment.expires_at > _now()).order_by(DeviceEnrollment.created_at).limit(50))
        return {"items": [_view(item) for item in items]}


@router.post("/v1/projects/{project_id}/device-enrollments/{enrollment_id}/decision")
def decide_enrollment(project_id: str, enrollment_id: str, payload: EnrollmentDecision, request: Request,
                      principal: Principal = Depends(require_principal("devices:approve"))):
    _project(principal, project_id)
    with request.app.state.session_factory() as session:
        item = session.scalar(select(DeviceEnrollment).where(
            DeviceEnrollment.enrollment_id == enrollment_id).with_for_update())
        if not item or item.project_id != project_id:
            raise HTTPException(404, "enrollment not found")
        if not hmac.compare_digest(payload.pairing_code, item.poll_secret_hash[:12].upper()):
            raise HTTPException(409, "pairing code mismatch")
        desired = "approved" if payload.decision == "approve" else "rejected"
        if item.status == desired:
            return _view(item)  # 应答丢失时同一决定可重试，不重复发凭据。
        if _view(item)["status"] != "pending":
            raise HTTPException(409, "enrollment no longer pending")
        if desired == "approved":
            if session.get(AccessToken, item.token_id):
                raise HTTPException(409, "token already exists")
            session.add(AccessToken(token_id=item.token_id, token_secret_hash=item.token_secret_hash,
                token_prefix="mem_v1." + item.token_id[:13], user_id=principal.user_id,
                project_id=project_id, scopes=list(DEVICE_SCOPES)))
        item.status = desired
        item.approved_by = principal.token_id
        session.commit()
        return _view(item)
