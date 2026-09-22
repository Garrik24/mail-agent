# -*- coding: utf-8 -*-
"""Заголовки исходящего письма: запрос уведомления о прочтении и «срочно».

Ставятся на уже собранное сообщение прямо перед SMTP — тот же объект
потом уходит копией в «Отправленные», поэтому заголовки есть и там.

Уведомление о прочтении (MDN, RFC 8098): Disposition-Notification-To плюс
два устаревших варианта для старых клиентов (The Bat!, корпоративный
Exchange). Получатель может отказаться отправлять уведомление — его
отсутствие не доказывает, что письмо не прочитано.

«Срочно»: X-Priority (Mail.ru, The Bat!, Thunderbird) и Importance из
RFC 2156 (Outlook/Exchange). X-MSMail-Priority не ставим намеренно: без
X-MimeOLE он добавляет баллы в SpamAssassin (правило MISSING_MIMEOLE).
"""

RECEIPT_HEADERS = (
    "Disposition-Notification-To",
    "Return-Receipt-To",
    "X-Confirm-Reading-To",
)

URGENT_HEADERS = {
    "X-Priority": "1 (Highest)",
    "Importance": "High",
}


def apply_read_receipt(msg, sender_email: str, enabled: bool):
    """Добавить (или убрать) запрос уведомления о прочтении.

    Уведомление придёт на sender_email — адрес отправителя. Повторный
    вызов не плодит дубли: заголовки сначала удаляются.
    """
    for header in RECEIPT_HEADERS:
        del msg[header]
    if enabled and sender_email:
        for header in RECEIPT_HEADERS:
            msg[header] = f"<{sender_email}>"
    return msg


def apply_urgent(msg, enabled: bool):
    """Добавить (или убрать) отметку важности — у получателя красный «!»."""
    for header in URGENT_HEADERS:
        del msg[header]
    if enabled:
        for header, value in URGENT_HEADERS.items():
            msg[header] = value
    return msg


def apply_send_options(msg, sender_email: str, read_receipt: bool = False,
                       urgent: bool = False) -> dict:
    """Проставить обе галочки и вернуть поля для ответа инструмента."""
    apply_read_receipt(msg, sender_email, read_receipt)
    apply_urgent(msg, urgent)
    return {
        "read_receipt_requested": bool(read_receipt and sender_email),
        "urgent": bool(urgent),
    }
