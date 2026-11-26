"""测试夹具：每个用例重建表并灌入种子数据。

直接复用本地 ccdev 库（隔离开发环境）；运行 pytest 会重置其中数据。
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev"
)

from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        # 重建过表结构：释放连接池，避免 psycopg 跨测试复用旧的预编译计划
        # （cached plan must not change result type）。
        engine.dispose()


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()
    with TestClient(app) as c:
        yield c
    engine.dispose()


FRIDAY = "2026-11-27"


def dt(hm: str) -> str:
    return f"{FRIDAY}T{hm}:00"
