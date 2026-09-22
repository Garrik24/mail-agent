# -*- coding: utf-8 -*-
"""Вложения из уже полученных или отправленных писем — по UID письма.

Ассистент ссылается на файл письмом-источником (UID + папка + имя), сервер
сам достаёт байты по IMAP и прикладывает их к исходящему письму. Файл не идёт
через чат и chunked-загрузку.

Любая ошибка поиска или проверки — AttachmentError, и письмо при этом не
отправляется: частичная отправка запрещена. Проверки (офисные форматы наружу,
лимит размера) выполняет сервер, а не ассистент.
"""

import email
import json
import logging
import mimetypes
import os
import re
import unicodedata

import mail_read

log = logging.getLogger(__name__)

# Картинки подписи и оформления письма — это не вложения для пересылки
SIGNATURE_IMG_RE = re.compile(
    r"^(image\d+\.(png|jpe?g|gif)|mailrusigimg_.*)$", re.IGNORECASE)

OFFICE_EXT = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".rtf", ".odt"}
# Те же форматы по MIME: имя файла может прийти без расширения
OFFICE_MIME = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/rtf",
    "text/rtf",
    "application/vnd.oasis.opendocument.text",
}

# Лимит Mail.ru — 25 МБ на письмо, держим запас
MAX_TOTAL_BYTES = 24 * 1024 * 1024

# Внутренние адреса — полные адреса, не домен: у контрагентов тоже бывают
# ящики на mail.ru. Расширяется переменной INTERNAL_EMAILS (через запятую).
DEFAULT_INTERNAL_EMAILS = ("stavgeo26@mail.ru", "business_mail.24@mail.ru")


class AttachmentError(Exception):
    """Вложение не найдено или не прошло проверку — письмо отправлять нельзя."""


# ------------------------------------------------------------------ разбор

def parse_spec(raw) -> list[dict]:
    """Параметр email_attachments -> [{uid, folder, filename}].

    Принимает JSON-строку со списком объектов; одиночный объект тоже
    понимает. Пустая строка — вложений из писем нет.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AttachmentError(
                f"email_attachments должен быть JSON-списком: {exc}") from None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise AttachmentError(
            "email_attachments должен быть JSON-списком объектов "
            '{"uid": "...", "folder": "...", "filename": "..."}')

    spec = []
    for n, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise AttachmentError(
                f"email_attachments, элемент {n}: ожидается объект "
                "с полями uid, folder, filename")
        uid = str(entry.get("uid", "")).strip()
        if not uid.isdigit():
            raise AttachmentError(
                f"email_attachments, элемент {n}: UID должен состоять только "
                f"из цифр, получено {uid!r}; UID бери из search_mail")
        spec.append({
            "uid": uid,
            "folder": str(entry.get("folder") or "").strip() or "INBOX",
            "filename": str(entry.get("filename") or "").strip(),
        })
    return spec


def _norm(name: str) -> str:
    """Имя файла для сравнения: MIME-декодирование, NFC, без регистра."""
    name = mail_read.decode_mime_header(name or "")
    return unicodedata.normalize("NFC", name).strip().casefold()


def _is_signature_image(part, filename: str) -> bool:
    if SIGNATURE_IMG_RE.match(filename):
        return True
    # Картинка, встроенная в HTML по Content-ID. Только image/*: PDF с
    # Content-ID и disposition inline — всё равно документ.
    return (part.get_content_maintype() == "image"
            and bool(part.get("Content-ID"))
            and part.get_content_disposition() != "attachment")


def extract_attachments(msg, wanted_filename: str = "") -> list[dict]:
    """Вложения письма с содержимым: [{filename, mime, content, size_bytes}].

    Пустой wanted_filename — все вложения, кроме картинок подписи. Заданный —
    точное совпадение после нормализации (RFC 2047/2231, NFC, без регистра);
    так можно взять и картинку, если она нужна явно.
    """
    wanted = _norm(wanted_filename)
    found, skipped = [], []
    for part, filename in mail_read.attachment_parts(msg):
        filename = unicodedata.normalize("NFC", filename).strip()
        if wanted:
            take = _norm(filename) == wanted
        else:
            take = not _is_signature_image(part, filename)
        if not take:
            skipped.append(filename)
            continue

        content = part.get_payload(decode=True) or b""
        if not content:
            raise AttachmentError(f"вложение «{filename}» пустое (0 байт)")
        mime = part.get_content_type()
        if mime == "application/octet-stream":
            mime = mimetypes.guess_type(filename)[0] or mime
        found.append({"filename": filename, "mime": mime,
                      "content": content, "size_bytes": len(content)})

    if found:
        return found
    if wanted:
        available = ", ".join(f"«{n}»" for n in skipped) or "вложений нет"
        raise AttachmentError(
            f"файл «{wanted_filename}» не найден. Доступны: {available}")
    if skipped:
        raise AttachmentError(
            "в письме нет вложений, кроме картинок подписи ("
            + ", ".join(f"«{n}»" for n in skipped)
            + "); нужную картинку укажи в filename явно")
    raise AttachmentError("в письме нет вложений")


def dedupe_names(items: list[dict], reserved=()) -> list[dict]:
    """Совпадающим именам — суффиксы « (2)», « (3)» перед расширением.

    reserved — имена, уже занятые в письме (например, PDF бланка).
    """
    used = {_norm(n) for n in reserved if n}
    for item in items:
        name = item["filename"]
        if _norm(name) in used:
            stem, dot, ext = name.rpartition(".")
            if not dot or not stem:
                stem, ext = name, ""
            n = 2
            while True:
                candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
                if _norm(candidate) not in used:
                    break
                n += 1
            item["filename"] = candidate
        used.add(_norm(item["filename"]))
    return items


# ---------------------------------------------------------------- проверки

def internal_emails() -> set[str]:
    """Белый список внутренних адресов; собственный ящик входит всегда."""
    emails = set(DEFAULT_INTERNAL_EMAILS)
    emails.update(os.environ.get("INTERNAL_EMAILS", "").split(","))
    emails.add(os.environ.get("MAIL_USERNAME", ""))
    return {e.strip().lower() for e in emails if "@" in e}


def is_office_file(item: dict) -> bool:
    ext = os.path.splitext(item["filename"].strip())[1].lower()
    return ext in OFFICE_EXT or item.get("mime", "").lower() in OFFICE_MIME


def check_total_size(total_bytes: int) -> None:
    if total_bytes > MAX_TOTAL_BYTES:
        raise AttachmentError(
            f"суммарный размер вложений {total_bytes / 1048576:.1f} МБ "
            f"превышает лимит {MAX_TOTAL_BYTES / 1048576:.0f} МБ на письмо")


def validate_outgoing(items: list[dict], recipients: list[str],
                      total_bytes: int) -> None:
    """Проверки перед SMTP. Любое нарушение — исключение, письмо не уходит.

    total_bytes — все вложения письма целиком, включая PDF бланка и файлы,
    приложенные другими способами.
    """
    check_total_size(total_bytes)
    internal = internal_emails()
    external = [r for r in recipients if r.strip().lower() not in internal]
    blocked = [a["filename"] for a in items if is_office_file(a)]
    if external and blocked:
        raise AttachmentError(
            "офисные файлы нельзя отправлять на внешние адреса: "
            f"{', '.join(blocked)} -> {', '.join(external)}. "
            "Приложи PDF-версию или отправь только на внутренние адреса")


def office_warnings(items: list[dict]) -> list[str]:
    """Предупреждения для превью: адресатов там ещё нет, решит send_letter."""
    return [f"«{a['filename']}» — офисный формат: на внешние адреса "
            "send_letter откажет, отправка возможна только на внутренние"
            for a in items if is_office_file(a)]


# ----------------------------------------------------------------- сборка

def collect_email_attachments(client, spec: list[dict],
                              reserved_names=()) -> list[dict]:
    """Скачать все вложения по спецификации email_attachments.

    client — IMAPClient: письмо читается его fetch_raw_message (BODY.PEEK[],
    письмо остаётся непрочитанным), папка — тем же резолвером, что у
    search_mail и get_email_body. Ошибка по любому элементу — исключение.
    """
    messages = {}
    result = []
    for entry in spec:
        uid, folder = entry["uid"], entry["folder"]
        source = f"uid {uid} / {folder}"
        key = (folder.casefold(), uid)
        try:
            if key not in messages:
                raw, _ = client.fetch_raw_message(uid, folder)
                messages[key] = email.message_from_bytes(raw)
            items = extract_attachments(messages[key], entry["filename"])
        except AttachmentError as exc:
            raise AttachmentError(f"{source}: {exc}") from None
        except (ValueError, RuntimeError) as exc:
            # fetch_raw_message: письма нет, UID кривой, папка не открылась
            raise AttachmentError(str(exc)) from None
        for item in items:
            item.update(uid=uid, folder=folder, source=source)
            result.append(item)

    check_total_size(sum(a["size_bytes"] for a in result))
    return dedupe_names(result, reserved_names)


def attachments_plan(items: list[dict]) -> list[dict]:
    """Что будет приложено — без содержимого, для превью и ответа отправки."""
    return [{"filename": a["filename"], "size_bytes": a["size_bytes"],
             "mime": a["mime"], "source": a["source"]} for a in items]
