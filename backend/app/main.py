"""FastAPI 应用入口。

网页端与（未来的）桌面端共用本模块，唯一区别是启动方式。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app import __version__
from app.api import accounts, companies, messages, oauth, rules, summaries, sync
from app.config import settings
from app.core.logging import logger, setup_logging
from app.db.session import engine, init_db
from app.models.account import Account
from app.services.classify_service import ClassifyService
from app.services.company_service import CompanyService
from app.services.sync_service import SyncService

scheduler = BackgroundScheduler(timezone="UTC")


def _scheduled_sync() -> None:
    """定时同步所有启用账号（收件箱 + 垃圾邮件等默认文件夹）。"""
    with Session(engine) as session:
        service = SyncService(session)
        accounts = session.exec(select(Account).where(Account.is_active == True)).all()  # noqa: E712
        for account in accounts:
            try:
                service.sync_account_folders(account)
            except Exception as exc:  # noqa: BLE001 - 定时任务不允许整体崩溃
                logger.error(f"[SCHEDULER] 同步 {account.email} 失败：{exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()
    setup_logging()
    init_db()
    with Session(engine) as session:
        ClassifyService(session).seed_defaults()
        # 启动时回填历史邮件的公司归并（仅当存在未归并邮件时才执行）
        processed = CompanyService(session).reaggregate_if_needed()
        if processed:
            logger.info(f"启动回填公司归并：{processed} 封邮件")
    logger.info(f"邮件聚合摘要工具 v{__version__} 启动，数据目录：{settings.data_dir}")

    if settings.sync_interval_seconds > 0:
        # 启动后立即同步一次，随后按间隔轮询（秒级），收件箱与垃圾邮件都会覆盖
        scheduler.add_job(
            _scheduled_sync,
            "interval",
            seconds=settings.sync_interval_seconds,
            id="periodic_sync",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now(),
        )
        scheduler.start()
        logger.info(f"定时同步已启用：启动后立即同步，之后每 {settings.sync_interval_seconds} 秒")
    else:
        logger.info("定时同步未启用（SYNC_INTERVAL_SECONDS=0）")

    yield

    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="多邮箱聚合摘要工具",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地单机使用；如需外网访问请收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts.router)
app.include_router(oauth.router)
app.include_router(messages.router)
app.include_router(sync.router)
app.include_router(rules.router)
app.include_router(rules.classify_router)
app.include_router(companies.router)
app.include_router(summaries.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


# 静态前端（放在最后，避免拦截 /api 路由）
if settings.static_dir.exists():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html")

    app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True), name="static")