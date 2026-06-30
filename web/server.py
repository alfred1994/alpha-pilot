import os
import sys
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# 确保根目录在 search path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.public_safety import is_control_api_enabled, is_production

is_prod = is_production()

app = FastAPI(
    title="Quant Pilot Web API",
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
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给公网仪表盘补充基础安全响应头。"""

    async def dispatch(self, request, call_next):
        if is_prod and not is_control_api_enabled() and request.url.path.startswith("/api/control"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if is_prod:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )
            if request.url.path.startswith("/api/") or request.url.path == "/" or request.url.path.endswith((".html", ".js", ".css")):
                response.headers.setdefault("Cache-Control", "no-store")
        return response


app.add_middleware(SecurityHeadersMiddleware)

# 动态加载子路由
from web.routers import status, database
app.include_router(status.router, prefix="/api", tags=["Status"])
app.include_router(database.router, prefix="/api", tags=["Database"])

if not is_prod or is_control_api_enabled():
    from web.routers import control
    app.include_router(control.router, prefix="/api", tags=["Control"])

# 挂载静态文件目录
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
