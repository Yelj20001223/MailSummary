"""邮件列表 / 详情 / 批量操作路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import MessageServiceDep
from app.schemas.message import MessageDetail, MessageListResponse


router = APIRouter(prefix="/api/messages", tags=["messages"])


class BulkAction(BaseModel):
    ids: list[int] = Field(default_factory=list)
    value: bool = True


class TagAction(BaseModel):
    ids: list[int] = Field(default_factory=list)
    tag: str = Field(min_length=1, max_length=16)
    value: bool = True


class BulkFilterAction(BaseModel):
    """按当前筛选条件批量操作（一键已读 / 一键删除），语义与列表查询一致。"""

    action: str = Field(pattern="^(read|archive|delete)$")
    value: bool = True
    account_id: int | None = None
    company_id: int | None = None
    folder: str | None = None
    inference: str | None = None
    unread_only: bool = False
    starred_only: bool = False
    category: str | None = None
    tag: str | None = None
    search: str | None = None


@router.get("", response_model=MessageListResponse)
def list_messages(
    service: MessageServiceDep,
    account_id: int | None = Query(default=None),
    company_id: int | None = Query(default=None),
    folder: str | None = Query(default=None),
    inference: str | None = Query(default=None),
    unread_only: bool = Query(default=False),
    starred_only: bool = Query(default=False),
    category: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MessageListResponse:
    return service.list_messages(
        account_id=account_id,
        company_id=company_id,
        folder=folder,
        inference=inference,
        unread_only=unread_only,
        starred_only=starred_only,
        category=category,
        tag=tag,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/{message_id}", response_model=MessageDetail)
def get_message(message_id: int, service: MessageServiceDep) -> MessageDetail:
    detail = service.get_detail(message_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件不存在")
    return detail


@router.post("/actions/read")
def mark_read(payload: BulkAction, service: MessageServiceDep) -> dict:
    return {"updated": service.mark_read(payload.ids, payload.value)}


@router.post("/actions/star")
def star(payload: BulkAction, service: MessageServiceDep) -> dict:
    return {"updated": service.star(payload.ids, payload.value)}


@router.post("/actions/archive")
def archive(payload: BulkAction, service: MessageServiceDep) -> dict:
    return {"updated": service.archive(payload.ids, payload.value)}


@router.post("/actions/tag")
def set_tag(payload: TagAction, service: MessageServiceDep) -> dict:
    return {"updated": service.set_tag(payload.ids, payload.tag, payload.value)}


@router.post("/actions/delete")
def delete_messages(payload: BulkAction, service: MessageServiceDep) -> dict:
    """删除本地邮件（不影响邮箱服务器）。"""
    return {"deleted": service.delete(payload.ids)}


@router.post("/actions/bulk")
def bulk_by_filter(payload: BulkFilterAction, service: MessageServiceDep) -> dict:
    """对当前筛选条件下的全部邮件执行批量操作（一键已读 / 一键删除）。"""
    filters = payload.model_dump(exclude={"action", "value"})
    count = service.apply_to_filter(payload.action, payload.value, **filters)
    return {"deleted" if payload.action == "delete" else "updated": count}