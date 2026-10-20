import base64
import binascii
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Depends
from starlette.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from tunnel_qa.api.router import router
from tunnel_qa.api.dependencies import get_access_context
from tunnel_qa.auth.router import router as auth_router
from tunnel_qa.auth.service import COOKIE, LoginLimiter, access_for, authenticate, bootstrap, require_same_origin, session_identity, safe_login_destination
from tunnel_qa.core.config import Settings, get_settings
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.tracing import get_trace_id, set_trace_id
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.enums import Role, SecurityLevel
from tunnel_qa.db.session import Database
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.qa.model_gateway import ModelGateway, build_model_gateway


def _basic_credentials(authorization: str | None) -> tuple[str, str] | None:
    if not authorization:
        return None
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


def register_models() -> None:
    from tunnel_qa.auth import models as auth_models  # noqa: F401
    from tunnel_qa.data import models as data_models  # noqa: F401
    from tunnel_qa.knowledge import models as knowledge_models  # noqa: F401
    from tunnel_qa.qa import models as qa_models  # noqa: F401
    from tunnel_qa.quality import models as quality_models  # noqa: F401
    from tunnel_qa.settings import models as settings_models  # noqa: F401


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    model_gateway: ModelGateway | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.public_access_enabled and settings.public_access_password is None:
        raise RuntimeError(
            "启用公网访问时必须配置 TUNNEL_QA_PUBLIC_ACCESS_PASSWORD"
        )
    public_password = (
        settings.public_access_password.get_secret_value()
        if settings.public_access_password is not None
        else ""
    )
    if settings.public_access_enabled and not public_password.strip():
        raise RuntimeError("PUBLIC_ACCESS_PASSWORD 不能为空")
    if settings.public_reader_password is not None:
        if not settings.public_reader_password.get_secret_value().strip():
            raise RuntimeError("PUBLIC_READER_PASSWORD 不能为空；未启用时请省略此项")
        if settings.public_reader_username == settings.public_access_username:
            raise RuntimeError("普通账号与管理员账号不能同名")
    register_models()
    database = database or Database(settings)
    database.create_schema()
    content_store = DocumentContentStore(settings)
    from tunnel_qa.settings.service import SystemSettingsService
    from tunnel_qa.data.cleanup import retry_cleanup

    with database.session_factory() as session:
        bootstrap(session, settings)
        SystemSettingsService(session).initialize(settings)
        retry_cleanup(session, content_store, settings)
        content_store.backfill(session)
    model_gateway = model_gateway or build_model_gateway(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="融合知识图谱、混合检索与 DeepSeek 的可追溯领域问答服务。",
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url="/api/redoc" if settings.environment != "production" else None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.content_store = content_store
    app.state.model_gateway = model_gateway
    app.state.login_limiter = LoginLimiter()
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
    # Single-process demo limit; valid credentials remain usable even after failures.
    failed_logins: deque[float] = deque(maxlen=20)

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = set_trace_id(request.headers.get("X-Trace-Id"))
        response: Response
        public_paths = {"/healthz", "/login", "/api/v1/auth/login", "/auth-assets.css", "/auth-pages.js", "/favicon.ico"}
        if settings.public_access_enabled and request.url.path not in public_paths:
            token = request.cookies.get(COOKIE)
            browser = "sec-fetch-mode" in request.headers or "text/html" in request.headers.get("accept", "")
            supplied = None if browser or token else _basic_credentials(request.headers.get("Authorization"))
            def resolve_identity():
                with database.session_factory() as db:
                    if token:
                        identity = session_identity(db, token)
                        if identity:
                            account, login = identity
                            return access_for(account), account.username, login.csrf_token
                    elif supplied:
                        account = authenticate(db, *supplied)
                        if account:
                            return access_for(account), account.username, None
                return None
            identity = await run_in_threadpool(resolve_identity)
            if identity:
                access, username, csrf = identity
                request.state.access_context = access
                request.state.account_username = username
                request.state.csrf_token = csrf
                try:
                    if request.method not in {"GET", "HEAD", "OPTIONS"}:
                        require_same_origin(request)
                        if token and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf or ""):
                            raise AppError("CSRF_REJECTED", "登录校验已失效，请刷新页面后重试", status_code=403)
                    response = await call_next(request)
                except AppError as exc:
                    response = JSONResponse(status_code=exc.status_code, content=exc.as_dict(trace_id))
            else:
                now = time.monotonic()
                while failed_logins and now - failed_logins[0] >= 60:
                    failed_logins.popleft()
                if supplied and len(failed_logins) >= 20:
                    response = JSONResponse(status_code=429, content={"error_code": "LOGIN_RATE_LIMITED", "message": "尝试次数过多，请稍后重试"}, headers={"Retry-After": "60"})
                elif not request.url.path.startswith("/api/"):
                    parameters = {}
                    if token:
                        parameters["reason"] = "expired"
                    if request.url.path == "/accounts":
                        parameters["next"] = "/accounts"
                    response = RedirectResponse("/login" + ("?" + urlencode(parameters) if parameters else ""), status_code=303)
                else:
                    response = JSONResponse(status_code=401, content={"error_code": "PUBLIC_AUTH_REQUIRED", "message": "登录已过期，请重新登录", "trace_id": trace_id})
                if supplied:
                    failed_logins.append(now)
                if token:
                    response.delete_cookie(COOKIE, path="/", httponly=True, secure=request.url.scheme == "https", samesite="lax")
                if not browser:
                    response.headers["WWW-Authenticate"] = 'Basic realm="Tunnel QA", charset="UTF-8"'
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        headers = {}
        if exc.status_code == 429 and "retry_after_seconds" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict(get_trace_id()), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({
                "error_code": "VALIDATION_ERROR",
                "message": "请求字段校验失败",
                "field": None,
                "retryable": False,
                "trace_id": get_trace_id(),
                "details": {"errors": [{k: v for k, v in error.items() if k not in {"input", "ctx"}} for error in exc.errors()]},
            }),
        )

    @app.get("/healthz")
    def liveness():
        return {"status": "ok", "version": settings.app_version}

    @app.get("/", response_class=HTMLResponse)
    def workbench():
        page = Path(__file__).parent / "web" / "index.html"
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            with database.session_factory() as db:
                identity = session_identity(db, token)
                if identity:
                    account, _ = identity
                    return RedirectResponse(safe_login_destination(request.query_params.get("next"),
                        can_manage=account.role == "system_admin"), status_code=303)
            parameters = {"reason": "expired"}
            if request.query_params.get("next") == "/accounts":
                parameters["next"] = "/accounts"
            response = RedirectResponse("/login?" + urlencode(parameters), status_code=303)
            response.delete_cookie(COOKIE, path="/", httponly=True, secure=request.url.scheme == "https", samesite="lax")
            return response
        return HTMLResponse((Path(__file__).parent / "web" / "login.html").read_text(encoding="utf-8"))

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(access: AccessContext = Depends(get_access_context)):
        access.require("settings:write")
        return HTMLResponse((Path(__file__).parent / "web" / "accounts.html").read_text(encoding="utf-8"))

    @app.get("/auth-assets.css")
    def auth_css():
        return Response((Path(__file__).parent / "web" / "auth.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/auth-pages.js")
    def auth_js():
        return Response((Path(__file__).parent / "web" / "auth.js").read_text(encoding="utf-8"), media_type="application/javascript")

    app.include_router(auth_router)
    app.include_router(router)
    return app
