import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev",
    )
    # 演示系统统一使用朴素本地时间（前端 datetime-local 与之对齐）
    app_name: str = "充电预约服务"
    cors_origins: list[str] = ["*"]


settings = Settings()
