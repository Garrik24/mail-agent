# -*- coding: utf-8 -*-
"""Интеграционные тесты: нормализация тела письма в хендлерах отправки.

Проверяют, что prepare_body реально подключён в send_new_email, send_reply,
send_letter и send_kp — и что поле body письма на бланке (список абзацев
для PDF) при этом не затрагивается.
"""

import pytest

import kp_tools
import tools


class FakeClient:
    """Заглушка IMAPClient: ничего не шлёт, только записывает вызовы."""

    calls = []

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send_email(self, **kw):
        FakeClient.calls.append(("send_email", kw))
        return {"status": "sent"}

    def send_reply(self, **kw):
        FakeClient.calls.append(("send_reply", kw))
        return {"status": "sent"}

    def send_letter_email(self, **kw):
        FakeClient.calls.append(("send_letter_email", kw))
        return {"status": "sent"}


class FakeMCP:
    """Собирает зарегистрированные инструменты по имени."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def registered(monkeypatch):
    FakeClient.calls = []
    renders = []

    def fake_letter_pdf(**kw):
        renders.append(("letter", kw))
        return b"%PDF-letter"

    def fake_kp_pdf(**kw):
        renders.append(("kp", kw))
        return b"%PDF-kp"

    monkeypatch.setattr(tools, "IMAPClient", FakeClient)
    monkeypatch.setattr(kp_tools, "IMAPClient", FakeClient)
    monkeypatch.setattr(tools, "_render_letter_pdf", fake_letter_pdf)
    monkeypatch.setattr(kp_tools, "_render_kp_pdf", fake_kp_pdf)

    mcp = FakeMCP()
    tools.register_tools(mcp)
    kp_tools.register_tools(mcp)
    return mcp.tools, FakeClient.calls, renders


def _last(calls, name):
    for kind, kw in reversed(calls):
        if kind == name:
            return kw
    raise AssertionError(f"вызов {name} не состоялся: {calls}")


PLAIN = 'Александр, добрый день.\n\nЗадание на выезд.\nСбор в 8:00.'
ESCAPED = '&lt;p&gt;Добрый день&lt;/p&gt;&lt;p&gt;Смета во вложении&lt;/p&gt;'


def test_send_new_email_normalizes_plain_text(registered):
    handlers, calls, _ = registered
    handlers["send_new_email"](to="a@b.ru", subject="Тест", body=PLAIN)
    sent = _last(calls, "send_email")["body"]
    assert sent == ('<p>Александр, добрый день.</p>'
                    '<p>Задание на выезд.<br>Сбор в 8:00.</p>')


def test_send_new_email_fixes_escaped_html(registered):
    handlers, calls, _ = registered
    handlers["send_new_email"](to="a@b.ru", subject="Тест", body=ESCAPED)
    assert _last(calls, "send_email")["body"] == (
        '<p>Добрый день</p><p>Смета во вложении</p>')


def test_send_reply_normalizes_body(registered):
    handlers, calls, _ = registered
    handlers["send_reply"](email_uid="123", body=PLAIN)
    sent = _last(calls, "send_reply")["body"]
    assert sent.startswith('<p>Александр, добрый день.</p>')
    assert '<br>' in sent


def test_send_letter_normalizes_email_body_only(registered):
    """email_body нормализуется, body (абзацы для PDF) — нет."""
    handlers, calls, renders = registered
    pdf_body = '[{"text": "Первый абзац.", "italic": false}]'
    handlers["send_letter"](
        to="a@b.ru", subject="Тема", addressee="Кому",
        isx_number="150", date_str="«18» августа 2026 г.",
        body=pdf_body, email_body=PLAIN,
    )
    assert _last(calls, "send_letter_email")["html_body"].startswith(
        '<p>Александр, добрый день.</p>')
    assert renders[-1][1]["body"] == pdf_body


def test_send_letter_default_email_body_still_works(registered):
    """Пустой email_body не ломается: собирается стандартная сопроводиловка."""
    handlers, calls, _ = registered
    handlers["send_letter"](
        to="a@b.ru", subject="Тема", addressee="Кому",
        isx_number="150", date_str="«18» августа 2026 г.",
        body='[{"text": "Абзац"}]',
    )
    html_body = _last(calls, "send_letter_email")["html_body"]
    assert "Здравствуйте!" in html_body
    assert "исх. № 150" in html_body


def test_send_kp_normalizes_email_body_only(registered):
    handlers, calls, renders = registered
    kp_body = '[{"text": "Состав работ", "bold": true}]'
    handlers["send_kp"](
        to="a@b.ru", subject="КП", email_body=ESCAPED,
        date_str="«18» августа 2026 г.", addressee="Кому",
        object_name="Объект", body=kp_body, price_total=450000,
        result="Технический отчёт", timeline="15 рабочих дней",
        pdf_filename="КП.pdf",
    )
    assert _last(calls, "send_letter_email")["html_body"] == (
        '<p>Добрый день</p><p>Смета во вложении</p>')
    assert renders[-1][1]["body"] == kp_body
