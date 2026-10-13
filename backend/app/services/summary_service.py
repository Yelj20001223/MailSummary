"""邮件摘要与信息抽取服务（P4）。

两级策略（对应需求第五节「摘要与信息抽取」）：

1. 规则优先（默认可用，无需任何配置）：
   正则抽取 金额 / 日期 / 截止时间 / 验证码 / 订单号 / 电话 / 待办动词，
   再用模板拼装 2–3 句中文摘要，并给出 urgency。
2. 可选 LLM：
   OpenAI 兼容的 Chat Completions 接口（覆盖 OpenAI / 本地 Ollama / 兼容代理）；
   输入 = 邮件头部 + 正文前 N 字，输出固定 JSON
   ``{summary, category, entities, action_items, urgency}``。
   未配置 key / 关闭开关 / 调用失败时自动降级为规则摘要，保证系统完整可用。

结果按 ``(message_id + 正文 hash)`` 缓存在 ``summaries`` 表，正文变化才会重新生成。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, col, select

from app.config import settings
from app.core.crypto import decrypt, encrypt, mask_secret
from app.core.logging import logger
from app.core.utils import sha256_text
from app.models.account import Account
from app.models.message import Message
from app.models.rule import Setting
from app.models.summary import Summary
from app.schemas.summary import (
    DigestItem,
    LlmConfigRead,
    LlmConfigUpdate,
    SummaryBatchResult,
    SummaryEntities,
    SummaryRead,
)
from app.services.message_service import to_message_read

# --------------------------- 正则（规则抽取） ---------------------------
_RE_AMOUNT = re.compile(r"[¥￥$]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:元|美元|人民币|万元)")
_RE_ORDER = re.compile(r"\b[A-Z]{2,}\d{6,}\b")
_RE_PHONE_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_PHONE_TEL = re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")
# 验证码：优先「关键字 + 数字」上下文
_RE_CAPTCHA_CTX = re.compile(r"(?:验证码|校验码|动态码|一次性密码|口令|code)[^\d]{0,10}(\d{4,8})", re.IGNORECASE)
_RE_CAPTCHA_BARE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_RE_CAPTCHA_HINT = re.compile(r"验证码|校验码|动态码|一次性密码|otp", re.IGNORECASE)
# 完整日期：2026年10月11日 / 2026-10-11 / 2026/10/11
_RE_DATE_FULL = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")
# 缺年份：10月11日 / 10-11
_RE_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RE_DEADLINE_HINT = re.compile(
    r"截止|deadline|前提供|有效期至|请于|务必于|最晚|截至|到期|"
    r"(?:在|于)?[^，。；\n]{0,20}?前(?:完成|提交|回复|答复|付款|支付|确认|处理|提供|缴费)",
    re.IGNORECASE,
)
_RE_ACTION = re.compile(
    r"请(?:确认|回复|答复|提供|付款|支付|缴费|审批|提交|补交|上传|查收|回复|反馈|处理|注意)|"
    r"务必|尽快|烦请|敬请|需要你|需要您|前提供|截止|抓紧",
)
_RE_URGENT = re.compile(r"紧急|尽快|立即|马上|asap|加急|重要|务必", re.IGNORECASE)
# 归一化时正文扫描上限，避免超长正文拖慢
_SCAN_LIMIT = 8000
_MAX_LIST = 8

KEY_LLM_CONFIG = "llm_config"

# LLM 系统提示：明确要求纯 JSON、不输出任何操作指令/代码
_SYSTEM_PROMPT = (
    "你是一个邮件摘要助手。请阅读用户给出的邮件，只输出**纯 JSON**，"
    "不要输出任何解释、Markdown、代码块，也不要输出任何操作指令或代码。\n"
    "JSON 结构：{\"summary\": \"2-3 句中文摘要\", \"category\": \"业务分类\", "
    "\"entities\": {\"amounts\": [], \"dates\": [], \"deadlines\": [], \"captcha\": [], "
    "\"orders\": [], \"phones\": []}, \"action_items\": [\"待办\"], "
    "\"urgency\": \"low|normal|high\"}\n"
    "只使用邮件中真实出现的信息，不要编造；无法确定的字段用空数组或空串。"
)

_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "ollama": ("http://localhost:11434/v1", "llama3.1"),
}


# --------------------------- 抽取 ---------------------------
def _norm_date(y: str | int, m: str | int, d: str | int) -> str:
    try:
        yi, mi, di = int(y), int(m), int(d)
        if not (1 <= mi <= 12 and 1 <= di <= 31):
            return ""
        return f"{yi:04d}-{mi:02d}-{di:02d}"
    except (TypeError, ValueError):
        return ""


def _dedup(seq: list[str], limit: int = _MAX_LIST) -> list[str]:
    out: list[str] = []
    for item in seq:
        item = (item or "").strip()
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _sentences(text: str) -> list[str]:
    """按中英文标点切句，用于待办 / 截止时间定位。"""
    parts = re.split(r"[。！？!?；;\n\r]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def extract_entities(text: str, subject: str = "") -> dict[str, list[str]]:
    """规则抽取关键实体。返回 dict（可直接存入 summaries.entities_json）。"""
    body = (text or "")[:_SCAN_LIMIT]
    whole = f"{subject or ''}\n{body}"

    amounts: list[str] = [m.group(0).strip() for m in _RE_AMOUNT.finditer(body)]
    orders: list[str] = [m.group(0).strip() for m in _RE_ORDER.finditer(whole)]
    phones: list[str] = (
        [m.group(0) for m in _RE_PHONE_MOBILE.finditer(body)]
        + [m.group(0) for m in _RE_PHONE_TEL.finditer(body)]
    )

    # 日期：优先完整日期，再补「M月D日」（无年份用当前年）
    dates: list[str] = []
    for m in _RE_DATE_FULL.finditer(body):
        nd = _norm_date(*m.groups())
        if nd:
            dates.append(nd)
    if not dates:
        year = datetime.now(timezone.utc).year
        for m in _RE_DATE_MD.finditer(body):
            nd = _norm_date(year, m.group(1), m.group(2))
            if nd:
                dates.append(nd)

    # 验证码：优先上下文命中；否则仅当主题/正文出现「验证码」时才取裸数字（避免误抓年份等）
    captcha: list[str] = []
    for m in _RE_CAPTCHA_CTX.finditer(whole):
        captcha.append(m.group(1))
    if not captcha and _RE_CAPTCHA_HINT.search(whole):
        for m in _RE_CAPTCHA_BARE.finditer(body):
            captcha.append(m.group(0))

    # 截止时间：命中提示词的句子里，带上其中出现的日期
    deadlines: list[str] = []
    for sent in _sentences(body):
        if _RE_DEADLINE_HINT.search(sent):
            dm = _RE_DATE_FULL.search(sent) or _RE_DATE_MD.search(sent)
            if dm:
                groups = dm.groups()
                nd = _norm_date(*groups) if len(groups) == 3 else _norm_date(
                    datetime.now(timezone.utc).year, *groups
                )
                deadlines.append(f"{nd}（{sent[:40]}）" if nd else sent[:60])
            else:
                deadlines.append(sent[:60])

    return {
        "amounts": _dedup(amounts),
        "orders": _dedup(orders),
        "phones": _dedup(phones),
        "dates": _dedup(dates),
        "captcha": _dedup(captcha, limit=3),
        "deadlines": _dedup(deadlines, limit=5),
    }


def extract_action_items(text: str) -> list[str]:
    """抽取待办句（含 请确认/请回复/请付款/截止 等动词）。"""
    items: list[str] = []
    for sent in _sentences((text or "")[:_SCAN_LIMIT]):
        if _RE_ACTION.search(sent):
            snippet = sent[:80]
            if snippet not in items:
                items.append(snippet)
        if len(items) >= 5:
            break
    return items


def _urgency(message: Message, entities: dict[str, list[str]], action_items: list[str]) -> str:
    text = f"{message.subject or ''} {(message.body_text or '')[:_SCAN_LIMIT]}"
    if entities.get("deadlines") or _RE_URGENT.search(text):
        return "high"
    if (message.importance_score or 0) >= 7:
        return "high"
    if action_items:
        return "normal"
    if (message.category or "") in ("市场广告", "社交", "系统通知"):
        return "low"
    return "normal"


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def build_rule_summary(message: Message) -> tuple[str, dict[str, list[str]], list[str], str]:
    """规则摘要：返回 (摘要文本, 实体, 待办, urgency)。"""
    entities = extract_entities(message.body_text or "", message.subject or "")
    action_items = extract_action_items(message.body_text or "")
    urgency = _urgency(message, entities, action_items)

    who = message.from_name or message.from_email or "对方"
    subject = message.subject or "(无主题)"
    when = _fmt_date(message.date_utc)
    s1 = f"{who} 于 {when} 发来「{subject}」" if when else f"{who} 发来「{subject}」"
    if message.category and message.category != "其他":
        s1 += f"，归类为「{message.category}」"
    s1 += "。"

    bits: list[str] = []
    if entities["amounts"]:
        bits.append("金额 " + "、".join(entities["amounts"][:3]))
    if entities["orders"]:
        bits.append("订单号 " + "、".join(entities["orders"][:3]))
    if entities["captcha"]:
        bits.append("验证码 " + "、".join(entities["captcha"][:2]))
    if entities["phones"]:
        bits.append("联系电话 " + "、".join(entities["phones"][:2]))
    if entities["deadlines"]:
        bits.append("截止时间 " + "、".join(entities["deadlines"][:2]))
    elif entities["dates"]:
        bits.append("涉及日期 " + "、".join(entities["dates"][:3]))
    s2 = ("关键信息：" + "；".join(bits) + "。") if bits else ""

    s3 = ("待办：" + "；".join(action_items[:2]) + "。") if action_items else ""
    summary = "".join([s1, s2, s3]) or f"「{subject}」暂无可用摘要。"
    return summary, entities, action_items, urgency


def _parse_json(content: str) -> dict | None:
    """从 LLM 输出中解析 JSON（容忍 ```json 代码块与前后噪声）。"""
    if not content:
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------- LLM 配置 ---------------------------
@dataclass(slots=True)
class LlmConfig:
    enabled: bool = False
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    max_chars: int = 4000
    timeout: float = 30.0

    @property
    def ready(self) -> bool:
        """是否具备调用条件（本地 Ollama 允许无 key）。"""
        return bool(self.enabled and self.base_url and self.model and (self.api_key or self.provider == "ollama"))


# --------------------------- 服务 ---------------------------
class SummaryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._llm: LlmConfig | None = None

    # ---------- 缓存键 ----------
    @staticmethod
    def _content_hash(message: Message) -> str:
        return sha256_text(f"{message.subject or ''}\x1f{(message.body_text or '')[:2000]}")

    # ---------- LLM 配置读写 ----------
    def load_llm_config(self, *, refresh: bool = False) -> LlmConfig:
        if self._llm is not None and not refresh:
            return self._llm
        row = self.session.exec(select(Setting).where(Setting.key == KEY_LLM_CONFIG)).first()
        data = dict(row.value_json or {}) if row else {}
        provider = data.get("provider") or settings.llm_provider or "openai"
        default_url, default_model = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])
        self._llm = LlmConfig(
            enabled=bool(data.get("enabled", settings.llm_enabled)),
            provider=provider,
            base_url=data.get("base_url") or settings.llm_base_url or default_url,
            model=data.get("model") or settings.llm_model or default_model,
            api_key=data.get("api_key_enc") and decrypt(data["api_key_enc"]) or settings.llm_api_key or "",
            max_chars=int(data.get("max_chars") or settings.llm_max_chars),
            timeout=float(data.get("timeout") or settings.llm_timeout),
        )
        return self._llm

    def save_llm_config(self, update: LlmConfigUpdate) -> LlmConfig:
        row = self.session.exec(select(Setting).where(Setting.key == KEY_LLM_CONFIG)).first()
        if row is None:
            row = Setting(key=KEY_LLM_CONFIG, value_json={})
        data = dict(row.value_json or {})
        payload = update.model_dump(exclude_unset=True)
        for key in ("enabled", "provider", "base_url", "model", "max_chars", "timeout"):
            if key in payload and payload[key] is not None:
                data[key] = payload[key]
        # api_key 单独处理：非空才更新（加密存储；空串=清除）
        if "api_key" in payload and payload["api_key"] is not None:
            data["api_key_enc"] = encrypt(payload["api_key"]) if payload["api_key"] else ""
        row.value_json = data
        self.session.add(row)
        self.session.commit()
        self._llm = None
        return self.load_llm_config(refresh=True)

    def llm_config_read(self) -> LlmConfigRead:
        cfg = self.load_llm_config(refresh=True)
        return LlmConfigRead(
            enabled=cfg.enabled,
            provider=cfg.provider,
            base_url=cfg.base_url,
            model=cfg.model,
            has_api_key=bool(cfg.api_key),
            api_key_masked=mask_secret(cfg.api_key),
            max_chars=cfg.max_chars,
            timeout=cfg.timeout,
        )

    # ---------- 生成 / 缓存 ----------
    def _cached(self, message_id: int, content_hash: str) -> Summary | None:
        return self.session.exec(
            select(Summary).where(
                Summary.message_pk == message_id,
                Summary.content_hash == content_hash,
            )
        ).first()

    def generate(self, message: Message, *, use_llm: bool = False, force: bool = False) -> Summary:
        """生成（或复用缓存）一封邮件的摘要，返回落库的 Summary。"""
        row, _reused = self._generate(message, use_llm=use_llm, force=force)
        return row

    def _generate(self, message: Message, *, use_llm: bool = False, force: bool = False) -> tuple[Summary, bool]:
        """返回 (Summary, 是否直接复用缓存)。"""
        message_id = message.id or 0
        content_hash = self._content_hash(message)
        cached = self._cached(message_id, content_hash)
        if cached is not None and not force:
            # 已缓存：除非用户显式请求 LLM 且缓存还是规则摘要
            if not (use_llm and cached.source != "llm"):
                return cached, True

        cfg = self.load_llm_config()
        llm_result: dict | None = None
        source = "rule"
        if use_llm and cfg.enabled:
            if cfg.ready:
                llm_result = self._llm_summarize(message, cfg)
                if llm_result is None:
                    logger.warning(f"[SUMMARY] LLM 调用失败或解析失败，降级规则摘要：message_id={message_id}")
            else:
                logger.info("[SUMMARY] LLM 已开启但未配置完整（缺 key/base_url/model），使用规则摘要")

        if llm_result is not None:
            text = str(llm_result.get("summary") or "").strip()
            entities = _coerce_entities(llm_result.get("entities"))
            action_items = _coerce_list(llm_result.get("action_items"))
            urgency = _coerce_urgency(llm_result.get("urgency"))
            if not text:
                llm_result = None  # 无摘要视为失败，降级
            else:
                source = "llm"

        if llm_result is None:
            text, entities, action_items, urgency = build_rule_summary(message)

        row = cached or Summary(message_pk=message_id)
        row.summary = text
        row.entities_json = entities
        row.action_items = action_items
        row.urgency = urgency
        row.source = source
        row.content_hash = content_hash
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row, False

    def get_or_generate(self, message_id: int, *, use_llm: bool = False) -> SummaryRead | None:
        message = self.session.get(Message, message_id)
        if message is None:
            return None
        row, reused = self._generate(message, use_llm=use_llm)
        return _to_read(row, cached_before=reused)

    def regenerate(self, message_id: int, *, use_llm: bool = False) -> SummaryRead | None:
        """强制重新生成（忽略缓存）。"""
        message = self.session.get(Message, message_id)
        if message is None:
            return None
        row = self.generate(message, use_llm=use_llm, force=True)
        return _to_read(row, cached_before=False)

    # ---------- 批量 ----------
    def batch(
        self,
        *,
        message_ids: list[int] | None = None,
        important_only: bool = False,
        use_llm: bool = False,
        force: bool = False,
        limit: int = 50,
    ) -> SummaryBatchResult:
        stmt = select(Message)
        if message_ids:
            stmt = stmt.where(col(Message.id).in_(message_ids))
        elif important_only:
            stmt = stmt.where(Message.importance_score >= _important_threshold(self.session))
        stmt = stmt.order_by(col(Message.date_utc).desc()).limit(limit)
        messages = self.session.exec(stmt).all()

        generated = llm_used = failed = 0
        done_ids: list[int] = []
        for msg in messages:
            try:
                row = self.generate(msg, use_llm=use_llm, force=force)
                generated += 1
                if row.source == "llm":
                    llm_used += 1
                done_ids.append(msg.id or 0)
            except Exception as exc:  # noqa: BLE001 - 单封失败不影响整批
                logger.error(f"[SUMMARY] 生成失败 message_id={msg.id}：{exc}")
                failed += 1
        return SummaryBatchResult(generated=generated, llm_used=llm_used, failed=failed, message_ids=done_ids)

    # ---------- 摘要区（重要邮件） ----------
    def digest(self, *, limit: int = 30) -> list[DigestItem]:
        stmt = (
            select(Message)
            .where(
                Message.is_archived == False,  # noqa: E712
                Message.importance_score >= _important_threshold(self.session),
            )
            .order_by(col(Message.date_utc).desc())
            .limit(limit)
        )
        messages = self.session.exec(stmt).all()
        emails = {a.id: a.email for a in self.session.exec(select(Account)).all()}
        items: list[DigestItem] = []
        for msg in messages:
            row = self.generate(msg)
            items.append(
                DigestItem(
                    message=to_message_read(msg, emails.get(msg.account_id)),
                    summary=row.summary,
                    urgency=row.urgency,
                    action_items=list(row.action_items or []),
                    entities=_coerce_entities(row.entities_json),
                )
            )
        return items

    # ---------- LLM 调用 ----------
    def _llm_input(self, message: Message, max_chars: int) -> str:
        head = "\n".join(
            [
                f"主题：{message.subject or '(无主题)'}",
                f"发件人：{message.from_name or ''} <{message.from_email or ''}>",
                f"时间：{_fmt_date(message.date_utc)}",
                f"分类：{message.category or '其他'}",
            ]
        )
        body = (message.body_text or "")[:max_chars]
        return f"{head}\n正文：\n{body}"

    def _llm_summarize(self, message: Message, cfg: LlmConfig) -> dict | None:
        """调用 OpenAI 兼容接口；任何异常都返回 None（触发降级）。"""
        import httpx

        url = cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": self._llm_input(message, cfg.max_chars)},
            ],
            "temperature": 0.2,
        }
        try:
            with httpx.Client(timeout=cfg.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权/格式错误统一降级
            logger.warning(f"[SUMMARY] LLM 请求异常：{exc}")
            return None
        return _parse_json(content)


# --------------------------- 辅助 ---------------------------
def _important_threshold(session: Session) -> int:
    from app.services.classify_service import DEFAULT_THRESHOLDS, KEY_THRESHOLDS

    row = session.exec(select(Setting).where(Setting.key == KEY_THRESHOLDS)).first()
    data = row.value_json if row and isinstance(row.value_json, dict) else {}
    try:
        return int(data.get("important", DEFAULT_THRESHOLDS["important"]))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLDS["important"]


def _coerce_list(value) -> list[str]:  # noqa: ANN001
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:8]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_urgency(value) -> str:  # noqa: ANN001
    v = str(value or "").strip().lower()
    return v if v in ("low", "normal", "high") else "normal"


def _coerce_entities(value) -> dict[str, list[str]]:  # noqa: ANN001
    keys = ("amounts", "dates", "deadlines", "captcha", "orders", "phones")
    src = value if isinstance(value, dict) else {}
    return {k: _coerce_list(src.get(k)) for k in keys}


def _to_read(row: Summary, *, cached_before: bool = False) -> SummaryRead:
    return SummaryRead(
        message_id=row.message_pk,
        summary=row.summary or "",
        entities=SummaryEntities(**_coerce_entities(row.entities_json)),
        action_items=list(row.action_items or []),
        urgency=row.urgency or "normal",
        source=row.source or "rule",
        cached=cached_before,
        created_at=row.created_at,
    )