"""
MCP инструменты для работы с почтой Mail.ru.
"""

import json
import logging
import smtplib
import os

import attachment_storage
from imap_client import IMAPClient

log = logging.getLogger(__name__)


def _public_base_url() -> str:
    """Публичный базовый URL сервиса для ссылок предпросмотра.

    Приоритет: явный PUBLIC_BASE_URL -> авто-домен Railway (RAILWAY_PUBLIC_DOMAIN)
    -> известный прод-домен как запасной вариант.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return "https://mail-mcp-server-production.up.railway.app"


def _render_letter_pdf(*, body, addressee, isx_number, date_str, subject,
                       salutation, yadisk_url, appendix, executor,
                       executor_phone) -> bytes:
    """Единый рендер PDF письма для send_letter и preview_letter.

    WeasyPrint импортируется лениво: если системные библиотеки не готовы,
    упадёт только конкретный инструмент, а не весь сервер.
    """
    from letter_render import render_letter_pdf, parse_paragraphs
    paragraphs = parse_paragraphs(body)
    return render_letter_pdf(
        addressee=addressee,
        isx_number=isx_number,
        date_str=date_str,
        subject=subject,
        salutation=salutation,
        paragraphs=paragraphs,
        yadisk_url=yadisk_url,
        appendix=appendix,
        executor=executor,
        executor_phone=executor_phone,
    )


def register_tools(mcp):
    """Регистрирует все MCP инструменты на экземпляре FastMCP."""

    def _with_imap(func):
        """Обёртка: создаёт IMAP соединение, вызывает func, закрывает."""
        def wrapper(*args, **kwargs):
            client = IMAPClient()
            try:
                client.connect()
                return func(client, *args, **kwargs)
            except Exception as e:
                log.error(f"Ошибка IMAP: {e}")
                return json.dumps({"error": str(e)}, ensure_ascii=False)
            finally:
                client.disconnect()
        return wrapper

    @mcp.tool()
    def get_new_emails(hours: int = 24, folder: str = "INBOX",
                       limit: int = 50) -> str:
        """Получить новые письма за последние N часов.

        Args:
            hours: За сколько часов показать письма (по умолчанию 24)
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            emails = client.get_emails(folder=folder, since_hours=hours,
                                       limit=limit)
            if not emails:
                return json.dumps(
                    {"message": f"Новых писем за {hours}ч нет", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def get_important_emails(folder: str = "INBOX",
                             limit: int = 50) -> str:
        """Получить письма с флагом Important/Flagged.

        Args:
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            emails = client.get_important_emails(folder=folder, limit=limit)
            if not emails:
                return json.dumps(
                    {"message": "Важных писем нет", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def search_emails(query: str = "", sender: str = "",
                      date_from: str = "", date_to: str = "",
                      folder: str = "INBOX", limit: int = 50) -> str:
        """Устаревший поиск писем. Предпочтительная замена — search_mail.

        search_mail умеет искать по нескольким ключевым словам сразу, по
        флагам и вложениям, отдаёт компактную выдачу и показывает режим
        поиска. Этот инструмент оставлен для совместимости со старыми
        сценариями и теперь тоже работает через UID SEARCH — возвращаемые
        UID пригодны для get_email_body, read_messages и move_email.

        Args:
            query: Текст для поиска в теме и теле письма
            sender: Email или имя отправителя
            date_from: Дата начала поиска в формате YYYY-MM-DD
            date_to: Дата конца поиска в формате YYYY-MM-DD (включительно)
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            # Если задан диапазон дат — старые первыми (хронологически)
            sort_order = "asc" if date_to else "desc"
            emails = client.search_emails(
                query=query, sender=sender, date_from=date_from,
                date_to=date_to, folder=folder, limit=limit,
                sort_order=sort_order,
            )
            if not emails:
                return json.dumps(
                    {"message": "Ничего не найдено", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def get_email_body(email_uid: str, folder: str = "INBOX",
                       max_chars: int = 5000,
                       strip_quotes: bool = True) -> str:
        """Получить содержимое письма по его UID.

        Тело отдаётся текстом (HTML приводится к читаемому виду), вложения —
        списком с именем, типом и размером. Просмотр НЕ помечает письмо
        прочитанным.

        Args:
            email_uid: UID письма (из search_mail, get_new_emails или
                       get_important_emails), только цифры
            folder: Папка почты, обычное имя, можно кириллицей
            max_chars: Максимум символов тела; при обрезке в ответе truncated=true
            strip_quotes: Убрать процитированную переписку в отдельное поле
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.get_email_body(
                email_uid=email_uid, folder=folder,
                max_chars=max_chars, strip_quotes=strip_quotes,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def analyze_email(email_uid: str, folder: str = "INBOX") -> str:
        """Получить структурированные данные письма для анализа:
        отправитель, тема, тело, вложения, флаги.

        Читает по UID и не помечает письмо прочитанным. Если тело пустое,
        а вложения есть — содержание письма достанет get_attachment_text.

        Args:
            email_uid: UID письма (из search_mail), только цифры
            folder: Папка почты, обычное имя, можно кириллицей
        """
        @_with_imap
        def _run(client: IMAPClient):
            data = client.get_email_body(email_uid=email_uid, folder=folder)
            if "error" in data:
                return json.dumps(data, ensure_ascii=False)
            analysis = {
                "uid": data.get("uid", email_uid),
                "sender": f"{data['sender_name']} <{data['sender_email']}>",
                "subject": data["subject"],
                "date": data["date"],
                "body": data.get("body", ""),
                "body_truncated": data.get("truncated", False),
                "attachments": data.get("attachments", []),
                "flags": data.get("flags", []),
                "flagged": data.get("flagged", False),
                "seen": data.get("seen", False),
                "has_attachments": len(data.get("attachments", [])) > 0,
                "instructions": (
                    "Проанализируй это письмо и определи: "
                    "1) Суть письма (2-3 предложения) "
                    "2) Требуемое действие от получателя "
                    "3) Срочность (высокая/средняя/низкая) "
                    "4) Категория (госорган, финансы, лид, информационное, спам)"
                ),
            }
            return json.dumps(analysis, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def prepare_reply(email_uid: str, folder: str = "INBOX") -> str:
        """Подготовить ответ: показать всех получателей ПЕРЕД отправкой.
        ОБЯЗАТЕЛЬНО вызови перед send_reply, чтобы показать пользователю
        кому пойдёт письмо (To, CC) и дать возможность изменить список.

        Args:
            email_uid: UID письма, на которое хотим ответить
            folder: Папка, где находится оригинальное письмо
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.get_reply_info(
                email_uid=email_uid, folder=folder,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def send_reply(email_uid: str, body: str,
                   folder: str = "INBOX",
                   reply_all: bool = False,
                   cc_override: str = "",
                   attachments: str = "",
                   attachment_ids: str = "") -> str:
        """Ответить на письмо. СНАЧАЛА вызови prepare_reply чтобы показать
        пользователю получателей и получить подтверждение.

        Письмо сохраняется в папку Отправленные автоматически.

        Args:
            email_uid: UID письма, на которое отвечаем
            body: Текст ответа
            folder: Папка, где находится оригинальное письмо
            reply_all: True = ответить всем (отправитель + CC + другие To).
                       False = ответить только отправителю.
            cc_override: Если нужно изменить CC вручную — список email через запятую.
                         Пример: "a@mail.ru, b@mail.ru". Пустая строка = авто.
            attachments: JSON-список вложений вида
                         [{"filename": "f.pdf", "content_base64": "...", "mime_type": "application/pdf"}].
                         Необязательно.
            attachment_ids: JSON-список upload_id, полученных через
                            attachment_upload_finish. Используй для файлов любого
                            размера вместо передачи полного base64 в attachments
                            (который ограничен размером tool call).
                            Файлы удаляются после отправки.
        """
        @_with_imap
        def _run(client: IMAPClient):
            cc_list = None
            if cc_override.strip():
                cc_list = [e.strip() for e in cc_override.split(",") if e.strip()]
            result = client.send_reply(
                email_uid=email_uid, body=body, folder=folder,
                reply_all=reply_all, cc_override=cc_list,
                attachments_json=attachments or None,
                attachment_ids_json=attachment_ids or None,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def forward_email(email_uid: str, to: str,
                      comment: str = "",
                      folder: str = "INBOX") -> str:
        """Переслать письмо (с вложениями) на указанный email-адрес.
        Пересылает полное письмо: тело + все вложения (PDF, DOC и т.д.).

        Оригинал читается строго по UID — идентификатор бери из search_mail
        или search_mail-совместимой выдачи, иначе можно переслать не то письмо.

        Args:
            email_uid: UID оригинального письма (только цифры)
            to: Email получателя (например, ashirovna2012@gmail.com)
            comment: Комментарий перед пересланным письмом (необязательно)
            folder: Папка с оригиналом (по умолчанию INBOX)
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.forward_email(
                email_uid=email_uid, to=to,
                comment=comment, folder=folder,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def send_new_email(to: str, subject: str, body: str,
                       cc: str = "",
                       attachment_urls: str = "",
                       attachments: str = "",
                       attachment_ids: str = "") -> str:
        """Отправить новое письмо (не ответ, а самостоятельное).
        Подпись добавляется автоматически. Тело поддерживает HTML.

        Args:
            to: Email получателя (например, client@example.com)
            subject: Тема письма
            body: Текст письма (HTML разметка поддерживается)
            cc: Копия — email через запятую (необязательно)
            attachment_urls: URL файлов для вложения через запятую (прямые ссылки или Яндекс.Диск). Необязательно.
            attachments: JSON-список вложений вида
                         [{"filename": "f.pdf", "content_base64": "...", "mime_type": "application/pdf"}].
                         Необязательно.
            attachment_ids: JSON-список upload_id, полученных через
                            attachment_upload_finish. Используй для файлов любого
                            размера вместо передачи полного base64 в attachments
                            (который ограничен размером tool call).
                            Файлы удаляются после отправки.
        """
        @_with_imap
        def _run(client: IMAPClient):
            cc_list = None
            if cc.strip():
                cc_list = [e.strip() for e in cc.split(",") if e.strip()]
            urls_list = None
            if attachment_urls.strip():
                urls_list = [u.strip() for u in attachment_urls.split(",") if u.strip()]
            result = client.send_email(
                to=to, subject=subject, body=body,
                cc=cc_list, attachment_urls=urls_list,
                attachments_json=attachments or None,
                attachment_ids_json=attachment_ids or None,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def attachment_upload_start(filename: str,
                                mime_type: str = "application/octet-stream") -> str:
        """Начать chunked-загрузку вложения. Используй, когда файл не помещается
        в один tool call (больше ~60 КБ с учётом base64).

        Далее:
          1) режь base64-содержимое файла на куски по ~50000 символов;
          2) вызывай attachment_upload_chunk(upload_id, chunk_base64) для каждого;
          3) вызови attachment_upload_finish(upload_id);
          4) передай upload_id в send_new_email/send_reply через параметр
             attachment_ids (JSON-список). Файл удалится после отправки.

        Сессия хранится 60 минут, лимит на файл — 25 МБ.

        Args:
            filename: Имя файла (например, "КП_Форелевый.pdf")
            mime_type: MIME-тип (например, "application/pdf")
        """
        try:
            result = attachment_storage.start_upload(filename, mime_type)
        except Exception as e:
            result = {"error": str(e)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def attachment_upload_chunk(upload_id: str, chunk_base64: str) -> str:
        """Передать очередной кусок base64-строки. Куски НЕ декодируются —
        просто конкатенируются на диске. Рекомендуемый размер куска — 50000 символов.

        Args:
            upload_id: ID сессии из attachment_upload_start
            chunk_base64: Кусок base64 (без пробелов/переносов — как есть)
        """
        try:
            result = attachment_storage.append_chunk(upload_id, chunk_base64)
        except Exception as e:
            result = {"upload_id": upload_id, "status": "error", "error": str(e)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def attachment_upload_finish(upload_id: str) -> str:
        """Финализировать chunked-загрузку: декодировать накопленный base64,
        проверить размер и подготовить файл для прикрепления к письму.
        После успеха upload_id можно передавать в send_new_email/send_reply
        через параметр attachment_ids.

        Args:
            upload_id: ID сессии
        """
        try:
            result = attachment_storage.finish_upload(upload_id)
        except Exception as e:
            result = {"upload_id": upload_id, "status": "error", "error": str(e)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def preview_letter(
        subject: str,
        addressee: str,
        isx_number: str,
        date_str: str,
        body: str,
        salutation: str = "",
        yadisk_url: str = "",
        appendix: str = "",
        pdf_filename: str = "Письмо.pdf",
        executor: str = "Виктория",
        executor_phone: str = "8 (938) 350-74-00",
    ) -> str:
        """
        Собрать PDF письма на фирменном бланке ООО «Ставропольгеодезия» и вернуть
        ССЫЛКУ для предпросмотра — БЕЗ отправки. Используй ПЕРЕД send_letter:
        покажи ссылку пользователю, чтобы он глазами проверил вёрстку, текст и
        положение печати/подписи. Отправляй send_letter только после явного «да».

        Параметры — те же контентные поля, что у send_letter (без to/cc/email_body):
        тот же текст даст тот же PDF. Ссылка действует ~60 минут.

        Args:
            subject: тема письма (она же — заголовок по центру в самом письме).
            addressee: блок адресата, строки через перенос '\\n'.
            isx_number: исходящий номер, например "150".
            date_str: дата в готовом виде, например "«03» июня 2026 г.".
            body: JSON-список абзацев {"text": "...", "italic": false} или обычный текст.
            salutation: обращение по центру. Пусто = без обращения.
            yadisk_url: ссылка на материалы (Я.Диск). Пусто = не выводится.
            appendix: текст после слова "Приложение:". Пусто = блок не выводится.
            pdf_filename: имя файла (влияет на имя при открытии PDF).
            executor / executor_phone: исполнитель и телефон в подвале письма.

        Returns:
            JSON со ссылкой preview_url (открыть в браузере), сроком жизни и размером PDF.
        """
        try:
            pdf = _render_letter_pdf(
                body=body, addressee=addressee, isx_number=isx_number,
                date_str=date_str, subject=subject, salutation=salutation,
                yadisk_url=yadisk_url, appendix=appendix, executor=executor,
                executor_phone=executor_phone,
            )
        except Exception as e:
            log.error(f"Ошибка сборки PDF предпросмотра: {e}")
            return json.dumps(
                {"error": f"Не удалось собрать PDF: {e}"}, ensure_ascii=False,
            )

        import preview_store
        token = preview_store.save_preview(pdf, pdf_filename)
        url = f"{_public_base_url()}/preview/{token}.pdf"
        return json.dumps({
            "preview_url": url,
            "expires_in_min": preview_store.TTL_SECONDS // 60,
            "pdf_size_bytes": len(pdf),
            "hint": ("Покажи эту ссылку пользователю для проверки бланка "
                     "(текст, вёрстка, печать). После подтверждения вызови "
                     "send_letter с теми же контентными полями плюс to/cc."),
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    def send_letter(
        to: str,
        subject: str,
        addressee: str,
        isx_number: str,
        date_str: str,
        body: str,
        salutation: str = "",
        yadisk_url: str = "",
        appendix: str = "",
        cc: str = "",
        email_body: str = "",
        pdf_filename: str = "Письмо.pdf",
        executor: str = "Виктория",
        executor_phone: str = "8 (938) 350-74-00",
        attach_pdf: bool = True,
    ) -> str:
        """
        Собрать письмо на фирменном бланке ООО «Ставропольгеодезия» (с печатью и подписью)
        и отправить его. Клиент передаёт ТОЛЬКО текст — PDF рендерится на сервере.

        ВАЖНО: перед отправкой сначала вызови preview_letter с теми же контентными
        полями, покажи пользователю ссылку и дождись подтверждения, что вёрстка,
        текст и положение печати корректны. send_letter вызывай только после «да».

        Args:
            to: email получателя (несколько — через запятую).
            subject: тема письма (она же — заголовок по центру в самом письме).
            addressee: блок адресата, строки через перенос '\\n'.
                Пример: "Генеральному директору\\nАО «Ставропольгоргаз»\\nБалахтину Р.В.\\nул. Маяковского, 9, г. Ставрополь".
            isx_number: исходящий номер, например "150".
            date_str: дата в готовом виде, например "«03» июня 2026 г.".
            body: JSON-список абзацев. Каждый абзац — {"text": "...", "italic": false}.
                italic=true — для абзаца, который надо выделить курсивом (например, описание объекта/линии).
                Можно передать и обычную строку: абзацы разобьются по пустой строке.
            salutation: обращение по центру (например "Уважаемый Роман Витальевич!"). Пусто = без обращения.
            yadisk_url: ссылка на материалы (Я.Диск). Если задана — добавится отдельной строкой и в тело письма.
            appendix: текст после слова "Приложение:". Пусто = блок не выводится.
            cc: копия (несколько — через запятую). Для рассылок согласований сюда обычно arsenal57737@mail.ru.
            email_body: HTML-тело письма-сопроводиловки. Пусто = соберётся автоматически.
            pdf_filename: имя файла вложения.
            executor / executor_phone: исполнитель и телефон в подвале письма.
            attach_pdf: прикладывать ли собранный PDF (по умолчанию да).

        Returns:
            JSON-строка с результатом отправки.
        """
        # --- рендер PDF (общий с preview_letter) ---
        try:
            pdf = _render_letter_pdf(
                body=body, addressee=addressee, isx_number=isx_number,
                date_str=date_str, subject=subject, salutation=salutation,
                yadisk_url=yadisk_url, appendix=appendix, executor=executor,
                executor_phone=executor_phone,
            )
        except Exception as e:
            log.error(f"Ошибка сборки PDF письма: {e}")
            return json.dumps(
                {"error": f"Не удалось собрать PDF письма: {e}"},
                ensure_ascii=False,
            )

        # --- тело письма по умолчанию ---
        if not email_body:
            link_html = (f'<br><br>Материалы топографической съёмки: '
                         f'<a href="{yadisk_url}">{yadisk_url}</a>') if yadisk_url else ""
            email_body = (
                "Здравствуйте!<br><br>"
                f"Направляем официальное письмо ООО «Ставропольгеодезия» "
                f"(исх. № {isx_number} от {date_str}) — во вложении (PDF).{link_html}<br><br>"
                "Ответ просим направить на адрес электронной почты stavgeo26@mail.ru."
            )

        # --- отправка (своя, без автоподписи — подпись/печать уже в PDF) ---
        cc_list = None
        if cc.strip():
            cc_list = [e.strip() for e in cc.split(",") if e.strip()]

        @_with_imap
        def _run(client: IMAPClient):
            result = client.send_letter_email(
                to=to, subject=subject, html_body=email_body, cc=cc_list,
                pdf_bytes=(pdf if attach_pdf else None), pdf_filename=pdf_filename,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def get_folders() -> str:
        """Получить список всех папок почтового ящика."""
        @_with_imap
        def _run(client: IMAPClient):
            folders = client.get_folders()
            return json.dumps(
                {"count": len(folders), "folders": folders},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def test_smtp() -> str:
        """Диагностика SMTP подключения к Mail.ru.
        Проверяет соединение, SSL, EHLO, авторизацию.
        Не отправляет писем — только тестирует подключение.
        """
        smtp_host = os.environ.get("MAIL_SMTP_HOST", "smtp.mail.ru")
        smtp_port = int(os.environ.get("MAIL_SMTP_PORT", "465"))
        mail_user = os.environ.get("MAIL_USERNAME", "")
        mail_pass = os.environ.get("MAIL_PASSWORD", "")

        steps = []
        try:
            # Шаг 1: подключение
            steps.append(f"1. Подключение к {smtp_host}:{smtp_port}...")
            if smtp_port == 465:
                smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            else:
                smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            steps.append("   OK: TCP+SSL соединение установлено")

            # Шаг 2: EHLO
            steps.append("2. Отправка EHLO...")
            code, ehlo_msg = smtp.ehlo()
            ehlo_str = ehlo_msg.decode("utf-8", errors="replace")
            steps.append(f"   OK: код {code}")
            # Извлекаем поддерживаемые методы AUTH
            auth_line = ""
            for line in ehlo_str.split("\n"):
                if "AUTH" in line.upper():
                    auth_line = line.strip()
                    break
            steps.append(f"   Методы авторизации: {auth_line}")

            # Шаг 3: STARTTLS (только для порта 587)
            if smtp_port != 465:
                steps.append("3. STARTTLS...")
                smtp.starttls()
                smtp.ehlo()
                steps.append("   OK: TLS установлен")

            # Шаг 4: авторизация
            steps.append(f"4. Авторизация как {mail_user}...")
            smtp.login(mail_user, mail_pass)
            steps.append("   OK: Авторизация успешна!")

            smtp.quit()
            steps.append("5. SMTP готов к отправке!")
            return json.dumps(
                {"status": "ok", "steps": steps},
                ensure_ascii=False, indent=2,
            )
        except smtplib.SMTPAuthenticationError as e:
            steps.append(f"   ОШИБКА АВТОРИЗАЦИИ: {e.smtp_code} {e.smtp_error}")
            error_msg = e.smtp_error.decode("utf-8", errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            return json.dumps(
                {
                    "status": "auth_error",
                    "error": error_msg,
                    "steps": steps,
                    "fix": (
                        "Ошибка авторизации SMTP. Нужен пароль для внешнего приложения Mail.ru. "
                        "Зайдите в mail.ru → Настройки → Безопасность → "
                        "Пароли для внешних приложений → Создать → "
                        "Тип: 'Полный доступ к Почте'. "
                        "Затем обновите MAIL_PASSWORD в Railway."
                    ),
                },
                ensure_ascii=False, indent=2,
            )
        except Exception as e:
            steps.append(f"   ОШИБКА: {type(e).__name__}: {e}")
            return json.dumps(
                {"status": "error", "error": str(e), "steps": steps},
                ensure_ascii=False, indent=2,
            )
