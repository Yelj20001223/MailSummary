"""IMAP 提供方实现（QQ / 163 / 企业微信 / Outlook / Gmail 的 IMAP 降级通道）。

设计要点：
- 只读：SELECT 使用 readonly=True，全程不做任何写操作。
- 增量：UIDVALIDITY + 最高已同步 UID，禁用序号。
- 编码：中文邮件常见 GBK/GB18030，统一做多编码兜底解码。
- 风控：网易系（163/QQ）登录后必须发送 IMAP ID 命令，否则报 Unsafe Login。
"""
from __future__ import annotations

import imaplib
import re
import time
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterator

from app.core.logging import logger
from app.models.account import Account
from app.services.providers.base import (
    AuthError,
    FolderInfo,
    MailProvider,
    ProviderError,
    RateLimitError,
    RawAttachment,
    RawMessage,
)

# 注册 IMAP ID 命令（网易系必需）
imaplib.Commands["ID"] = ("AUTH", "SELECTED")

_CHARSET_FALLBACKS = ("utf-8", "gb18030", "gbk", "big5", "latin-1")
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')
_FETCH_META_RE = re.compile(rb"UID\s+(?P<uid>\d+).*?FLAGS\s+\((?P<flags>[^)]*)\)", re.S)
_BATCH_UID = 50  # 单次 FETCH 的 UID 数量（网络层分批，避免超长命令行）
# 定位垃圾邮件夹的关键字（各服务商夹名不一，按关键字匹配）
_JUNK_HINTS = ("junk", "spam", "垃圾")


def _decode_bytes(payload: bytes, charset: str | None) -> str:
    """多编码兜底解码。"""
    candidates = [charset] if charset else []
    candidates += [c for c in _CHARSET_FALLBACKS if c != charset]
    for enc in candidates:
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _header_str(value: str | None) -> str:
    """解码 MIME 编码的头部（=?utf-8?B?...?=）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _strip_html(html: str) -> str:
    """极简 HTML 去标签，用于无纯文本正文时的兜底。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class ImapProvider(MailProvider):
    """基于 imaplib 的只读 IMAP 提供方。"""

    def __init__(self, account: Account, secret: str) -> None:
        super().__init__(account, secret)
        self._conn: imaplib.IMAP4_SSL | None = None
        self._junk_folder: str | None = None
        self._junk_scanned = False

    # --------------------------- 连接管理 ---------------------------
    def connect(self) -> None:
        if self._conn is not None:
            return
        host = self.account.imap_host
        port = self.account.imap_port or 993
        if not host:
            raise ProviderError("缺少 IMAP 服务器地址")

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                conn = imaplib.IMAP4_SSL(host, port, timeout=30)
                conn.login(self.account.username or self.account.email, self.secret)
                self._send_id(conn)
                self._conn = conn
                logger.info(f"[IMAP] 已连接 {host}:{port} ({self.account.email})")
                return
            except imaplib.IMAP4.error as exc:
                msg = str(exc)
                if "Unsafe Login" in msg or "Login Fail" in msg or "authentication" in msg.lower():
                    raise AuthError(f"IMAP 认证失败：{msg}") from exc
                last_exc = exc
            except (OSError, imaplib.IMAP4.abort) as exc:
                last_exc = exc
            # 指数退避重试
            delay = 1.5 ** attempt
            logger.warning(f"[IMAP] 第 {attempt} 次连接失败，{delay:.1f}s 后重试：{last_exc}")
            time.sleep(delay)

        raise ProviderError(f"IMAP 连接失败：{last_exc}")

    @staticmethod
    def _send_id(conn: imaplib.IMAP4_SSL) -> None:
        """发送 RFC 2971 ID 命令（QQ/163/企业微信必需）。"""
        try:
            args = '("name" "mail-summary" "version" "0.1.0" "vendor" "python-imaplib")'
            typ, data = conn._simple_command("ID", args)  # noqa: SLF001
            conn._untagged_response(typ, data, "ID")  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - 非网易服务器可能不支持 ID
            logger.debug(f"[IMAP] ID 命令未生效（可忽略）：{exc}")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._conn = None

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # --------------------------- 能力实现 ---------------------------
    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            typ, _ = self.conn.select("INBOX", readonly=True)
            if typ != "OK":
                return False, f"无法打开 INBOX：{typ}"
            return True, "连接成功，INBOX 可读"
        except AuthError as exc:
            return False, str(exc)
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"连接异常：{exc}"
        finally:
            self.close()

    def list_folders(self) -> list[FolderInfo]:
        folders: list[FolderInfo] = []
        typ, data = self.conn.list()
        if typ != "OK":
            raise ProviderError(f"LIST 失败：{typ}")
        for raw in data or []:
            if not isinstance(raw, bytes):
                continue
            m = _LIST_RE.search(raw)
            if not m:
                continue
            name = m.group("name").strip().strip(b'"').decode("utf-8", errors="replace")
            flags = m.group("flags").decode("utf-8", errors="replace")
            delimiter = m.group("delim").decode("utf-8", errors="replace") or "/"
            # 跳过 \Noselect 的容器节点（无邮件）
            if "\\Noselect" in flags:
                continue
            folders.append(FolderInfo(name=name, delimiter=delimiter))
        # 去重（部分服务器会返回重复项）
        seen: set[str] = set()
        unique: list[FolderInfo] = []
        for f in folders:
            if f.name in seen:
                continue
            seen.add(f.name)
            unique.append(f)
        return unique

    def _find_junk_folder(self) -> str | None:
        """按关键字在文件夹列表中定位垃圾邮件夹；找不到返回 None（只扫描一次并缓存）。"""
        if self._junk_scanned:
            return self._junk_folder
        self._junk_scanned = True
        try:
            for f in self.list_folders():
                if any(h in f.name.lower() for h in _JUNK_HINTS):
                    self._junk_folder = f.name
                    logger.info(f"[IMAP] {self.account.email} 垃圾邮件夹：{f.name}")
                    break
        except Exception as exc:  # noqa: BLE001 - 定位失败不影响收件箱同步
            logger.warning(f"[IMAP] {self.account.email} 定位垃圾邮件夹失败（已跳过）：{exc}")
        if self._junk_folder is None:
            logger.warning(f"[IMAP] {self.account.email} 未找到垃圾邮件夹，本次仅同步收件箱")
        return self._junk_folder

    def resolve_folder(self, folder: str) -> str:  # noqa: D102
        # 内部通用名 Junk → 该服务商实际的垃圾邮件夹名
        if folder == "Junk":
            return self._find_junk_folder() or "INBOX"
        return folder

    def default_folders(self) -> list[str]:  # noqa: D102
        folders = ["INBOX"]
        if self._find_junk_folder():
            folders.append("Junk")
        return folders

    def _select(self, folder: str) -> tuple[int, int]:
        """以只读方式打开文件夹，返回 (uidvalidity, exists)。"""
        typ, data = self.conn.select(self.resolve_folder(folder), readonly=True)
        if typ != "OK":
            raise ProviderError(f"无法打开文件夹 {folder}：{data}")
        exists = int(data[0]) if data and data[0] else 0
        uidvalidity = 0
        resp = self.conn.response("UIDVALIDITY")
        if resp and resp[1] and resp[1][0]:
            uidvalidity = int(resp[1][0])
        return uidvalidity, exists

    def folder_uidvalidity(self, folder: str) -> tuple[int, int]:
        return self._select(folder)

    def _search_uids(
        self,
        *,
        start_uid: int | None,
        since: datetime | None,
    ) -> list[int]:
        if start_uid is not None:
            typ, data = self.conn.uid("SEARCH", None, f"{start_uid}:*")
        elif since is not None:
            typ, data = self.conn.uid("SEARCH", None, f'(SINCE {since.strftime("%d-%b-%Y")})')
        else:
            typ, data = self.conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise ProviderError(f"SEARCH 失败：{typ}")
        uids: list[int] = []
        for chunk in data or []:
            if not chunk:
                continue
            uids.extend(int(x) for x in chunk.split())
        # UID SEARCH a:* 在无匹配时会返回最大 UID，需要显式过滤
        if start_uid is not None:
            uids = [u for u in uids if u >= start_uid]
        uids.sort()
        return uids

    def fetch_messages(
        self,
        folder: str,
        *,
        start_uid: int | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[RawMessage]:
        uidvalidity, _exists = self._select(folder)
        uids = self._search_uids(start_uid=start_uid, since=since)
        if limit is not None:
            uids = uids[:limit]
        logger.info(
            f"[IMAP] {self.account.email} {folder} uidvalidity={uidvalidity} "
            f"待拉取={len(uids)} (start_uid={start_uid})"
        )

        for i in range(0, len(uids), _BATCH_UID):
            batch = uids[i : i + _BATCH_UID]
            yield from self._fetch_batch(batch)

    def _fetch_batch(self, uids: list[int]) -> Iterator[RawMessage]:
        uidset = ",".join(str(u) for u in uids)
        try:
            typ, data = self.conn.uid("FETCH", uidset, "(UID RFC822 FLAGS)")
        except imaplib.IMAP4.error as exc:
            msg = str(exc)
            if "rate" in msg.lower() or "too many" in msg.lower():
                raise RateLimitError(msg) from exc
            raise ProviderError(f"FETCH 失败：{msg}") from exc
        if typ != "OK":
            raise ProviderError(f"FETCH 失败：{typ}")

        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta, raw = item[0], item[1]
            if not isinstance(raw, bytes):
                continue
            try:
                yield self._parse_message(meta, raw)
            except Exception as exc:  # noqa: BLE001 - 单封解析失败不应中断整批
                logger.warning(f"[IMAP] 解析邮件失败（已跳过）：{exc}")

    # --------------------------- 邮件解析 ---------------------------
    @staticmethod
    def _parse_message(meta: bytes, raw: bytes) -> RawMessage:
        m = _FETCH_META_RE.search(meta or b"")
        uid = int(m.group("uid")) if m else 0
        flags_raw = m.group("flags").decode("utf-8", errors="replace") if m else ""
        flags = flags_raw.split()

        msg = BytesParser(policy=policy.default).parsebytes(raw)

        # 发件人
        from_name, from_email = "", ""
        addr_list = getaddresses([_header_str(msg.get("From", ""))])
        if addr_list:
            from_name, from_email = addr_list[0][0], addr_list[0][1]

        to_list = [
            {"name": _header_str(n), "email": e}
            for n, e in getaddresses([_header_str(v) for v in msg.get_all("To", [])])
        ]
        cc_list = [
            {"name": _header_str(n), "email": e}
            for n, e in getaddresses([_header_str(v) for v in msg.get_all("Cc", [])])
        ]

        # 日期（统一转 UTC）
        date_utc: datetime | None = None
        try:
            dt = parsedate_to_datetime(msg.get("Date", ""))
            if dt is not None:
                date_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            date_utc = None

        body_text, body_html, attachments = ImapProvider._extract_content(msg)

        references = (msg.get("References") or "").split()

        return RawMessage(
            uid=uid,
            flags=flags,
            message_id=(msg.get("Message-ID") or "").strip() or None,
            in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
            references=references,
            subject=_header_str(msg.get("Subject", "")),
            from_name=from_name,
            from_email=from_email,
            to_list=to_list,
            cc_list=cc_list,
            date_utc=date_utc,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            raw_bytes=raw,
        )

    @staticmethod
    def _extract_content(msg) -> tuple[str, str | None, list[RawAttachment]]:  # noqa: ANN001
        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[RawAttachment] = []

        parts = msg.walk() if msg.is_multipart() else [msg]
        for part in parts:
            ctype = part.get_content_type()
            filename = part.get_filename()
            disposition = part.get_content_disposition()

            if filename or disposition == "attachment":
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    RawAttachment(
                        filename=_header_str(filename or "unnamed"),
                        size=len(payload),
                        mime=ctype,
                        content_id=(part.get("Content-ID") or "").strip("<>") or None,
                        is_inline=disposition == "inline",
                    )
                )
                continue

            if ctype == "text/plain":
                text_parts.append(ImapProvider._decode_part(part))
            elif ctype == "text/html":
                html_parts.append(ImapProvider._decode_part(part))

        body_text = "\n".join(p for p in text_parts if p).strip()
        body_html = "\n".join(html_parts).strip() or None
        if not body_text and body_html:
            body_text = _strip_html(body_html)
        return body_text, body_html, attachments

    @staticmethod
    def _decode_part(part) -> str:  # noqa: ANN001
        try:
            content = part.get_content()
            if isinstance(content, str):
                return content
        except Exception:  # noqa: BLE001 - 编码异常时走兜底
            pass
        payload = part.get_payload(decode=True) or b""
        return _decode_bytes(payload, part.get_content_charset())