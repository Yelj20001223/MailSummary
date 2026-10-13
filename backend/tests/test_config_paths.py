"""配置路径解析：源码运行 vs PyInstaller 打包运行。

重点保证：打包后数据落在用户目录 `%APPDATA%\\MailSummary`，**与 exe 位置解耦**，
从而重新打包 / 清空 build / 移动 exe 都不会清除已导入的账号与邮件。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app import config


def test_source_run_uses_repo_root(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert config._base_dir() == Path(config.__file__).resolve().parents[2]


def test_frozen_uses_appdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config._base_dir() == tmp_path / "MailSummary"


def test_frozen_falls_back_to_localappdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config._base_dir() == tmp_path / "MailSummary"


def test_bundle_dir_uses_meipass(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config._bundle_dir() == tmp_path


def test_bundle_dir_falls_back_to_base(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert config._bundle_dir() == config._base_dir()