"""分类规则配置 / 重新分类 / 统计路由。

规则与阈值全部落库，界面可视化编辑，无需改代码。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import select

from app.api.deps import ClassifyServiceDep
from app.models.category import Category
from app.models.rule import Rule, Setting
from app.schemas.classify import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    ClassifyConfigRead,
    ClassifyConfigUpdate,
    ClassifyStats,
    ReclassifyRequest,
    ReclassifyResult,
    RuleCreate,
    RuleRead,
    RuleUpdate,
)
from app.services.classify_service import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    KEY_THRESHOLDS,
    KEY_WEIGHTS,
    KEY_WHITELIST,
)

router = APIRouter(prefix="/api/rules", tags=["rules"])
classify_router = APIRouter(prefix="/api/classify", tags=["classify"])


# --------------------------- 规则 CRUD ---------------------------
@router.get("", response_model=list[RuleRead])
def list_rules(service: ClassifyServiceDep) -> list[RuleRead]:
    rules = service.session.exec(select(Rule).order_by(Rule.id)).all()
    return [RuleRead(**r.model_dump()) for r in rules]


@router.post("", response_model=RuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(payload: RuleCreate, service: ClassifyServiceDep) -> RuleRead:
    rule = Rule(**payload.model_dump())
    service.session.add(rule)
    service.session.commit()
    service.session.refresh(rule)
    service.load_config(refresh=True)
    return RuleRead(**rule.model_dump())


@router.put("/{rule_id}", response_model=RuleRead)
def update_rule(rule_id: int, payload: RuleUpdate, service: ClassifyServiceDep) -> RuleRead:
    rule = service.session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
    service.session.add(rule)
    service.session.commit()
    service.session.refresh(rule)
    service.load_config(refresh=True)
    return RuleRead(**rule.model_dump())


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, service: ClassifyServiceDep) -> None:
    rule = service.session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")
    service.session.delete(rule)
    service.session.commit()
    service.load_config(refresh=True)


@router.post("/reset", response_model=list[RuleRead])
def reset_rules(service: ClassifyServiceDep) -> list[RuleRead]:
    """恢复内置默认规则（覆盖现有规则）。"""
    for rule in service.session.exec(select(Rule)).all():
        service.session.delete(rule)
    service.session.commit()
    service.seed_defaults()
    service.load_config(refresh=True)
    rules = service.session.exec(select(Rule).order_by(Rule.id)).all()
    return [RuleRead(**r.model_dump()) for r in rules]


# --------------------------- 全局配置（白名单 / 权重 / 阈值） ---------------------------
@classify_router.get("/config", response_model=ClassifyConfigRead)
def get_config(service: ClassifyServiceDep) -> ClassifyConfigRead:
    cfg = service.load_config(refresh=True)
    return ClassifyConfigRead(
        whitelist=cfg.whitelist,
        weights=cfg.weights,
        thresholds=cfg.thresholds,
        categories=service.list_categories(),
    )


def _upsert_setting(service: ClassifyServiceDep, key: str, value) -> None:  # noqa: ANN001
    setting = service.session.exec(select(Setting).where(Setting.key == key)).first()
    if setting is None:
        setting = Setting(key=key, value_json=value)
    else:
        setting.value_json = value
    service.session.add(setting)


@classify_router.put("/config", response_model=ClassifyConfigRead)
def update_config(payload: ClassifyConfigUpdate, service: ClassifyServiceDep) -> ClassifyConfigRead:
    if payload.whitelist is not None:
        _upsert_setting(service, KEY_WHITELIST, payload.whitelist)
    if payload.weights is not None:
        _upsert_setting(service, KEY_WEIGHTS, payload.weights)
    if payload.thresholds is not None:
        _upsert_setting(service, KEY_THRESHOLDS, payload.thresholds)
    service.session.commit()
    cfg = service.load_config(refresh=True)
    return ClassifyConfigRead(
        whitelist=cfg.whitelist,
        weights=cfg.weights,
        thresholds={**DEFAULT_THRESHOLDS, **cfg.thresholds},
        categories=service.list_categories(),
    )


@classify_router.post("/reset", response_model=ClassifyConfigRead)
def reset_config(service: ClassifyServiceDep) -> ClassifyConfigRead:
    _upsert_setting(service, KEY_WHITELIST, [])
    _upsert_setting(service, KEY_WEIGHTS, DEFAULT_WEIGHTS)
    _upsert_setting(service, KEY_THRESHOLDS, DEFAULT_THRESHOLDS)
    service.session.commit()
    cfg = service.load_config(refresh=True)
    return ClassifyConfigRead(
        whitelist=cfg.whitelist, weights=cfg.weights, thresholds=cfg.thresholds, categories=service.list_categories()
    )


# --------------------------- 分类管理（增删改） ---------------------------
@classify_router.get("/categories", response_model=list[CategoryRead])
def list_categories(service: ClassifyServiceDep) -> list[CategoryRead]:
    rows = service.session.exec(
        select(Category).order_by(Category.sort_order, Category.id)
    ).all()
    return [CategoryRead(**c.model_dump()) for c in rows]


@classify_router.post("/categories", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryCreate, service: ClassifyServiceDep) -> CategoryRead:
    try:
        category = service.add_category(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return CategoryRead(**category.model_dump())


@classify_router.put("/categories/{name}", response_model=CategoryRead)
def rename_category(name: str, payload: CategoryUpdate, service: ClassifyServiceDep) -> CategoryRead:
    try:
        category = service.rename_category(name, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return CategoryRead(**category.model_dump())


@classify_router.delete("/categories/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(name: str, service: ClassifyServiceDep) -> None:
    try:
        service.delete_category(name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# --------------------------- 重新分类 & 统计 ---------------------------
@classify_router.post("/reclassify", response_model=ReclassifyResult)
def reclassify(payload: ReclassifyRequest, service: ClassifyServiceDep) -> ReclassifyResult:
    updated = service.reclassify_all(payload.account_id)
    return ReclassifyResult(updated=updated)


@classify_router.get("/stats", response_model=ClassifyStats)
def stats(
    service: ClassifyServiceDep,
    account_id: int | None = Query(default=None),
    unread_only: bool = Query(default=False),
) -> ClassifyStats:
    return ClassifyStats(**service.stats(account_id=account_id, unread_only=unread_only))