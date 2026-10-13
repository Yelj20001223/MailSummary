"""IMAP FETCH 响应解析测试。

回归点：请求 ``(UID RFC822 FLAGS)`` 时，响应里夹着 RFC822 字面量，imaplib 会把
字面量之后的尾巴（FLAGS 等）拆成 data 列表里紧随其后的独立 bytes 片段。旧实现用
单条 ``UID\\s+\\d+.*?FLAGS`` 正则去匹配整段 meta，FLAGS 不在同一片段时就整体失配，
UID 落成 0 —— 后果是增量游标 last_uid 永远停在 0，每轮同步把全量邮件重拉一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.providers.imap import ImapProvider  # noqa: E402

_RAW = (
    b"From: a@b.com\r\n"
    b"Subject: hi\r\n"
    b"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n"
    b"\r\nbody\r\n"
)


def test_parse_uid_and_flags_from_stitched_meta() -> None:
    """字面量尾巴拼回后，UID 与 FLAGS 都应正确解析。"""
    meta = b"1 (UID 123 RFC822 {456}  FLAGS (\\Seen))"
    msg = ImapProvider._parse_message(meta, _RAW)
    assert msg.uid == 123
    assert msg.is_read is True


def test_parse_uid_without_flags() -> None:
    """仅有 UID、没有 FLAGS 时也必须能解出 UID（旧实现这里返回 0）。"""
    msg = ImapProvider._parse_message(b"1 (UID 456 RFC822 {789}", _RAW)
    assert msg.uid == 456
    assert msg.is_read is False


def test_parse_flags_only_starred() -> None:
    msg = ImapProvider._parse_message(b"1 (FLAGS (\\Flagged))", _RAW)
    assert msg.uid == 0
    assert msg.is_starred is True


class _FakeConn:
    """只实现 _fetch_batch 用到的 uid('FETCH', ...) 调用。"""

    def __init__(self, data: list) -> None:
        self._data = data

    def uid(self, command: str, *args) -> tuple[str, list]:  # noqa: ANN002
        assert command == "FETCH"
        return "OK", self._data


def _provider_with(data: list) -> ImapProvider:
    provider = ImapProvider.__new__(ImapProvider)  # 跳过 __init__（不建真实连接）
    provider._conn = _FakeConn(data)
    return provider


def test_fetch_batch_stitches_literal_tail() -> None:
    """imaplib 对多 UID 的返回形如 [(meta, literal), tail, (meta, literal), tail]。"""
    data = [
        (b"1 (UID 11 RFC822 {20}", _RAW), b" FLAGS (\\Seen))",
        (b"2 (UID 22 RFC822 {20}", _RAW), b" FLAGS ())",
    ]
    msgs = list(_provider_with(data)._fetch_batch([11, 22]))
    assert [m.uid for m in msgs] == [11, 22]
    assert msgs[0].is_read is True
    assert msgs[1].is_read is False
