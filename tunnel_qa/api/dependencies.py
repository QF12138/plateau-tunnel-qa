from collections.abc import Iterator

from fastapi import Header, Request
from sqlalchemy.orm import Session

from tunnel_qa.core.enums import Role, SecurityLevel
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.db.session import Database


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_session(request: Request) -> Iterator[Session]:
    database = get_database(request)
    session = database.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_access_context(
    request: Request,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_role: str | None = Header(None, alias="X-Role"),
    x_security_level: str = Header("internal", alias="X-Security-Level"),
    x_organization: str | None = Header(None, alias="X-Organization"),
) -> AccessContext:
    verified = getattr(request.state, "access_context", None)
    if verified is not None:
        return verified
    settings = request.app.state.settings
    if settings.public_access_enabled:
        raise AppError("AUTH_REQUIRED", "请先登录", status_code=401)
    if not settings.trust_identity_headers:
        if request.client and request.client.host in {"127.0.0.1", "::1"}:
            return AccessContext(settings.public_access_user_id, Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)
        raise AppError("AUTH_REQUIRED", "非本机访问必须启用身份验证", status_code=401)
    if not x_user_id or not x_role:
        raise AppError("AUTH_REQUIRED", "可信网关未提供身份信息", status_code=401)
    try:
        role = Role(x_role)
    except ValueError as exc:
        raise AppError("INVALID_ROLE", f"未知角色: {x_role}", status_code=401) from exc
    try:
        level = SecurityLevel(x_security_level)
    except ValueError as exc:
        raise AppError(
            "INVALID_SECURITY_LEVEL", f"未知密级: {x_security_level}", status_code=401
        ) from exc
    return AccessContext(
        user_id=x_user_id,
        role=role,
        security_level=level,
        organization=x_organization,
    )

