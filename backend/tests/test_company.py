"""公司聚合单元测试：域名归并 / 公司名解析优先级 / 多联系人 / 合并 / 跨邮箱聚合 / 搜索。"""
from __future__ import annotations

from sqlmodel import select

from app.models.account import Account
from app.models.company import Company, Contact
from app.models.message import Attachment, Message
from app.services.company_service import (
    CompanyService,
    _company_from_display_name,
    domain_to_company_name,
    extract_signature_company,
    resolve_company_name,
)
from app.services.message_service import MessageService
from app.services.sync_service import SyncService
from tests.conftest import FakeProvider, make_message


# --------------------------- 公司名解析 ---------------------------
def test_domain_to_company_name():
    assert domain_to_company_name("abc-group.com") == "ABC Group"
    assert domain_to_company_name("corp.com.cn") == "Corp"
    assert domain_to_company_name("mail.acme.io") == "Acme"
    assert domain_to_company_name("") == ""


def test_extract_signature_company():
    body = "你好，请查收。\n\n-- \n北京示例科技有限公司\n电话 123"
    assert "示例科技" in extract_signature_company(body)
    assert extract_signature_company("没有公司的普通正文") == ""
    # 中英混排（含空格）也要能完整识别
    assert extract_signature_company("谢谢\n--\nAcme 集团有限公司") == "Acme 集团有限公司"


def test_resolve_company_name_priority():
    # a. 签名优先
    src, name = resolve_company_name("example.com", "张三", "--\n上海测试集团有限公司")
    assert src == "signature" and "测试集团" in name
    # b. 无签名时用域名主词
    src2, name2 = resolve_company_name("abc-group.com", "李四", "")
    assert src2 == "domain" and name2 == "ABC Group"
    # c. 无域名时用 display name
    src3, name3 = resolve_company_name("", "王五 | 星辰科技", "")
    assert src3 == "display_name" and name3 == "星辰科技"


def test_company_from_display_name():
    assert _company_from_display_name("张三 | XX科技") == "XX科技"
    assert _company_from_display_name("张三") == ""


# --------------------------- 归并 ---------------------------
def test_get_or_create_company_idempotent(session):
    svc = CompanyService(session)
    c1, created1 = svc.get_or_create_company_by_domain("corp.com", "Corp")
    c2, created2 = svc.get_or_create_company_by_domain("corp.com", "Corp")
    session.commit()
    assert created1 is True and created2 is False
    assert c1.id == c2.id
    assert len(session.exec(select(Company)).all()) == 1


def test_default_name_upgraded_by_signature(session):
    """先用域名默认名建公司，随后带签名线索 → 升级展示名并保留旧名作别名。"""
    svc = CompanyService(session)
    c1, _ = svc.get_or_create_company_by_domain("corp.com", "Corp")
    c2, created = svc.get_or_create_company_by_domain("corp.com", "星辰科技有限公司")
    session.commit()
    assert created is False
    assert c1.id == c2.id
    assert c2.name == "星辰科技有限公司"
    assert "Corp" in (c2.aliases or [])


def test_two_senders_same_domain_one_company(session):
    svc = CompanyService(session)
    m1 = svc.resolve(from_email="a@corp.com", from_name="A")
    m2 = svc.resolve(from_email="b@corp.com", from_name="B")
    session.commit()
    assert m1.company is not None and m2.company is not None
    assert m1.company.id == m2.company.id
    assert m1.contact.id != m2.contact.id
    assert m1.contact.company_id == m1.company.id
    assert len(session.exec(select(Contact)).all()) == 2


# --------------------------- 与同步集成 ---------------------------
def test_sync_links_company_and_contact(session, account):
    raw = make_message(1, subject="关于合同签署")
    provider = FakeProvider(account, "secret", [raw])
    SyncService(session, provider_factory=lambda _a, _s: provider)._sync_folder(
        account, provider, "INBOX", full=False
    )
    msg = session.exec(select(Message)).first()
    assert msg.company_id is not None
    company = session.get(Company, msg.company_id)
    assert "corp.com" in company.domains
    assert company.message_count == 1
    contact = session.get(Contact, msg.contact_id)
    assert contact.email == "zhangsan@corp.com"
    assert contact.company_id == company.id


def test_cross_account_same_company(session, account):
    """同一发件公司域名出现在两个邮箱账号 → 聚合成同一公司（跨邮箱聚合）。"""
    acc2 = Account(
        name="第二邮箱",
        email="me2@example.com",
        provider="163",
        auth_mode="imap",
        imap_host="imap.163.com",
        imap_port=993,
        username="me2@example.com",
        secret_enc="",
    )
    session.add(acc2)
    session.commit()
    session.refresh(acc2)

    p1 = FakeProvider(account, "s", [make_message(1)])
    p2 = FakeProvider(acc2, "s", [make_message(1, message_id="<b-1@corp.com>")])
    prov = {account.id: p1, acc2.id: p2}
    svc = SyncService(session, provider_factory=lambda a, _s: prov[a.id])
    svc._sync_folder(account, p1, "INBOX", full=False)
    svc._sync_folder(acc2, p2, "INBOX", full=False)

    companies = session.exec(select(Company)).all()
    assert len(companies) == 1
    assert companies[0].message_count == 2
    msgs = session.exec(select(Message).where(Message.company_id == companies[0].id)).all()
    assert len(msgs) == 2
    assert len({m.account_id for m in msgs}) == 2


# --------------------------- 重跑归并 ---------------------------
def test_reaggregate_backfills_existing_messages(session, account):
    session.add(
        Message(account_id=account.id, uid=1, from_email="a@foo.com", from_name="A",
                body_text="签名\n上海测试集团有限公司", body_hash="h1")
    )
    session.add(
        Message(account_id=account.id, uid=2, from_email="b@foo.com", body_hash="h2")
    )
    session.commit()

    processed = CompanyService(session).reaggregate()
    assert processed == 2
    companies = session.exec(select(Company)).all()
    assert len(companies) == 1
    assert "测试集团" in companies[0].name  # 签名优先于域名默认名
    msgs = session.exec(select(Message)).all()
    assert all(m.company_id == companies[0].id for m in msgs)
    assert companies[0].message_count == 2


# --------------------------- 合并 ---------------------------
def test_merge_companies(session, account):
    svc = CompanyService(session)
    c1, _ = svc.get_or_create_company_by_domain("a.com", "A公司")
    c2, _ = svc.get_or_create_company_by_domain("b.com", "B公司")
    session.add(Message(account_id=account.id, uid=1, from_email="x@b.com", company_id=c2.id, body_hash="h1"))
    session.add(Message(account_id=account.id, uid=2, from_email="y@a.com", company_id=c1.id, body_hash="h2"))
    session.commit()

    merged = svc.merge(c1.id, [c2.id])
    assert session.get(Company, c2.id) is None
    assert {"a.com", "b.com"} <= set(merged.domains)
    assert "B公司" in (merged.aliases or [])
    msgs = session.exec(select(Message).where(Message.company_id == c1.id)).all()
    assert len(msgs) == 2
    assert merged.message_count == 2


# --------------------------- 详情 / 往来摘要 ---------------------------
def test_company_detail_summary_and_attachments(session, account):
    svc = CompanyService(session)
    company, _ = svc.get_or_create_company_by_domain("acme.com", "Acme集团")
    m = Message(
        account_id=account.id, uid=1, subject="发票", from_email="a@acme.com",
        body_text="本次金额 ¥1,200，请在截止日前付款", company_id=company.id,
        category="财务发票", body_hash="h1",
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    session.add(Attachment(message_pk=m.id, filename="发票.pdf", size=100, mime="application/pdf"))
    session.commit()
    svc._refresh_company_stats(company.id)

    detail = svc.get_detail(company.id)
    assert detail is not None
    assert detail.company.message_count == 1
    assert detail.summary.total == 1
    assert "Acme" in detail.summary.text
    assert detail.summary.attachment_count == 1
    assert detail.attachments[0].filename == "发票.pdf"
    assert detail.messages[0].id == m.id


# --------------------------- 全局搜索扩展 ---------------------------
def test_search_matches_company_and_attachment_name(session, account):
    svc = CompanyService(session)
    company, _ = svc.get_or_create_company_by_domain("acme.com", "Acme 集团")
    m1 = Message(account_id=account.id, uid=1, subject="普通邮件", from_email="a@acme.com",
                 company_id=company.id, body_hash="h1")
    m2 = Message(account_id=account.id, uid=2, subject="另一个", from_email="x@other.com", body_hash="h2")
    session.add(m1)
    session.add(m2)
    session.commit()
    session.refresh(m1)
    session.refresh(m2)
    session.add(Attachment(message_pk=m2.id, filename="报价单.pdf", size=10, mime="application/pdf"))
    session.commit()

    ms = MessageService(session)
    by_company = ms.list_messages(search="Acme")
    assert {i.id for i in by_company.items} == {m1.id}
    by_attachment = ms.list_messages(search="报价单")
    assert {i.id for i in by_attachment.items} == {m2.id}


def test_list_companies_sorted_and_filtered(session):
    svc = CompanyService(session)
    c1, _ = svc.get_or_create_company_by_domain("a.com", "A公司")
    c2, _ = svc.get_or_create_company_by_domain("b.com", "B公司")
    c1.message_count = 5
    c2.message_count = 1
    c2.starred = True
    session.add(c1)
    session.add(c2)
    session.commit()

    cards = svc.list_companies()
    assert cards[0].name == "B公司"  # 星标优先
    starred = svc.list_companies(starred_only=True)
    assert [c.name for c in starred] == ["B公司"]