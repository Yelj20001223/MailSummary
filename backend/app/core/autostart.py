"""开机自启动管理（Windows）。

实现方式：在当前用户注册表
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 下写入/删除一个
名为 ``MailSummary`` 的字符串值。只操作当前用户、只操作本工具自己的项，
不需要管理员权限，也不改动系统级配置。

写入的命令按运行方式区分：
- 打包运行（``sys.frozen``）：``"<exe 绝对路径>"``。
- 源码运行：``"<pythonw.exe>" "<desktop.py 绝对路径>"``，优先用 pythonw
  以免每次开机弹出一个控制台窗口。

非 Windows 平台不支持，统一返回 ``(False, 原因)``，不影响其余功能。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.config import ROOT_DIR
from app.core.logging import logger

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "MailSummary"


def is_supported() -> bool:
    """当前平台是否支持开机自启（仅 Windows）。"""
    return sys.platform == "win32"


def build_command() -> str:
    """构造开机要执行的命令行（带引号，兼容含空格的路径）。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable).resolve()
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    desktop = (ROOT_DIR / "desktop.py").resolve()
    return f'"{interpreter}" "{desktop}"'


def is_enabled() -> bool:
    """注册表中是否存在本工具的自启项。"""
    if not is_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(f"[AUTOSTART] 读取注册表失败：{exc}")
        return False


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """开启/关闭开机自启，返回 ``(是否成功, 提示信息)``。"""
    if not is_supported():
        return False, "当前平台不支持开机自启"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, build_command())
                return True, "已开启开机自启"
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass  # 本来就没有，视作已是关闭状态
            return True, "已关闭开机自启"
    except OSError as exc:
        logger.warning(f"[AUTOSTART] 写入注册表失败：{exc}")
        return False, f"写入注册表失败：{exc}"
