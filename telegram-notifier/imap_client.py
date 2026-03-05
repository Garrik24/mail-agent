"""
IMAP клиент для Telegram нотификатора.
Упрощённая версия: только получение новых писем.
"""

import hashlib
import imaplib
import email
import email.header
import email.message
import logging
import os
from email.utils import parseaddr

log = logging.getLogger(__name__)

IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", "imap.mail.ru")
IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
MAIL_USER = os.environ.get("MAIL_USERNAME", "")
MAIL_PASS = os.environ.get("MAIL_PASSWORD", "")

# IMAP-флаги папок, которые игнорируем
IGNORED_FLAGS = {"\\Sent", "\\Drafts", "\\Trash", "\\Spam", "\\Junk"}

# Имена папок, которые игнорируем (fallback если флагов нет)
IGNORED_FOLDERS = {"Spam", "Junk", "Trash", "Drafts", "Sent"}

# Отправители, которых игнорируем
IGNORED_SENDERS = {"noreply@", "newsletter@", "no-reply@", "info-noreply@",
                   "mailer-daemon@", "postmaster@"}


def decode_header_value(value: str) -> str:
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


def is_ignored_sender(sender_email: str) -> bool:
    """Проверить, что отправитель в списке игнорируемых."""
    sender_lower = sender_email.lower()
    for prefix in IGNORED_SENDERS:
        if sender_lower.startswith(prefix):
            return True
    return False


def get_checkable_folders(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Получить список папок для проверки (исключая Sent/Trash/Drafts/Spam)."""
    status, data = conn.list()
    if status != "OK":
        return ["INBOX"]
    folders = []
    for item in data:
        if isinstance(item, bytes):
            decoded = item.decode("utf-8", errors="replace")

            # Извлекаем флаги из скобок: (\Sent) "/" "name"
            flags_part = ""
            if decoded.startswith("("):
                end = decoded.index(")")
                flags_part = decoded[1:end]

            # Пропускаем по IMAP-флагам
            skip = False
            for flag in IGNORED_FLAGS:
                if flag in flags_part:
                    skip = True
                    break
            if skip:
                continue

            # Извлекаем имя папки
            parts = decoded.rsplit('" "', 1)
            if len(parts) == 2:
                name = parts[1].rstrip('"')
            else:
                parts2 = decoded.rsplit(" ", 1)
                name = parts2[-1].strip('"')

            # Fallback: проверяем имя (для папок без флагов)
            if name in IGNORED_FOLDERS:
                continue

            folders.append(name)
    return folders if folders else ["INBOX"]


def fetch_recent_emails(since_date_str: str,
                        processed_ids: set[str]) -> list[dict]:
    """
    Получить новые письма из всех папок с момента since_date_str.
    since_date_str: формат "DD-Mon-YYYY" для IMAP SINCE.
    processed_ids: множество уже обработанных message_id.
    Возвращает список словарей с данными писем.
    """
    conn = None
    emails = []
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(MAIL_USER, MAIL_PASS)

        folders = get_checkable_folders(conn)
        log.info(f"Проверяю {len(folders)} папок")

        for folder in folders:
            try:
                status, _ = conn.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue

                status, data = conn.search(None, f'(SINCE "{since_date_str}")')
                if status != "OK" or not data[0]:
                    continue

                uids = data[0].split()
                log.info(f"Папка {folder}: {len(uids)} писем с {since_date_str}")

                for uid in uids:
                    try:
                        status, msg_data = conn.fetch(uid, "(RFC822)")
                        if status != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)

                        _, sender_email = parseaddr(msg.get("From", ""))

                        message_id = msg.get("Message-ID", "").strip()
                        # Fallback ID если Message-ID пустой
                        if not message_id:
                            raw_key = f"{sender_email}|{msg.get('Subject', '')}|{msg.get('Date', '')}"
                            message_id = f"fallback-{hashlib.md5(raw_key.encode()).hexdigest()}"
                        # Пропускаем уже обработанные
                        if message_id in processed_ids:
                            continue
                        # Пропускаем свои исходящие
                        if sender_email.lower() == MAIL_USER.lower():
                            continue
                        # Пропускаем игнорируемых отправителей
                        if is_ignored_sender(sender_email):
                            continue

                        sender_name = decode_header_value(
                            parseaddr(msg.get("From", ""))[0]
                        ) or sender_email
                        subject = decode_header_value(
                            msg.get("Subject", "Без темы")
                        )
                        body = get_body(msg)
                        date_str = msg.get("Date", "")
                        is_flagged = "\\Flagged" in str(
                            msg_data[0][0] if isinstance(msg_data[0][0], bytes)
                            else b""
                        )

                        emails.append({
                            "message_id": message_id,
                            "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                            "folder": folder,
                            "sender_name": sender_name,
                            "sender_email": sender_email,
                            "subject": subject,
                            "body": body[:5000],
                            "date": date_str,
                            "is_flagged": is_flagged,
                        })
                    except Exception as e:
                        log.error(f"Ошибка чтения письма {uid} в {folder}: {e}")
            except Exception as e:
                log.error(f"Ошибка обработки папки {folder}: {e}")

    except Exception as e:
        log.error(f"Ошибка IMAP подключения: {e}")
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass

    log.info(f"Всего новых писем: {len(emails)}")
    return emails
