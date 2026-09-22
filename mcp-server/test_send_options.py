# -*- coding: utf-8 -*-
"""Тесты галочек отправки: уведомление о прочтении и «срочно».

Заголовки проверяются на том, что реально ушло в SMTP-заглушку и легло
копией в «Отправленные». IMAP/SMTP-заглушки — из test_email_attachments.

Запуск:  python -m pytest test_send_options.py -v
"""

import email
import json
from email.message import EmailMessage

import pytest

import imap_client
import kp_tools
import mail_headers
import tools
from imap_client import IMAPClient
from test_email_attachments import (LETTER, FakeIMAP, FakeMCP, FakeSMTP,
                                    make_message)

SENDER = "stavgeo26@mail.ru"
RECEIPT = mail_headers.RECEIPT_HEADERS
ORIGINAL_ID = "<orig-500@example.ru>"


def message_with_id() -> bytes:
    raw = email.message_from_bytes(make_message(
        "Претензия", [("Акт.pdf", b"%PDF-act", "application/pdf")]))
    raw["Message-ID"] = ORIGINAL_ID
    return raw.as_bytes()


@pytest.fixture
def box(monkeypatch):
    imap = FakeIMAP({"INBOX": {"500": message_with_id()}})
    monkeypatch.setattr(IMAPClient, "connect",
                        lambda self: setattr(self, "conn", imap))
    monkeypatch.setattr(imap_client, "MAIL_USER", SENDER)
    FakeSMTP.sent = []
    monkeypatch.setattr(imap_client.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(imap_client.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(tools, "_render_letter_pdf", lambda **kw: b"%PDF-letter")
    monkeypatch.setattr(kp_tools, "_render_kp_pdf", lambda **kw: b"%PDF-kp")

    mcp = FakeMCP()
    tools.register_tools(mcp)
    kp_tools.register_tools(mcp)
    return mcp.tools, imap


def sent_msg(index=-1):
    return email.message_from_string(FakeSMTP.sent[index][2])


def receipt_headers(msg) -> dict:
    return {h: msg.get_all(h) for h in RECEIPT}


def assert_receipt(msg):
    for header in RECEIPT:
        assert msg.get_all(header) == [f"<{SENDER}>"], header


def assert_no_receipt(msg):
    assert all(msg.get(h) is None for h in RECEIPT), receipt_headers(msg)


NEW = dict(to="client@example.ru", subject="Претензия", body="Добрый день")


# ------------------------------------------------ 1–2. send_letter

def test_send_letter_requests_receipt_by_default(box):
    handlers, _ = box
    result = json.loads(handlers["send_letter"](to="client@example.ru",
                                                **LETTER))
    assert result["read_receipt_requested"] is True
    assert result["urgent"] is False
    assert_receipt(sent_msg())
    assert sent_msg().get("X-Priority") is None


def test_send_letter_without_receipt(box):
    handlers, _ = box
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER, read_receipt=False))
    assert result["read_receipt_requested"] is False
    assert_no_receipt(sent_msg())


# ------------------------------------------------ 3. send_new_email

def test_send_new_email_no_receipt_by_default(box):
    handlers, _ = box
    result = json.loads(handlers["send_new_email"](**NEW))
    assert result["read_receipt_requested"] is False
    assert_no_receipt(sent_msg())


def test_send_new_email_with_receipt(box):
    handlers, _ = box
    result = json.loads(handlers["send_new_email"](**NEW, read_receipt=True))
    assert result["read_receipt_requested"] is True
    assert_receipt(sent_msg())


# ------------------------------------------------ 4. send_reply, forward_email

def test_send_reply_receipt_keeps_threading_headers(box):
    handlers, _ = box
    result = json.loads(handlers["send_reply"](
        email_uid="500", body="Ответ на претензию", read_receipt=True))
    assert result["read_receipt_requested"] is True
    msg = sent_msg()
    assert_receipt(msg)
    assert msg["In-Reply-To"] == ORIGINAL_ID
    assert msg["References"] == ORIGINAL_ID


def test_forward_email_with_receipt(box):
    handlers, _ = box
    result = json.loads(handlers["forward_email"](
        email_uid="500", to="lawyer@example.ru", read_receipt=True))
    assert result["status"] == "forwarded"
    assert result["read_receipt_requested"] is True
    assert_receipt(sent_msg())


def test_forward_email_splits_several_addresses(box):
    handlers, _ = box
    handlers["forward_email"](email_uid="500",
                              to="a@example.ru, Юрист <b@example.ru>")
    assert FakeSMTP.sent[-1][1] == ["a@example.ru", "b@example.ru"]
    assert_no_receipt(sent_msg())


# ------------------------------------------------ 5. повторный вызов

def test_double_apply_keeps_single_headers():
    msg = EmailMessage()
    for _ in range(2):
        mail_headers.apply_send_options(msg, SENDER, read_receipt=True,
                                        urgent=True)
    for header in (*RECEIPT, *mail_headers.URGENT_HEADERS):
        assert len(msg.get_all(header)) == 1, header


def test_apply_false_removes_headers():
    msg = EmailMessage()
    mail_headers.apply_send_options(msg, SENDER, True, True)
    options = mail_headers.apply_send_options(msg, SENDER, False, False)
    assert options["read_receipt_requested"] is False
    assert options["urgent"] is False
    for header in (*RECEIPT, *mail_headers.URGENT_HEADERS):
        assert msg.get(header) is None


def test_no_sender_address_means_no_receipt():
    msg = EmailMessage()
    options = mail_headers.apply_send_options(msg, "", read_receipt=True)
    assert options["read_receipt_requested"] is False
    assert_no_receipt(msg)


# ------------------------------------------------ 6. превью

def test_preview_reports_flags_and_sends_nothing(box):
    handlers, imap = box
    default = json.loads(handlers["preview_letter"](**LETTER))
    assert default["read_receipt"] is True
    assert default["urgent"] is False
    custom = json.loads(handlers["preview_letter"](
        **LETTER, read_receipt=False, urgent=True))
    assert custom["read_receipt"] is False
    assert custom["urgent"] is True
    assert FakeSMTP.sent == []
    assert imap.appended == []


# ------------------------------------------------ 7. копия в «Отправленных»

def test_sent_copy_has_same_headers(box):
    handlers, imap = box
    handlers["send_letter"](to="client@example.ru", **LETTER, urgent=True)
    copy = email.message_from_bytes(imap.appended[-1][1])
    assert copy["Disposition-Notification-To"] == f"<{SENDER}>"
    assert copy["X-Priority"] == "1 (Highest)"


# ------------------------------------------------ «срочно»

@pytest.mark.parametrize("tool, kwargs", [
    ("send_new_email", NEW),
    ("send_reply", dict(email_uid="500", body="Срочный ответ")),
    ("forward_email", dict(email_uid="500", to="lawyer@example.ru")),
    ("send_letter", dict(to="client@example.ru", **LETTER)),
    ("send_kp", dict(to="client@example.ru", subject="КП",
                     email_body="Добрый день", date_str="«22» сентября 2026 г.",
                     addressee="Кому", object_name="Объект",
                     body='[{"text": "Работы"}]', price_total=100000,
                     result="Отчёт", timeline="10 рабочих дней",
                     pdf_filename="КП.pdf")),
])
def test_urgent_flag_in_every_send_tool(box, tool, kwargs):
    handlers, _ = box
    result = json.loads(handlers[tool](**kwargs, urgent=True))
    assert result["urgent"] is True
    msg = sent_msg()
    assert msg["X-Priority"] == "1 (Highest)"
    assert msg["Importance"] == "High"
    # спам-фильтры штрафуют X-MSMail-Priority без X-MimeOLE
    assert msg.get("X-MSMail-Priority") is None


def test_urgent_off_by_default(box):
    handlers, _ = box
    result = json.loads(handlers["send_new_email"](**NEW))
    assert result["urgent"] is False
    assert sent_msg().get("X-Priority") is None
    assert sent_msg().get("Importance") is None


def test_send_kp_defaults_unchanged(box):
    """КП по умолчанию без запроса о прочтении и без «срочно»."""
    handlers, _ = box
    result = json.loads(handlers["send_kp"](
        to="client@example.ru", subject="КП", email_body="Добрый день",
        date_str="«22» сентября 2026 г.", addressee="Кому",
        object_name="Объект", body='[{"text": "Работы"}]',
        price_total=100000, result="Отчёт", timeline="10 рабочих дней",
        pdf_filename="КП.pdf"))
    assert result["read_receipt_requested"] is False
    assert result["urgent"] is False
    assert_no_receipt(sent_msg())


# ------------------------------------------------ Message-ID

@pytest.mark.parametrize("tool, kwargs", [
    ("send_new_email", NEW),
    ("send_reply", dict(email_uid="500", body="Ответ")),
    ("forward_email", dict(email_uid="500", to="lawyer@example.ru")),
    ("send_letter", dict(to="client@example.ru", **LETTER)),
])
def test_message_id_same_in_smtp_copy_and_result(box, tool, kwargs):
    """Письмо, копия в «Отправленных» и ответ инструмента — один Message-ID."""
    handlers, imap = box
    result = json.loads(handlers[tool](**kwargs))
    sent_id = sent_msg()["Message-ID"]
    assert sent_id and sent_id.startswith("<") and sent_id.endswith("@mail.ru>")
    copy = email.message_from_bytes(imap.appended[-1][1])
    assert copy["Message-ID"] == sent_id
    assert result["message_id"] == sent_id


def test_message_ids_are_unique(box):
    handlers, _ = box
    handlers["send_new_email"](**NEW)
    handlers["send_new_email"](**NEW)
    assert sent_msg(0)["Message-ID"] != sent_msg(1)["Message-ID"]


def test_existing_message_id_is_kept():
    msg = EmailMessage()
    msg["Message-ID"] = "<own@example.ru>"
    for _ in range(2):
        options = mail_headers.apply_send_options(msg, SENDER, True, True)
    assert msg.get_all("Message-ID") == ["<own@example.ru>"]
    assert options["message_id"] == "<own@example.ru>"
