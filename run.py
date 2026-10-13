"""本地启动入口：python run.py

网页端与（未来的）桌面端共用同一套核心代码，仅启动方式不同。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让根目录下的 run.py 也能直接找到 backend/app 包
BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.log_level.lower(),
        app_dir=str(BACKEND_DIR),
    )


if __name__ == "__main__":
    main()