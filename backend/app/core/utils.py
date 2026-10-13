"""跨模块通用工具函数。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（带时区），统一用于所有时间字段。"""
    return datetime.now(timezone.utc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def stable_int_id(text: str) -> int:
    """把字符串型消息 id 稳定映射为正整数。

    Gmail / Graph 的消息 id 是字符串，而 ``Message.uid`` 是整数列；
    用 SHA256 前 15 位十六进制转 int（< 2^60），保证同一 id 恒得同一 uid。
    """
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return int(digest[:15], 16)


def strip_html(html: str) -> str:
    """极简 HTML 去标签，用于只有 HTML 正文时兜底生成纯文本。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_domain(email: str) -> str:
    """从邮箱地址提取小写域名；非法输入返回空串。

    P2 用于白名单匹配，P3 复用为「发件公司」主键候选。
    """
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower().strip(">")


def body_fingerprint(from_email: str, subject: str, date_utc: datetime | None, body_head: str) -> str:
    """去重指纹：SHA256(发件人 + 主题 + 日期 + 正文前 2KB)。

    在缺失 Message-ID 时作为兜底去重键。
    """
    date_str = date_utc.isoformat() if date_utc else ""
    payload = "\x1f".join([from_email or "", subject or "", date_str, (body_head or "")[:2048]])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()