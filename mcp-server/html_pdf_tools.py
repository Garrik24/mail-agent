# -*- coding: utf-8 -*-
"""MCP-инструменты preview_html_pdf / send_html_pdf.

Логика как у preview_letter / send_letter: клиент передаёт только текст,
PDF рендерится на сервере и прикладывается к письму, base64 через клиента
не ходит. Рендер или вложение упали — письмо не уходит.
"""
import json
import logging
import re

import html_pdf
import mail_errors
from imap_client import IMAPClient
from sanitize import prepare_body
from tools import _public_base_url

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False, indent=2)


def _clean_filename(name: str) -> str:
    """Имя вложения: без путей и управляющих символов, всегда с .pdf."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name or "").strip()
    if not name:
        name = "Документ"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _parse_recipients(to: str, cc: str):
    """to — ровно один адрес, cc — несколько через запятую.
    Возвращает (to, cc_list) или бросает ValueError."""
    to = (to or "").strip()
    if not _EMAIL.match(to):
        raise ValueError(f"Поле to должно содержать один корректный адрес: {to!r}")
    cc_list = [e.strip() for e in (cc or "").split(",") if e.strip()]
    for addr in cc_list:
        if not _EMAIL.match(addr):
            raise ValueError(f"Некорректный адрес в cc: {addr!r}")
    return to, cc_list


def register_tools(mcp):
    @mcp.tool()
    def preview_html_pdf(html: str, pdf_filename: str, seal: bool = False) -> str:
        """
        Собрать PDF из HTML на сервере и вернуть ССЫЛКУ для предпросмотра —
        БЕЗ отправки. Используй ПЕРЕД send_html_pdf: покажи ссылку
        пользователю и дождись явного «да».

        Рендер: A4, кириллица, Times New Roman / Liberation Serif, таблицы
        не рвутся между страницами. Внешние ресурсы (http, file) запрещены —
        картинки только как data:-URI.

        Args:
            html: HTML документа (фрагмент или полная страница).
            pdf_filename: имя файла вложения (.pdf добавится само).
            seal: true — маркер <img data-seal> в HTML заменяется на печать
                с подписью (тот же файл, что в send_letter). Печать
                позиционируется абсолютно в месте маркера, позади текста;
                сдвиг задавай style маркера, например
                <img data-seal style="left:60mm; top:-8mm">, поставив его
                внутри блока с position:relative на строке подписи.

        Returns:
            JSON: preview_url, expires_in_min, pdf_size_bytes, sha256.
        """
        pdf_filename = _clean_filename(pdf_filename)
        try:
            pdf = html_pdf.render_html_pdf(html, seal)
        except Exception as e:
            log.error(f"Ошибка сборки HTML-PDF (предпросмотр): {e}")
            return _err(f"Не удалось собрать PDF: {e}")

        import preview_store
        token = preview_store.save_preview(pdf, pdf_filename)
        return json.dumps({
            "ok": True,
            "preview_url": f"{_public_base_url()}/preview/{token}.pdf",
            "expires_in_min": preview_store.TTL_SECONDS // 60,
            **html_pdf.pdf_facts(pdf),
            "hint": ("Покажи ссылку пользователю. После подтверждения вызови "
                     "send_html_pdf с теми же html / pdf_filename / seal "
                     "плюс to / subject / email_body."),
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    def send_html_pdf(
        html: str,
        pdf_filename: str,
        to: str,
        subject: str,
        email_body: str,
        seal: bool = False,
        cc: str = "",
        read_receipt: bool = False,
        urgent: bool = False,
    ) -> str:
        """
        Собрать PDF из HTML на сервере и отправить письмом с этим PDF во
        вложении. Клиент передаёт ТОЛЬКО текст. Если рендер или вложение
        упали — письмо НЕ отправляется, возвращается ошибка.

        ВАЖНО: сначала вызови preview_html_pdf с теми же html / pdf_filename /
        seal, покажи ссылку и дождись явного «да». Тестовую отправку делай на
        stavgeo26@mail.ru.

        Args:
            html: HTML документа (см. preview_html_pdf).
            pdf_filename: имя файла вложения.
            to: ОДИН адрес получателя.
            subject: тема письма.
            email_body: сопроводительный текст — HTML (<p>…</p>) или обычный
                текст с переносами строк: сервер сам разобьёт на абзацы.
            seal: подставить печать с подписью вместо <img data-seal>.
            cc: копия, несколько адресов через запятую.
            read_receipt: запросить уведомление о прочтении (по умолчанию false).
            urgent: пометка «срочно» (по умолчанию false).

        Returns:
            JSON с результатом отправки, pdf_size_bytes и sha256 вложения.
        """
        email_body = prepare_body(email_body)
        pdf_filename = _clean_filename(pdf_filename)
        try:
            to, cc_list = _parse_recipients(to, cc)
        except ValueError as e:
            return _err(f"Письмо не отправлено: {e}", sent=False)
        if not (email_body or "").strip():
            return _err("Письмо не отправлено: email_body пустой", sent=False)

        try:
            pdf = html_pdf.render_html_pdf(html, seal)
        except Exception as e:
            log.error(f"Ошибка сборки HTML-PDF: {e}")
            return _err(f"Не удалось собрать PDF — письмо не отправлено: {e}",
                        sent=False)

        client = IMAPClient()
        try:
            client.connect()
            result = client.send_letter_email(
                to=to, subject=subject, html_body=email_body,
                cc=cc_list or None, pdf_bytes=pdf, pdf_filename=pdf_filename,
                read_receipt=read_receipt, urgent=urgent,
            )
            if "error" not in result and not result.get("attachment"):
                return _err("PDF не приложился — проверь отправку", sent=False)
            result.update(html_pdf.pdf_facts(pdf))
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка отправки HTML-PDF: {e}")
            return _err(mail_errors.describe(e), sent=False)
        finally:
            client.disconnect()
