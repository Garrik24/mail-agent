"""Инструменты чтения писем: текст из вложений и пакетный разбор пачки писем.

Нужны для разбора писем-согласований: у ресурсоснабжающих организаций тело
письма пустое, всё содержание лежит во вложенном PDF, поэтому классифицировать
письмо по теме невозможно — нужен текст вложения.

Соединение и разрешение имён папок переиспользуются из первого патча
(mail_tools_patch, imap_utf7) — тот патч не изменяется.
"""

import email
import logging

try:  # плоский импорт (так стартует сервис: python main.py из mcp-server/)
    import mail_read
    from imap_utf7 import quote_folder, resolve_folder
    from mail_tools_patch import _imap_connection, validate_uids
except ImportError:  # пакетный импорт
    from . import mail_read
    from .imap_utf7 import quote_folder, resolve_folder
    from .mail_tools_patch import _imap_connection, validate_uids

log = logging.getLogger(__name__)

MAX_BATCH = 30
# Тело короче этого считаем пустым и идём искать текст во вложении
EMPTY_BODY_THRESHOLD = 100
# Сколько страниц скана распознавать в пакетном режиме
BATCH_OCR_MAX_PAGES = 2

UID_HINT = ("проверь, что UID получен из search_mail и указана верная папка "
            "(список папок — list_folders)")


def _select(imap, folder: str) -> str:
    raw = resolve_folder(imap, folder)
    typ, _ = imap.select(quote_folder(raw))
    if typ != "OK":
        raise ValueError(f"Не удалось открыть папку: {folder}")
    return raw


def _fetch_message(imap, uid: str, folder: str):
    """UID FETCH одного письма целиком, без снятия флага «непрочитано»."""
    typ, data = imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
    chunk = data[0] if data else None
    if typ != "OK" or not isinstance(chunk, tuple) or len(chunk) < 2:
        raise ValueError(
            f"Письмо с UID {uid} не найдено в папке {folder}; {UID_HINT}")
    prefix = chunk[0] if isinstance(chunk[0], bytes) else b""
    msg = email.message_from_bytes(chunk[1])
    return msg, mail_read.parse_flags_list(prefix)


def _get_attachment_text_impl(email_uid: str, folder: str = "INBOX",
                              attachment_name: str = "",
                              max_chars: int = 5000,
                              ocr: str = "auto") -> dict:
    valid, invalid = validate_uids([email_uid])
    if invalid:
        return {"ok": False,
                "reason": f"UID должен состоять только из цифр: {invalid[0]!r}; "
                          f"{UID_HINT}"}

    with _imap_connection() as imap:
        _select(imap, folder)
        try:
            msg, _ = _fetch_message(imap, valid[0], folder)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    found = mail_read.find_attachment(msg, attachment_name)
    if not found:
        available = [a["filename"] for a in mail_read.list_attachments(msg)]
        if attachment_name and available:
            return {"ok": False,
                    "reason": f"Вложение «{attachment_name}» не найдено",
                    "available": available}
        return {"ok": False, "reason": "У письма нет вложений"}

    result = mail_read.extract_attachment_text(
        found["filename"], found["content_type"], found["data"], max_chars,
        ocr=ocr)
    result["uid"] = valid[0]
    result["filename"] = found["filename"]
    result["content_type"] = found["content_type"]
    result["size_bytes"] = len(found["data"])
    return result


def _summarize(msg, uid: str, flags: list[str], max_chars_each: int,
               include_attachment_text: bool, ocr: str = "auto") -> dict:
    """Компактная сводка по письму: содержание из тела либо из вложения."""
    body = mail_read.extract_body(msg)
    own, _ = mail_read.split_quotes(body["text"])
    attachments = mail_read.list_attachments(msg)

    source = "body"
    text = own
    if include_attachment_text and len(own.strip()) < EMPTY_BODY_THRESHOLD:
        found = mail_read.find_attachment(msg)
        if found and found["filename"].lower().endswith((".pdf", ".docx")):
            # В пачке распознаём не больше двух страниц на письмо: иначе
            # разбор 30 сканов растянется на минуты.
            extracted = mail_read.extract_attachment_text(
                found["filename"], found["content_type"], found["data"],
                max_chars_each, ocr=ocr, ocr_max_pages=BATCH_OCR_MAX_PAGES)
            if extracted.get("ok"):
                text = extracted["text"]
                source = f"attachment:{found['filename']}"
            else:
                source = f"attachment:{found['filename']} — {extracted.get('reason', '')}"

    text, truncated = mail_read.truncate(text, max_chars_each)
    return {
        "uid": uid,
        "date": msg.get("Date", ""),
        "from": mail_read.decode_mime_header(msg.get("From", "")),
        "subject": mail_read.decode_mime_header(msg.get("Subject", "")),
        "flags": flags,
        "flagged": "\\Flagged" in flags,
        "seen": "\\Seen" in flags,
        "attachments": [a["filename"] for a in attachments],
        "text": text,
        "source": source,
        "truncated": truncated,
    }


def _read_messages_impl(email_uids, folder: str = "INBOX",
                        max_chars_each: int = 800,
                        include_attachment_text: bool = True,
                        ocr: str = "auto") -> dict:
    valid, invalid = validate_uids(email_uids)
    if invalid:
        return {"error": f"UID должны состоять только из цифр: {invalid}; "
                         f"{UID_HINT}"}
    if not valid:
        return {"error": "Не передано ни одного UID"}
    skipped = []
    if len(valid) > MAX_BATCH:
        skipped = valid[MAX_BATCH:]
        valid = valid[:MAX_BATCH]

    messages = []
    with _imap_connection() as imap:
        _select(imap, folder)
        for uid in valid:
            # Одно нечитаемое письмо не должно ронять весь разбор пачки
            try:
                msg, flags = _fetch_message(imap, uid, folder)
                messages.append(_summarize(msg, uid, flags, max_chars_each,
                                           include_attachment_text, ocr))
            except Exception as exc:
                log.warning(f"read_messages: UID {uid} не прочитан: {exc}")
                messages.append({"uid": uid, "error": str(exc)})

    result = {"folder": folder, "count": len(messages), "messages": messages}
    if skipped:
        result["skipped_uids"] = skipped
        result["note"] = (f"За раз читается не больше {MAX_BATCH} писем; "
                          f"остальные {len(skipped)} пропущены — вызови ещё раз")
    return result


def register_tools(mcp):
    """Регистрирует инструменты чтения на экземпляре FastMCP."""

    @mcp.tool()
    def get_attachment_text(email_uid: str, folder: str = "INBOX",
                            attachment_name: str = "",
                            max_chars: int = 5000,
                            ocr: str = "auto") -> dict:
        """Извлечь текст из вложения письма (PDF, DOCX, текстовые файлы).

        Нужен, когда тело письма пустое, а всё содержание — во вложении:
        так приходят ответы ресурсоснабжающих организаций и ведомств.
        Если в PDF нет текстового слоя (скан), текст распознаётся через OCR
        (русский + английский, до 5 страниц); поле source покажет text-layer
        или ocr.

        Args:
            email_uid: UID письма (из search_mail), только цифры
            folder: Папка, обычное имя, можно кириллицей
            attachment_name: Имя вложения; пусто — первый PDF или DOCX
            max_chars: Максимум символов текста
            ocr: "auto" — распознавать скан, если текстового слоя нет;
                 "off" — только текстовый слой
        """
        try:
            return _get_attachment_text_impl(email_uid, folder,
                                             attachment_name, max_chars, ocr)
        except Exception as exc:
            log.error(f"get_attachment_text: {exc}")
            return {"ok": False, "reason": str(exc)}

    @mcp.tool()
    def read_messages(email_uids: list[str], folder: str = "INBOX",
                      max_chars_each: int = 800,
                      include_attachment_text: bool = True,
                      ocr: str = "auto") -> dict:
        """Прочитать пачку писем разом и получить по каждому краткую сводку.

        По письму возвращается UID, дата, отправитель, тема, флаги, имена
        вложений и начало содержания. Содержание берётся из тела письма, а
        если тело пустое или очень короткое — из первого PDF или DOCX во
        вложении; поле source показывает, откуда взят текст ("body" или
        "attachment:имя.pdf").

        Разбирать пачку писем этим инструментом дешевле, чем вызывать
        get_email_body по одному. За раз читается не больше 30 писем.

        Args:
            email_uids: Список UID (из search_mail), только цифры
            folder: Папка, обычное имя, можно кириллицей
            max_chars_each: Максимум символов содержания на письмо
            include_attachment_text: Доставать текст из вложения, если тело пустое
            ocr: "auto" — распознавать сканы (до 2 страниц на письмо),
                 "off" — не распознавать, так быстрее
        """
        try:
            return _read_messages_impl(email_uids, folder, max_chars_each,
                                       include_attachment_text, ocr)
        except Exception as exc:
            log.error(f"read_messages: {exc}")
            return {"error": str(exc)}

    log.info("Зарегистрированы инструменты чтения: get_attachment_text, "
             "read_messages")
