"""邮件分类与打分服务（P2）。

设计原则：规则优先、可由界面编辑、无需改代码。
- 关键词评分规则存放在 `rules` 表（「规则配置」页可视化编辑）。
- 白名单 / 阈值 / 权重存放在 `settings` 表。
- 数据库尚无规则时回退到 `DEFAULT_RULES`，保证开箱即用（也让单元测试无需预置数据）。

评分（对应需求「第四节 分类维度」）：
  +5  发件人 / 域名命中重要联系人白名单
  +4  主题含 合同/协议/法务/律师函
  +3  主题含 发票/付款/报价/订单/对账/账单
  +3  主题含 验证码/登录/安全/告警
  +3  主题含 面试/招聘/offer
  +2  正文含 金额 / 订单号 / 截止日期
  +2  是回复邮件且收件人是我
  +1  收件人是我 或 正文 @我
  +1  含 PDF / Excel / Word 附件
  >=7 重要；4-6 待办；<4 普通
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from sqlmodel import Session, select

from app.core.utils import extract_domain
from app.models.account import Account
from app.models.category import Category
from app.models.message import Attachment, Message
from app.models.rule import Rule, Setting
from app.services.providers.base import RawMessage

# --------------------------- 枚举（与需求一一对应） ---------------------------
CATEGORIES: list[str] = [
    "合同法务",
    "财务发票",
    "订单采购",
    "人事招聘",
    "客户往来",
    "系统通知",
    "验证码安全",
    "市场广告",
    "社交",
    "其他",
]

TAGS: list[str] = ["待回复", "待付款", "合同", "发票", "招聘", "通知", "广告", "垃圾"]

CATEGORY_OTHER = "其他"

# 设置键名
KEY_WHITELIST = "important_senders"
KEY_WEIGHTS = "classify_weights"
KEY_THRESHOLDS = "classify_thresholds"

DEFAULT_WHITELIST: list[str] = []
DEFAULT_WEIGHTS: dict[str, int] = {
    "whitelist": 5,
    "entities": 2,
    "reply": 2,
    "to_me": 1,
    "attachment": 1,
}
DEFAULT_THRESHOLDS: dict[str, int] = {"important": 7, "todo": 4}

# 内置默认规则（首次启动会写入 rules 表供界面编辑）
DEFAULT_RULES: list[dict] = [
    {
        "name": "合同法务",
        "field": "subject",
        "op": "in_list",
        "value": "合同,协议,法务,律师函",
        "score": 4,
        "tag": "合同",
        "category": "合同法务",
    },
    {
        "name": "财务订单",
        "field": "subject",
        "op": "in_list",
        "value": "发票,付款,报价,订单,对账,账单",
        "score": 3,
        "tag": "发票",
        "category": "财务发票",
    },
    {
        "name": "安全通知",
        "field": "subject",
        "op": "in_list",
        "value": "验证码,登录,安全,告警",
        "score": 3,
        "tag": "通知",
        "category": "验证码安全",
    },
    {
        "name": "人事招聘",
        "field": "subject",
        "op": "in_list",
        "value": "面试,招聘,offer",
        "score": 3,
        "tag": "招聘",
        "category": "人事招聘",
    },
    {
        "name": "市场广告",
        "field": "subject",
        "op": "in_list",
        "value": "促销,优惠,折扣,活动,推广,广告",
        "score": 0,
        "tag": "广告",
        "category": "市场广告",
    },
    {
        "name": "系统通知发件人",
        "field": "from",
        "op": "in_list",
        "value": "noreply,no-reply,notification,mailer-daemon",
        "score": 0,
        "tag": "通知",
        "category": "系统通知",
    },
]

# --------------------------- 正文实体正则 ---------------------------
_RE_AMOUNT = re.compile(r"[¥￥$]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:元|美元)")
_RE_ORDER = re.compile(r"\b[A-Z]{2,}\d{6,}\b")
_RE_DEADLINE = re.compile(r"截止|deadline|前提供|有效期至|请于.*前", re.IGNORECASE)
_RE_REPLY_ASK = re.compile(r"请回复|请确认|请答复|请反馈|请尽快|前提供|需求确认|期待回复")
_RE_PAY_ASK = re.compile(r"付款|账单|对账|待付|汇款|转账")

# 附件类型判断（PDF / Excel / Word）
_OFFICE_MIME_HINTS = ("pdf", "excel", "spreadsheet", "word", "msword", "wordprocessingml")
_OFFICE_EXT = (".pdf", ".xls", ".xlsx", ".csv", ".doc", ".docx")


def _is_office_attachment(mime: str, filename: str) -> bool:
    mime_l = (mime or "").lower()
    name_l = (filename or "").lower()
    if any(hint in mime_l for hint in _OFFICE_MIME_HINTS):
        return True
    return name_l.endswith(_OFFICE_EXT)


# --------------------------- 配置与结果 ---------------------------
@dataclass(slots=True)
class RuleSpec:
    name: str
    field: str
    op: str
    value: str
    score: int
    tag: str | None = None
    category: str | None = None
    enabled: bool = True
    id: int | None = None


@dataclass(slots=True)
class ClassifyConfig:
    rules: list[RuleSpec] = field(default_factory=list)
    whitelist: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))


@dataclass(slots=True)
class ClassifyInput:
    """协议无关的分类输入（同步与重新分类共用）。"""

    subject: str = ""
    body_text: str = ""
    from_email: str = ""
    from_name: str = ""
    to_emails: list[str] = field(default_factory=list)
    is_reply: bool = False
    attachment_names: list[str] = field(default_factory=list)
    attachment_mimes: list[str] = field(default_factory=list)
    account_email: str | None = None

    @property
    def domain(self) -> str:
        return extract_domain(self.from_email)

    @property
    def to_me(self) -> bool:
        if not self.account_email:
            return False
        target = self.account_email.strip().lower()
        return any((e or "").strip().lower() == target for e in self.to_emails)

    @property
    def has_office_attachment(self) -> bool:
        pairs = list(zip(self.attachment_mimes, self.attachment_names))
        return any(_is_office_attachment(m, n) for m, n in pairs)


@dataclass(slots=True)
class ClassifyResult:
    importance_score: int
    category: str
    tags: list[str]
    level: str = "normal"  # important | todo | normal
    matched_rules: list[str] = field(default_factory=list)


# --------------------------- 引擎 ---------------------------
class ClassifyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._config: ClassifyConfig | None = None

    # ---------- 配置加载 ----------
    def load_config(self, *, refresh: bool = False) -> ClassifyConfig:
        if self._config is not None and not refresh:
            return self._config

        rules = self.session.exec(select(Rule).where(Rule.enabled == True)).all()  # noqa: E712
        if rules:
            specs = [
                RuleSpec(
                    id=r.id,
                    name=r.name,
                    field=r.field,
                    op=r.op,
                    value=r.value,
                    score=r.score,
                    tag=r.tag,
                    category=r.category,
                    enabled=r.enabled,
                )
                for r in rules
            ]
        else:
            # 回退内置默认规则（未初始化或用户清空时仍可用）
            specs = [RuleSpec(**d) for d in DEFAULT_RULES]

        self._config = ClassifyConfig(
            rules=specs,
            whitelist=self._get_setting(KEY_WHITELIST, DEFAULT_WHITELIST),
            weights={**DEFAULT_WEIGHTS, **self._get_setting(KEY_WEIGHTS, {})},
            thresholds={**DEFAULT_THRESHOLDS, **self._get_setting(KEY_THRESHOLDS, {})},
        )
        return self._config

    def _get_setting(self, key: str, default):  # noqa: ANN001, ANN202
        setting = self.session.exec(select(Setting).where(Setting.key == key)).first()
        if setting is None:
            return default
        if setting.value_json is not None:
            return setting.value_json
        if setting.value:
            # 逗号分隔的纯文本也接受
            if isinstance(default, list):
                return [s.strip() for s in setting.value.split(",") if s.strip()]
            return setting.value
        return default

    # ---------- 分类 ----------
    def classify(self, data: ClassifyInput, config: ClassifyConfig | None = None) -> ClassifyResult:
        cfg = config or self.load_config()
        score = 0
        tags: set[str] = set()
        category_candidates: list[tuple[int, str]] = []
        matched: list[str] = []

        # 1) 关键词规则
        for rule in cfg.rules:
            if not rule.enabled:
                continue
            if _rule_matches(rule, data):
                score += rule.score
                matched.append(rule.name)
                if rule.tag:
                    tags.add(rule.tag)
                if rule.category:
                    category_candidates.append((rule.score, rule.category))

        # 2) 白名单（发件人或域名）
        if _whitelist_hit(cfg.whitelist, data):
            score += cfg.weights.get("whitelist", 5)
            matched.append("重要联系人白名单")

        # 3) 正文实体（金额 / 订单号 / 截止日期）
        if _has_entities(data.body_text):
            score += cfg.weights.get("entities", 2)
            matched.append("正文实体")

        # 4) 回复邮件且收件人是我
        if data.is_reply and data.to_me:
            score += cfg.weights.get("reply", 2)
            matched.append("回复并按我")

        # 5) 收件人是我 或 正文 @我
        if data.to_me or "@我" in data.body_text:
            score += cfg.weights.get("to_me", 1)

        # 6) 含 Office 附件
        if data.has_office_attachment:
            score += cfg.weights.get("attachment", 1)

        # 派生标签
        if _RE_REPLY_ASK.search(data.body_text) or _RE_REPLY_ASK.search(data.subject):
            tags.add("待回复")
        if _RE_PAY_ASK.search(data.body_text):
            tags.add("待付款")

        score = max(0, score)
        category = self._resolve_category(category_candidates, tags, cfg)
        level = self._level(score, cfg.thresholds)
        return ClassifyResult(
            importance_score=score,
            category=category,
            tags=sorted(tags),
            level=level,
            matched_rules=matched,
        )

    def _resolve_category(
        self,
        candidates: list[tuple[int, str]],
        tags: set[str],
        cfg: ClassifyConfig,
    ) -> str:
        if candidates:
            # 取评分最高的规则的分类（稳定：同分取先出现的）
            return max(candidates, key=lambda c: c[0])[1]
        if "广告" in tags and self._category_exists("市场广告"):
            return "市场广告"
        if "通知" in tags and self._category_exists("系统通知"):
            return "系统通知"
        return self.default_category()

    @staticmethod
    def _level(score: int, thresholds: dict[str, int]) -> str:
        if score >= thresholds.get("important", 7):
            return "important"
        if score >= thresholds.get("todo", 4):
            return "todo"
        return "normal"

    # ---------- 便捷入口 ----------
    def classify_raw(
        self,
        raw: RawMessage,
        account_email: str | None,
        config: ClassifyConfig | None = None,
    ) -> ClassifyResult:
        data = ClassifyInput(
            subject=raw.subject,
            body_text=raw.body_text,
            from_email=raw.from_email,
            from_name=raw.from_name,
            to_emails=[t.get("email", "") for t in (raw.to_list or [])],
            is_reply=bool(raw.in_reply_to or raw.references),
            attachment_names=[a.filename for a in raw.attachments],
            attachment_mimes=[a.mime for a in raw.attachments],
            account_email=account_email,
        )
        return self.classify(data, config)

    # ---------- 批量重新分类 ----------
    def reclassify_all(self, account_id: int | None = None) -> int:
        cfg = self.load_config(refresh=True)
        stmt = select(Message)
        if account_id:
            stmt = stmt.where(Message.account_id == account_id)
        messages = self.session.exec(stmt).all()

        # 一次性取全部附件，避免 N+1
        att_map: dict[int, list[Attachment]] = {}
        for att in self.session.exec(select(Attachment)).all():
            att_map.setdefault(att.message_pk, []).append(att)

        updated = 0
        for msg in messages:
            atts = att_map.get(msg.id or 0, [])
            data = ClassifyInput(
                subject=msg.subject,
                body_text=msg.body_text,
                from_email=msg.from_email,
                from_name=msg.from_name,
                to_emails=[t.get("email", "") for t in (msg.to_json or [])],
                is_reply=bool(msg.thread_id),
                attachment_names=[a.filename for a in atts],
                attachment_mimes=[a.mime for a in atts],
            )
            account = self.session.get(Account, msg.account_id)
            if account:
                data.account_email = account.email
            result = self.classify(data, cfg)
            if (
                msg.importance_score != result.importance_score
                or msg.category != result.category
                or (msg.tags_json or []) != result.tags
            ):
                updated += 1
            msg.importance_score = result.importance_score
            msg.category = result.category
            msg.tags_json = result.tags
            self.session.add(msg)
        self.session.commit()
        return updated

    # ---------- 统计（供分类树 / 标签树） ----------
    def stats(self, account_id: int | None = None, unread_only: bool = False) -> dict:
        stmt = select(Message).where(Message.is_archived == False)  # noqa: E712
        if account_id:
            stmt = stmt.where(Message.account_id == account_id)
        if unread_only:
            stmt = stmt.where(Message.is_read == False)  # noqa: E712
        messages = self.session.exec(stmt).all()

        names = self.list_categories()
        cat_counts: dict[str, int] = {c: 0 for c in names}
        cat_unread: dict[str, int] = {c: 0 for c in names}
        tag_counts: dict[str, int] = {t: 0 for t in TAGS}
        for m in messages:
            cat = m.category or CATEGORY_OTHER
            # 防御：消息里出现但分类表已无的名称也补进列表，避免邮件被隐藏
            if cat not in cat_counts:
                cat_counts[cat] = 0
                cat_unread[cat] = 0
                names.append(cat)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if not m.is_read:
                cat_unread[cat] = cat_unread.get(cat, 0) + 1
            for tag in m.tags_json or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        return {
            "categories": [
                {"name": c, "count": cat_counts.get(c, 0), "unread": cat_unread.get(c, 0)}
                for c in names
            ],
            "tags": [
                {"name": t, "count": tag_counts.get(t, 0)} for t in TAGS if tag_counts.get(t, 0) > 0
            ],
        }

    # ---------- 分类管理（增删改查） ----------
    def list_categories(self) -> list[str]:
        """按 sort_order, id 返回分类名称；表为空时回退内置默认。"""
        rows = self.session.exec(
            select(Category).order_by(Category.sort_order, Category.id)
        ).all()
        if rows:
            return [c.name for c in rows]
        return list(CATEGORIES)

    def _category_exists(self, name: str) -> bool:
        return self.session.exec(select(Category).where(Category.name == name)).first() is not None

    def default_category(self) -> str:
        """分类从邮件/规则改写的回退目标：优先「其他」，否则第一个分类。"""
        names = self.list_categories()
        if CATEGORY_OTHER in names:
            return CATEGORY_OTHER
        return names[0] if names else CATEGORY_OTHER

    def add_category(self, name: str) -> Category:
        name = (name or "").strip()
        if not name:
            raise ValueError("分类名称不能为空")
        if self._category_exists(name):
            raise ValueError(f"分类「{name}」已存在")
        max_order = self.session.exec(select(Category)).all()
        next_order = max((c.sort_order for c in max_order), default=-1) + 1
        category = Category(name=name, sort_order=next_order, is_builtin=False)
        self.session.add(category)
        self.session.commit()
        self.session.refresh(category)
        return category

    def rename_category(self, old: str, new: str) -> Category:
        new = (new or "").strip()
        if not new:
            raise ValueError("分类名称不能为空")
        category = self.session.exec(select(Category).where(Category.name == old)).first()
        if category is None:
            raise ValueError(f"分类「{old}」不存在")
        if new != old and self._category_exists(new):
            raise ValueError(f"分类「{new}」已存在")
        category.name = new
        self.session.add(category)
        # 同步改写邮件与规则上的分类名
        self._rewrite_category(old, new)
        self.session.commit()
        self.session.refresh(category)
        self.load_config(refresh=True)
        return category

    def delete_category(self, name: str) -> str:
        """删除分类并把其下邮件/规则改写到回退分类，返回回退分类名。"""
        category = self.session.exec(select(Category).where(Category.name == name)).first()
        if category is None:
            raise ValueError(f"分类「{name}」不存在")
        remaining = [c for c in self.list_categories() if c != name]
        if not remaining:
            raise ValueError("至少保留一个分类，无法删除最后一个")
        fallback = CATEGORY_OTHER if CATEGORY_OTHER in remaining else remaining[0]
        self.session.delete(category)
        self._rewrite_category(name, fallback)
        self.session.commit()
        self.load_config(refresh=True)
        return fallback

    def _rewrite_category(self, old: str, new: str) -> None:
        if old == new:
            return
        for msg in self.session.exec(select(Message).where(Message.category == old)).all():
            msg.category = new
            self.session.add(msg)
        for rule in self.session.exec(select(Rule).where(Rule.category == old)).all():
            rule.category = new
            self.session.add(rule)

    # ---------- 首启种子数据 ----------
    def seed_defaults(self) -> None:
        """把默认规则 / 设置 / 分类写入数据库，供界面编辑（幂等）。"""
        existing = self.session.exec(select(Rule)).first()
        if existing is None:
            for d in DEFAULT_RULES:
                self.session.add(Rule(**d))
            self.session.commit()

        if self.session.exec(select(Setting).where(Setting.key == KEY_WHITELIST)).first() is None:
            self.session.add(Setting(key=KEY_WHITELIST, value_json=DEFAULT_WHITELIST))
        if self.session.exec(select(Setting).where(Setting.key == KEY_WEIGHTS)).first() is None:
            self.session.add(Setting(key=KEY_WEIGHTS, value_json=DEFAULT_WEIGHTS))
        if self.session.exec(select(Setting).where(Setting.key == KEY_THRESHOLDS)).first() is None:
            self.session.add(Setting(key=KEY_THRESHOLDS, value_json=DEFAULT_THRESHOLDS))
        self.session.commit()

        # 分类表为空时按内置顺序初始化（只做一次）
        if self.session.exec(select(Category)).first() is None:
            for i, cname in enumerate(CATEGORIES):
                self.session.add(Category(name=cname, sort_order=i, is_builtin=True))
            self.session.commit()


# --------------------------- 匹配工具 ---------------------------
def _rule_text(rule: RuleSpec, data: ClassifyInput) -> str:
    field_name = (rule.field or "subject").lower()
    if field_name == "subject":
        return data.subject or ""
    if field_name == "body":
        return data.body_text or ""
    if field_name == "from":
        return f"{data.from_name} {data.from_email}"
    if field_name == "domain":
        return data.domain
    if field_name == "attachment":
        return " ".join(data.attachment_names) + " " + " ".join(data.attachment_mimes)
    return data.subject or ""


def _rule_matches(rule: RuleSpec, data: ClassifyInput) -> bool:
    text = _rule_text(rule, data)
    value = rule.value or ""
    op = (rule.op or "contains").lower()

    if op == "contains":
        return value.lower() in text.lower() if value else False
    if op == "equals":
        return text.strip().lower() == value.strip().lower()
    if op == "regex":
        try:
            return re.search(value, text, re.IGNORECASE) is not None
        except re.error:
            return False
    if op == "in_list":
        needles = [s.strip().lower() for s in value.split(",") if s.strip()]
        haystack = text.lower()
        return any(n in haystack for n in needles)
    return False


def _whitelist_hit(whitelist: Iterable[str], data: ClassifyInput) -> bool:
    domain = data.domain
    sender = (data.from_email or "").strip().lower()
    for entry in whitelist:
        e = (entry or "").strip().lower().lstrip("@")
        if not e:
            continue
        if e == sender:
            return True
        if domain and (e == domain or domain.endswith("." + e)):
            return True
    return False


def _has_entities(body: str) -> bool:
    if not body:
        return False
    return bool(
        _RE_AMOUNT.search(body)
        or _RE_ORDER.search(body)
        or _RE_DEADLINE.search(body)
    )