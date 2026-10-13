"""全局配置。使用 pydantic-settings 从 .env / 环境变量加载并校验。

网页端与桌面端共用同一份配置。区别只在路径解析：
- 源码运行：可写根 = 仓库根；只读资源根 = 仓库根。
- PyInstaller 打包（sys.frozen）：可写根 = 用户数据目录 `%APPDATA%\\MailSummary`
  （.env / data / logs 落在这里，与 exe 解耦，重新打包或移动 exe 都不会丢数据）；
  只读资源根 = PyInstaller 解压目录（sys._MEIPASS），静态前端从这里加载。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _base_dir() -> Path:
    """可写根目录。

    - 源码运行：仓库根目录（config.py 的上两级）。
    - 打包运行：用户数据目录 `%APPDATA%\\MailSummary`（取不到 APPDATA 时回退 LOCALAPPDATA）。
      刻意**不使用 exe 所在目录**：重新打包、清空 build/dist、把 exe 挪到别处，
      都不会动到已导入的账号与邮件。
    """
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return root / "MailSummary"
    return Path(__file__).resolve().parents[2]


def _bundle_dir() -> Path:
    """只读资源根目录：打包后为 _MEIPASS 解压目录，否则与可写根一致。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _base_dir()


# 可写根目录（源码运行 = 仓库根；打包后 = %APPDATA%\MailSummary，与 exe 解耦）
ROOT_DIR = _base_dir()
# 只读资源根目录（打包后为 _MEIPASS，内含 backend/static 等）
BUNDLE_DIR = _bundle_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 服务 ----------
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    # ---------- 存储 ----------
    data_dir: Path = Path("data")
    db_path: Path = Path("data/mail.db")
    eml_dir: Path = Path("data/eml")
    secret_key_file: Path = Path("data/.secret_key")

    # ---------- 同步 ----------
    # 定时同步间隔（秒），默认 30 秒；过短可能触发服务商 IMAP 风控
    sync_interval_seconds: int = 30
    initial_sync_days: int = 30
    fetch_batch_size: int = 200
    max_eml_bytes: int = 20 * 1024 * 1024

    # ---------- 通知 ----------
    notify_enabled: bool = False

    # ---------- OAuth2（P5：Gmail API / Microsoft Graph） ----------
    # 回调地址前缀：必须与 OAuth 应用注册中的 Redirect URI 完全一致
    app_public_base_url: str = "http://127.0.0.1:8000"

    # ---------- 摘要 / LLM（P4，可选；未配置时仅用规则摘要） ----------
    llm_enabled: bool = False
    llm_provider: str = "openai"  # openai | ollama
    llm_base_url: str = ""  # 留空按 provider 取默认（openai: api.openai.com/v1；ollama: localhost:11434/v1）
    llm_api_key: str = ""
    llm_model: str = ""  # 留空按 provider 取默认
    llm_max_chars: int = 4000
    llm_timeout: float = 30.0

    @field_validator("data_dir", "db_path", "eml_dir", "secret_key_file", mode="after")
    @classmethod
    def _to_absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (ROOT_DIR / v)

    def ensure_dirs(self) -> None:
        """确保运行时目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.eml_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_key_file.parent.mkdir(parents=True, exist_ok=True)

    @property
    def static_dir(self) -> Path:
        """静态前端目录。打包后位于只读的 _MEIPASS 中，回退到可写根以兼容源码运行。"""
        candidate = BUNDLE_DIR / "backend" / "static"
        if candidate.exists():
            return candidate
        return ROOT_DIR / "backend" / "static"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()