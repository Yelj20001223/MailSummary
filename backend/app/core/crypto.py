"""凭证加解密。

设计原则：数据库中绝不存明文密码/授权码。
- 主密钥：优先使用 keyring 系统钥匙串；若不可用，则回退到本地密钥文件
  （data/.secret_key，权限 600，已在 .gitignore 中排除）。
- 加密算法：Fernet（AES-128-CBC + HMAC-SHA256）。
"""
from __future__ import annotations

import stat
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.core.logging import logger

_KEYRING_SERVICE = "mail-summary"
_KEYRING_USER = "fernet-master-key"


def _load_or_create_key() -> bytes:
    """获取 Fernet 主密钥：keyring 优先，失败回退到本地文件。"""
    # 1) 尝试 keyring
    try:
        import keyring  # type: ignore

        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if existing:
            return existing.encode("utf-8")
        new_key = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, new_key.decode("utf-8"))
        logger.info("已生成新的主密钥并保存到系统 keyring")
        return new_key
    except Exception as exc:  # noqa: BLE001 - keyring 不可用时静默降级
        logger.debug(f"keyring 不可用，回退到本地密钥文件：{exc}")

    # 2) 回退：本地密钥文件
    key_file = settings.secret_key_file
    if key_file.exists():
        return key_file.read_bytes().strip()

    new_key = Fernet.generate_key()
    key_file.write_bytes(new_key)
    try:
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass
    logger.warning(f"已生成本地主密钥：{key_file}（请勿泄露或提交到仓库）")
    return new_key


@lru_cache
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """加密敏感字符串（授权码 / 密码 / 令牌）。空串原样返回。"""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """解密。失败时返回空串并记录告警（不抛异常，避免同步整体中断）。"""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("凭证解密失败：主密钥可能已变更，请重新录入该账号的授权码")
        return ""


def mask_secret(value: str) -> str:
    """用于日志/接口返回的脱敏显示。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"