from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import operations, power, reservations, resources, search

app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    description=(
        "充电预约演示系统：固定枪口 / 资源池两种粒度、事务级容量重算、幂等确认、"
        "维护影响分析，以及站点级分时功率预算（期望整段预留 / 最低保证+5分钟动态分配）、"
        "实时功率回报、限额修订与电网紧急限额运营处置。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resources.router)
app.include_router(search.router)
app.include_router(reservations.router)
app.include_router(operations.router)
app.include_router(power.router)
app.include_router(power.ops_router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
