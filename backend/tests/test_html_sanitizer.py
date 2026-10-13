"""HTML 邮件正文消毒单元测试。"""
from __future__ import annotations

from sqlmodel import select

from app.core.html_sanitizer import sanitize_html
from app.models.message import Message
from app.services.message_service import MessageService


def test_keeps_normal_formatting():
    raw = '<p style="color:red">你好 <b>世界</b></p><table><tr><td colspan="2">单元格</td></tr></table>'
    out = sanitize_html(raw)
    assert '<p style="color:red">' in out
    assert "<b>世界</b>" in out
    assert 'colspan="2"' in out


def test_drops_script_and_its_content():
    out = sanitize_html("<div>前</div><script>alert('x')</script><div>后</div>")
    assert "script" not in out
    assert "alert" not in out
    assert "前" in out and "后" in out


def test_drops_event_handlers():
    out = sanitize_html('<img src="https://a.com/x.png" onerror="alert(1)" onload="x()" />')
    assert "onerror" not in out
    assert "onload" not in out
    assert 'src="https://a.com/x.png"' in out


def test_blocks_javascript_urls():
    assert "javascript:" not in sanitize_html('<a href="javascript:alert(1)">点我</a>')
    # 空白/换行混淆同样要拦住
    assert "javascript" not in sanitize_html('<a href="java\nscript:alert(1)">点我</a>')
    assert "点我" in sanitize_html('<a href="javascript:alert(1)">点我</a>')


def test_keeps_http_and_mailto_links():
    out = sanitize_html('<a href="https://example.com">站点</a><a href="mailto:a@b.com">邮件</a>')
    assert 'href="https://example.com"' in out
    assert 'href="mailto:a@b.com"' in out


def test_drops_iframe_and_form():
    out = sanitize_html('<iframe src="https://evil.com"></iframe><form><input /></form>正文')
    assert "iframe" not in out
    assert "<input" not in out
    assert "正文" in out


def test_unknown_tag_is_unwrapped_but_text_kept():
    out = sanitize_html("<custom-tag>保留文字</custom-tag>")
    assert "custom-tag" not in out
    assert "保留文字" in out


def test_style_block_kept_but_import_stripped():
    out = sanitize_html("<style>@import url(https://evil.com/a.css); .x > b { color: red }</style>")
    assert ".x > b { color: red }" in out
    assert "@import" not in out


def test_style_attribute_with_expression_removed():
    out = sanitize_html('<div style="width:expression(alert(1))">x</div>')
    assert "expression" not in out
    assert "<div>x</div>" in out


def test_empty_input_passthrough():
    assert sanitize_html(None) is None
    assert sanitize_html("") == ""


def test_detail_endpoint_sanitizes_html(session, account):
    session.add(
        Message(
            account_id=account.id,
            uid=1,
            body_hash="h1",
            body_text="纯文本",
            body_html='<p>正文</p><script>alert(1)</script>',
        )
    )
    session.commit()
    msg = session.exec(select(Message)).first()

    detail = MessageService(session).get_detail(msg.id)

    assert detail is not None
    assert "<p>正文</p>" in (detail.body_html or "")
    assert "script" not in (detail.body_html or "")