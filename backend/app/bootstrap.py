"""容器启动引导：建表，仅在空库时灌入种子数据（可重复执行）。

需要强制重置演示数据时运行：python -m app.seed

ensure_new_columns() 负责给旧版本已存在的 sites/reservations 表幂等补齐
“站点级分时功率预算”新增列；全新库由 Base.metadata.create_all 直接建出。
"""

from sqlalchemy import text

from .database import SessionLocal, engine
from .models import Base, Site
from .seed import seed

# 旧库升级时需要的 PG 枚举类型（标签与 app/enums.py 成员名/值一致）
_ENUM_SQL = [
    """
    DO $$ BEGIN
        CREATE TYPE allocationpolicy AS ENUM ('RESERVE_EXPECTED', 'GUARANTEED_MIN');
    EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """,
]

# 旧表补列（IF NOT EXISTS，可重复执行）
_COLUMN_SQL = [
    "ALTER TABLE sites ADD COLUMN IF NOT EXISTS transformer_limit_kw NUMERIC(8,2) DEFAULT 180",
    "ALTER TABLE sites ADD COLUMN IF NOT EXISTS allocation_policy allocationpolicy "
    "DEFAULT 'GUARANTEED_MIN'",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS expected_power_kw NUMERIC(6,2) DEFAULT 90",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS allocation_policy allocationpolicy "
    "DEFAULT 'GUARANTEED_MIN'",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS allocated_power_kw NUMERIC(6,2)",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS current_available_power_kw NUMERIC(6,2)",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS battery_derated BOOLEAN DEFAULT FALSE",
    "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS last_reading_at TIMESTAMP",
]


def ensure_new_columns() -> None:
    with engine.begin() as conn:
        for stmt in _ENUM_SQL:
            conn.execute(text(stmt))
        for stmt in _COLUMN_SQL:
            conn.execute(text(stmt))


def bootstrap() -> None:
    Base.metadata.create_all(engine)
    ensure_new_columns()
    db = SessionLocal()
    try:
        if db.query(Site).count() == 0:
            db.close()
            seed()
        else:
            print("数据库已有数据，跳过种子灌入。")
    finally:
        db.close()


if __name__ == "__main__":
    bootstrap()
