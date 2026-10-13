"""增量同步服务。

核心保证：不重复、不漏、可断点续传。
- IMAP 增量：UIDVALIDITY + 最高已同步 UID（禁用序号）。
- UIDVALIDITY 变化 → 重建全量（旧邮件靠去重逻辑保留）。
- 去重：优先 Message-ID，其次 body_hash 指纹。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlmodel import Session, select

from app.config import settings
from app.core.logging import logger
from app.core.utils import body_fingerprint, utcnow
from app.models.account import Account
from app.models.message import Attachment, Message
from app.models.sync_state import SyncState
from app.services.classify_service import ClassifyService
from app.services.company_service import CompanyService
from app.services.providers.base import MailProvider, RawMessage
from app.services.providers.imap import ImapProvider
from app.services.summary_service import SummaryService


@dataclass
class SyncResult:
    account_id: int
    account_email: str
    folder: str
    fetched: int = 0
    inserted: int = 0
    skipped_dup: int = 0
    uidvalidity: int = 0
    last_uid: int = 0
    delta_token: str = ""
    rebuilt: bool = False
    status: str = "ok"
    error: str | None = None


def _effective_mode(account: Account) -> str:
    """决定实际使用的通道。

    采用 OAuth 的 provider（Gmail API / Graph）在已授权时走 API；
    未授权（无令牌）时回退 IMAP（应用专用密码），满足需求中的「可退化」。
    """
    mode = account.auth_mode or "imap"
    if mode in ("gmail_api", "graph"):
        return mode if account.oauth_token_enc else "imap"
    return mode


def default_provider_factory(account: Account, secret: str) -> MailProvider:
    """根据账号实际通道构造 provider（IMAP / Gmail API / Microsoft Graph）。"""
    mode = _effective_mode(account)
    if mode == "gmail_api":
        from app.services.providers.gmail import GmailProvider

        return GmailProvider(account, secret)
    if mode == "graph":
        from app.services.providers.graph import GraphProvider

        return GraphProvider(account, secret)
    return ImapProvider(account, secret)


class SyncService:
    """同步编排。provider 通过工厂注入，便于单元测试替换为 Fake。"""

    def __init__(self, session: Session, provider_factory=default_provider_factory) -> None:  # noqa: ANN001
        self.session = session
        self.provider_factory = provider_factory
        # 分类器：配置缓存于实例内，一次同步只加载一次规则
        self.classifier = ClassifyService(session)
        # 公司归并：域名 → 公司 / 联系人（P3）
        self.company = CompanyService(session)
        # 摘要：重要邮件入库即生成规则摘要（P4，不调用 LLM）
        self.summary = SummaryService(session)

    # --------------------------- 对外入口 ---------------------------
    def sync_account(self, account: Account, folder: str = "INBOX", full: bool = False) -> SyncResult:
        """同步单个账号的指定文件夹。"""
        secret = self._resolve_secret(account)
        if not secret:
            result = SyncResult(
                account_id=account.id or 0,
                account_email=account.email,
                folder=folder,
                status="error",
                error=self._missing_credential_message(account),
            )
            self._mark_error(account, result.error or "凭证缺失")
            return result

        account.last_sync_status = "syncing"
        self.session.add(account)
        self.session.commit()

        provider = self.provider_factory(account, secret)
        try:
            result = self._sync_folder(account, provider, folder, full=full)
            account.last_sync_status = "ok" if result.status == "ok" else "error"
            account.last_error = result.error
            account.last_sync_at = utcnow()
            account.unread_count = self._count_unread(account.id or 0)
            self.session.add(account)
            self.session.commit()
            logger.info(
                f"[SYNC] {account.email} {folder} 完成：UID区间→{result.last_uid}，"
                f"新增 {result.inserted}，重复跳过 {result.skipped_dup}，重建={result.rebuilt}"
            )
            return result
        except Exception as exc:  # noqa: BLE001 - 同步失败要落库并继续其它账号
            logger.exception(f"[SYNC] {account.email} {folder} 失败：{exc}")
            self._mark_error(account, str(exc))
            return SyncResult(
                account_id=account.id or 0,
                account_email=account.email,
                folder=folder,
                status="error",
                error=str(exc),
            )
        finally:
            provider.close()

    def sync_account_folders(
        self,
        account: Account,
        folders: list[str] | None = None,
        *,
        full: bool = False,
    ) -> list[SyncResult]:
        """同步账号的一组文件夹（默认由 provider 决定：收件箱 + 垃圾邮件）。

        单个文件夹失败不影响其它文件夹；最终状态按整体是否全部成功汇总。
        """
        secret = self._resolve_secret(account)
        if not secret:
            error = self._missing_credential_message(account)
            self._mark_error(account, error)
            return [
                SyncResult(
                    account_id=account.id or 0,
                    account_email=account.email,
                    folder="",
                    status="error",
                    error=error,
                )
            ]

        account.last_sync_status = "syncing"
        self.session.add(account)
        self.session.commit()

        provider = self.provider_factory(account, secret)
        results: list[SyncResult] = []
        try:
            targets = list(folders) if folders else provider.default_folders()
            for name in targets:
                try:
                    results.append(self._sync_folder(account, provider, name, full=full))
                except Exception as exc:  # noqa: BLE001 - 单夹失败不中断其它文件夹
                    logger.exception(f"[SYNC] {account.email} {name} 失败：{exc}")
                    results.append(
                        SyncResult(
                            account_id=account.id or 0,
                            account_email=account.email,
                            folder=name,
                            status="error",
                            error=str(exc),
                        )
                    )
            account.last_sync_status = "ok" if all(r.status == "ok" for r in results) else "error"
            account.last_error = next((r.error for r in results if r.error), None)
            account.last_sync_at = utcnow()
            account.unread_count = self._count_unread(account.id or 0)
            self.session.add(account)
            self.session.commit()
            logger.info(
                f"[SYNC] {account.email} 完成：{len(results)} 个文件夹（{targets}），"
                f"新增 {sum(r.inserted for r in results)}"
            )
            return results
        finally:
            provider.close()

    # --------------------------- 凭证解析 ---------------------------
    def _resolve_secret(self, account: Account) -> str:
        """按实际通道解析凭证：OAuth 用令牌包，IMAP 用授权码/应用密码。"""
        from app.core.crypto import decrypt

        if _effective_mode(account) in ("gmail_api", "graph"):
            return decrypt(account.oauth_token_enc)
        return decrypt(account.secret_enc)

    @staticmethod
    def _missing_credential_message(account: Account) -> str:
        mode = account.auth_mode or "imap"
        if mode in ("gmail_api", "graph"):
            if account.oauth_token_enc:
                return "OAuth 令牌解密失败，请重新点击『连接并授权』"
            return "尚未完成 OAuth 授权：请点击『连接并授权』，或填写应用专用密码改用 IMAP"
        return "凭证缺失或解密失败，请重新录入授权码"

    # --------------------------- 单文件夹同步 ---------------------------
    def _sync_folder(
        self,
        account: Account,
        provider: MailProvider,
        folder: str,
        *,
        full: bool,
    ) -> SyncResult:
        # 游标型 provider（Gmail historyId / Graph deltaLink）走独立路径
        if provider.supports_delta():
            return self._sync_delta(account, provider, folder, full=full)

        account_id = account.id or 0
        state = self._get_or_create_state(account_id, folder)

        uidvalidity, _exists = provider.folder_uidvalidity(folder)
        rebuilt = False

        # UIDVALIDITY 变化 → 重建全量
        if uidvalidity and uidvalidity != state.uidvalidity:
            if state.uidvalidity:
                logger.warning(
                    f"[SYNC] {account.email} {folder} UIDVALIDITY 变化 "
                    f"{state.uidvalidity} → {uidvalidity}，重建全量同步"
                )
                rebuilt = True
            state.uidvalidity = uidvalidity
            state.last_uid = 0
            state.initial_done = False
        if full:
            state.last_uid = 0
            state.initial_done = False
            rebuilt = True

        # 决定拉取起点
        initial = not state.initial_done
        start_uid: int | None = None
        since = None
        if initial:
            since = utcnow() - timedelta(days=settings.initial_sync_days)
        else:
            start_uid = state.last_uid + 1

        result = SyncResult(
            account_id=account_id,
            account_email=account.email,
            folder=folder,
            uidvalidity=uidvalidity,
            last_uid=state.last_uid,
            rebuilt=rebuilt,
        )

        batch_size = settings.fetch_batch_size
        while True:
            batch = list(
                provider.fetch_messages(
                    folder,
                    start_uid=start_uid,
                    since=since,
                    limit=batch_size,
                )
            )
            if not batch:
                break

            for raw in batch:
                inserted = self._store_message(account, raw, folder, uidvalidity)
                if inserted:
                    result.inserted += 1
                else:
                    result.skipped_dup += 1
            result.fetched += len(batch)

            new_max = max((r.uid for r in batch), default=state.last_uid)
            if new_max <= state.last_uid and not initial:
                # 无进展，避免死循环
                logger.warning(f"[SYNC] {account.email} {folder} UID 无进展，提前结束")
                break

            state.last_uid = max(state.last_uid, new_max)
            state.last_sync_at = utcnow()
            # 首次全量：第一页之后转为按 UID 续拉
            initial = False
            since = None
            start_uid = state.last_uid + 1
            self.session.add(state)
            self.session.commit()

            if len(batch) < batch_size:
                break

        state.initial_done = True
        state.uidvalidity = uidvalidity or state.uidvalidity
        state.last_sync_at = utcnow()
        state.last_error = None
        self.session.add(state)
        self.session.commit()

        result.last_uid = state.last_uid
        result.uidvalidity = state.uidvalidity
        return result

    # --------------------------- 游标型增量同步 ---------------------------
    def _sync_delta(
        self,
        account: Account,
        provider: MailProvider,
        folder: str,
        *,
        full: bool,
    ) -> SyncResult:
        """Gmail historyId / Graph deltaLink 增量。

        首次（无游标）按回看天数回填并记录游标；之后用游标只取变化项。
        游标失效时回退一次全量回填，并更新游标。
        """
        account_id = account.id or 0
        state = self._get_or_create_state(account_id, folder)

        cursor = None if full else (state.delta_token or None)
        rebuilt = bool(full and state.delta_token)
        result = SyncResult(
            account_id=account_id,
            account_email=account.email,
            folder=folder,
            rebuilt=rebuilt,
        )

        since = utcnow() - timedelta(days=settings.initial_sync_days) if cursor is None else None
        batch = provider.fetch_delta(
            folder, cursor=cursor, since=since, limit=settings.fetch_batch_size
        )

        if batch.needs_full_resync:
            logger.warning(f"[SYNC] {account.email} {folder} 增量游标已失效，回退全量回填")
            state.delta_token = ""
            state.initial_done = False
            self.session.add(state)
            self.session.commit()
            result.rebuilt = True
            since = utcnow() - timedelta(days=settings.initial_sync_days)
            batch = provider.fetch_delta(
                folder, cursor=None, since=since, limit=settings.fetch_batch_size
            )

        for raw in batch.messages:
            if self._store_message(account, raw, folder, 0):
                result.inserted += 1
            else:
                result.skipped_dup += 1
        result.fetched = len(batch.messages)

        if batch.cursor:
            state.delta_token = batch.cursor
        state.initial_done = True
        state.last_sync_at = utcnow()
        state.last_error = None
        self.session.add(state)
        self.session.commit()

        result.delta_token = state.delta_token
        logger.info(
            f"[SYNC] {account.email} {folder} 游标同步完成：拉取 {result.fetched}，"
            f"新增 {result.inserted}，重复 {result.skipped_dup}，"
            f"游标={result.delta_token[:24]}"
        )
        return result

    # --------------------------- 落库与去重 ---------------------------
    def _store_message(
        self,
        account: Account,
        raw: RawMessage,
        folder: str,
        uidvalidity: int,
    ) -> bool:
        """返回 True 表示新增，False 表示重复跳过。"""
        account_id = account.id or 0

        # 1) Message-ID 去重
        if raw.message_id:
            existing = self.session.exec(
                select(Message).where(
                    Message.account_id == account_id,
                    Message.message_id == raw.message_id,
                )
            ).first()
            if existing:
                return self._refresh_existing(existing, raw, folder)

        # 2) body_hash 指纹去重（无 Message-ID 时的兜底）
        fingerprint = body_fingerprint(
            raw.from_email, raw.subject, raw.date_utc, raw.body_text
        )
        dup = self.session.exec(
            select(Message).where(
                Message.account_id == account_id,
                Message.body_hash == fingerprint,
            )
        ).first()
        if dup:
            return self._refresh_existing(dup, raw, folder)

        eml_path = self._save_eml(account_id, raw, uidvalidity)

        # 入库即分类打分（规则优先，未配置时可回退内置默认规则）
        cls = self.classifier.classify_raw(raw, account.email)

        message = Message(
            account_id=account_id,
            uid=raw.uid,
            uidvalidity=uidvalidity,
            folder=folder,
            inference_classification=raw.inference_classification,
            message_id=raw.message_id,
            thread_id=raw.references[0] if raw.references else raw.in_reply_to,
            is_read=raw.is_read,
            is_starred=raw.is_starred,
            from_name=raw.from_name,
            from_email=raw.from_email,
            to_json=raw.to_list,
            cc_json=raw.cc_list,
            subject=raw.subject,
            body_text=raw.body_text,
            body_html=raw.body_html,
            date_utc=raw.date_utc,
            has_attachment=raw.has_attachment,
            importance_score=cls.importance_score,
            category=cls.category,
            tags_json=cls.tags,
            raw_eml_path=eml_path,
            body_hash=fingerprint,
        )
        self.session.add(message)
        self.session.flush()  # 取得自增 id

        for att in raw.attachments:
            self.session.add(
                Attachment(
                    message_pk=message.id or 0,
                    filename=att.filename,
                    size=att.size,
                    mime=att.mime,
                    content_id=att.content_id,
                    is_inline=att.is_inline,
                )
            )
        # 公司 / 联系人归并（域名 → 公司，多对一）
        self.company.link_message(message)
        # 重要邮件入库即生成规则摘要（进「摘要区」；不进 LLM）
        if cls.level == "important":
            try:
                self.summary.generate(message)
            except Exception as exc:  # noqa: BLE001 - 摘要失败不影响入库
                logger.warning(f"[SYNC] 生成规则摘要失败：{exc}")
        return True

    def _refresh_existing(self, existing: Message, raw: RawMessage, folder: str) -> bool:
        """命中已有邮件时只刷新少量可变字段（已读 / 焦点分区 / 文件夹），不新增记录。

        增量同步（尤其 Graph delta）会重复返回已入库的邮件；若不刷新，则「其他邮件」
        分区标记、已读状态等永远停留在首次入库时的值。返回 False 表示未新增。
        """
        changed = False
        if existing.is_read != raw.is_read:
            existing.is_read = raw.is_read
            changed = True
        if raw.inference_classification and existing.inference_classification != raw.inference_classification:
            existing.inference_classification = raw.inference_classification
            changed = True
        if folder and existing.folder != folder:
            existing.folder = folder
            changed = True
        if changed:
            self.session.add(existing)
            self.session.commit()
        return False

    @staticmethod
    def _save_eml(account_id: int, raw: RawMessage, uidvalidity: int) -> str | None:
        """原始邮件落盘为 .eml（超过大小上限则跳过，只存元数据）。"""
        if not raw.raw_bytes or len(raw.raw_bytes) > settings.max_eml_bytes:
            return None
        folder = settings.eml_dir / str(account_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uidvalidity}_{raw.uid}.eml"
        try:
            path.write_bytes(raw.raw_bytes)
        except OSError as exc:
            logger.warning(f"[SYNC] 写入 .eml 失败：{exc}")
            return None
        try:
            return str(path.relative_to(settings.data_dir.parent))
        except ValueError:
            return str(path)

    # --------------------------- 工具方法 ---------------------------
    def _get_or_create_state(self, account_id: int, folder: str) -> SyncState:
        state = self.session.exec(
            select(SyncState).where(
                SyncState.account_id == account_id,
                SyncState.folder == folder,
            )
        ).first()
        if state is None:
            state = SyncState(account_id=account_id, folder=folder)
            self.session.add(state)
            self.session.commit()
            self.session.refresh(state)
        return state

    def _count_unread(self, account_id: int) -> int:
        rows = self.session.exec(
            select(Message).where(
                Message.account_id == account_id,
                Message.is_read == False,  # noqa: E712 - SQL 比较需用 ==
                Message.is_archived == False,  # noqa: E712
            )
        ).all()
        return len(rows)

    def _mark_error(self, account: Account, error: str) -> None:
        account.last_sync_status = "error"
        account.last_error = error
        account.last_sync_at = utcnow()
        self.session.add(account)
        self.session.commit()