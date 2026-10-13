"""公司聚合路由：列表 / 详情 / 编辑 / 合并 / 重跑归并。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CompanyServiceDep
from app.schemas.company import (
    CompanyCard,
    CompanyDetail,
    CompanyMergeRequest,
    CompanyMergeResult,
    CompanyUpdate,
    ReaggregateResult,
)

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("", response_model=list[CompanyCard])
def list_companies(
    service: CompanyServiceDep,
    starred_only: bool = Query(default=False),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CompanyCard]:
    return service.list_companies(
        starred_only=starred_only, search=search, limit=limit, offset=offset
    )


@router.post("/reaggregate", response_model=ReaggregateResult)
def reaggregate(service: CompanyServiceDep) -> ReaggregateResult:
    """清空并重建全部邮件的公司归并（历史邮件 / 规则变更后使用）。"""
    return ReaggregateResult(processed=service.reaggregate())


@router.post("/merge", response_model=CompanyMergeResult)
def merge(payload: CompanyMergeRequest, service: CompanyServiceDep) -> CompanyMergeResult:
    company = service.merge(payload.primary_id, payload.merge_ids)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标公司不存在")
    return CompanyMergeResult(company=service._to_card(company), merged=len(payload.merge_ids))


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(company_id: int, service: CompanyServiceDep) -> CompanyDetail:
    detail = service.get_detail(company_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公司不存在")
    return detail


@router.put("/{company_id}", response_model=CompanyCard)
def update_company(company_id: int, payload: CompanyUpdate, service: CompanyServiceDep) -> CompanyCard:
    company = service.update(company_id, payload)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公司不存在")
    return service._to_card(company)