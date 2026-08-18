"""
IMAP клиент для Mail.ru с поддержкой reconnect и русских кодировок.
"""

import base64
import imaplib
import email
import email.header
import email.message
import json
import smtplib
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import parseaddr, formataddr, formatdate

import attachment_storage
import mail_read
from imap_utf7 import quote_folder, resolve_folder

log = logging.getLogger(__name__)

UID_HINT = ("проверь, что UID получен из search_mail и указана верная папка "
            "(список папок — list_folders)")

IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", "imap.mail.ru")
IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "465"))
MAIL_USER = os.environ.get("MAIL_USERNAME", "")
MAIL_PASS = os.environ.get("MAIL_PASSWORD", "")


def decode_header_value(value: str) -> str:
    """Декодирует заголовок письма (RFC 2047) с поддержкой русских кодировок."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def get_body(msg: email.message.Message) -> str:
    """Извлекает текстовое тело письма (text/plain приоритет, fallback на text/html)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(charset, errors="replace")
                    break
        if not body:
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if ct == "text/html" and "attachment" not in cd:
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(charset, errors="replace")
                        break
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(charset, errors="replace")
    return body.strip()


def get_attachments_info(msg: email.message.Message) -> list[dict]:
    """Возвращает список вложений (имя + размер) без скачивания содержимого."""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        cd = str(part.get("Content-Disposition", ""))
        if "attachment" in cd or "inline" in cd:
            filename = part.get_filename()
            if filename:
                filename = decode_header_value(filename)
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else 0
                attachments.append({"filename": filename, "size_bytes": size})
    return attachments


def parse_recipients(header_value: str) -> list[dict]:
    """Парсит заголовок с адресами (To, CC) в список {name, email}."""
    if not header_value:
        return []
    decoded = decode_header_value(header_value)
    # Разделяем по запятой, парсим каждый адрес
    recipients = []
    for part in decoded.split(","):
        part = part.strip()
        if not part:
            continue
        name, addr = parseaddr(part)
        if addr:
            recipients.append({
                "name": decode_header_value(name) or addr,
                "email": addr,
            })
    return recipients


def attach_base64_files(msg: MIMEMultipart,
                        attachments_json: str | None) -> list[str]:
    """Прикрепляет к msg вложения из JSON-строки с base64-содержимым.

    attachments_json: JSON-строка вида
    [{"filename": "f.pdf", "content_base64": "...", "mime_type": "application/pdf"}]

    Возвращает список имён успешно добавленных файлов.
    """
    if not attachments_json:
        return []
    try:
        items = json.loads(attachments_json)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning(f"Не удалось распарсить attachments JSON: {e}")
        return []
    if not isinstance(items, list):
        log.warning("attachments JSON должен быть списком объектов")
        return []

    added: list[str] = []
    for att in items:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename", "attachment")
        content_b64 = att.get("content_base64", "")
        mime_type = att.get("mime_type", "application/octet-stream")

        try:
            content = base64.b64decode(content_b64)
        except Exception as e:
            log.warning(f"Битое вложение {filename}: {e}")
            continue

        main_type, _, sub_type = mime_type.partition("/")
        if not main_type or not sub_type:
            main_type, sub_type = "application", "octet-stream"

        part = MIMEBase(main_type, sub_type)
        part.set_payload(content)
        encoders.encode_base64(part)
        # UTF-8 filename (RFC 2231) — поддерживает кириллицу
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", filename),
        )
        msg.attach(part)
        added.append(filename)
        log.info(f"Добавлено вложение: {filename} ({len(content)} байт)")
    return added


def attach_uploaded_files(msg: MIMEMultipart,
                          attachment_ids_json: str | None) -> list[str]:
    """Прикрепляет к msg файлы из chunked-upload хранилища по upload_id.

    attachment_ids_json: JSON-список строк-UUID (upload_id), полученных через
    attachment_upload_finish. Вложения читаются из хранилища, но НЕ удаляются —
    удаление происходит у вызывающего после успешной SMTP-отправки.

    Возвращает список upload_id, которые были прикреплены.
    """
    if not attachment_ids_json:
        return []
    try:
        ids = json.loads(attachment_ids_json)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"attachment_ids должен быть JSON-списком: {e}")
    if not isinstance(ids, list):
        raise ValueError("attachment_ids должен быть JSON-списком")

    used: list[str] = []
    for upload_id in ids:
        if not isinstance(upload_id, str):
            raise ValueError(f"upload_id должен быть строкой: {upload_id!r}")
        content, filename, mime_type = attachment_storage.read_attachment(
            upload_id,
        )

        main_type, _, sub_type = mime_type.partition("/")
        if not main_type or not sub_type:
            main_type, sub_type = "application", "octet-stream"

        part = MIMEBase(main_type, sub_type)
        part.set_payload(content)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", filename),
        )
        msg.attach(part)
        used.append(upload_id)
        log.info(
            f"Прикреплён загруженный: {filename} "
            f"({len(content)} байт, id={upload_id})"
        )
    return used


def parse_email_message(msg: email.message.Message, uid: str = "") -> dict:
    """Парсит email.message.Message в словарь."""
    sender_name, sender_email = parseaddr(msg.get("From", ""))
    return {
        "uid": uid,
        "message_id": msg.get("Message-ID", "").strip(),
        "subject": decode_header_value(msg.get("Subject", "Без темы")),
        "sender_name": decode_header_value(sender_name) or sender_email,
        "sender_email": sender_email,
        "to": decode_header_value(msg.get("To", "")),
        "to_list": parse_recipients(msg.get("To", "")),
        "cc": decode_header_value(msg.get("Cc", "")),
        "cc_list": parse_recipients(msg.get("Cc", "")),
        "reply_to": decode_header_value(msg.get("Reply-To", "")),
        "reply_to_email": parseaddr(msg.get("Reply-To", ""))[1] or "",
        "date": msg.get("Date", ""),
        "flags": "",
        "body_preview": get_body(msg)[:500],
        "attachments": get_attachments_info(msg),
    }


class IMAPClient:
    """IMAP клиент с автоматическим reconnect."""

    def __init__(self):
        self.conn: imaplib.IMAP4_SSL | None = None

    def connect(self):
        """Подключиться к IMAP серверу."""
        log.info(f"Подключение к IMAP {IMAP_HOST}:{IMAP_PORT}")
        self.conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        self.conn.login(MAIL_USER, MAIL_PASS)
        log.info("IMAP подключён")

    def disconnect(self):
        """Отключиться от IMAP."""
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _ensure_connected(self):
        if self.conn is None:
            self.connect()
        try:
            self.conn.noop()
        except Exception:
            log.warning("IMAP соединение потеряно, переподключаюсь...")
            self.connect()

    def _select_folder(self, folder: str = "INBOX"):
        """Выбрать папку по человекочитаемому имени, в том числе кириллицей.

        Имя ищется в ответе LIST и кодируется в modified UTF-7 (resolve_folder),
        поэтому «СОГЛАСОВАНИЯ» открывается по имени как есть.
        """
        self._ensure_connected()
        raw = resolve_folder(self.conn, folder)
        status, _ = self.conn.select(quote_folder(raw))
        if status != "OK":
            raise RuntimeError(f"Не удалось открыть папку: {folder}")

    def get_folders(self) -> list[dict]:
        """Получить список папок."""
        self._ensure_connected()
        status, data = self.conn.list()
        if status != "OK":
            return []
        folders = []
        for item in data:
            if isinstance(item, bytes):
                decoded = item.decode("utf-8", errors="replace")
                # Формат: (\\HasNoChildren) "/" "INBOX/Subfolder"
                parts = decoded.rsplit('" "', 1)
                if len(parts) == 2:
                    name = parts[1].rstrip('"')
                else:
                    parts2 = decoded.rsplit(" ", 1)
                    name = parts2[-1].strip('"')
                folders.append({"name": name, "raw": decoded})
        return folders

    def get_emails(self, folder: str = "INBOX", since_hours: int = 24,
                   limit: int = 50) -> list[dict]:
        """Получить письма за последние N часов."""
        self._select_folder(folder)
        since_date = (datetime.now(timezone.utc) - timedelta(hours=since_hours))
        date_str = since_date.strftime("%d-%b-%Y")
        status, data = self.conn.uid("SEARCH", f'(SINCE "{date_str}")')
        if status != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        # Берём последние limit
        uids = uids[-limit:]
        return self._fetch_emails(uids)

    def get_important_emails(self, folder: str = "INBOX",
                             limit: int = 50) -> list[dict]:
        """Получить письма с флагом Important/Flagged."""
        self._select_folder(folder)
        status, data = self.conn.uid("SEARCH", "FLAGGED")
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:]
        return self._fetch_emails(uids)

    def search_emails(self, query: str = "", sender: str = "",
                      date_from: str = "", date_to: str = "",
                      folder: str = "INBOX", limit: int = 50,
                      sort_order: str = "desc") -> list[dict]:
        """Поиск писем по тексту, отправителю, дате.

        Критерии собираются той же проверенной логикой, что и в search_mail:
        плоский список токенов, ключевые слова как UTF-8 bytes, UID SEARCH.
        Прежний вариант склеивал критерии в скобках и уходил через search()
        по порядковым номерам — отсюда и падение на кириллице, и пустая
        выдача по отправителю.

        sort_order: "asc" = старые первыми, "desc" = новые первыми (по умолчанию).
        """
        from mail_tools_patch import _search_uids, build_criteria

        self._select_folder(folder)
        try:
            criteria = build_criteria(
                keywords=[query] if query else None, scope="both",
                date_from=date_from, date_to=date_to, sender=sender,
            )
        except ValueError as exc:
            log.warning(f"Некорректная дата в search_emails: {exc}")
            criteria = build_criteria(
                keywords=[query] if query else None, scope="both",
                sender=sender,
            )

        uid_list, mode = _search_uids(self.conn, criteria)
        log.info(f"search_emails: mode={mode}, найдено {len(uid_list)}")
        uids = [u.encode() for u in uid_list]
        if sort_order == "asc":
            selected_uids = uids[:limit]
        else:
            selected_uids = uids[-limit:]
        return self._fetch_emails(selected_uids)

    def fetch_raw_message(self, email_uid: str,
                          folder: str = "INBOX") -> tuple[bytes, list[str]]:
        """Читает письмо целиком по UID, не трогая флаг «непрочитано».

        BODY.PEEK[] вместо BODY[] — иначе просмотр письма проставляет \\Seen.
        Возвращает (сырое письмо, флаги). Кидает ValueError с понятным текстом,
        если UID некорректен или письма в папке нет.
        """
        uid = str(email_uid).strip()
        if not uid.isdigit():
            raise ValueError(
                f"UID должен состоять только из цифр, получено: {email_uid!r}; "
                f"{UID_HINT}"
            )
        self._select_folder(folder)
        status, data = self.conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        chunk = data[0] if data else None
        if status != "OK" or not isinstance(chunk, tuple) or len(chunk) < 2:
            raise ValueError(
                f"Письмо с UID {uid} не найдено в папке {folder}; {UID_HINT}"
            )
        prefix = chunk[0] if isinstance(chunk[0], bytes) else b""
        return chunk[1], mail_read.parse_flags_list(prefix)

    def get_email_body(self, email_uid: str, folder: str = "INBOX",
                       max_chars: int = 5000,
                       strip_quotes: bool = True) -> dict:
        """Получить содержимое письма по UID. Флаг «непрочитано» не снимается.

        Тело отдаётся текстом: text/plain, либо HTML, приведённый к тексту.
        max_chars обрезает тело и выставляет truncated; strip_quotes выносит
        процитированную переписку в отдельное поле.
        """
        try:
            raw, flags = self.fetch_raw_message(email_uid, folder)
        except ValueError as exc:
            return {"error": str(exc)}

        msg = email.message_from_bytes(raw)
        result = mail_read.build_message_dict(
            msg, uid=str(email_uid).strip(), flags=flags,
            max_chars=max_chars, strip_quotes=strip_quotes,
        )
        # Поля, на которые опираются get_reply_info, send_reply и analyze_email
        result["to_list"] = parse_recipients(msg.get("To", ""))
        result["cc_list"] = parse_recipients(msg.get("Cc", ""))
        result["reply_to_email"] = parseaddr(msg.get("Reply-To", ""))[1] or ""
        result["body_full"] = result["body"]
        result["body_preview"] = result["body"][:500]
        return result

    def _fetch_emails(self, uids: list[bytes]) -> list[dict]:
        """Загрузить заголовки писем по списку UID (не по номерам в папке)."""
        emails = []
        for uid in uids:
            uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
            try:
                status, data = self.conn.uid(
                    "FETCH", uid_str, "(FLAGS BODY.PEEK[HEADER])")
                chunk = data[0] if data else None
                if status != "OK" or not isinstance(chunk, tuple) or len(chunk) < 2:
                    continue
                msg = email.message_from_bytes(chunk[1])
                parsed = parse_email_message(msg, uid_str)
                prefix = chunk[0] if isinstance(chunk[0], bytes) else b""
                parsed["flags"] = mail_read.parse_flags_list(prefix)
                parsed["body_preview"] = ""  # Только заголовки для списка
                emails.append(parsed)
            except Exception as e:
                log.error(f"Ошибка чтения письма UID {uid_str}: {e}")
        return emails

    def _find_sent_folder(self) -> str:
        """Находит папку Отправленные по IMAP-флагу \\Sent."""
        self._ensure_connected()
        status, data = self.conn.list()
        if status != "OK":
            return ""
        for item in data:
            if isinstance(item, bytes):
                decoded = item.decode("utf-8", errors="replace")
                if "\\Sent" in decoded:
                    # Извлекаем имя папки
                    parts = decoded.rsplit('" "', 1)
                    if len(parts) == 2:
                        return parts[1].rstrip('"')
        return ""

    def _save_to_sent(self, msg: MIMEMultipart):
        """Сохраняет отправленное письмо в папку Отправленные."""
        try:
            sent_folder = self._find_sent_folder()
            if not sent_folder:
                log.warning("Папка Отправленные не найдена")
                return
            self._ensure_connected()
            self.conn.append(
                f'"{sent_folder}"',
                "\\Seen",
                None,
                msg.as_bytes(),
            )
            log.info(f"Копия сохранена в {sent_folder}")
        except Exception as e:
            log.error(f"Не удалось сохранить в Отправленные: {e}")

    def get_reply_info(self, email_uid: str,
                       folder: str = "INBOX") -> dict:
        """Получить информацию о получателях для ответа на письмо.
        Возвращает to, cc, reply_to — кому пойдёт ответ."""
        original = self.get_email_body(email_uid, folder)
        if "error" in original:
            return original

        # Кому ответить (Reply-To приоритетнее From)
        reply_to = original.get("reply_to_email", "") or original["sender_email"]
        reply_to_name = original.get("reply_to", "") or original["sender_name"]

        # CC получатели (исключая нас самих)
        cc_list = [
            r for r in original.get("cc_list", [])
            if r["email"].lower() != MAIL_USER.lower()
        ]

        # To получатели из оригинала (кроме нас — это другие люди в To)
        other_to = [
            r for r in original.get("to_list", [])
            if r["email"].lower() != MAIL_USER.lower()
        ]

        return {
            "original_subject": original["subject"],
            "reply_to": {"name": reply_to_name, "email": reply_to},
            "cc_recipients": cc_list,
            "other_to_recipients": other_to,
            "total_recipients_if_reply_all": 1 + len(cc_list) + len(other_to),
            "hint": (
                "Используй send_reply с reply_all=true чтобы ответить всем, "
                "или reply_all=false чтобы ответить только отправителю. "
                "Можно передать cc_override чтобы изменить список CC."
            ),
        }

    def _extract_attachments_data(self, msg: email.message.Message) -> list[dict]:
        """Извлекает вложения с содержимым (для пересылки)."""
        attachments = []
        if not msg.is_multipart():
            return attachments
        for part in msg.walk():
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd or "inline" in cd:
                filename = part.get_filename()
                if filename:
                    filename = decode_header_value(filename)
                    payload = part.get_payload(decode=True)
                    if payload:
                        attachments.append({
                            "filename": filename,
                            "content_type": part.get_content_type(),
                            "data": payload,
                        })
        return attachments

    def forward_email(self, email_uid: str, to: str,
                      comment: str = "",
                      folder: str = "INBOX") -> dict:
        """Переслать письмо (с вложениями) на указанный email.

        Args:
            email_uid: UID оригинального письма
            to: Email получателя
            comment: Комментарий перед пересланным телом (необязательно)
            folder: Папка с оригиналом
        """
        # 1. Забираем полное оригинальное письмо по UID.
        # Именно UID: по порядковому номеру можно молча взять чужое письмо
        # и переслать его постороннему адресату.
        try:
            raw, _ = self.fetch_raw_message(email_uid, folder)
        except ValueError as exc:
            return {"error": str(exc)}

        original = email.message_from_bytes(raw)

        # 2. Парсим заголовки
        orig_subject = decode_header_value(original.get("Subject", "(без темы)"))
        orig_from = decode_header_value(original.get("From", ""))
        orig_date = original.get("Date", "")
        orig_to = decode_header_value(original.get("To", ""))

        # 3. Извлекаем вложения с данными
        attachments = self._extract_attachments_data(original)

        # 4. Извлекаем тело
        original_body = get_body(original)

        # 5. Собираем новое MIME-сообщение
        fwd_subject = f"Fwd: {orig_subject}"

        fwd_header = (
            "---------- Пересланное сообщение ----------\n"
            f"От: {orig_from}\n"
            f"Дата: {orig_date}\n"
            f"Тема: {orig_subject}\n"
            f"Кому: {orig_to}\n"
            "--------------------------------------------\n\n"
        )

        body_parts = []
        if comment.strip():
            body_parts.append(comment.strip())
            body_parts.append("\n\n")
        body_parts.append(fwd_header)
        body_parts.append(original_body)

        signature = (
            "\n\n--\n"
            "С уважением,\n"
            'ООО "Ставропольгеодезия"\n'
            "тел: +7 (938) 350-74-00\n"
            "WhatsApp\n"
            "г. Ставрополь, ул. Тельмана, 41, оф. 37\n"
            "Ставропольгеодезия.рф"
        )
        body_parts.append(signature)
        full_body = "".join(body_parts)

        msg = MIMEMultipart("mixed")
        msg["From"] = formataddr((MAIL_USER.split("@")[0], MAIL_USER))
        msg["To"] = to
        msg["Subject"] = fwd_subject
        msg["Date"] = formatdate(localtime=True)

        # Текстовая часть
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        # Вложения
        from email.mime.application import MIMEApplication
        for att in attachments:
            part = MIMEApplication(att["data"], Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            msg.attach(part)

        # 6. Отправляем
        try:
            log.info(f"SMTP: пересылка {fwd_subject} -> {to} ({len(attachments)} вложений)")
            if SMTP_PORT == 465:
                smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            try:
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.sendmail(MAIL_USER, [to], msg.as_string())
            finally:
                smtp.quit()

            # Сохраняем в Отправленные
            self._save_to_sent(msg)

            log.info(f"Письмо переслано: to={to}, тема: {fwd_subject}")
            return {
                "status": "forwarded",
                "to": to,
                "subject": fwd_subject,
                "attachments_count": len(attachments),
                "attachments": [a["filename"] for a in attachments],
            }

        except Exception as e:
            log.error(f"Ошибка пересылки: {e}")
            return {"error": str(e)}

    def send_reply(self, email_uid: str, body: str,
                   folder: str = "INBOX",
                   reply_all: bool = False,
                   cc_override: list[str] | None = None,
                   attachments_json: str | None = None,
                   attachment_ids_json: str | None = None) -> dict:
        """Ответить на письмо через SMTP.

        reply_all: если True — отвечает всем (To + CC оригинала)
        cc_override: если указан — используется вместо оригинальных CC
        attachments_json: JSON-список вложений с base64-содержимым.
        attachment_ids_json: JSON-список upload_id из chunked-upload.

        body ожидается в HTML (нормализуется в tools.send_reply).
        """
        original = self.get_email_body(email_uid, folder)
        if "error" in original:
            return original

        # Кому отвечаем
        reply_to = original.get("reply_to_email", "") or original["sender_email"]

        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        msg = MIMEMultipart("mixed")
        msg["From"] = formataddr((MAIL_USER.split("@")[0], MAIL_USER))
        msg["To"] = reply_to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["In-Reply-To"] = original.get("message_id", "")
        msg["References"] = original.get("message_id", "")

        # CC получатели
        cc_emails = []
        if cc_override is not None:
            cc_emails = cc_override
        elif reply_all:
            # Собираем всех из CC + To (кроме нас и кроме reply_to)
            seen = {MAIL_USER.lower(), reply_to.lower()}
            for r in original.get("cc_list", []):
                if r["email"].lower() not in seen:
                    cc_emails.append(r["email"])
                    seen.add(r["email"].lower())
            for r in original.get("to_list", []):
                if r["email"].lower() not in seen:
                    cc_emails.append(r["email"])
                    seen.add(r["email"].lower())

        if cc_emails:
            msg["Cc"] = ", ".join(cc_emails)

        # HTML: тело приходит уже нормализованным (sanitize.prepare_body),
        # в text/plain теги показались бы получателю как обычный текст.
        msg.attach(MIMEText(body, "html", "utf-8"))

        attached_files = attach_base64_files(msg, attachments_json)
        uploaded_ids = attach_uploaded_files(msg, attachment_ids_json)

        try:
            log.info(f"SMTP подключение: {SMTP_HOST}:{SMTP_PORT}")
            if SMTP_PORT == 465:
                smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            log.info("SMTP подключён, авторизация...")
            try:
                smtp.login(MAIL_USER, MAIL_PASS)
                log.info("SMTP авторизация успешна, отправка...")
                all_recipients = [reply_to] + cc_emails
                smtp.sendmail(MAIL_USER, all_recipients, msg.as_string())
            finally:
                smtp.quit()

            # Сохраняем в Отправленные
            self._save_to_sent(msg)

            # После успешной отправки удаляем использованные uploads
            for uid in uploaded_ids:
                attachment_storage.delete_attachment(uid)

            result = {
                "status": "sent",
                "to": reply_to,
                "subject": subject,
            }
            if cc_emails:
                result["cc"] = cc_emails
            if attached_files:
                result["attachments"] = attached_files
            if uploaded_ids:
                result["uploaded_attachments"] = uploaded_ids
            log.info(f"Ответ отправлен: to={reply_to}, cc={cc_emails}, тема: {subject}")
            return result

        except Exception as e:
            log.error(f"Ошибка отправки: {e}")
            return {"error": str(e)}

    def _download_attachment(self, url: str) -> dict | None:
        """Скачивает файл по URL и возвращает {filename, data, content_type}."""
        try:
            log.info(f"Скачивание вложения: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")

                # Имя файла из Content-Disposition или из URL
                cd = resp.headers.get("Content-Disposition", "")
                filename = ""
                if "filename=" in cd:
                    # Парсим filename из Content-Disposition
                    for part in cd.split(";"):
                        part = part.strip()
                        if part.startswith("filename="):
                            filename = part.split("=", 1)[1].strip('" ')
                        elif part.startswith("filename*="):
                            # RFC 5987: filename*=UTF-8''encoded_name
                            raw = part.split("'", 2)[-1] if "'" in part else part.split("=", 1)[1]
                            filename = urllib.parse.unquote(raw.strip('" '))

                if not filename:
                    # Извлекаем из пути URL
                    parsed = urllib.parse.urlparse(url)
                    filename = os.path.basename(parsed.path) or "attachment"
                    filename = urllib.parse.unquote(filename)

                log.info(f"Вложение скачано: {filename} ({len(data)} байт)")
                return {
                    "filename": filename,
                    "data": data,
                    "content_type": content_type,
                }
        except Exception as e:
            log.error(f"Не удалось скачать {url}: {e}")
            return None

    def send_email(self, to: str, subject: str, body: str,
                   cc: list[str] | None = None,
                   attachment_urls: list[str] | None = None,
                   attachments_json: str | None = None,
                   attachment_ids_json: str | None = None) -> dict:
        """Отправить новое письмо.

        Args:
            to: Email получателя
            subject: Тема письма
            body: Текст письма (HTML)
            cc: Список CC получателей
            attachment_urls: Список URL файлов для вложения
            attachments_json: JSON-список вложений с base64-содержимым
            attachment_ids_json: JSON-список upload_id из chunked-upload
        """
        signature = (
            '<br><br><div style="border-top:1px solid #ccc;padding-top:10px;margin-top:10px;">'
            '<i style="font-family:Arial,sans-serif;">'
            "<b>С уважением,</b><br>"
            'ООО "Ставропольгеодезия"<br>'
            "тел: +7 (938) 350-74-00<br>"
            "WhatsApp<br>"
            "г. Ставрополь, ул. Тельмана, 41, оф. 37<br>"
            '<a href="http://ставропольгеодезия.рф">Ставропольгеодезия.рф</a>'
            "</i></div>"
            '<br><div style="font-size:11px;color:#888;font-style:italic;">'
            "УВЕДОМЛЕНИЕ О КОНФИДЕНЦИАЛЬНОСТИ: Это электронное сообщение и любые документы, "
            "приложенные к нему, содержат конфиденциальную информацию. Настоящим уведомляем Вас "
            "о том, что если это сообщение не предназначено Вам, использование, копирование, "
            "распространение информации, содержащейся в настоящем сообщении, а также осуществление "
            "любых действий на основе этой информации, строго запрещено. Если Вы получили это "
            "сообщение по ошибке, пожалуйста, сообщите об этом отправителю по электронной почте "
            "и удалите это сообщение.<br>"
            "CONFIDENTIALITY NOTICE: This email and any files attached to it are confidential. "
            "If you are not the intended recipient you are notified that using, copying, distributing "
            "or taking any action in reliance on the contents of this information is strictly prohibited. "
            "If you have received this email in error please notify the sender and delete this email."
            "</div>"
        )

        msg = MIMEMultipart("mixed")
        msg["From"] = formataddr(("ООО Ставропольгеодезия", MAIL_USER))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)

        if cc:
            msg["Cc"] = ", ".join(cc)

        # HTML тело + подпись
        html_body = body + signature
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        # Вложения по URL
        downloaded_files = []
        if attachment_urls:
            for url in attachment_urls:
                att = self._download_attachment(url.strip())
                if att:
                    part = MIMEApplication(att["data"], Name=att["filename"])
                    part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
                    msg.attach(part)
                    downloaded_files.append(att["filename"])

        # Вложения из base64 (attachments JSON)
        b64_files = attach_base64_files(msg, attachments_json)
        downloaded_files.extend(b64_files)

        # Вложения из chunked-upload хранилища
        uploaded_ids = attach_uploaded_files(msg, attachment_ids_json)

        # Отправка через SMTP
        try:
            all_recipients = [to]
            if cc:
                all_recipients.extend(cc)

            log.info(f"SMTP: отправка нового письма -> {to}, тема: {subject}")
            if SMTP_PORT == 465:
                smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            try:
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.sendmail(MAIL_USER, all_recipients, msg.as_string())
            finally:
                smtp.quit()

            # Сохраняем в Отправленные
            self._save_to_sent(msg)

            # После успешной отправки удаляем использованные uploads
            for uid in uploaded_ids:
                attachment_storage.delete_attachment(uid)

            result = {
                "status": "sent",
                "to": to,
                "subject": subject,
            }
            if cc:
                result["cc"] = cc
            if downloaded_files:
                result["attachments"] = downloaded_files
            if uploaded_ids:
                result["uploaded_attachments"] = uploaded_ids
            log.info(f"Письмо отправлено: to={to}, тема: {subject}")
            return result

        except Exception as e:
            log.error(f"Ошибка отправки: {e}")
            return {"error": str(e)}

    def send_letter_email(self, to: str, subject: str, html_body: str,
                          cc: list[str] | None = None,
                          pdf_bytes: bytes | None = None,
                          pdf_filename: str = "Письмо.pdf") -> dict:
        """Отправить письмо с готовым PDF-вложением (официальный бланк).

        В отличие от send_email НЕ добавляет автоподпись и блок
        конфиденциальности — html_body отправляется как есть
        (подпись/печать уже внутри PDF). Креды, From и сохранение в
        «Отправленные» переиспользуются из общей SMTP-логики.

        Args:
            to: Email получателя
            subject: Тема письма
            html_body: HTML-тело письма-сопроводиловки (как есть, без автоподписи)
            cc: Список CC получателей
            pdf_bytes: Готовые байты PDF (None — отправить без вложения)
            pdf_filename: Имя файла вложения (поддерживается кириллица)
        """
        msg = MIMEMultipart("mixed")
        msg["From"] = formataddr(("ООО Ставропольгеодезия", MAIL_USER))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        if cc:
            msg["Cc"] = ", ".join(cc)

        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if pdf_bytes:
            part = MIMEApplication(pdf_bytes, _subtype="pdf")
            # 3-кортеж (charset, lang, value) => корректное RFC 2231
            # кодирование имени файла с кириллицей.
            part.add_header(
                "Content-Disposition", "attachment",
                filename=("utf-8", "", pdf_filename),
            )
            msg.attach(part)

        try:
            all_recipients = [to]
            if cc:
                all_recipients.extend(cc)

            log.info(f"SMTP: отправка письма-бланка -> {to}, тема: {subject}")
            if SMTP_PORT == 465:
                smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            try:
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.sendmail(MAIL_USER, all_recipients, msg.as_string())
            finally:
                smtp.quit()

            # Сохраняем в Отправленные
            self._save_to_sent(msg)

            result = {
                "status": "sent",
                "to": to,
                "subject": subject,
            }
            if cc:
                result["cc"] = cc
            if pdf_bytes:
                result["attachment"] = pdf_filename
            log.info(f"Письмо-бланк отправлено: to={to}, тема: {subject}")
            return result

        except Exception as e:
            log.error(f"Ошибка отправки письма-бланка: {e}")
            return {"error": str(e)}
