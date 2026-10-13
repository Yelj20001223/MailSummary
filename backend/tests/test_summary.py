"""摘要与信息抽取单元测试（P4）：规则抽取 / 模板摘要 / 缓存 / LLM 降级 / 摘要区。"""
from __future__ import annotations

from sqlmodel import select

from app.models.message import Message
from app.models.summary import Summary
from app.schemas.summary import LlmConfigUpdate
from app.services.summary_service import (
    SummaryService,
    build_rule_summary,
    extract_action_items,
    extract_entities,
)

BODY = """您好：
本次订单金额 ¥12,800.00，订单号 PO2026011234。
请在 2026年10月20日 前完成付款，逾期作废。
验证码 837261 请勿泄露。
联系电话 13812345678 / 010-88886666。
请回复确认，谢谢。"""


# --------------------------- 规则抽取 ---------------------------
def test_extract_entities_core():
    ents = extract_entities(BODY, "付款通知")
    assert "¥12,800.00" in ents["amounts"]
    assert "PO2026011234" in ents["orders"]
    assert "13812345678" in ents["phones"]
    assert "010-88886666" in ents["phones"]
    assert "2026-10-20" in ents["dates"]
    assert "837261" in ents["captcha"]
    assert ents["deadlines"], "应识别出「请…前完成」类截止时间"


def test_extract_captcha_requires_context():
    # 没有「验证码」字样时，不应把裸数字当验证码（避免误抓）
    ents = extract_entities("订单编号 12345678 已生成", "订单")
    assert ents["captcha"] == []


def test_extract_action_items():
    items = extract_action_items(BODY)
    assert any("请回复" in it for it in items)


def test_dates_without_year_use_current_year():
    ents = extract_entities("请于 10月1日 前提交材料", "")
    assert ents["dates"] and ents["dates"][0].endswith("-10-01")


# --------------------------- 模板摘要 ---------------------------
def test_build_rule_summary():
    msg = Message(
        account_id=1,
        from_name="李四",
        from_email="lisi@corp.com",
        subject="付款通知",
        body_text=BODY,
        category="财务发票",
        importance_score=8,
    )
    text, ents, actions, urgency = build_rule_summary(msg)
    assert "李四" in text and "付款通知" in text
    assert "¥12,800.00" in text
    assert "财务发票" in text
    assert actions
    assert urgency == "high"  # 有截止时间


# --------------------------- 缓存 ---------------------------
def test_summary_cached_by_content_hash(session, account):
    msg = Message(
        account_id=account.id, uid=1, subject="通知", from_email="a@x.com",
        body_text=BODY, body_hash="h1",
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)

    svc = SummaryService(session)
    first = svc.get_or_generate(msg.id)
    assert first is not None and first.cached is False
    second = svc.get_or_generate(msg.id)
    assert second is not None and second.cached is True
    # 只落一条缓存
    assert len(session.exec(select(Summary)).all()) == 1

    # 正文变化 → hash 变化 → 重新生成（产生新的缓存行）
    msg.body_text = BODY + "\n补充：另需付款 ¥1,000。"
    session.add(msg)
    session.commit()
    third = svc.get_or_generate(msg.id)
    assert third is not None and third.cached is False
    assert len(session.exec(select(Summary)).all()) == 2


# --------------------------- LLM 配置与降级 ---------------------------
def test_llm_config_masked(session):
    svc = SummaryService(session)
    svc.save_llm_config(
        LlmConfigUpdate(
            enabled=True, provider="openai",
            base_url="http://127.0.0.1:9/v1", model="gpt-test",
            api_key="sk-secret-123456",
        )
    )
    read = svc.llm_config_read()
    assert read.enabled and read.has_api_key
    assert "secret" not in read.api_key_masked
    assert read.model == "gpt-test"


def test_llm_disabled_falls_back_to_rule(session, account):
    """LLM 已开启但未配置完整 → 规则摘要，不报错。"""
    msg = Message(account_id=account.id, uid=1, subject="付款通知",
                  from_email="a@x.com", body_text=BODY, body_hash="h1")
    session.add(msg)
    session.commit()
    session.refresh(msg)

    svc = SummaryService(session)
    svc.save_llm_config(LlmConfigUpdate(enabled=True, provider="openai", base_url=""))
    result = svc.get_or_generate(msg.id, use_llm=True)
    assert result is not None
    assert result.source == "rule"
    assert "¥12,800.00" in result.summary


def test_llm_call_failure_falls_back_to_rule(session, account):
    """LLM 接口不可达 → 捕获异常并降级规则摘要。"""
    msg = Message(account_id=account.id, uid=1, subject="付款通知",
                  from_email="a@x.com", body_text=BODY, body_hash="h1")
    session.add(msg)
    session.commit()
    session.refresh(msg)

    svc = SummaryService(session)
    svc.save_llm_config(
        LlmConfigUpdate(
            enabled=True, provider="openai",
            base_url="http://127.0.0.1:9/v1", model="gpt-test",
            api_key="sk-x", timeout=1.0,
        )
    )
    result = svc.get_or_generate(msg.id, use_llm=True)
    assert result is not None
    assert result.source == "rule"  # 降级成功


# --------------------------- 摘要区 / 批量 ---------------------------
def test_digest_returns_only_important(session, account):
    session.add(Message(account_id=account.id, uid=1, subject="重要通知",
                        from_email="a@x.com", body_text=BODY,
                        importance_score=8, body_hash="h1"))
    session.add(Message(account_id=account.id, uid=2, subject="普通邮件",
                        from_email="b@x.com", body_text="随便聊聊", body_hash="h2"))
    session.commit()

    items = SummaryService(session).digest()
    assert len(items) == 1
    assert items[0].message.subject == "重要通知"
    assert items[0].summary


def test_batch_important_only(session, account):
    session.add(Message(account_id=account.id, uid=1, subject="重要通知",
                        from_email="a@x.com", body_text=BODY,
                        importance_score=9, body_hash="h1"))
    session.add(Message(account_id=account.id, uid=2, subject="普通",
                        from_email="b@x.com", body_text="x", body_hash="h2"))
    session.commit()

    result = SummaryService(session).batch(important_only=True)
    assert result.generated == 1
    assert result.failed == 0