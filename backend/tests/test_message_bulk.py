"""邮件批量操作测试：按筛选一键已读 / 一键删除（级联清理 + .eml 落盘）。"""
from __future__ import annotations

from sqlmodel import select

from app.models.account import Account
from app.models.label import Label, MessageLabel
from app.models.message import Attachment, Message
from app.models.summary import Summary
from app.services.message_service import MessageService


def _message(session, account: Account, uid: int, **kw) -> Message:
    msg = Message(account_id=account.id, uid=uid, **kw)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


def test_apply_to_filter_marks_only_unread(session, account):
    m1 = _message(session, account, uid=1, is_read=False, category="其他")
    m2 = _message(session, account, uid=2, is_read=True, category="其他")
    m3 = _message(session, account, uid=3, is_read=False, category="系统通知")

    svc = MessageService(session)
    updated = svc.apply_to_filter("read", True, unread_only=True)

    assert updated == 2
    assert session.get(Message, m1.id).is_read is True
    assert session.get(Message, m2.id).is_read is True
    assert session.get(Message, m3.id).is_read is True
    # 只对未读生效，不影响其他字段
    assert session.get(Message, m3.id).category == "系统通知"


def test_apply_to_filter_respects_account_scope(session, account):
    other = Account(
        name="第二个邮箱",
        email="other@example.com",
        provider="qq",
        auth_mode="imap",
        imap_host="imap.qq.com",
        imap_port=993,
        username="other@example.com",
        secret_enc="",
    )
    session.add(other)
    session.commit()
    session.refresh(other)

    mine = _message(session, account, uid=1, is_read=False)
    theirs = _message(session, other, uid=1, is_read=False)

    MessageService(session).apply_to_filter("read", True, account_id=account.id)

    assert session.get(Message, mine.id).is_read is True
    assert session.get(Message, theirs.id).is_read is False


def test_apply_to_filter_category_read(session, account):
    a = _message(session, account, uid=1, is_read=False, category="财务发票")
    b = _message(session, account, uid=2, is_read=False, category="其他")

    updated = MessageService(session).apply_to_filter("read", True, category="财务发票")

    assert updated == 1
    assert session.get(Message, a.id).is_read is True
    assert session.get(Message, b.id).is_read is False


def test_delete_by_filter_cascades_related_rows(session, account, tmp_path):
    eml = tmp_path / "1.eml"
    eml.write_text("raw", encoding="utf-8")

    label = Label(name="待回复")
    session.add(label)
    session.commit()
    session.refresh(label)

    keep = _message(session, account, uid=1, category="其他")
    target = _message(
        session, account, uid=2, category="财务发票", raw_eml_path=str(eml)
    )
    session.add(Attachment(message_pk=target.id, filename="a.pdf", size=10, mime="application/pdf"))
    session.add(Summary(message_pk=target.id, summary="规则摘要"))
    session.add(MessageLabel(message_pk=target.id, label_id=label.id))
    session.commit()

    keep_id, target_id = keep.id, target.id
    deleted = MessageService(session).apply_to_filter("delete", category="财务发票")

    assert deleted == 1
    assert session.get(Message, target_id) is None
    assert session.get(Message, keep_id) is not None
    assert session.exec(select(Attachment).where(Attachment.message_pk == target_id)).all() == []
    assert session.exec(select(Summary).where(Summary.message_pk == target_id)).all() == []
    assert session.exec(select(MessageLabel).where(MessageLabel.message_pk == target_id)).all() == []
    assert not eml.exists()


def test_delete_by_ids_only_removes_given(session, account):
    m1 = _message(session, account, uid=1)
    m2 = _message(session, account, uid=2)

    deleted = MessageService(session).delete([m1.id])

    assert deleted == 1
    assert session.get(Message, m1.id) is None
    assert session.get(Message, m2.id) is not None


def test_delete_empty_ids_is_noop(session, account):
    _message(session, account, uid=1)
    assert MessageService(session).delete([]) == 0


def test_apply_to_filter_rejects_unknown_action(session, account):
    import pytest

    with pytest.raises(ValueError):
        MessageService(session).apply_to_filter("nuke")