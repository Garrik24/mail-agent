"""Рабочий поиск по IMAP и перемещение писем по папкам.

Добавляет пять инструментов: list_folders, create_folder, search_mail,
move_email, move_emails. Существующие инструменты не трогает — подключение
двумя строками в main.py, откат = удаление этих двух строк.

Ключевые моменты:
* критерии SEARCH уходят как UTF-8 bytes + объявление CHARSET UTF-8, иначе
  imaplib кодирует str в ascii и русский запрос падает;
* имена папок кодируются в modified UTF-7 (см. imap_utf7);
* метаданные читаются одним FETCH без скачивания тел писем;
* перемещение — UID MOVE, с деградацией до UID COPY + \\Deleted.

Только стандартная библиотека.
"""

import email.parser
import email.utils
import imaplib
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.header import decode_header, make_header

try:  # плоский импорт (так стартует сервис: python main.py из mcp-server/)
    from imap_utf7 import (decode_mutf7, encode_mutf7, list_folders_raw,
                           quote_folder, resolve_folder)
except ImportError:  # пакетный импорт
    from .imap_utf7 import (decode_mutf7, encode_mutf7, list_folders_raw,
                            quote_folder, resolve_folder)

log = logging.getLogger(__name__)

# --- Креды: имена переменных окружения, которые уже используются в проекте,
# плюс распространённые синонимы. Ничего не переименовываем и не добавляем.
_LOGIN_VARS = ("MAIL_USERNAME", "MAIL_LOGIN", "MAIL_USER", "IMAP_USER",
               "IMAP_LOGIN", "EMAIL_USER", "MAIL_ADDRESS")
_PASSWORD_VARS = ("MAIL_PASSWORD", "MAIL_PASS", "IMAP_PASSWORD", "IMAP_PASS",
                  "EMAIL_PASSWORD")
_HOST_VARS = ("MAIL_IMAP_HOST", "IMAP_HOST")
_PORT_VARS = ("MAIL_IMAP_PORT", "IMAP_PORT")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

FETCH_BATCH = 200
FETCH_ITEMS = ("(UID FLAGS BODYSTRUCTURE "
               "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)])")

_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)")
_BODYSTRUCTURE_RE = re.compile(rb"\bBODYSTRUCTURE\s+", re.IGNORECASE)
_NAME_PARAM_RE = re.compile(r'"(?:FILE)?NAME"\s+"((?:[^"\\]|\\.)*)"',
                            re.IGNORECASE)
_PDF_SUBTYPE_RE = re.compile(r'"APPLICATION"\s+"PDF"', re.IGNORECASE)


# ---------------------------------------------------------------- соединение

def _first_env(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


@contextmanager
def _imap_connection():
    """Подключение к IMAP на время одной операции."""
    login = _first_env(_LOGIN_VARS)
    password = _first_env(_PASSWORD_VARS)
    host = _first_env(_HOST_VARS, "imap.mail.ru")
    port = int(_first_env(_PORT_VARS, "993") or "993")
    if not login or not password:
        raise RuntimeError(
            "Не найдены логин/пароль почты в переменных окружения "
            f"({'/'.join(_LOGIN_VARS)}, {'/'.join(_PASSWORD_VARS)})"
        )
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(login, password)
        yield imap
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _has_capability(imap, name: str) -> bool:
    caps = getattr(imap, "capabilities", ()) or ()
    return name.upper() in {str(c).upper() for c in caps}


# ------------------------------------------------------------- критерии SEARCH

def imap_date(value: str) -> str:
    """YYYY-MM-DD -> 08-Jun-2026. Месяцы списком, не через локаль."""
    dt = datetime.strptime(value.strip(), "%Y-%m-%d")
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def _quoted(value: str) -> bytes:
    """Строковый аргумент SEARCH как UTF-8 bytes — именно bytes, не str."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'.encode("utf-8")


def _or_chain(groups: list[list]) -> list:
    """Префиксная OR-цепочка IMAP: [t1,t2,t3] -> OR OR t1 t2 t3."""
    if not groups:
        return []
    result = list(groups[0])
    for group in groups[1:]:
        result = ["OR"] + result + list(group)
    return result


def _keyword_group(keyword: str, scope: str) -> list:
    term = _quoted(keyword)
    if scope == "subject":
        return ["SUBJECT", term]
    if scope == "body":
        return ["BODY", term]
    return ["OR", "SUBJECT", term, "BODY", term]


def build_criteria(keywords=None, scope="both", date_from="", date_to="",
                   sender="", flagged_only=False, unseen_only=False) -> list:
    """Собирает список токенов критериев SEARCH (str и bytes вперемешку)."""
    criteria: list = []
    if flagged_only:
        criteria.append("FLAGGED")
    if unseen_only:
        criteria.append("UNSEEN")
    if sender:
        criteria += ["FROM", _quoted(sender)]
    if date_from:
        criteria += ["SINCE", imap_date(date_from)]
    if date_to:
        # BEFORE строго раньше указанной даты, поэтому date_to + 1 день
        dt = datetime.strptime(date_to.strip(), "%Y-%m-%d") + timedelta(days=1)
        criteria += ["BEFORE", f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"]

    kw = [k for k in (keywords or []) if k and k.strip()]
    if kw:
        criteria += _or_chain([_keyword_group(k.strip(), scope) for k in kw])
    if not criteria:
        criteria = ["ALL"]
    return criteria


def needs_utf8(criteria: list) -> bool:
    """True, если хоть один аргумент содержит байты > 127."""
    for token in criteria:
        if isinstance(token, (bytes, bytearray)) and any(b > 127 for b in token):
            return True
        if isinstance(token, str) and any(ord(c) > 127 for c in token):
            return True
    return False


# ------------------------------------------------------------- разбор ответов

def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _balanced_slice(data: bytes, start: int) -> bytes:
    """Возвращает сбалансированный по скобкам фрагмент, начиная с '(' в start."""
    if start >= len(data) or data[start:start + 1] != b"(":
        return b""
    depth, in_quotes, escaped = 0, False, False
    for i in range(start, len(data)):
        ch = data[i:i + 1]
        if escaped:
            escaped = False
            continue
        if ch == b"\\" and in_quotes:
            escaped = True
            continue
        if ch == b'"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if ch == b"(":
            depth += 1
        elif ch == b")":
            depth -= 1
            if depth == 0:
                return data[start:i + 1]
    return data[start:]


def parse_bodystructure(bodystructure) -> dict:
    """Имена вложений, их количество и признак PDF из BODYSTRUCTURE."""
    if isinstance(bodystructure, (bytes, bytearray)):
        text = bytes(bodystructure).decode("utf-8", errors="replace")
    else:
        text = bodystructure or ""

    names: list[str] = []
    for raw in _NAME_PARAM_RE.findall(text):
        name = _decode_header_value(raw.replace('\\"', '"').replace("\\\\", "\\"))
        if name and name not in names:
            names.append(name)

    count = len(re.findall(r'"ATTACHMENT"', text, re.IGNORECASE))
    has_pdf = bool(_PDF_SUBTYPE_RE.search(text)) or any(
        n.lower().endswith(".pdf") for n in names
    )
    return {
        "attachments": names,
        "attachment_count": max(count, len(names)),
        "has_attachment": bool(count or names),
        "has_pdf": has_pdf,
    }


def parse_flags(raw_flags) -> list[str]:
    if isinstance(raw_flags, (bytes, bytearray)):
        raw_flags = bytes(raw_flags).decode("utf-8", errors="replace")
    return [f for f in (raw_flags or "").split() if f]


def parse_fetch_item(prefix: bytes, header_bytes: bytes) -> dict:
    """Собирает письмо из префикса FETCH и блока заголовков."""
    uid_match = _UID_RE.search(prefix or b"")
    uid = uid_match.group(1).decode() if uid_match else ""

    flags_match = _FLAGS_RE.search(prefix or b"")
    flags = parse_flags(flags_match.group(1) if flags_match else b"")

    bs_match = _BODYSTRUCTURE_RE.search(prefix or b"")
    bodystructure = (_balanced_slice(prefix, bs_match.end())
                     if bs_match else b"")
    att = parse_bodystructure(bodystructure)

    msg = email.parser.BytesParser().parsebytes(header_bytes or b"",
                                                headersonly=True)
    subject = _decode_header_value(msg.get("Subject", ""))
    from_raw = _decode_header_value(msg.get("From", ""))
    name, addr = email.utils.parseaddr(from_raw)
    date_raw = msg.get("Date", "")
    date_iso = ""
    if date_raw:
        try:
            date_iso = email.utils.parsedate_to_datetime(date_raw).strftime(
                "%Y-%m-%d %H:%M")
        except Exception:
            date_iso = ""

    return {
        "uid": uid,
        "date": date_iso,
        "date_raw": date_raw,
        "subject": subject,
        "from": from_raw,
        "from_name": name or addr,
        "from_email": addr,
        "to": _decode_header_value(msg.get("To", "")),
        "cc": _decode_header_value(msg.get("Cc", "")),
        "message_id": (msg.get("Message-ID", "") or "").strip(),
        "flags": flags,
        "flagged": "\\Flagged" in flags,
        "seen": "\\Seen" in flags,
        "answered": "\\Answered" in flags,
        **att,
    }


def parse_fetch_response(data) -> list[dict]:
    """Разбирает весь ответ imaplib на FETCH в список писем."""
    items = []
    for chunk in data or []:
        if not isinstance(chunk, tuple) or len(chunk) < 2:
            continue
        prefix = chunk[0] if isinstance(chunk[0], (bytes, bytearray)) else b""
        body = chunk[1] if isinstance(chunk[1], (bytes, bytearray)) else b""
        items.append(parse_fetch_item(bytes(prefix), bytes(body)))
    return items


def format_compact(item: dict) -> str:
    """18357 | 2026-06-08 09:45 | Имя <addr> | Тема [флаг, pdf]"""
    marks = []
    if item.get("flagged"):
        marks.append("флаг")
    if not item.get("seen", True):
        marks.append("непроч")
    if item.get("has_pdf"):
        marks.append("pdf")
    elif item.get("has_attachment"):
        marks.append(f"вложений: {item.get('attachment_count', 0)}")
    suffix = f" [{', '.join(marks)}]" if marks else ""

    sender = item.get("from") or item.get("from_email") or ""
    return (f"{item.get('uid', '')} | {item.get('date', '')} | {sender} | "
            f"{item.get('subject', '')}{suffix}")


# ------------------------------------------------------------------ операции

def _search_uids(imap, criteria: list) -> tuple[list[str], str]:
    """SEARCH с тремя ступенями фолбэка. Возвращает (uids, mode)."""
    use_charset = needs_utf8(criteria)

    if use_charset:
        try:
            typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
            if typ == "OK":
                return _split_uids(data), "server"
        except Exception as exc:
            log.warning(f"SEARCH CHARSET UTF-8 не прошёл: {exc}")

    try:
        typ, data = imap.uid("SEARCH", *criteria)
        if typ == "OK":
            return _split_uids(data), "server" if not use_charset else "server-nocharset"
    except Exception as exc:
        log.warning(f"SEARCH без CHARSET не прошёл: {exc}")

    return [], "client-filter"


def _split_uids(data) -> list[str]:
    uids: list[str] = []
    for chunk in data or []:
        if isinstance(chunk, (bytes, bytearray)):
            uids += [u.decode() for u in bytes(chunk).split()]
        elif isinstance(chunk, str):
            uids += chunk.split()
    return uids


def _fetch_metadata(imap, uids: list[str]) -> list[dict]:
    """FETCH метаданных пачками по FETCH_BATCH UID, без скачивания тел."""
    items: list[dict] = []
    for start in range(0, len(uids), FETCH_BATCH):
        batch = uids[start:start + FETCH_BATCH]
        typ, data = imap.uid("FETCH", ",".join(batch), FETCH_ITEMS)
        if typ != "OK":
            log.warning(f"FETCH вернул {typ} для пачки из {len(batch)} UID")
            continue
        items += parse_fetch_response(data)
    return items


def _list_folders_impl() -> dict:
    with _imap_connection() as imap:
        folders = list_folders_raw(imap)
    return {"count": len(folders), "folders": folders}


def _create_folder_impl(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "Имя папки не может быть пустым"}
    raw = encode_mutf7(name)
    with _imap_connection() as imap:
        existing = {f["name"].casefold() for f in list_folders_raw(imap)}
        if name.casefold() in existing:
            return {"created": False, "name": name, "raw": raw,
                    "message": "Папка уже существует"}
        typ, data = imap.create(quote_folder(raw))
        if typ != "OK":
            detail = b" ".join(d for d in data if isinstance(d, bytes))
            return {"created": False, "name": name, "raw": raw,
                    "error": detail.decode("utf-8", errors="replace")}
    return {"created": True, "name": name, "raw": raw}


def _search_mail_impl(keywords=None, scope="both", folder="INBOX",
                      date_from="", date_to="", sender="", flagged_only=False,
                      unseen_only=False, has_attachment=False, pdf_only=False,
                      limit=100, compact=True) -> dict:
    scope = (scope or "both").lower()
    if scope not in ("subject", "body", "both"):
        return {"error": f"scope должен быть subject|body|both, получено: {scope}"}
    kw = [k.strip() for k in (keywords or []) if k and k.strip()]

    try:
        criteria = build_criteria(kw, scope, date_from, date_to, sender,
                                  flagged_only, unseen_only)
    except ValueError as exc:
        return {"error": f"Некорректная дата (ожидается YYYY-MM-DD): {exc}"}

    with _imap_connection() as imap:
        raw_folder = resolve_folder(imap, folder)
        typ, _ = imap.select(quote_folder(raw_folder))
        if typ != "OK":
            return {"error": f"Не удалось открыть папку: {folder}"}

        uids, mode = _search_uids(imap, criteria)

        if mode == "client-filter":
            # Сервер отказал: сужаем выборку тем, что заведомо ASCII
            # (даты, флаги, латинский отправитель), ключевые слова применим
            # к темам у себя. Тела писем при этом не проверяются.
            fallback = build_criteria(
                None, scope, date_from, date_to,
                sender if sender.isascii() else "", flagged_only, unseen_only,
            )
            typ, data = imap.uid("SEARCH", *fallback)
            uids = _split_uids(data) if typ == "OK" else []

        total_found = len(uids)
        uids = uids[-limit:] if limit and limit > 0 else uids
        items = _fetch_metadata(imap, uids) if uids else []

    if mode == "client-filter":
        if sender and not sender.isascii():
            low = sender.casefold()
            items = [i for i in items if low in i.get("from", "").casefold()]
        if kw:
            lowered = [k.casefold() for k in kw]
            items = [i for i in items
                     if any(k in i.get("subject", "").casefold() for k in lowered)]

    if has_attachment:
        items = [i for i in items if i.get("has_attachment")]
    if pdf_only:
        items = [i for i in items if i.get("has_pdf")]

    items.sort(key=lambda i: i.get("date") or "", reverse=True)

    result = {
        "mode": mode,
        "folder": folder,
        "folder_raw": raw_folder,
        "count": len(items),
        "total_matched": total_found,
        "compact": bool(compact),
    }
    if compact:
        result["emails"] = [format_compact(i) for i in items]
    else:
        result["emails"] = items
    return result


def validate_uids(email_uids) -> tuple[list[str], list[str]]:
    """Разделяет UID на валидные (только цифры) и мусорные."""
    valid, invalid = [], []
    for uid in email_uids or []:
        text = str(uid).strip()
        if text.isdigit():
            valid.append(text)
        else:
            invalid.append(text)
    return valid, invalid


def _move_impl(email_uids: list[str], target_folder: str,
               source_folder: str = "INBOX",
               expunge_fallback: bool = False) -> dict:
    valid, invalid = validate_uids(email_uids)
    if invalid:
        return {"error": f"UID должны состоять только из цифр: {invalid}",
                "moved": [], "failed": [str(u) for u in (email_uids or [])]}
    if not valid:
        return {"error": "Не передано ни одного UID", "moved": [], "failed": []}
    if not (target_folder or "").strip():
        return {"error": "Не указана папка назначения", "moved": [],
                "failed": valid}

    uid_set = ",".join(valid)
    with _imap_connection() as imap:
        raw_target = resolve_folder(imap, target_folder)
        raw_source = resolve_folder(imap, source_folder)
        typ, _ = imap.select(quote_folder(raw_source))
        if typ != "OK":
            return {"error": f"Не удалось открыть папку: {source_folder}",
                    "moved": [], "failed": valid}

        if _has_capability(imap, "MOVE"):
            typ, data = imap.uid("MOVE", uid_set, quote_folder(raw_target))
            if typ != "OK":
                return {"error": _detail(data), "method": "UID MOVE",
                        "moved": [], "failed": valid}
            return {"moved": valid, "failed": [], "method": "UID MOVE",
                    "target_folder": target_folder, "target_raw": raw_target,
                    "source_folder": source_folder, "count": len(valid)}

        # MOVE недоступен: копируем и помечаем оригиналы \Deleted
        typ, data = imap.uid("COPY", uid_set, quote_folder(raw_target))
        if typ != "OK":
            return {"error": _detail(data), "method": "UID COPY",
                    "moved": [], "failed": valid}
        typ, data = imap.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            return {"error": f"Скопировано, но не помечено к удалению: {_detail(data)}",
                    "method": "UID COPY", "moved": valid, "failed": [],
                    "expunged": False}

        method, expunged = "UID COPY + STORE \\Deleted", False
        if _has_capability(imap, "UIDPLUS"):
            typ, _ = imap.uid("EXPUNGE", uid_set)
            expunged = typ == "OK"
            method += " + UID EXPUNGE"
        elif expunge_fallback:
            # Голый EXPUNGE вычистит из папки ВСЕ письма, ранее помеченные
            # к удалению — только по явному запросу вызывающего.
            typ, _ = imap.expunge()
            expunged = typ == "OK"
            method += " + EXPUNGE (expunge_fallback)"

        result = {"moved": valid, "failed": [], "method": method,
                  "expunged": expunged, "target_folder": target_folder,
                  "target_raw": raw_target, "source_folder": source_folder,
                  "count": len(valid)}
        if not expunged:
            result["note"] = ("Копии созданы, оригиналы помечены \\Deleted, но не "
                              "удалены: сервер без UIDPLUS. Передайте "
                              "expunge_fallback=true, если это допустимо.")
        return result


def _detail(data) -> str:
    parts = [d.decode("utf-8", errors="replace") if isinstance(d, bytes) else str(d)
             for d in (data or []) if d]
    return " ".join(parts) or "IMAP вернул ошибку"


# --------------------------------------------------------------- регистрация

def register_tools(mcp):
    """Регистрирует инструменты работы с папками и поиска на экземпляре FastMCP."""

    @mcp.tool()
    def list_folders() -> dict:
        """Список папок ящика: имя, raw-имя IMAP, разделитель, флаги.

        Человекочитаемое имя раскодировано из modified UTF-7, поэтому
        кириллические папки («Согласование», «Отправленные») видны как есть.
        """
        try:
            return _list_folders_impl()
        except Exception as exc:
            log.error(f"list_folders: {exc}")
            return {"error": str(exc)}

    @mcp.tool()
    def create_folder(name: str) -> dict:
        """Создать папку по обычному имени, в том числе кириллицей.

        Args:
            name: Имя папки, например «Согласование» или «INBOX/Архив»
        """
        try:
            return _create_folder_impl(name)
        except Exception as exc:
            log.error(f"create_folder: {exc}")
            return {"error": str(exc)}

    @mcp.tool()
    def search_mail(keywords: list[str] | None = None, scope: str = "both",
                    folder: str = "INBOX", date_from: str = "",
                    date_to: str = "", sender: str = "",
                    flagged_only: bool = False, unseen_only: bool = False,
                    has_attachment: bool = False, pdf_only: bool = False,
                    limit: int = 100, compact: bool = True) -> dict:
        """Поиск писем с поддержкой русских слов, флагов и вложений.

        Поле mode в ответе показывает, как отработал поиск:
        server — SEARCH с CHARSET UTF-8; server-nocharset — без объявления
        кодировки; client-filter — сервер отказал, письма отобраны по датам и
        отправителю, ключевые слова применены к темам на нашей стороне
        (тела писем при этом не проверяются).

        Args:
            keywords: Ключевые слова, ищутся по ИЛИ между собой
            scope: Где искать — subject, body или both
            folder: Папка, обычное имя, можно кириллицей
            date_from: Дата начала YYYY-MM-DD, включительно
            date_to: Дата конца YYYY-MM-DD, включительно
            sender: Фрагмент адреса или имени отправителя
            flagged_only: Только письма с флагом
            unseen_only: Только непрочитанные
            has_attachment: Только письма с вложениями
            pdf_only: Только письма с PDF
            limit: Максимум писем, берутся самые свежие
            compact: Одна строка на письмо вместо полного объекта
        """
        try:
            return _search_mail_impl(
                keywords=keywords, scope=scope, folder=folder,
                date_from=date_from, date_to=date_to, sender=sender,
                flagged_only=flagged_only, unseen_only=unseen_only,
                has_attachment=has_attachment, pdf_only=pdf_only,
                limit=limit, compact=compact,
            )
        except Exception as exc:
            log.error(f"search_mail: {exc}")
            return {"error": str(exc)}

    @mcp.tool()
    def move_email(email_uid: str, target_folder: str,
                   source_folder: str = "INBOX") -> dict:
        """Переместить одно письмо в папку.

        Args:
            email_uid: UID письма (только цифры)
            target_folder: Папка назначения, обычное имя
            source_folder: Папка-источник, по умолчанию INBOX
        """
        try:
            return _move_impl([email_uid], target_folder, source_folder)
        except Exception as exc:
            log.error(f"move_email: {exc}")
            return {"error": str(exc)}

    @mcp.tool()
    def move_emails(email_uids: list[str], target_folder: str,
                    source_folder: str = "INBOX",
                    expunge_fallback: bool = False) -> dict:
        """Переместить пачку писем одной командой.

        Args:
            email_uids: Список UID (только цифры)
            target_folder: Папка назначения, обычное имя
            source_folder: Папка-источник, по умолчанию INBOX
            expunge_fallback: Разрешить голый EXPUNGE, если сервер без
                UIDPLUS. Внимание: вычистит из папки все письма, ранее
                помеченные к удалению.
        """
        try:
            return _move_impl(email_uids, target_folder, source_folder,
                              expunge_fallback)
        except Exception as exc:
            log.error(f"move_emails: {exc}")
            return {"error": str(exc)}

    log.info("Зарегистрированы инструменты: list_folders, create_folder, "
             "search_mail, move_email, move_emails")
