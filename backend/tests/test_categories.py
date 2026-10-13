"""分类自定义（增删改）单元测试：种子 / 增删改 / 邮件与规则改写 / 统计。"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.models.category import Category
from app.models.message import Message
from app.models.rule import Rule
from app.services.classify_service import CATEGORIES, CATEGORY_OTHER, ClassifyService


def _svc(session) -> ClassifyService:
    return ClassifyService(session)


def _add_message(session, account, category: str, subject: str = "测试") -> Message:
    msg = Message(account_id=account.id, category=category, subject=subject, uid=1)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


# --------------------------- 种子 ---------------------------
def test_seed_creates_builtin_categories(session):
    svc = _svc(session)
    svc.seed_defaults()
    names = svc.list_categories()
    assert names == CATEGORIES
    rows = session.exec(select(Category)).all()
    assert all(c.is_builtin for c in rows)
    assert [c.sort_order for c in rows] == list(range(len(CATEGORIES)))


def test_seed_idempotent(session):
    svc = _svc(session)
    svc.seed_defaults()
    svc.seed_defaults()
    assert len(session.exec(select(Category)).all()) == len(CATEGORIES)


def test_list_categories_falls_back_when_empty(session):
    # 未种子时回退内置默认，保证开箱即用
    assert _svc(session).list_categories() == CATEGORIES


# --------------------------- 新增 ---------------------------
def test_add_category_appends_at_end(session):
    svc = _svc(session)
    svc.seed_defaults()
    cat = svc.add_category("项目跟进")
    assert cat.name == "项目跟进"
    assert cat.is_builtin is False
    names = svc.list_categories()
    assert names[-1] == "项目跟进"
    assert len(names) == len(CATEGORIES) + 1


def test_add_duplicate_rejected(session):
    svc = _svc(session)
    svc.seed_defaults()
    with pytest.raises(ValueError):
        svc.add_category("合同法务")


def test_add_empty_rejected(session):
    with pytest.raises(ValueError):
        _svc(session).add_category("   ")


# --------------------------- 重命名 ---------------------------
def test_rename_rewrites_messages_and_rules(session, account):
    svc = _svc(session)
    svc.seed_defaults()
    _add_message(session, account, "合同法务", subject="合同 A")
    session.add(Rule(name="r1", category="合同法务", field="subject", op="contains", value="合同"))
    session.commit()

    svc.rename_category("合同法务", "法律法规")

    assert "法律法规" in svc.list_categories()
    assert "合同法务" not in svc.list_categories()
    assert session.exec(select(Message)).first().category == "法律法规"
    assert session.exec(select(Rule)).first().category == "法律法规"


def test_rename_to_existing_rejected(session):
    svc = _svc(session)
    svc.seed_defaults()
    with pytest.raises(ValueError):
        svc.rename_category("合同法务", "财务发票")


def test_rename_unknown_rejected(session):
    with pytest.raises(ValueError):
        _svc(session).rename_category("不存在的分类", "新名")


# --------------------------- 删除 ---------------------------
def test_delete_rewrites_to_other(session, account):
    svc = _svc(session)
    svc.seed_defaults()
    _add_message(session, account, "合同法务")
    svc.delete_category("合同法务")

    assert "合同法务" not in svc.list_categories()
    assert session.exec(select(Message)).first().category == CATEGORY_OTHER


def test_delete_without_other_falls_back_to_first(session, account):
    svc = _svc(session)
    # 只保留两个分类且没有「其他」
    session.add(Category(name="甲", sort_order=0, is_builtin=True))
    session.add(Category(name="乙", sort_order=1, is_builtin=True))
    session.commit()
    _add_message(session, account, "乙")

    fallback = svc.delete_category("乙")
    assert fallback == "甲"
    assert session.exec(select(Message)).first().category == "甲"


def test_delete_last_category_rejected(session):
    svc = _svc(session)
    session.add(Category(name="唯一", sort_order=0, is_builtin=True))
    session.commit()
    with pytest.raises(ValueError):
        svc.delete_category("唯一")


# --------------------------- 统计 ---------------------------
def test_stats_reflects_db_categories(session, account):
    svc = _svc(session)
    svc.seed_defaults()
    svc.add_category("项目跟进")
    _add_message(session, account, "项目跟进")

    stats = svc.stats()
    names = [c["name"] for c in stats["categories"]]
    assert "项目跟进" in names
    row = next(c for c in stats["categories"] if c["name"] == "项目跟进")
    assert row["count"] == 1


def test_stats_includes_orphan_category(session, account):
    """消息引用了分类表里已不存在的名称时，仍应出现在统计中，避免邮件被隐藏。"""
    svc = _svc(session)
    svc.seed_defaults()
    _add_message(session, account, "历史遗留分类")

    names = [c["name"] for c in svc.stats()["categories"]]
    assert "历史遗留分类" in names


def test_delete_then_stats_no_longer_lists(session, account):
    svc = _svc(session)
    svc.seed_defaults()
    _add_message(session, account, "合同法务")
    svc.delete_category("合同法务")
    names = [c["name"] for c in svc.stats()["categories"]]
    assert "合同法务" not in names