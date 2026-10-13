"""摘要 / 信息抽取相关 schema（P4）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.message import MessageRead


class SummaryEntities(BaseModel):
    """规则 / LLM 抽取的关键实体。"""

    amounts: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    captcha: list[str] = Field(default_factory=list)
    orders: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)


class SummaryRead(BaseModel):
    message_id: int
    summary: str = ""
    entities: SummaryEntities = Field(default_factory=SummaryEntities)
    action_items: list[str] = Field(default_factory=list)
    urgency: str = "normal"  # low | normal | high
    source: str = "rule"  # rule | llm
    cached: bool = False
    created_at: datetime | None = None


class SummaryBatchRequest(BaseModel):
    message_ids: list[int] | None = None
    important_only: bool = False
    use_llm: bool = False
    force: bool = False
    limit: int = Field(default=50, ge=1, le=500)


class SummaryBatchResult(BaseModel):
    generated: int = 0
    llm_used: int = 0
    failed: int = 0
    message_ids: list[int] = Field(default_factory=list)


class DigestItem(BaseModel):
    message: MessageRead
    summary: str = ""
    urgency: str = "normal"
    action_items: list[str] = Field(default_factory=list)
    entities: SummaryEntities = Field(default_factory=SummaryEntities)


class LlmConfigRead(BaseModel):
    enabled: bool = False
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    has_api_key: bool = False
    api_key_masked: str = ""
    max_chars: int = 4000
    timeout: float = 30.0


class LlmConfigUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    max_chars: int | None = Field(default=None, ge=500, le=20000)
    timeout: float | None = Field(default=None, ge=1, le=120)