"""桌面端设置相关 schema（开机自启 / 关闭行为）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DesktopConfigRead(BaseModel):
    desktop: bool = False  # 当前是否运行在桌面端
    auto_start: bool = False  # 是否已开启开机自启（以注册表实际状态为准）
    auto_start_supported: bool = False  # 当前平台是否支持开机自启
    close_action: Literal["exit", "tray"] = "exit"


class DesktopConfigUpdate(BaseModel):
    auto_start: bool | None = None
    close_action: Literal["exit", "tray"] | None = None
