"""桌面端偏好 / 开机自启模块的单元测试。

刻意不触碰真实注册表：`set_enabled` 只在「不支持平台」分支被断言，
写入/删除注册表的行为留给打包后的桌面端手动验证。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import ROOT_DIR, settings
from app.core import autostart, desktop_prefs
from app.core.desktop_prefs import DesktopPrefs


def _use_tmp_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)


def test_load_defaults_when_file_missing(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data_dir(monkeypatch, tmp_path)
    prefs = desktop_prefs.load_prefs()
    assert prefs.close_action == desktop_prefs.CLOSE_ACTION_EXIT


def test_save_then_load_roundtrip(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data_dir(monkeypatch, tmp_path)
    desktop_prefs.save_prefs(DesktopPrefs(close_action=desktop_prefs.CLOSE_ACTION_TRAY))

    saved = json.loads((tmp_path / "desktop_prefs.json").read_text(encoding="utf-8"))
    assert saved == {"close_action": "tray"}
    assert desktop_prefs.load_prefs().close_action == desktop_prefs.CLOSE_ACTION_TRAY


def test_invalid_close_action_falls_back_to_exit(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "desktop_prefs.json").write_text(
        json.dumps({"close_action": "explode"}), encoding="utf-8"
    )
    assert desktop_prefs.load_prefs().close_action == desktop_prefs.CLOSE_ACTION_EXIT

    # 保存时也会把非法值纠正回默认
    assert desktop_prefs.save_prefs(DesktopPrefs(close_action="nope")).close_action == "exit"


def test_corrupt_file_does_not_raise(monkeypatch, tmp_path: Path) -> None:
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "desktop_prefs.json").write_text("{ not json", encoding="utf-8")
    assert desktop_prefs.load_prefs().close_action == desktop_prefs.CLOSE_ACTION_EXIT


def test_autostart_unsupported_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    ok, message = autostart.set_enabled(True)
    assert ok is False
    assert "不支持" in message


def test_build_command_source_mode_points_at_desktop_py() -> None:
    command = autostart.build_command()
    assert command.startswith('"')
    if not getattr(sys, "frozen", False):
        assert "desktop.py" in command
        assert str(ROOT_DIR) in command or "desktop.py" in command


# ---------------- desktop.py 的关闭行为（用假窗口验证，不启动 GUI） ----------------
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import desktop  # noqa: E402


class _FakeWindow:
    def __init__(self) -> None:
        self.hide_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1


def _set_close_action(monkeypatch, tmp_path: Path, action: str) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    desktop_prefs.save_prefs(DesktopPrefs(close_action=action))


def test_on_closing_hides_window_when_tray(monkeypatch, tmp_path: Path) -> None:
    _set_close_action(monkeypatch, tmp_path, desktop_prefs.CLOSE_ACTION_TRAY)
    monkeypatch.setattr(desktop, "_exiting", False)
    window = _FakeWindow()
    # 返回 False = 拦截关闭，并把窗口藏进托盘
    assert desktop._on_closing(window) is False
    assert window.hide_calls == 1


def test_on_closing_exits_when_close_action_is_exit(monkeypatch, tmp_path: Path) -> None:
    _set_close_action(monkeypatch, tmp_path, desktop_prefs.CLOSE_ACTION_EXIT)
    monkeypatch.setattr(desktop, "_exiting", False)
    window = _FakeWindow()
    # 返回 True = 放行关闭，不隐藏
    assert desktop._on_closing(window) is True
    assert window.hide_calls == 0


def test_on_closing_ignores_tray_pref_after_tray_quit(monkeypatch, tmp_path: Path) -> None:
    _set_close_action(monkeypatch, tmp_path, desktop_prefs.CLOSE_ACTION_TRAY)
    monkeypatch.setattr(desktop, "_exiting", True)  # 托盘菜单点了「退出」
    window = _FakeWindow()
    assert desktop._on_closing(window) is True
    assert window.hide_calls == 0
