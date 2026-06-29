import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 确保根目录在 search path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = FastAPI(title="Quant Pilot Web API", description="AI 交易员自愈协同仪表盘后端 API")

# 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 动态加载子路由
from web.routers import status, database, control
app.include_router(status.router, prefix="/api", tags=["Status"])
app.include_router(database.router, prefix="/api", tags=["Database"])
app.include_router(control.router, prefix="/api", tags=["Control"])

# 挂载静态文件目录
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
