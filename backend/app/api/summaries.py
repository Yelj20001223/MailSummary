"""摘要 / 信息抽取路由（P4）。

- 单封摘要：``GET /api/summaries/{message_id}``（缺省用规则；``use_llm=true`` 尝试 LLM）
- 重新生成：``POST /api/summaries/{message_id}/generate``
- 批量生成：``POST /api/summaries/batch``
- 摘要区（重要邮件）：``GET /api/summaries/digest``
- LLM 配置：``GET/PUT /api/summaries/config``（未配置时系统完整可用）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import SummaryServiceDep
from app.schemas.summary import (
    DigestItem,
    LlmConfigRead,
    LlmConfigUpdate,
    SummaryBatchRequest,
    SummaryBatchResult,
    SummaryRead,
)

router = APIRouter(prefix="/api/summaries", tags=["summaries"])


# --------------------------- LLM 配置（先于 /{message_id} 声明） ---------------------------
@router.get("/config", response_model=LlmConfigRead)
def get_llm_config(service: SummaryServiceDep) -> LlmConfigRead:
    return service.llm_config_read()


@router.put("/config", response_model=LlmConfigRead)
def update_llm_config(payload: LlmConfigUpdate, service: SummaryServiceDep) -> LlmConfigRead:
    service.save_llm_config(payload)
    return service.llm_config_read()


# --------------------------- 批量 / 摘要区 ---------------------------
@router.post("/batch", response_model=SummaryBatchResult)
def batch_summaries(payload: SummaryBatchRequest, service: SummaryServiceDep) -> SummaryBatchResult:
    return service.batch(
        message_ids=payload.message_ids,
        important_only=payload.important_only,
        use_llm=payload.use_llm,
        force=payload.force,
        limit=payload.limit,
    )


@router.get("/digest", response_model=list[DigestItem])
def digest(service: SummaryServiceDep, limit: int = Query(default=30, ge=1, le=200)) -> list[DigestItem]:
    return service.digest(limit=limit)


# --------------------------- 单封摘要 ---------------------------
@router.get("/{message_id}", response_model=SummaryRead)
def get_summary(
    message_id: int,
    service: SummaryServiceDep,
    use_llm: bool = Query(default=False),
) -> SummaryRead:
    result = service.get_or_generate(message_id, use_llm=use_llm)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件不存在")
    return result


@router.post("/{message_id}/generate", response_model=SummaryRead)
def regenerate_summary(
    message_id: int,
    service: SummaryServiceDep,
    use_llm: bool = Query(default=False),
) -> SummaryRead:
    result = service.regenerate(message_id, use_llm=use_llm)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件不存在")
    return result