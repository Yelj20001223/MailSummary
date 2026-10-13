"""分类与评分单元测试：关键词规则 / 白名单 / 阈值 / 标签 / 重新分类 / 同步接入。"""
from __future__ import annotations

from sqlmodel import select

from app.models.message import Message
from app.models.rule import Rule, Setting
from app.services.classify_service import (
    CATEGORIES,
    KEY_WHITELIST,
    ClassifyInput,
    ClassifyService,
)
from app.services.sync_service import SyncService
from tests.conftest import FakeProvider, make_message


# --------------------------- 引擎纯逻辑 ---------------------------
def _input(**kw) -> ClassifyInput:
    base = dict(
        subject="",
        body_text="",
        from_email="someone@corp.com",
        from_name="张三",
        to_emails=["tester@example.com"],
        account_email="tester@example.com",
    )
    base.update(kw)
    return ClassifyInput(**base)


def _svc(session) -> ClassifyService:
    return ClassifyService(session)


def test_subject_contract_category_and_tag(session):
    result = _svc(session).classify(_input(subject="关于合同签署的说明"))
    assert result.category == "合同法务"
    assert "合同" in result.tags
    # 主题命中 +4，收件人是我 +1
    assert result.importance_score == 5
    assert result.level == "todo"


def test_subject_finance_category(session):
    result = _svc(session).classify(_input(subject="请查收发票与对账单"))
    assert result.category == "财务发票"
    assert "发票" in result.tags


def test_subject_security_and_recruit(session):
    svc = _svc(session)
    assert svc.classify(_input(subject="您的登录验证码")).category == "验证码安全"
    assert svc.classify(_input(subject="面试邀请")).category == "人事招聘"
    assert "招聘" in svc.classify(_input(subject="招聘 · offer")).tags


def test_ad_keyword_category(session):
    result = _svc(session).classify(_input(subject="双十一大促销活动"))
    assert result.category == "市场广告"
    assert "广告" in result.tags


def test_whitelist_sender_and_domain(session):
    session.add(Setting(key=KEY_WHITELIST, value_json=["boss@corp.com", "vip.com"]))
    session.commit()
    svc = _svc(session).load_config(refresh=True)
    service = _svc(session)

    # 精确发件人命中：白名单 +5，收件人是我 +1
    r1 = service.classify(_input(from_email="boss@corp.com", subject="随便"), svc)
    assert r1.importance_score == 6

    # 域名命中：白名单 +5（+1 收件人）
    r2 = service.classify(_input(from_email="a@mail.vip.com", subject="随便"), svc)
    assert r2.importance_score == 6


def test_body_entities_weight(session):
    svc = _svc(session)
    cfg = svc.load_config(refresh=True)
    # 屏蔽「收件人是我」信号，单独验证实体权重
    base = dict(to_emails=[], account_email=None)
    # 主题『报价』命中财务订单 +3，实体 +2
    r = svc.classify(_input(subject="报价", body_text="本次金额 ¥12,000，请知悉", **base), cfg)
    assert r.importance_score == 5
    assert r.category == "财务发票"

    r2 = svc.classify(_input(subject="无关键词", body_text="订单号 ABC123456 已生成", **base), cfg)
    assert r2.importance_score == 2  # 仅实体 +2


def test_reply_and_to_me_weights(session):
    svc = _svc(session)
    cfg = svc.load_config(refresh=True)
    r = svc.classify(_input(subject="Re: 无关键词", is_reply=True), cfg)
    # 回复且收件人是我 +2，收件人是我 +1
    assert r.importance_score == 3
    assert r.level == "normal"


def test_office_attachment_weight(session):
    svc = _svc(session)
    cfg = svc.load_config(refresh=True)
    r = svc.classify(
        _input(subject="无关键词", attachment_names=["合同.pdf"], attachment_mimes=["application/pdf"]),
        cfg,
    )
    assert r.importance_score == 2  # 收件人是我 +1，附件 +1


def test_thresholds_mapping():
    t = {"important": 7, "todo": 4}
    assert ClassifyService._level(7, t) == "important"
    assert ClassifyService._level(6, t) == "todo"
    assert ClassifyService._level(4, t) == "todo"
    assert ClassifyService._level(3, t) == "normal"


def test_derived_tags(session):
    svc = _svc(session)
    cfg = svc.load_config(refresh=True)
    r = svc.classify(_input(subject="请回复确认", body_text="请于周五前提供材料"), cfg)
    assert "待回复" in r.tags

    r2 = svc.classify(_input(subject="账单", body_text="请及时付款"), cfg)
    assert "待付款" in r2.tags


# --------------------------- 规则驱动（DB 优先） ---------------------------
def test_db_rules_override_defaults(session):
    session.add(
        Rule(name="自定义发票", field="subject", op="contains", value="发票", score=9,
             tag="发票", category="财务发票")
    )
    session.commit()
    svc = _svc(session)
    cfg = svc.load_config(refresh=True)
    assert len(cfg.rules) == 1
    r = svc.classify(_input(subject="发票通知"), cfg)
    assert r.importance_score == 10  # 9 + 收件人是我 1
    assert r.level == "important"
    assert r.category == "财务发票"


def test_seed_defaults_is_idempotent(session):
    svc = _svc(session)
    svc.seed_defaults()
    n1 = len(session.exec(select(Rule)).all())
    svc.seed_defaults()
    n2 = len(session.exec(select(Rule)).all())
    assert n1 == n2 and n1 > 0


# --------------------------- 重新分类 ---------------------------
def test_reclassify_all_updates_messages(session, account):
    session.add(
        Message(
            account_id=account.id, uid=1, subject="关于合同签署", from_email="a@corp.com",
            to_json=[{"email": account.email}], body_hash="h1", category="其他",
        )
    )
    session.commit()
    updated = ClassifyService(session).reclassify_all()
    assert updated == 1
    msg = session.exec(select(Message)).first()
    assert msg.category == "合同法务"
    assert "合同" in (msg.tags_json or [])


# --------------------------- 与同步集成 ---------------------------
def test_sync_auto_classifies(session, account):
    raw = make_message(1, subject="请查收发票")
    provider = FakeProvider(account, "secret", [raw])
    SyncService(session, provider_factory=lambda _a, _s: provider)._sync_folder(
        account, provider, "INBOX", full=False
    )
    msg = session.exec(select(Message)).first()
    assert msg.category == "财务发票"
    assert msg.importance_score >= 3
    assert msg.category in CATEGORIES