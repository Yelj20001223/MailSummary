"""桌面端偏好设置。

仅桌面端（desktop.py）使用；网页端不读写本文件，行为不受影响。

存放位置为可写数据目录下的 ``desktop_prefs.json``（打包后位于
``%APPDATA%\\MailSummary``），与程序打包产物完全解耦——重新打包、
移动或删除 exe 都不会丢失设置。

字段：
- ``close_action``：点击窗口关闭按钮时的行为。
  ``exit`` = 直接退出程序；``tray`` = 隐藏到系统托盘（后台继续同步邮件）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import settings
from app.core.logging import logger

CLOSE_ACTION_EXIT = "exit"
CLOSE_ACTION_TRAY = "tray"
VALID_CLOSE_ACTIONS: tuple[str, ...] = (CLOSE_ACTION_EXIT, CLOSE_ACTION_TRAY)


@dataclass
class DesktopPrefs:
    close_action: str = CLOSE_ACTION_EXIT


def _prefs_path() -> Path:
    return settings.data_dir / "desktop_prefs.json"


def load_prefs() -> DesktopPrefs:
    """读取桌面偏好；文件缺失或损坏时回退默认值（不抛异常）。"""
    try:
        raw = json.loads(_prefs_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DesktopPrefs()
    except (OSError, ValueError) as exc:
        logger.warning(f"[DESKTOP] 读取偏好失败，使用默认值：{exc}")
        return DesktopPrefs()

    if not isinstance(raw, dict):
        return DesktopPrefs()
    close_action = raw.get("close_action", CLOSE_ACTION_EXIT)
    if close_action not in VALID_CLOSE_ACTIONS:
        close_action = CLOSE_ACTION_EXIT
    return DesktopPrefs(close_action=close_action)


def save_prefs(prefs: DesktopPrefs) -> DesktopPrefs:
    """原子写入偏好文件（先写临时文件再替换，避免中途崩溃留下半截 JSON）。"""
    if prefs.close_action not in VALID_CLOSE_ACTIONS:
        prefs.close_action = CLOSE_ACTION_EXIT
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(asdict(prefs), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning(f"[DESKTOP] 写入偏好失败：{exc}")
    return prefs
