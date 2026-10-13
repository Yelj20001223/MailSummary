"""分类 / 规则相关 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.services.classify_service import CATEGORIES

_FIELDS = {"subject", "body", "from", "domain", "attachment"}
_OPS = {"contains", "regex", "in_list", "equals"}


class RuleRead(BaseModel):
    id: int
    name: str
    enabled: bool
    field: str
    op: str
    value: str
    score: int
    tag: str | None = None
    category: str | None = None


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    field: str = "subject"
    op: str = "contains"
    value: str = ""
    score: int = 0
    tag: str | None = None
    category: str | None = None

    @field_validator("field")
    @classmethod
    def _check_field(cls, v: str) -> str:
        if v not in _FIELDS:
            raise ValueError(f"field 必须是 {sorted(_FIELDS)} 之一")
        return v

    @field_validator("op")
    @classmethod
    def _check_op(cls, v: str) -> str:
        if v not in _OPS:
            raise ValueError(f"op 必须是 {sorted(_OPS)} 之一")
        return v

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str | None) -> str | None:
        # 分类已支持自定义，此处仅做长度约束
        if v and len(v) > 32:
            raise ValueError("category 名称过长")
        return v


class RuleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    field: str | None = None
    op: str | None = None
    value: str | None = None
    score: int | None = None
    tag: str | None = None
    category: str | None = None


class ClassifyConfigRead(BaseModel):
    whitelist: list[str] = Field(default_factory=list)
    weights: dict[str, int] = Field(default_factory=dict)
    thresholds: dict[str, int] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=lambda: list(CATEGORIES))


class ClassifyConfigUpdate(BaseModel):
    whitelist: list[str] | None = None
    weights: dict[str, int] | None = None
    thresholds: dict[str, int] | None = None


class ReclassifyRequest(BaseModel):
    account_id: int | None = None


class ReclassifyResult(BaseModel):
    updated: int


class CategoryCount(BaseModel):
    name: str
    count: int
    unread: int = 0


class CategoryRead(BaseModel):
    id: int
    name: str
    sort_order: int = 0
    is_builtin: bool = False


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class CategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class TagCount(BaseModel):
    name: str
    count: int


class ClassifyStats(BaseModel):
    categories: list[CategoryCount] = Field(default_factory=list)
    tags: list[TagCount] = Field(default_factory=list)