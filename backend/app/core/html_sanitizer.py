"""HTML 邮件正文消毒：白名单过滤，剔除脚本 / 事件 / 危险协议。

设计约束（与项目约定一致）：
- 只用标准库 ``html.parser``，不引入 bleach/nh3 等第三方依赖；
- 只做「保留白名单」，未知标签一律丢弃标签本身、保留其文本；
- 前端还会把正文放进 ``sandbox`` iframe 并用 CSP 拦截外链资源，
  此处是第一道防线（去脚本、去 on* 事件、去 javascript: 协议）。
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# 连同内容一起丢弃（这些标签的文本不是正文，保留反而会泄漏脚本/样式源码）
_DROP_CONTENT_TAGS = {
    "script",
    "noscript",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "applet",
    "svg",
    "math",
    "template",
    "textarea",
    "title",
}

# 丢弃标签本身、保留内部文本
_DROP_TAGS = {
    "link",
    "meta",
    "base",
    "input",
    "button",
    "select",
    "option",
    "form",
    "fieldset",
    "label",
    "audio",
    "video",
    "source",
    "track",
    "canvas",
    "map",
    "area",
    "html",
    "head",
    "body",
}

# 允许保留的标签
_ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "blockquote", "br", "caption", "center", "cite",
    "code", "col", "colgroup", "dd", "del", "div", "dl", "dt", "em", "figcaption",
    "figure", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins",
    "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span",
    "strike", "strong", "style", "sub", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "u", "ul", "var",
}

# 自闭合标签：不写结束标签
_VOID_TAGS = {"br", "hr", "col", "img"}

# 通用属性 + 各标签专属属性白名单
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "*": {"title", "dir", "lang", "style", "class", "align", "valign", "width", "height", "bgcolor", "color"},
    "a": {"href", "target"},
    "img": {"src", "alt", "border"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "table": {"border", "cellpadding", "cellspacing"},
    "col": {"span"},
    "colgroup": {"span"},
    "font": {"face", "size"},
}

# 需要校验协议的属性
_URL_ATTRS = {"href", "src"}
_SAFE_URL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "cid:", "data:image/")

# style 属性 / <style> 内容里的危险片段
_BAD_CSS = re.compile(r"(expression\s*\(|@import|javascript:|behavior\s*:)", re.I)


class _HTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_depth = 0  # >0 表示正处于「连内容一起丢弃」的标签内
        self._style_depth = 0

    # ---------- 属性 ----------
    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS["*"] | _ALLOWED_ATTRS.get(tag, set())
        parts: list[str] = []
        for name, value in attrs:
            name = (name or "").lower()
            if not name or name.startswith("on") or name not in allowed:
                continue
            if value is None:
                parts.append(f" {name}")
                continue
            value = value.strip()
            if name in _URL_ATTRS:
                probe = re.sub(r"[\s\x00-\x1f]", "", value).lower()
                if not probe.startswith(_SAFE_URL_PREFIXES):
                    continue
            if name == "style" and _BAD_CSS.search(value):
                continue
            parts.append(f' {name}="{escape(value, quote=True)}"')
        return "".join(parts)

    # ---------- HTMLParser 回调 ----------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth or tag in _DROP_TAGS or tag not in _ALLOWED_TAGS:
            return
        if tag == "style":
            self._style_depth += 1
        suffix = " />" if tag in _VOID_TAGS else ">"
        self._out.append(f"<{tag}{self._clean_attrs(tag, attrs)}{suffix}")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth or tag in _VOID_TAGS or tag not in _ALLOWED_TAGS:
            return
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._style_depth:
            # <style> 内容是 CSS，不能转义（否则 > 选择器全废）；只剔除危险片段
            self._out.append(_BAD_CSS.sub("", data))
        else:
            self._out.append(escape(data))

    def handle_comment(self, data: str) -> None:  # noqa: D102
        return

    def handle_decl(self, decl: str) -> None:  # noqa: D102
        return

    def handle_pi(self, data: str) -> None:  # noqa: D102
        return

    def result(self) -> str:
        return "".join(self._out)


def sanitize_html(raw: str | None) -> str | None:
    """把邮件 HTML 正文过滤成安全子集；输入为空时原样返回。"""
    if not raw:
        return raw
    parser = _HTMLSanitizer()
    parser.feed(raw)
    parser.close()
    return parser.result()