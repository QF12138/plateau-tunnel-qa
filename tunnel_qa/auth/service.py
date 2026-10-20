import hashlib
import math
import secrets
import time
from collections import deque
from threading import Lock
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from tunnel_qa.auth.models import LoginSession, UserAccount
from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import Role, SecurityLevel
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext

COOKIE = "tunnel_session"
SESSION_SECONDS = 8 * 60 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return f"pbkdf2_sha256$600000${salt}${derived.hex()}"


def check_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, iterations, salt, expected = (encoded or DUMMY_HASH).split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return bool(encoded) and secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32


def bootstrap(session: Session, settings: Settings) -> None:
    # One-time migration: restarting never resets passwords changed in the UI.
    if session.scalar(select(UserAccount.id).limit(1)) or not settings.public_access_password:
        return
    session.add(UserAccount(id=settings.public_access_user_id, username=settings.public_access_username.lower(),
        display_name="系统管理员", password_hash=hash_password(settings.public_access_password.get_secret_value()),
        role="system_admin", security_level="confidential", enabled=True))
    reader = settings.public_reader_password
    session.add(UserAccount(id=settings.public_reader_username, username=settings.public_reader_username.lower(), display_name="普通用户",
        password_hash=hash_password(reader.get_secret_value()) if reader else None,
        role="field_engineer", security_level=settings.public_reader_security_level.value, enabled=reader is not None))
    session.commit()


def authenticate(session: Session, username: str, password: str) -> UserAccount | None:
    account = session.scalar(select(UserAccount).where(UserAccount.username == username.strip().lower()))
    valid = check_password(password, account.password_hash if account else None)
    return account if valid and account and account.enabled else None


def access_for(account: UserAccount) -> AccessContext:
    return AccessContext(account.id, Role(account.role), SecurityLevel(account.security_level))


def issue_session(session: Session, account: UserAccount, *, previous_token: str | None = None) -> tuple[str, str]:
    # Serialize with account updates before issuing a token. Recheck the current
    # account so password reset/disable cannot race with an old-password login.
    encoded = account.password_hash
    session.execute(update(UserAccount).where(UserAccount.id == account.id).values(enabled=UserAccount.enabled))
    session.refresh(account)
    if not account.enabled or account.password_hash != encoded:
        session.rollback()
        raise AppError("LOGIN_CHANGED", "账号状态已改变，请重新登录", status_code=401)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    session.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
    if previous_token and len(previous_token) <= 200:
        session.execute(delete(LoginSession).where(
            LoginSession.token_hash == hashlib.sha256(previous_token.encode()).hexdigest()))
    session.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=account.id,
                             csrf_token=csrf, expires_at=time.time() + SESSION_SECONDS))
    session.commit()
    return token, csrf


def session_identity(session: Session, token: str) -> tuple[UserAccount, LoginSession] | None:
    if len(token) > 200:
        return None
    login = session.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
    if not login or login.expires_at <= time.time():
        return None
    account = session.get(UserAccount, login.user_id)
    return (account, login) if account and account.enabled else None


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    try:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return  # Non-browser API clients may omit Origin; Cookie writes still need CSRF.
    expected = _origin_tuple(f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}")
    if not expected or _origin_tuple(origin) != expected:
        raise AppError("ORIGIN_REJECTED", "请从本站页面提交请求", status_code=403)


def safe_login_destination(target: str | None, *, can_manage: bool) -> str:
    # Exact allowlist avoids protocol-relative, encoded, or backslash redirects.
    return "/accounts" if target == "/accounts" and can_manage else "/"


class LoginLimiter:
    """Bounded single-process throttling, keyed by username (not proxy-supplied IP)."""
    def __init__(self) -> None:
        self.failures: dict[tuple[str, str], deque[float]] = {}
        self.lock = Lock()

    def _prune(self, now: float) -> None:
        for key in list(self.failures):
            entries = self.failures[key]
            while entries and now - entries[0] >= 60:
                entries.popleft()
            if not entries:
                del self.failures[key]

    def check(self, username: str, *, scope: str = "login") -> None:
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            entries = self.failures.get((scope, username.strip().lower()), ())
            all_entries = sorted(value for key, queue in self.failures.items() if key[0] == scope for value in queue)
            unblock_at = []
            if len(entries) >= 10:
                unblock_at.append(entries[len(entries) - 10] + 60)
            if len(all_entries) >= 100:
                unblock_at.append(all_entries[len(all_entries) - 100] + 60)
            if unblock_at:
                seconds = max(1, math.ceil(max(unblock_at) - now))
                raise AppError("LOGIN_RATE_LIMITED", f"尝试次数过多，请 {seconds} 秒后重试", status_code=429,
                               retryable=True, details={"retry_after_seconds": seconds})

    def failed(self, username: str, *, scope: str = "login") -> None:
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            self.failures.setdefault((scope, username.strip().lower()), deque(maxlen=10)).append(now)

    def succeeded(self, username: str, *, scope: str = "login") -> None:
        with self.lock:
            self.failures.pop((scope, username.strip().lower()), None)
