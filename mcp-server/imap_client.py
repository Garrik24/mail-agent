"""
IMAP клиент для Mail.ru с поддержкой reconnect и русских кодировок.
"""

import imaplib
import email
import email.header
import email.message
import smtplib
import logging
import os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr, formataddr, formatdate

log = logging.getLogger(__name__)

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
        """Выбрать папку. Для русских имён используем кавычки."""
        self._ensure_connected()
        status, _ = self.conn.select(f'"{folder}"')
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
        status, data = self.conn.search(None, f'(SINCE "{date_str}")')
        if status != "OK" or not data[0]:
            return []

        uids = data[0].split()
        # Берём последние limit
        uids = uids[-limit:]
        return self._fetch_emails(uids)

    def get_important_emails(self, folder: str = "INBOX",
                             limit: int = 50) -> list[dict]:
        """Получить письма с флагом Important/Flagged."""
        self._select_folder(folder)
        status, data = self.conn.search(None, "FLAGGED")
        if status != "OK" or not data[0]:
            return []
        uids = data[0].split()[-limit:]
        return self._fetch_emails(uids)

    def search_emails(self, query: str = "", sender: str = "",
                      date_from: str = "", folder: str = "INBOX",
                      limit: int = 50) -> list[dict]:
        """Поиск писем по тексту, отправителю, дате."""
        self._select_folder(folder)
        criteria = []
        if query:
            criteria.append(f'(OR SUBJECT "{query}" BODY "{query}")')
        if sender:
            criteria.append(f'(FROM "{sender}")')
        if date_from:
            # date_from в формате YYYY-MM-DD
            try:
                dt = datetime.strptime(date_from, "%Y-%m-%d")
                criteria.append(f'(SINCE "{dt.strftime("%d-%b-%Y")}")')
            except ValueError:
                pass
        if not criteria:
            criteria.append("ALL")

        search_str = " ".join(criteria) if len(criteria) > 1 else criteria[0]
        status, data = self.conn.search(None, search_str)
        if status != "OK" or not data[0]:
            return []
        uids = data[0].split()[-limit:]
        return self._fetch_emails(uids)

    def get_email_body(self, email_uid: str,
                       folder: str = "INBOX") -> dict:
        """Получить полное содержимое письма по UID."""
        self._select_folder(folder)
        status, data = self.conn.fetch(email_uid.encode(), "(RFC822 FLAGS)")
        if status != "OK" or not data or not data[0]:
            return {"error": f"Письмо {email_uid} не найдено"}
        raw = data[0][1]
        msg = email.message_from_bytes(raw)
        result = parse_email_message(msg, email_uid)
        result["body_full"] = get_body(msg)
        result["body_preview"] = result["body_full"][:500]
        # Извлечь флаги
        flags_data = data[0][0] if isinstance(data[0][0], bytes) else b""
        result["flags"] = flags_data.decode("utf-8", errors="replace")
        return result

    def _fetch_emails(self, uids: list[bytes]) -> list[dict]:
        """Загрузить письма по списку UID."""
        emails = []
        for uid in uids:
            try:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                status, data = self.conn.fetch(uid, "(RFC822.HEADER FLAGS)")
                if status != "OK" or not data or not data[0]:
                    continue
                raw_header = data[0][1]
                msg = email.message_from_bytes(raw_header)
                parsed = parse_email_message(msg, uid_str)
                # Флаги
                flags_data = data[0][0] if isinstance(data[0][0], bytes) else b""
                parsed["flags"] = flags_data.decode("utf-8", errors="replace")
                parsed["body_preview"] = ""  # Только заголовки для списка
                emails.append(parsed)
            except Exception as e:
                log.error(f"Ошибка чтения письма {uid}: {e}")
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

    def send_reply(self, email_uid: str, body: str,
                   folder: str = "INBOX",
                   reply_all: bool = False,
                   cc_override: list[str] | None = None) -> dict:
        """Ответить на письмо через SMTP.

        reply_all: если True — отвечает всем (To + CC оригинала)
        cc_override: если указан — используется вместо оригинальных CC
        """
        original = self.get_email_body(email_uid, folder)
        if "error" in original:
            return original

        # Кому отвечаем
        reply_to = original.get("reply_to_email", "") or original["sender_email"]

        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        msg = MIMEMultipart()
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

        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.login(MAIL_USER, MAIL_PASS)
                smtp.send_message(msg)

            # Сохраняем в Отправленные
            self._save_to_sent(msg)

            result = {
                "status": "sent",
                "to": reply_to,
                "subject": subject,
            }
            if cc_emails:
                result["cc"] = cc_emails
            log.info(f"Ответ отправлен: to={reply_to}, cc={cc_emails}, тема: {subject}")
            return result

        except Exception as e:
            log.error(f"Ошибка отправки: {e}")
            return {"error": str(e)}
