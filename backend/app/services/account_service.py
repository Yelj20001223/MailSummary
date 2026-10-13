"""账号管理服务：创建 / 更新 / 删除 / 连通性测试。"""
from __future__ import annotations

import shutil

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, func, select

from app.config import settings
from app.core.crypto import encrypt
from app.core.logging import logger
from app.models.account import Account
from app.models.company import Company, Contact
from app.models.label import MessageLabel
from app.models.message import Attachment, Message
from app.models.summary import Summary
from app.models.sync_state import SyncState
from app.schemas.account import PROVIDER_PRESETS, AccountCreate, AccountTestRequest, AccountUpdate
from app.services.providers.base import ProviderError
from app.services.providers.imap import ImapProvider


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_accounts(self) -> list[Account]:
        return list(self.session.exec(select(Account).order_by(Account.id)).all())

    def get(self, account_id: int) -> Account | None:
        return self.session.get(Account, account_id)

    def create(self, payload: AccountCreate) -> Account:
        preset = PROVIDER_PRESETS[payload.provider]
        account = Account(
            name=payload.name,
            email=str(payload.email),
            provider=payload.provider,
            auth_mode=preset.auth_mode,
            imap_host=payload.imap_host or preset.imap_host,
            imap_port=payload.imap_port or preset.imap_port,
            use_ssl=preset.use_ssl if payload.use_ssl is None else payload.use_ssl,
            username=payload.username or str(payload.email),
            secret_enc=encrypt(payload.secret),
            color=payload.color or self._pick_color(),
        )
        self.session.add(account)
        self.session.commit()
        self.session.refresh(account)
        logger.info(f"[ACCOUNT] 新增账号 {account.email} (provider={account.provider})")
        return account

    def update(self, account: Account, payload: AccountUpdate) -> Account:
        if payload.name is not None:
            account.name = payload.name
        if payload.color is not None:
            account.color = payload.color
        if payload.is_active is not None:
            account.is_active = payload.is_active
        if payload.secret:
            account.secret_enc = encrypt(payload.secret)
        self.session.add(account)
        self.session.commit()
        self.session.refresh(account)
        return account

    def delete(self, account: Account) -> None:
        """删除账号并级联清理其全部关联数据。

        顺序：先删从属行（附件 / 摘要 / 标签关联）→ 邮件 → 同步状态 → 账号本身，
        再清理该账号落盘的原始邮件目录，最后重跑公司归并移除空公司，
        避免外键约束报错或残留孤儿数据。
        """
        account_id = account.id
        email = account.email

        count = self.session.exec(
            select(func.count()).select_from(Message).where(Message.account_id == account_id)
        ).one()
        # 子表用子查询定位，避免把大量 message id 塞进 IN 子句
        msg_ids = select(Message.id).where(Message.account_id == account_id)
        self.session.exec(sa_delete(Attachment).where(Attachment.message_pk.in_(msg_ids)))
        self.session.exec(sa_delete(Summary).where(Summary.message_pk.in_(msg_ids)))
        self.session.exec(sa_delete(MessageLabel).where(MessageLabel.message_pk.in_(msg_ids)))
        self.session.exec(sa_delete(Message).where(Message.account_id == account_id))
        self.session.exec(sa_delete(SyncState).where(SyncState.account_id == account_id))
        self.session.delete(account)
        self.session.commit()

        self._remove_eml_dir(account_id)
        self._drop_empty_companies()
        logger.info(f"[ACCOUNT] 删除账号 {email}（连带清理邮件 {count} 封）")

    @staticmethod
    def _remove_eml_dir(account_id: int | None) -> None:
        """删除该账号落盘的原始邮件目录（不存在则忽略）。"""
        if account_id is None:
            return
        folder = settings.eml_dir / str(account_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def _drop_empty_companies(self) -> None:
        """重跑公司归并刷新统计，并移除已无任何邮件的空公司（保留用户加星的）。"""
        from app.services.company_service import CompanyService  # 局部导入，避免循环依赖

        CompanyService(self.session).reaggregate()
        empty_ids = [
            c.id for c in self.session.exec(select(Company)).all()
            if c.id is not None and not c.starred and (c.message_count or 0) == 0
        ]
        if empty_ids:
            self.session.exec(sa_delete(Contact).where(Contact.company_id.in_(empty_ids)))
            self.session.exec(sa_delete(Company).where(Company.id.in_(empty_ids)))
            self.session.commit()

    @staticmethod
    def _pick_color() -> str:
        palette = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
        return palette[len(palette) % 7] if palette else "#4f46e5"


def test_account(req: AccountTestRequest) -> tuple[bool, str]:
    """不落库，直接构造临时账号测试连通性（供『测试连接』按钮使用）。"""
    preset = PROVIDER_PRESETS.get(req.provider)
    if preset is None:
        return False, f"不支持的 provider：{req.provider}"

    temp = Account(
        name="test",
        email=str(req.email),
        provider=req.provider,
        auth_mode=preset.auth_mode,
        imap_host=req.imap_host or preset.imap_host,
        imap_port=req.imap_port or preset.imap_port,
        use_ssl=preset.use_ssl if req.use_ssl is None else req.use_ssl,
        username=req.username or str(req.email),
    )
    try:
        if preset.auth_mode != "imap":
            return False, (
                f"{preset.label} 推荐使用 OAuth2：请先保存账号，再在账号列表点击『连接并授权』；"
                "如需走 IMAP 降级，请填写应用专用密码后重试"
            )
        provider = ImapProvider(temp, req.secret)
        return provider.test_connection()
    except ProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"测试异常：{exc}"