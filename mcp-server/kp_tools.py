# -*- coding: utf-8 -*-
"""MCP-инструменты коммерческих предложений: preview_kp / send_kp.

Переиспользуют рендер-пайплайн писем: WeasyPrint (kp_render), хранилище
предпросмотра (preview_store) и SMTP-отправку с PDF-вложением
(IMAPClient.send_letter_email). PDF всегда собирается на сервере —
никакой передачи файлов через ассистента.
"""
import json
import logging

from imap_client import IMAPClient
from tools import _public_base_url

log = logging.getLogger(__name__)


def _render_kp_pdf(**kwargs) -> bytes:
    """Единый рендер PDF КП для preview_kp и send_kp.

    WeasyPrint импортируется лениво: если системные библиотеки не готовы,
    упадёт только конкретный инструмент, а не весь сервер.
    """
    from kp_render import render_kp_pdf
    return render_kp_pdf(**kwargs)


def register_tools(mcp):
    """Регистрирует инструменты КП на экземпляре FastMCP."""

    @mcp.tool()
    def preview_kp(
        date_str: str,
        addressee: str,
        object_name: str,
        body: str,
        price_total: float,
        result: str,
        timeline: str,
        pdf_filename: str,
        entity: str = "ooo",
        kp_number: str = "",
        object_address: str = "",
        cadastral_number: str = "",
        works_table: str = "",
        payment_terms: str = "",
        validity: str = "",
    ) -> str:
        """
        Собрать PDF коммерческого предложения (КП) ООО «Ставропольгеодезия»
        на фирменном бланке (баннер, реквизиты, блок «Наши клиенты», печать,
        подпись) и вернуть ССЫЛКУ для предпросмотра — БЕЗ отправки.

        Порядок работы: 1) preview_kp -> покажи ссылку пользователю, он глазами
        проверяет вёрстку, цифры и печать; 2) по желанию — тестовая отправка
        send_kp на stavgeo26@mail.ru; 3) только после явного «да» — send_kp на
        адрес клиента (перед отправкой покажи адрес получателя).

        ВАЖНО: суммы, сроки и номер КП — только со слов пользователя.
        Прочерки и заглушки запрещены. Сумму прописью сервер генерирует сам
        из price_total — передавать её не нужно.

        Args:
            date_str: дата в готовом виде, например "«13» августа 2026 г.".
            addressee: блок адресата, строки через '\\n' (должность /
                организация / ФИО или "Руководителю…").
            object_name: наименование объекта работ.
            body: JSON-список абзацев [{"text": "...", "bold": false}] —
                состав работ по ТЗ и пояснения. Можно обычный текст:
                абзацы разобьются по пустой строке.
            price_total: итоговая сумма в рублях, только цифрой
                (например 450000 или 1234567.50).
            result: что получает заказчик по итогу работ.
            timeline: срок, формат "X рабочих дней с момента поступления аванса".
            pdf_filename: имя файла вида "КП_Организация_Объект.pdf".
            entity: "ooo" (полный бланк с баннером, по умолчанию) или
                "ip" (ИП-версия: без баннера и «Наших клиентов»).
            kp_number: номер КП. Не передан — блок «№» не выводится вовсе.
            object_address: адрес объекта. Пусто = строка не выводится.
            cadastral_number: кадастровый номер участка. Пусто = не выводится.
            works_table: JSON-список строк [{"name": "...", "volume": "...",
                "price": "..."}] — табличный состав работ. Таблица не рвётся
                между страницами.
            payment_terms: порядок оплаты. Пусто = стандарт компании
                (50% аванс / 50% после Акта).
            validity: срок действия КП. Пусто = блок не выводится.

        Returns:
            JSON со ссылкой preview_url (открыть в браузере), сроком жизни
            (~60 минут) и размером PDF.
        """
        try:
            pdf = _render_kp_pdf(
                entity=entity, kp_number=kp_number, date_str=date_str,
                addressee=addressee, object_name=object_name,
                object_address=object_address,
                cadastral_number=cadastral_number, body=body,
                works_table=works_table, price_total=price_total,
                payment_terms=payment_terms, result=result,
                timeline=timeline, validity=validity,
            )
        except Exception as e:
            log.error(f"Ошибка сборки PDF КП (предпросмотр): {e}")
            return json.dumps(
                {"error": f"Не удалось собрать PDF КП: {e}"},
                ensure_ascii=False,
            )

        import preview_store
        token = preview_store.save_preview(pdf, pdf_filename)
        url = f"{_public_base_url()}/preview/{token}.pdf"
        return json.dumps({
            "preview_url": url,
            "expires_in_min": preview_store.TTL_SECONDS // 60,
            "pdf_size_bytes": len(pdf),
            "hint": ("Покажи эту ссылку пользователю для проверки КП "
                     "(вёрстка, цифры, печать). После подтверждения вызови "
                     "send_kp с теми же контентными полями плюс "
                     "to/subject/email_body."),
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    def send_kp(
        to: str,
        subject: str,
        email_body: str,
        date_str: str,
        addressee: str,
        object_name: str,
        body: str,
        price_total: float,
        result: str,
        timeline: str,
        pdf_filename: str,
        entity: str = "ooo",
        kp_number: str = "",
        object_address: str = "",
        cadastral_number: str = "",
        works_table: str = "",
        payment_terms: str = "",
        validity: str = "",
    ) -> str:
        """
        Собрать PDF коммерческого предложения (КП) на сервере и отправить
        письмом с PDF-вложением. Клиент передаёт ТОЛЬКО текст — файл
        рендерится и прикладывается на сервере. Если рендер или прикрепление
        упали — письмо НЕ отправляется, возвращается ошибка.

        ВАЖНО: перед отправкой сначала вызови preview_kp с теми же контентными
        полями, покажи пользователю ссылку и дождись явного «да». Перед
        send_kp покажи пользователю адрес получателя. Тестовую отправку
        (по желанию) делай на stavgeo26@mail.ru.

        Суммы, сроки и номер КП — только со слов пользователя. Прочерки и
        заглушки запрещены. Полный текст КП в теле письма не дублируется.

        Args:
            to: email получателя (несколько — через запятую).
            subject: тема письма.
            email_body: короткое сопроводительное письмо, НАСТОЯЩИЙ HTML
                (<p>…</p>, как у send_letter; переносы '\\n' не работают).
            date_str: дата в готовом виде, например "«13» августа 2026 г.".
            addressee: блок адресата, строки через '\\n'.
            object_name: наименование объекта работ.
            body: JSON-список абзацев [{"text": "...", "bold": false}].
            price_total: итоговая сумма в рублях, только цифрой.
            result: что получает заказчик по итогу работ.
            timeline: срок, формат "X рабочих дней с момента поступления аванса".
            pdf_filename: имя файла вида "КП_Организация_Объект.pdf".
            entity: "ooo" (по умолчанию) или "ip".
            kp_number: номер КП. Не передан — блок «№» не выводится.
            object_address: адрес объекта (опционально).
            cadastral_number: кадастровый номер участка (опционально).
            works_table: JSON-список строк таблицы работ (опционально).
            payment_terms: порядок оплаты. Пусто = стандарт компании.
            validity: срок действия КП (опционально).

        Returns:
            JSON-строка с результатом отправки.
        """
        # --- рендер PDF (общий с preview_kp); упал — НЕ отправляем ---
        try:
            pdf = _render_kp_pdf(
                entity=entity, kp_number=kp_number, date_str=date_str,
                addressee=addressee, object_name=object_name,
                object_address=object_address,
                cadastral_number=cadastral_number, body=body,
                works_table=works_table, price_total=price_total,
                payment_terms=payment_terms, result=result,
                timeline=timeline, validity=validity,
            )
        except Exception as e:
            log.error(f"Ошибка сборки PDF КП: {e}")
            return json.dumps(
                {"error": f"Не удалось собрать PDF КП — письмо не отправлено: {e}"},
                ensure_ascii=False,
            )
        if not pdf:
            return json.dumps(
                {"error": "Рендер вернул пустой PDF — письмо не отправлено"},
                ensure_ascii=False,
            )

        client = IMAPClient()
        try:
            client.connect()
            result_send = client.send_letter_email(
                to=to, subject=subject, html_body=email_body,
                pdf_bytes=pdf, pdf_filename=pdf_filename,
            )
            result_send["pdf_size_bytes"] = len(pdf)
            return json.dumps(result_send, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка отправки КП: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        finally:
            client.disconnect()
