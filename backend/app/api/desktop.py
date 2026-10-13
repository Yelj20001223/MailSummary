"""桌面端设置路由（P：桌面体验）。

网页端与桌面端共用同一套后端，但这两个开关只对桌面端有意义：
- 开机自启：写入/删除 Windows 注册表 Run 项（见 ``app.core.autostart``）。
- 关闭窗口行为：``exit`` 直接退出，``tray`` 隐藏到系统托盘继续后台收信。

前端通过 ``GET /api/desktop/config`` 的 ``desktop`` 字段判断是否展示该分区，
因此网页端访问时这两项不会出现，行为不受影响。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.core import autostart
from app.core.desktop_prefs import load_prefs, save_prefs
from app.core.logging import logger
from app.schemas.desktop import DesktopConfigRead, DesktopConfigUpdate

router = APIRouter(prefix="/api/desktop", tags=["desktop"])


def _read_config() -> DesktopConfigRead:
    prefs = load_prefs()
    return DesktopConfigRead(
        desktop=settings.desktop_mode,
        auto_start=autostart.is_enabled(),
        auto_start_supported=autostart.is_supported(),
        close_action=prefs.close_action,
    )


@router.get("/config", response_model=DesktopConfigRead)
def get_desktop_config() -> DesktopConfigRead:
    return _read_config()


@router.put("/config", response_model=DesktopConfigRead)
def update_desktop_config(payload: DesktopConfigUpdate) -> DesktopConfigRead:
    if payload.close_action is not None:
        prefs = load_prefs()
        prefs.close_action = payload.close_action
        save_prefs(prefs)

    if payload.auto_start is not None:
        ok, message = autostart.set_enabled(payload.auto_start)
        if not ok:
            logger.warning(f"[DESKTOP] 设置开机自启失败：{message}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    return _read_config()
