"""公司聚合服务（P3）。

核心职责：
1. 域名 → 公司归并：同一发件域名的邮件聚成一个「公司 / 客户」实体。
2. 公司名解析（优先级，可被界面覆盖）：
   a. 正文签名档中的公司名（中文后缀正则）
   b. 域名主词（abc-group.com → ABC Group）
   c. 发件人 display name（张三 | XX科技 → XX科技）
3. 联系人归并：email → contact → company（多对一，同域名多人同公司）。
4. 往来摘要：按时间线 / 联系人 / 分类 / 标签 / 附件 / 关键实体汇总。
5. 合并公司：把若干公司归并到主公司（域名、联系人、邮件一并转移）。

设计约束：全程只读邮件，本模块只做本地归并，不触发任何发信/写邮箱操作。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import String, cast
from sqlmodel import Session, col, func, select

from app.core.logging import logger
from app.core.utils import extract_domain
from app.models.account import Account
from app.models.company import Company, Contact
from app.models.message import Attachment, Message
from app.schemas.classify import CategoryCount, TagCount
from app.schemas.company import (
    AttachmentRead,
    CompanyCard,
    CompanyDetail,
    CompanySummary,
    CompanyUpdate,
    ContactRead,
)
from app.services.message_service import to_message_read

# --------------------------- 公司名解析 ---------------------------
# 中文公司后缀词（签名档识别用）
# 「修饰词」+「终缀」拼装，可覆盖 有限公司 / 集团有限公司 / 科技有限公司 等组合
_COMPANY_MOD = (
    "集团|控股|股份|有限|责任|科技|网络|信息|技术|传媒|物流|地产|置业|"
    "文化|电子|实业|国际|发展|投资|贸易|建设|工程|医药|生物|教育|食品|机械|"
    "事务|管理|咨询|服务|设计|制造"
)
_COMPANY_TAIL_TOKENS = (
    "集团|控股|股份|有限|责任|公司|事务所|研究院|研究所|银行|证券|保险|"
    "大学|学院|医院|工作室|中心|商行|超市|酒店|大药房|管理|咨询|服务|设计"
)
_RE_COMPANY_CN = re.compile(
    rf"[\u4e00-\u9fa5A-Za-z0-9（）()·&\-\u3000 ]{{2,32}}?(?:{_COMPANY_TAIL_TOKENS}){{1,6}}"
)
_RE_MOD = re.compile(_COMPANY_MOD)
# 终缀（不含「公司」：「公司」需带修饰词才算，避免「欢迎光临本公司」之类误判）
_COMPANY_TERMINALS = (
    "集团", "事务所", "研究院", "研究所", "银行", "证券", "保险", "大学", "学院",
    "医院", "工作室", "中心", "商行", "超市", "酒店", "大药房", "管理", "咨询", "服务", "设计",
)
# 签名档常见提示词（出现后更可能是签名区）
_RE_SIGN_HINT = re.compile(r"^(--|——|—|此致|敬礼|顺颂|商祺|祝|发自我的|sent from)", re.IGNORECASE)
# 从 display name 判断公司线索
_RE_COMPANY_HINT = re.compile(r"公司|集团|有限|科技|网络|信息|技术|银行|事务所|研究院|中心|工作室|inc|ltd|llc|corp|group", re.IGNORECASE)

# 二级公共后缀（用于域名主词归并：corp.com.cn → corp）
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "mil"}
_SIGNATURE_TAIL_LINES = 20


def domain_to_company_name(domain: str) -> str:
    """域名主词 → 展示名。abc-group.com → ABC Group。"""
    if not domain:
        return ""
    parts = [p for p in domain.lower().split(".") if p]
    if not parts:
        return ""
    parts = parts[:-1]  # 去掉顶级域
    if not parts:
        return domain.title()
    if len(parts) >= 2 and parts[-1] in _SECOND_LEVEL and len(parts[-1]) <= 3:
        parts = parts[:-1]  # 去掉 co.uk / com.cn 之类二级后缀
    word = parts[-1] if parts else domain
    tokens = [t for t in re.split(r"[-_.]", word) if t]
    return " ".join(t.upper() if len(t) <= 3 else t.capitalize() for t in tokens) or domain


def _company_from_display_name(name: str) -> str:
    """从发件人 display name 提取公司名（张三 | XX科技 → XX科技）。"""
    if not name:
        return ""
    for sep in ("|", "｜", "·", "－", "|"):
        if sep in name:
            segs = [s.strip() for s in name.split(sep) if s.strip()]
            for seg in reversed(segs):
                if _RE_COMPANY_HINT.search(seg):
                    return seg
            return segs[-1] if segs else ""
    if _RE_COMPANY_HINT.search(name):
        return name.strip()
    return ""


def _valid_company_token(token: str) -> bool:
    """校验候选串确实是公司名（避免「没有公司」之类误判）。"""
    token = (token or "").strip()
    if len(token) < 3:
        return False
    if token.endswith("公司"):
        return bool(_RE_MOD.search(token[:-2]))
    return any(token.endswith(t) and len(token) > len(t) for t in _COMPANY_TERMINALS)


def extract_signature_company(body_text: str) -> str:
    """从正文签名档（末尾若干行）抽取公司名；无则返回空串。"""
    if not body_text:
        return ""
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-_SIGNATURE_TAIL_LINES:]
    found = ""
    for line in tail:
        best = ""
        for m in _RE_COMPANY_CN.finditer(line[:64]):
            token = m.group(0).strip()
            if _valid_company_token(token) and len(token) > len(best):
                best = token
        if best:
            found = best
    return found.strip()


def resolve_company_name(domain: str, from_name: str, body_text: str) -> tuple[str, str]:
    """公司名解析，返回 (来源, 名称)。来源：signature | domain | display_name。"""
    sig = extract_signature_company(body_text)
    if sig:
        return "signature", sig
    dn = domain_to_company_name(domain)
    if dn:
        return "domain", dn
    nm = _company_from_display_name(from_name)
    if nm:
        return "display_name", nm
    return "domain", domain or "未知公司"


def _default_names_for(domain: str) -> set[str]:
    out = {domain}
    pretty = domain_to_company_name(domain)
    if pretty:
        out.add(pretty)
    return out


# --------------------------- 往来摘要正则 ---------------------------
_RE_AMOUNT = re.compile(r"[¥￥$]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:元|美元)")
_RE_ORDER = re.compile(r"\b[A-Z]{2,}\d{6,}\b")
_RE_DEADLINE = re.compile(r"截止|deadline|前提供|有效期至", re.IGNORECASE)
_RE_CAPTCHA = re.compile(r"\b\d{4,8}\b")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# --------------------------- 结果对象 ---------------------------
@dataclass(slots=True)
class CompanyMatch:
    company: Company | None = None
    contact: Contact | None = None
    created_company: bool = False
    created_contact: bool = False


class CompanyService:
    """公司聚合编排。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._domain_cache: dict[str, Company] = {}

    # ==================================================================
    # 一、归并：域名 → 公司 → 联系人
    # ==================================================================
    def _find_company_by_domain(self, domain: str) -> Company | None:
        if domain in self._domain_cache:
            return self._domain_cache[domain]
        for company in self.session.exec(select(Company)).all():
            for d in company.domains or []:
                self._domain_cache[(d or "").lower()] = company
        return self._domain_cache.get(domain)

    def get_or_create_company_by_domain(self, domain: str, name_hint: str = "") -> tuple[Company, bool]:
        """按域名取公司；不存在则新建。返回 (公司, 是否新建)。"""
        domain = (domain or "").strip().lower()
        if not domain:
            raise ValueError("域名不能为空")

        existing = self._find_company_by_domain(domain)
        if existing is not None:
            # 若现有名还是域名默认名，且新线索更具体 → 升级展示名并保留旧名作别名
            if name_hint and name_hint != existing.name and existing.name in _default_names_for(domain):
                existing.aliases = list({*(existing.aliases or []), existing.name})
                existing.name = name_hint
                self.session.add(existing)
            return existing, False

        resolved = name_hint or domain_to_company_name(domain) or domain
        company = Company(name=resolved, domains=[domain])
        self.session.add(company)
        self.session.flush()
        self._domain_cache[domain] = company
        return company, True

    def get_or_create_contact(self, email: str, name: str = "", company: Company | None = None) -> tuple[Contact | None, bool]:
        """按邮箱取联系人；不存在则新建，并归属到公司。"""
        email = (email or "").strip().lower()
        if not email:
            return None, False
        contact = self.session.exec(select(Contact).where(Contact.email == email)).first()
        if contact is None:
            contact = Contact(
                email=email,
                name=name or "",
                company_id=company.id if company else None,
            )
            self.session.add(contact)
            self.session.flush()
            return contact, True
        if name and not contact.name:
            contact.name = name
        if company and contact.company_id != company.id:
            contact.company_id = company.id
        self.session.add(contact)
        return contact, False

    def resolve(self, *, from_email: str, from_name: str = "", body_text: str = "") -> CompanyMatch:
        """为一封邮件解析出公司 / 联系人（不落库绑定，交由调用方写回）。"""
        domain = extract_domain(from_email)
        company: Company | None = None
        contact: Contact | None = None
        created_c = created_ct = False

        if domain:
            _, hint = resolve_company_name(domain, from_name, body_text)
            company, created_c = self.get_or_create_company_by_domain(domain, hint)
        if from_email:
            contact, created_ct = self.get_or_create_contact(from_email, from_name, company)

        return CompanyMatch(company, contact, created_c, created_ct)

    # ==================================================================
    # 二、写回邮件 + 维护统计
    # ==================================================================
    def link_message(self, message: Message) -> CompanyMatch:
        """同步落库后调用：回填 company_id / contact_id 并累加统计。"""
        match = self.resolve(
            from_email=message.from_email,
            from_name=message.from_name,
            body_text=message.body_text,
        )
        self._apply(message, match, accumulate=True)
        self.session.add(message)
        return match

    def _apply(self, message: Message, match: CompanyMatch, *, accumulate: bool) -> None:
        if match.company is not None:
            message.company_id = match.company.id
            if accumulate:
                match.company.message_count = (match.company.message_count or 0) + 1
                if message.date_utc and (
                    match.company.last_message_at is None or message.date_utc > match.company.last_message_at
                ):
                    match.company.last_message_at = message.date_utc
            self.session.add(match.company)
        if match.contact is not None:
            message.contact_id = match.contact.id
            if accumulate:
                match.contact.message_count = (match.contact.message_count or 0) + 1
            self.session.add(match.contact)

    # ==================================================================
    # 三、重跑归并（历史邮件 / 规则变更后）
    # ==================================================================
    def reaggregate(self) -> int:
        """清空并重建全部邮件 → 公司 / 联系人 的绑定与统计，返回处理邮件数。"""
        messages = self.session.exec(
            select(Message).order_by(col(Message.date_utc))
        ).all()
        self._domain_cache.clear()

        for m in messages:
            m.company_id = None
            m.contact_id = None
        for company in self.session.exec(select(Company)).all():
            company.message_count = 0
            company.last_message_at = None
        for contact in self.session.exec(select(Contact)).all():
            contact.message_count = 0
        self.session.flush()

        for m in messages:
            match = self.resolve(
                from_email=m.from_email,
                from_name=m.from_name,
                body_text=m.body_text,
            )
            self._apply(m, match, accumulate=True)
        self.session.commit()
        logger.info(f"[COMPANY] 重跑归并完成：处理 {len(messages)} 封邮件")
        return len(messages)

    def reaggregate_if_needed(self) -> int:
        """仅在检测到「有发件人但未归并」的邮件时才重跑（用于启动回填）。"""
        pending = self.session.exec(
            select(func.count())
            .select_from(Message)
            .where(col(Message.company_id).is_(None), col(Message.from_email) != "")
        ).one()
        if pending:
            return self.reaggregate()
        return 0

    # ==================================================================
    # 四、查询：公司列表 / 详情 / 往来摘要
    # ==================================================================
    def _unread_by_company(self) -> dict[int, int]:
        rows = self.session.exec(
            select(Message.company_id, func.count())
            .where(
                Message.is_read == False,  # noqa: E712
                Message.is_archived == False,  # noqa: E712
                col(Message.company_id).is_not(None),
            )
            .group_by(Message.company_id)
        ).all()
        return {int(cid): int(n) for cid, n in rows if cid is not None}

    @staticmethod
    def _to_card(company: Company, unread: int = 0) -> CompanyCard:
        return CompanyCard(
            id=company.id or 0,
            name=company.name,
            domains=list(company.domains or []),
            aliases=list(company.aliases or []),
            logo=company.logo,
            industry=company.industry,
            notes=company.notes,
            starred=company.starred,
            message_count=company.message_count or 0,
            unread=unread,
            last_message_at=company.last_message_at,
        )

    def list_companies(
        self,
        *,
        starred_only: bool = False,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CompanyCard]:
        stmt = select(Company)
        if starred_only:
            stmt = stmt.where(Company.starred == True)  # noqa: E712
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                col(Company.name).ilike(like)
                | cast(Company.domains, String).ilike(like)
                | cast(Company.aliases, String).ilike(like)
            )
        stmt = stmt.order_by(
            col(Company.starred).desc(),
            col(Company.last_message_at).desc(),
            col(Company.message_count).desc(),
        )
        rows = self.session.exec(stmt.offset(offset).limit(limit)).all()
        unread = self._unread_by_company()
        return [self._to_card(c, unread.get(c.id or 0, 0)) for c in rows]

    def get_detail(self, company_id: int, message_limit: int = 300) -> CompanyDetail | None:
        company = self.session.get(Company, company_id)
        if company is None:
            return None

        messages = self.session.exec(
            select(Message)
            .where(Message.company_id == company_id)
            .order_by(col(Message.date_utc).desc())
            .limit(message_limit)
        ).all()
        emails = {a.id: a.email for a in self.session.exec(select(Account)).all()}
        message_reads = [to_message_read(m, emails.get(m.account_id)) for m in messages]

        contacts = self.session.exec(
            select(Contact)
            .where(Contact.company_id == company_id)
            .order_by(col(Contact.message_count).desc())
        ).all()

        msg_ids = [m.id for m in messages if m.id is not None]
        attachment_rows = (
            self.session.exec(select(Attachment).where(col(Attachment.message_pk).in_(msg_ids))).all()
            if msg_ids
            else []
        )
        attachments = [
            AttachmentRead(
                filename=a.filename,
                size=a.size,
                mime=a.mime,
                message_id=a.message_pk,
            )
            for a in attachment_rows
        ]

        summary = self._build_summary(
            company, messages, contacts, attachment_rows
        )
        unread = sum(1 for m in messages if not m.is_read and not m.is_archived)
        return CompanyDetail(
            company=self._to_card(company, unread),
            contacts=[
                ContactRead(id=c.id or 0, email=c.email, name=c.name, message_count=c.message_count or 0)
                for c in contacts
            ],
            messages=message_reads,
            attachments=attachments,
            summary=summary,
        )

    def _build_summary(
        self,
        company: Company,
        messages: list[Message],
        contacts: list[Contact],
        attachments: list[Attachment],
    ) -> CompanySummary:
        total = len(messages)
        unread = sum(1 for m in messages if not m.is_read and not m.is_archived)
        dates = [m.date_utc for m in messages if m.date_utc]
        first_at = min(dates) if dates else None
        last_at = max(dates) if dates else None

        cat_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        amounts: list[str] = []
        orders: list[str] = []
        deadlines = 0
        for m in messages:
            cat = m.category or "其他"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            for t in m.tags_json or []:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            body = m.body_text or ""
            for hit in _RE_AMOUNT.findall(body)[:1]:
                if hit and hit not in amounts:
                    amounts.append(hit)
            for hit in _RE_ORDER.findall(body)[:1]:
                if hit and hit not in orders:
                    orders.append(hit)
            if _RE_DEADLINE.search(body):
                deadlines += 1

        categories = [CategoryCount(name=k, count=v) for k, v in sorted(cat_counts.items(), key=lambda kv: -kv[1])]
        tags = [TagCount(name=k, count=v) for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])]

        if total == 0:
            text = f"「{company.name}」暂无往来邮件。"
        else:
            parts = [f"与「{company.name}」共往来 {total} 封邮件" + (f"，其中未读 {unread} 封" if unread else "")]
            if first_at and last_at:
                parts.append(f"时间跨度 {_fmt_dt(first_at)} ~ {_fmt_dt(last_at)}")
            top_contacts = [c for c in contacts if (c.message_count or 0) > 0][:3]
            if top_contacts:
                parts.append("主要联系人：" + "、".join(f"{c.name or c.email}（{c.message_count}）" for c in top_contacts))
            if categories and categories[0].count:
                parts.append(f"邮件以「{categories[0].name}」为主（{categories[0].count} 封）")
            if attachments:
                parts.append(f"含附件 {len(attachments)} 个")
            if amounts:
                parts.append("涉及金额 " + "、".join(amounts[:3]))
            if orders:
                parts.append("订单号 " + "、".join(orders[:3]))
            if deadlines:
                parts.append(f"含截止/时限提示 {deadlines} 封")
            if tags:
                parts.append("标签：" + "、".join(f"{t.name}×{t.count}" for t in tags[:4]))
            text = "。".join(parts) + "。"

        return CompanySummary(
            text=text,
            total=total,
            unread=unread,
            first_at=first_at,
            last_at=last_at,
            contacts=[
                ContactRead(id=c.id or 0, email=c.email, name=c.name, message_count=c.message_count or 0)
                for c in contacts
            ],
            categories=categories,
            tags=tags,
            key_entities={"amounts": amounts[:8], "orders": orders[:8], "deadlines": deadlines},
            attachment_count=len(attachments),
        )

    # ==================================================================
    # 五、编辑 / 合并
    # ==================================================================
    def update(self, company_id: int, payload: CompanyUpdate) -> Company | None:
        company = self.session.get(Company, company_id)
        if company is None:
            return None
        data = payload.model_dump(exclude_unset=True)
        # 允许空串清空，但主名称不能为空
        if data.get("name") is not None and not str(data["name"]).strip():
            data.pop("name")
        if data.get("domains") is not None:
            data["domains"] = [d.strip().lower() for d in data["domains"] if d and d.strip()]
        for key, value in data.items():
            setattr(company, key, value)
        self.session.add(company)
        self.session.commit()
        self.session.refresh(company)
        self._domain_cache.clear()
        return company

    def merge(self, primary_id: int, merge_ids: Iterable[int]) -> Company | None:
        """把 merge_ids 中的公司并入 primary_id（域名 / 别名 / 联系人 / 邮件全部转移）。"""
        primary = self.session.get(Company, primary_id)
        if primary is None:
            return None
        merged = 0
        for mid in merge_ids:
            if mid == primary_id:
                continue
            other = self.session.get(Company, mid)
            if other is None:
                continue
            primary.domains = list(dict.fromkeys([*(primary.domains or []), *(other.domains or [])]))
            primary.aliases = list(
                dict.fromkeys([*(primary.aliases or []), *(other.aliases or []), other.name])
            )
            if not primary.notes and other.notes:
                primary.notes = other.notes
            if not primary.industry and other.industry:
                primary.industry = other.industry
            primary.starred = primary.starred or other.starred

            for m in self.session.exec(select(Message).where(Message.company_id == mid)).all():
                m.company_id = primary_id
                self.session.add(m)
            for ct in self.session.exec(select(Contact).where(Contact.company_id == mid)).all():
                ct.company_id = primary_id
                self.session.add(ct)
            self.session.delete(other)
            merged += 1

        self.session.add(primary)
        self.session.commit()
        self._refresh_company_stats(primary_id)
        self._domain_cache.clear()
        logger.info(f"[COMPANY] 合并公司：{primary_id} ← {merged} 个")
        return self.session.get(Company, primary_id)

    def _refresh_company_stats(self, company_id: int) -> None:
        company = self.session.get(Company, company_id)
        if company is None:
            return
        messages = self.session.exec(select(Message).where(Message.company_id == company_id)).all()
        company.message_count = len(messages)
        dates = [m.date_utc for m in messages if m.date_utc]
        company.last_message_at = max(dates) if dates else None
        self.session.add(company)

        contacts = self.session.exec(select(Contact).where(Contact.company_id == company_id)).all()
        for ct in contacts:
            ct.message_count = len(
                self.session.exec(select(Message).where(Message.contact_id == ct.id)).all()
            )
            self.session.add(ct)
        self.session.commit()