import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tunnel_qa.api.dependencies import get_access_context, get_session
from tunnel_qa.auth.models import AccountEvent, LoginSession, UserAccount
from tunnel_qa.auth.service import (COOKIE, SESSION_SECONDS, authenticate, check_password,
    hash_password, issue_session, require_same_origin, safe_login_destination)
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext

router = APIRouter(prefix="/api/v1")


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=256)
    next: str | None = Field(default=None, max_length=2048)


class AccountInput(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["system_admin", "field_engineer"] = "field_engineer"
    enabled: bool = True
    password: SecretStr | None = Field(default=None, min_length=8, max_length=128)
    admin_password: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("显示名称不能为空")
        return value.strip()


def public_account(account: UserAccount) -> dict:
    return {"id": account.id, "username": account.username, "display_name": account.display_name,
            "role": account.role, "enabled": account.enabled,
            "password_configured": bool(account.password_hash), "created_at": account.created_at}


@router.post("/auth/login")
def login(payload: LoginInput, request: Request, response: Response, session: Session = Depends(get_session)):
    require_same_origin(request)
    limiter = request.app.state.login_limiter
    limiter.check(payload.username)
    account = authenticate(session, payload.username, payload.password.get_secret_value())
    if account is None:
        limiter.failed(payload.username)
        raise AppError("LOGIN_FAILED", "账号或密码错误，或账号尚未启用", status_code=401)
    token, csrf = issue_session(session, account, previous_token=request.cookies.get(COOKIE))
    limiter.succeeded(payload.username)
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "csrf_token": csrf,
            "redirect_to": safe_login_destination(payload.next, can_manage=account.role == "system_admin")}


@router.post("/auth/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session),
           access: AccessContext = Depends(get_access_context)):
    token = request.cookies.get(COOKIE)
    if token:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        session.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/accounts")
def list_accounts(session: Session = Depends(get_session), access: AccessContext = Depends(get_access_context)):
    access.require("settings:write")
    return {"accounts": [public_account(row) for row in session.scalars(select(UserAccount).order_by(UserAccount.created_at)).all()]}


def confirm_admin(payload: AccountInput, request: Request, session: Session, access: AccessContext) -> None:
    access.require("settings:write")
    session.execute(update(UserAccount).where(UserAccount.id == access.user_id).values(enabled=UserAccount.enabled))
    actor = session.get(UserAccount, access.user_id, populate_existing=True)
    request.app.state.login_limiter.check(access.user_id, scope="admin-confirmation")
    if not actor or not actor.enabled or actor.role != "system_admin" or not check_password(payload.admin_password.get_secret_value(), actor.password_hash):
        request.app.state.login_limiter.failed(access.user_id, scope="admin-confirmation")
        raise AppError("ADMIN_PASSWORD_REQUIRED", "管理员当前密码不正确", status_code=403)
    request.app.state.login_limiter.succeeded(access.user_id, scope="admin-confirmation")


@router.post("/accounts", status_code=201)
def create_account(payload: AccountInput, request: Request, session: Session = Depends(get_session),
                   access: AccessContext = Depends(get_access_context)):
    confirm_admin(payload, request, session, access)
    if payload.enabled and not payload.password:
        raise AppError("PASSWORD_REQUIRED", "启用账号前请先设置密码")
    account = UserAccount(username=payload.username, display_name=payload.display_name, role=payload.role,
        enabled=payload.enabled, password_hash=hash_password(payload.password.get_secret_value()) if payload.password else None,
        security_level="confidential" if payload.role == "system_admin" else "internal")
    session.add(account)
    try:
        session.flush()
        session.add(AccountEvent(actor_id=access.user_id, target_id=account.id, action="create"))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AppError("USERNAME_EXISTS", "此登录账号已存在", status_code=409)
    return public_account(account)


@router.put("/accounts/{account_id}")
def update_account(account_id: str, payload: AccountInput, request: Request, session: Session = Depends(get_session),
                   access: AccessContext = Depends(get_access_context)):
    confirm_admin(payload, request, session, access)
    # Acquire the primary write lock before checking current roles/account state.
    session.execute(update(UserAccount).where(UserAccount.id == access.user_id).values(enabled=UserAccount.enabled))
    account = session.get(UserAccount, account_id, populate_existing=True)
    if not account:
        raise AppError("ACCOUNT_NOT_FOUND", "账号不存在", status_code=404)
    if account_id == access.user_id and (not payload.enabled or payload.role != "system_admin"):
        raise AppError("KEEP_CURRENT_ADMIN", "不能停用或降级当前登录的管理员")
    if payload.enabled and not (payload.password or account.password_hash):
        raise AppError("PASSWORD_REQUIRED", "启用账号前请先设置密码")
    account.username, account.display_name = payload.username, payload.display_name
    account.role, account.enabled = payload.role, payload.enabled
    account.security_level = "confidential" if payload.role == "system_admin" else "internal"
    if payload.password:
        account.password_hash = hash_password(payload.password.get_secret_value())
    # Every account edit invalidates its sessions, including self password changes.
    session.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
    session.add(AccountEvent(actor_id=access.user_id, target_id=account.id,
                            action="update_with_password" if payload.password else "update"))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AppError("USERNAME_EXISTS", "此登录账号已存在", status_code=409)
    return {**public_account(account), "relogin_required": account_id == access.user_id}
