"""规则与设置模型（P2 规则配置页的数据基础）。"""
from __future__ import annotations

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Rule(SQLModel, table=True):
    """分类评分规则。可在 UI 中可视化编辑。"""

    __tablename__ = "rules"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    enabled: bool = True
    # 匹配条件：{"field": "subject", "op": "contains", "value": "合同"}
    field: str = Field(default="subject", description="subject | body | from | domain | attachment")
    op: str = Field(default="contains", description="contains | regex | in_list | equals")
    value: str = ""
    score: int = 0
    tag: str | None = None
    category: str | None = None


class Setting(SQLModel, table=True):
    """通用 KV 设置（白名单、阈值、LLM 开关等）。"""

    __tablename__ = "settings"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    value: str = ""
    value_json: dict | list | None = Field(default=None, sa_column=Column(JSON))