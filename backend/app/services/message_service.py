"""邮件查询服务：列表 / 详情 / 批量操作。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import String, cast
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlmodel import Session, col, func, select

from app.core.html_sanitizer import sanitize_html
from app.core.logging import logger
from app.models.account import Account
from app.models.company import Company
from app.models.label import MessageLabel
from app.models.message import Attachment, Message
from app.models.summary import Summary
from app.schemas.message import MessageDetail, MessageListResponse, MessageRead


def _preview(text: str, n: int = 140) -> str:
    flat = " ".join((text or "").split())
    return flat[:n] + ("…" if len(flat) > n else "")


def to_message_read(m: Message, account_email: str | None) -> MessageRead:
    """把 Message ORM 转成前端用的 MessageRead（列表 / 公司时间线共用）。"""
    return MessageRead(
        id=m.id or 0,
        account_id=m.account_id,
        account_email=account_email,
        folder=m.folder,
        subject=m.subject or "(无主题)",
        from_name=m.from_name,
        from_email=m.from_email,
        date_utc=m.date_utc,
        is_read=m.is_read,
        is_starred=m.is_starred,
        is_archived=m.is_archived,
        has_attachment=m.has_attachment,
        importance_score=m.importance_score,
        category=m.category,
        tags=m.tags_json or [],
        preview=_preview(m.body_text),
    )


class MessageService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --------------------------- 列表 ---------------------------
    def _filter_conditions(
        self,
        *,
        account_id: int | None = None,
        company_id: int | None = None,
        folder: str | None = None,
        inference: str | None = None,
        unread_only: bool = False,
        starred_only: bool = False,
        category: str | None = None,
        tag: str | None = None,
        search: str | None = None,
    ) -> list:  # noqa: ANN202
        """列表与「按筛选批量操作」共用的筛选条件，避免两处逻辑漂移。"""
        conds: list = []
        if account_id:
            conds.append(Message.account_id == account_id)
        if company_id:
            conds.append(Message.company_id == company_id)
        if folder:
            conds.append(Message.folder == folder)
        if inference:
            # Outlook 焦点收件箱分区：focused / other
            conds.append(Message.inference_classification == inference)
        if unread_only:
            conds.append(Message.is_read == False)  # noqa: E712
        if starred_only:
            conds.append(Message.is_starred == True)  # noqa: E712
        if category:
            conds.append(Message.category == category)
        if tag:
            # tags_json 以 JSON 数组存储，非 ASCII 会被转义（如 "通知" 存成 "\u901a\u77e5"），
            # 直接对文本做 LIKE 匹配不到中文标签。改用 json_each 展开后精确比较成员。
            je = func.json_each(Message.tags_json).table_valued("value").alias("je")
            conds.append(sa_select(1).select_from(je).where(je.c.value == tag).exists())
        if search:
            conds.append(self._search_clause(search))
        return conds

    def list_messages(
        self,
        *,
        account_id: int | None = None,
        company_id: int | None = None,
        folder: str | None = None,
        inference: str | None = None,
        unread_only: bool = False,
        starred_only: bool = False,
        category: str | None = None,
        tag: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MessageListResponse:
        conds = self._filter_conditions(
            account_id=account_id,
            company_id=company_id,
            folder=folder,
            inference=inference,
            unread_only=unread_only,
            starred_only=starred_only,
            category=category,
            tag=tag,
            search=search,
        )
        stmt = select(Message)
        if conds:
            stmt = stmt.where(*conds)

        total = self.session.exec(
            select(func.count()).select_from(stmt.subquery())
        ).one()

        rows = self.session.exec(
            stmt.order_by(col(Message.date_utc).desc()).offset(offset).limit(limit)
        ).all()

        emails = {a.id: a.email for a in self.session.exec(select(Account)).all()}
        items = [self._to_read(m, emails.get(m.account_id)) for m in rows]

        unread_total = len(
            self.session.exec(
                select(Message).where(
                    Message.is_read == False,  # noqa: E712
                    Message.is_archived == False,  # noqa: E712
                )
            ).all()
        )
        return MessageListResponse(total=total, unread_total=unread_total, items=items)

    def _search_clause(self, search: str):  # noqa: ANN202
        """全局搜索：主题 / 正文 / 发件人 / 公司名 / 附件名（P3 扩展）。"""
        like = f"%{search}%"
        clause = (
            col(Message.subject).ilike(like)
            | col(Message.body_text).ilike(like)
            | col(Message.from_email).ilike(like)
            | col(Message.from_name).ilike(like)
        )
        # 公司名 / 域名命中
        company_ids = [
            c.id
            for c in self.session.exec(
                select(Company).where(
                    col(Company.name).ilike(like)
                    | cast(Company.domains, String).ilike(like)
                    | cast(Company.aliases, String).ilike(like)
                )
            ).all()
            if c.id is not None
        ]
        if company_ids:
            clause = clause | col(Message.company_id).in_(company_ids)
        # 附件名命中
        att_msg_ids = self.session.exec(
            select(Attachment.message_pk).where(col(Attachment.filename).ilike(like)).distinct()
        ).all()
        if att_msg_ids:
            clause = clause | col(Message.id).in_(list(att_msg_ids))
        return clause

    # --------------------------- 详情 ---------------------------
    def get_detail(self, message_id: int) -> MessageDetail | None:
        msg = self.session.get(Message, message_id)
        if msg is None:
            return None
        account = self.session.get(Account, msg.account_id)
        attachments = self.session.exec(
            select(Attachment).where(Attachment.message_pk == message_id)
        ).all()
        base = to_message_read(msg, account.email if account else None).model_dump()
        return MessageDetail(
            **base,
            to=msg.to_json or [],
            cc=msg.cc_json or [],
            body_text=msg.body_text,
            # 读取时消毒，老数据无需回填也能受益于后续规则更新
            body_html=sanitize_html(msg.body_html),
            attachments=[
                {
                    "filename": a.filename,
                    "size": a.size,
                    "mime": a.mime,
                    "is_inline": a.is_inline,
                }
                for a in attachments
            ],
            company_id=msg.company_id,
            message_id=msg.message_id,
        )

    # --------------------------- 批量操作 ---------------------------
    def mark_read(self, ids: list[int], value: bool = True) -> int:
        return self._bulk_update(ids, is_read=value)

    def star(self, ids: list[int], value: bool = True) -> int:
        return self._bulk_update(ids, is_starred=value)

    def archive(self, ids: list[int], value: bool = True) -> int:
        return self._bulk_update(ids, is_archived=value)

    def set_tag(self, ids: list[int], tag: str, value: bool = True) -> int:
        """批量添加 / 移除单个标签。"""
        if not ids or not tag:
            return 0
        rows = self.session.exec(select(Message).where(col(Message.id).in_(ids))).all()
        changed = 0
        for row in rows:
            tags = list(row.tags_json or [])
            if value and tag not in tags:
                tags.append(tag)
            elif not value and tag in tags:
                tags.remove(tag)
            else:
                continue
            row.tags_json = tags
            self.session.add(row)
            changed += 1
        self.session.commit()
        return changed

    def _bulk_update(self, ids: list[int], **fields) -> int:  # noqa: ANN003
        if not ids:
            return 0
        rows = self.session.exec(select(Message).where(col(Message.id).in_(ids))).all()
        for row in rows:
            for key, value in fields.items():
                setattr(row, key, value)
            self.session.add(row)
        self.session.commit()
        return len(rows)

    # --------------------------- 按筛选批量操作 / 删除 ---------------------------
    def apply_to_filter(self, action: str, value: bool = True, **filters) -> int:  # noqa: ANN003
        """对当前筛选条件下的全部邮件执行批量操作（不受列表分页限制）。"""
        conds = self._filter_conditions(**filters)
        if action == "delete":
            return self._delete_where(conds)
        fields = {"read": {"is_read": value}, "archive": {"is_archived": value}}.get(action)
        if fields is None:
            raise ValueError(f"不支持的批量动作：{action}")
        stmt = sa_update(Message).values(**fields)
        if conds:
            stmt = stmt.where(*conds)
        result = self.session.execute(stmt)
        self.session.commit()
        return result.rowcount or 0

    def delete(self, ids: list[int]) -> int:
        """删除本地邮件（只动本地库，不改动邮箱服务器）。"""
        if not ids:
            return 0
        return self._delete_where([col(Message.id).in_(ids)])

    def _delete_where(self, conds: list) -> int:  # noqa: ANN202
        """级联删除：附件 / 摘要 / 标签关联 → 邮件 → 落盘 .eml。

        顺序与账号删除保持一致，避免外键报错或残留孤儿行。
        """
        if conds:
            targets = select(Message.id).where(*conds)
            paths = self.session.exec(select(Message.raw_eml_path).where(*conds)).all()
            condition = sa_delete(Message).where(*conds)
        else:
            targets = select(Message.id)
            paths = self.session.exec(select(Message.raw_eml_path)).all()
            condition = sa_delete(Message)

        count = self.session.exec(
            select(func.count()).select_from(targets.subquery())
        ).one()

        self.session.exec(sa_delete(Attachment).where(Attachment.message_pk.in_(targets)))
        self.session.exec(sa_delete(Summary).where(Summary.message_pk.in_(targets)))
        self.session.exec(sa_delete(MessageLabel).where(MessageLabel.message_pk.in_(targets)))
        self.session.exec(condition)
        self.session.commit()

        self._unlink_eml(paths)
        logger.info(f"[MESSAGE] 已删除本地邮件 {count} 封")
        return count

    @staticmethod
    def _unlink_eml(paths: list) -> None:  # noqa: ANN001
        """删除这些邮件落盘的原始 .eml，避免磁盘残留。"""
        for path in paths:
            if not path:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - 极端文件系统异常
                logger.warning(f"[MESSAGE] 删除 .eml 失败：{exc}")

    # --------------------------- 转换 ---------------------------
    def _to_read(self, m: Message, account_email: str | None) -> MessageRead:
        return to_message_read(m, account_email)