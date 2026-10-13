"""账号删除的级联清理测试。

覆盖：删除账号时连带清理其邮件 / 附件 / 摘要 / 标签关联 / 同步状态与落盘 .eml，
并刷新公司归并、移除仅由该账号产生的空公司；其他账号数据不受影响。
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.config import settings
from app.models.account import Account
from app.models.company import Company
from app.models.label import Label, MessageLabel
from app.models.message import Attachment, Message
from app.models.summary import Summary
from app.models.sync_state import SyncState
from app.services.account_service import AccountService


@pytest.fixture(autouse=True)
def _isolate_eml_dir(tmp_path, monkeypatch):
    """把 .eml 落盘目录指向临时目录，避免测试误删真实用户数据。"""
    monkeypatch.setattr(settings, "eml_dir", tmp_path)
    return tmp_path


def _second_account(session, email: str = "other@example.com") -> Account:
    acc = Account(
        name="第二个邮箱",
        email=email,
        provider="qq",
        auth_mode="imap",
        imap_host="imap.qq.com",
        imap_port=993,
        username=email,
        secret_enc="",
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


def _message(session, account: Account, uid: int, **kw) -> Message:
    msg = Message(account_id=account.id, uid=uid, **kw)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


def test_delete_account_cascades_all_related_rows(session, account):
    other = _second_account(session)
    keep = _message(session, other, uid=1, subject="保留邮件")
    keep_id = keep.id

    label = Label(name="待回复")
    session.add(label)
    session.commit()
    session.refresh(label)

    m1 = _message(session, account, uid=10, subject="待删1")
    m2 = _message(session, account, uid=11, subject="待删2")
    session.add(Attachment(message_pk=m1.id, filename="a.pdf", size=10, mime="application/pdf"))
    session.add(Summary(message_pk=m1.id, summary="规则摘要"))
    session.add(MessageLabel(message_pk=m2.id, label_id=label.id))
    session.add(SyncState(account_id=account.id, folder="INBOX", last_uid=11))
    session.commit()

    aid = account.id
    AccountService(session).delete(account)

    assert session.get(Account, aid) is None
    assert session.exec(select(Message).where(Message.account_id == aid)).all() == []
    assert session.exec(select(Attachment)).all() == []
    assert session.exec(select(Summary)).all() == []
    assert session.exec(select(MessageLabel)).all() == []
    assert session.exec(select(SyncState).where(SyncState.account_id == aid)).all() == []
    # 其他账号的邮件不受影响
    assert session.get(Message, keep_id) is not None


def test_delete_account_removes_eml_dir(session, account, tmp_path):
    folder = tmp_path / str(account.id)
    folder.mkdir(parents=True)
    (folder / "1_1.eml").write_text("raw", encoding="utf-8")

    AccountService(session).delete(account)

    assert not folder.exists()


def test_delete_account_drops_empty_companies_but_keeps_starred(session, account):
    other = _second_account(session)

    corp_a = Company(name="A公司", domains=["corp-a.com"], message_count=1)
    corp_b = Company(name="B公司", domains=["corp-b.com"], message_count=1)
    corp_c = Company(name="星标公司", domains=["starred.com"], starred=True)
    session.add(corp_a)
    session.add(corp_b)
    session.add(corp_c)
    session.commit()
    for c in (corp_a, corp_b, corp_c):
        session.refresh(c)

    _message(session, account, uid=1, from_email="x@corp-a.com", company_id=corp_a.id)
    _message(session, other, uid=2, from_email="y@corp-b.com", company_id=corp_b.id)
    session.commit()

    AccountService(session).delete(account)

    names = {c.name for c in session.exec(select(Company)).all()}
    assert "A公司" not in names      # 仅由被删账号产生的空公司被清理
    assert "B公司" in names          # 其他账号仍在使用，保留
    assert "星标公司" in names        # 用户加星的空公司不受影响


def test_delete_account_refreshes_remaining_company_stats(session, account):
    other = _second_account(session)
    corp = Company(name="B公司", domains=["corp-b.com"])
    session.add(corp)
    session.commit()
    session.refresh(corp)
    cid = corp.id

    _message(session, account, uid=1, from_email="x@corp-b.com", company_id=cid)
    _message(session, other, uid=2, from_email="y@corp-b.com", company_id=cid)
    session.commit()

    AccountService(session).delete(account)

    refreshed = session.get(Company, cid)
    assert refreshed is not None
    assert refreshed.message_count == 1  # 只剩其他账号的那封