import os
import sqlite3
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware


from web.public_safety import is_control_api_enabled, is_production

is_prod = is_production()
workspace_enabled = os.environ.get("ALPHAPILOT_WORKSPACE_ENABLED", "0" if is_prod else "1").lower() in {"1", "true", "yes"}

app = FastAPI(
    title="AlphaPilot Web API",
    description="AI 交易员自愈协同仪表盘后端 API",
    docs_url=None if is_prod else "/docs",
    redoc_url=None if is_prod else "/redoc",
    openapi_url=None if is_prod else "/openapi.json"
)

# 跨域配置
cors_origins = ["*"]
allow_credentials = True

env_origins = os.environ.get("ALPHAPILOT_CORS_ORIGINS")
if env_origins:
    cors_origins = [o.strip() for o in env_origins.split(",") if o.strip()]
elif is_prod:
    # 生产环境不允许通配符加 Credentials，防止反射任意 Origin
    cors_origins = ["https://alphapilot.pp.ua"]
else:
    # 开发环境为了避免 CORS 问题列出常见本地端口，允许 localhost
    cors_origins = [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173"
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=allow_credentials and "*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给公网仪表盘补充基础安全响应头。"""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/workspace") and not workspace_enabled:
            return JSONResponse(status_code=503, content={"detail": "用户工作台尚未开放。"}, headers={"Cache-Control": "no-store"})
        if is_prod and not is_control_api_enabled() and request.url.path.startswith("/api/control"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        response = await call_next(request)
        if request.url.path.startswith("/api/workspace"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/workspace"):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if is_prod:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )
            if request.url.scheme == "https":
                response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
            if request.url.path.startswith(("/api/", "/workspace")) or request.url.path == "/" or request.url.path.endswith(".html"):
                response.headers.setdefault("Cache-Control", "no-store")
            else:
                response.headers.setdefault("Cache-Control", "public, max-age=0, must-revalidate")
        return response


app.add_middleware(SecurityHeadersMiddleware)

# 动态加载子路由
from web.routers import status, database, research, returns
app.include_router(status.router, prefix="/api", tags=["Status"])
app.include_router(database.router, prefix="/api", tags=["Database"])
app.include_router(research.router, prefix="/api", tags=["Research"])
app.include_router(returns.router, prefix="/api", tags=["Returns"])

if workspace_enabled:
    from services.identity import IdentityError
    from services.market_types import MarketError
    from web.routers import identity, workspace

    @app.exception_handler(MarketError)
    async def workspace_error(request, error):
        code = 404 if error.code == "not_found" else 409 if error.code in {"account_exists", "idempotency_conflict", "strategy_changed"} else 422
        if error.code.endswith("unavailable") or error.code in {"missing_credentials", "provider_unavailable", "paper_required"}:
            code = 503
        return JSONResponse(status_code=code, content={"detail": error.message, "code": error.code}, headers={"Cache-Control": "no-store"})

    @app.exception_handler(IdentityError)
    async def identity_error(request, error):
        headers = {"Cache-Control": "no-store"}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse(status_code=error.status_code, content={"detail": error.message}, headers=headers)

    app.include_router(identity.router, prefix="/api/workspace", tags=["Identity"])
    app.include_router(workspace.router, prefix="/api/workspace", tags=["Workspace"])

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "数据服务暂不可用，请稍后重试。"}, headers={"Cache-Control": "no-store"})

if not is_prod or is_control_api_enabled():
    from web.routers import control
    app.include_router(control.router, prefix="/api", tags=["Control"])

# 挂载静态文件目录
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
