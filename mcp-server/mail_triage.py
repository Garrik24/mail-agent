"""Отбор писем-согласований для папки СОГЛАСОВАНИЯ.

Искать такие письма по слову «согласование» бесполезно — в большинстве из
них этого слова нет. Реальные признаки другие: письмо пришло с официальной
почты организации, оно является ответом на наш исходящий запрос, тема
выглядит как «в ответ на ваше письмо» / «исх. № N» (или темы нет совсем),
а тело пустое, потому что всё содержание лежит во вложенном PDF.

Инструмент только предлагает кандидатов и объясняет причины — письма он не
перемещает. Перенос делает move_emails после подтверждения пользователя.
"""

import email.utils
import logging
import re

try:  # плоский импорт (так стартует сервис: python main.py из mcp-server/)
    import mail_read
    import mail_tools_patch
    from mail_read_tools import _fetch_message, _select
    from mail_tools_patch import (_imap_connection, _search_uids,
                                  build_criteria)
except ImportError:  # пакетный импорт
    from . import mail_read
    from . import mail_tools_patch
    from .mail_read_tools import _fetch_message, _select
    from .mail_tools_patch import (_imap_connection, _search_uids,
                                   build_criteria)

log = logging.getLogger(__name__)

# Скоринг идёт по метаданным, поэтому потолок высокий: столько писем
# сервер отдаёт пачками за разумное время
DEFAULT_MAX_SCAN = 5000
# Порог 3 ловит вообще всю деловую переписку с вложениями (договоры, УПД,
# тендеры): на живом ящике это 67 писем из 200. При 5 остаётся короткий
# проверяемый список — те самые ответы организаций.
DEFAULT_MIN_SCORE = 5
SHORT_BODY = 200

# Темы ответов на наши исходящие. Слова «согласование» тут может и не быть.
_SUBJECT_PATTERNS = (
    (re.compile(r"в\s+ответ\s+на", re.IGNORECASE), "тема «в ответ на»"),
    (re.compile(r"на\s+(ваш|ваше|ваш[еи]\s+письмо|№)", re.IGNORECASE),
     "тема «на ваше письмо»"),
    (re.compile(r"\bисх\.?\s*(№|N|#)?\s*\d+", re.IGNORECASE),
     "в теме исходящий номер"),
    (re.compile(r"о\s+согласовани", re.IGNORECASE), "тема о согласовании"),
    (re.compile(r"о\s+рассмотрении", re.IGNORECASE), "тема о рассмотрении"),
    (re.compile(r"о\s+предоставлении", re.IGNORECASE), "тема о предоставлении"),
    (re.compile(r"(технически[ех]|ТУ)\s+услови", re.IGNORECASE),
     "тема о технических условиях"),
)

# Почта, с которой согласования не приходят: рассылки, роботы, площадки
_MASS_SENDER_RE = re.compile(
    r"(no-?reply|noreply|notification|mailer|robot|info@|news|digest|"
    r"support@|billing|rassylka|sabylink|saby|avito|hh\.ru|telko|"
    r"yandex\.ru|gmail\.com|mail\.ru|bk\.ru|list\.ru|inbox\.ru)",
    re.IGNORECASE)

# Организации, от которых приходят согласования, обычно на своём домене
_ORG_HINT_RE = re.compile(
    r"(gaz|vodokanal|skvk|energo|elektro|set|rzd|gup|mup|admin|gov|"
    r"rosreestr|kadastr|tek|teplo|svyaz|rostelecom|transneft|kraygaz|"
    r"vodokanal|geo|proekt|stroy)", re.IGNORECASE)


DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".tif", ".tiff")


def score_features(subject: str, from_raw: str, has_reply_header: bool,
                   doc_names: list[str], body_is_short) -> tuple[int, list[str]]:
    """Считает балл по уже вытащенным признакам письма.

    body_is_short: True/False, либо None — если размер тела неизвестен
    (тогда за него баллы просто не начисляются, а не угадываются).
    """
    sender_email = email.utils.parseaddr(from_raw)[1].lower()
    domain = sender_email.split("@")[-1] if "@" in sender_email else ""

    score = 0
    reasons: list[str] = []

    if has_reply_header:
        score += 2
        reasons.append("ответ на нашу переписку (In-Reply-To/References)")
    elif subject.lower().startswith(("re:", "ре:")):
        score += 1
        reasons.append("тема начинается с Re:")

    for pattern, label in _SUBJECT_PATTERNS:
        if pattern.search(subject):
            score += 2
            reasons.append(label)
            break

    if not subject.strip():
        score += 1
        reasons.append("темы нет совсем")

    if doc_names:
        score += 1
        reasons.append(f"документ во вложении: {doc_names[0]}")
        if body_is_short:
            score += 2
            reasons.append("тело пустое или в одну строку — суть во вложении")

    if _MASS_SENDER_RE.search(sender_email):
        score -= 3
        reasons.append("похоже на рассылку или робота")
    elif domain:
        score += 1
        reasons.append(f"письмо с домена организации: {domain}")
        if _ORG_HINT_RE.search(domain):
            score += 1
            reasons.append("домен профильной организации")

    return score, reasons


def score_message(msg, uid: str = "", flags=None) -> dict:
    """Оценивает уже загруженное письмо целиком.

    Точный, но дорогой путь: требует скачать письмо. Для прохода по всему
    ящику используется score_meta, работающий на одних метаданных.
    """
    flags = flags or []
    subject = mail_read.decode_mime_header(msg.get("Subject", ""))
    from_raw = mail_read.decode_mime_header(msg.get("From", ""))
    body = mail_read.extract_body(msg)
    own_text, _ = mail_read.split_quotes(body["text"])
    attachments = mail_read.list_attachments(msg)
    doc_names = [a["filename"] for a in attachments
                 if a["filename"].lower().endswith(DOC_EXTENSIONS)]

    score, reasons = score_features(
        subject, from_raw,
        bool(msg.get("In-Reply-To") or msg.get("References")),
        doc_names, len(own_text.strip()) < SHORT_BODY,
    )
    return {
        "uid": uid,
        "score": score,
        "reasons": reasons,
        "subject": subject or "(без темы)",
        "from": from_raw,
        "sender_email": email.utils.parseaddr(from_raw)[1].lower(),
        "date": msg.get("Date", ""),
        "attachments": [a["filename"] for a in attachments],
        "has_document": bool(doc_names),
        "body_preview": own_text[:200],
        "flags": flags,
    }


# Всё, что нужно для отбора, лежит в метаданных: скачивать письма целиком
# ради скоринга — значит упереться в пару сотен писем вместо всего ящика.
FETCH_ITEMS = ("(UID FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS "
               "(DATE FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)])")
FETCH_BATCH = 200

_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_BODYSTRUCTURE_RE = re.compile(rb"\bBODYSTRUCTURE\s+", re.IGNORECASE)
# body-type-text: "TEXT" "PLAIN" (параметры) id описание кодировка РАЗМЕР строки
_TEXT_SIZE_RE = re.compile(
    rb'"TEXT"\s+"PLAIN"\s+(?:\((?:[^()]|\([^()]*\))*\)|NIL)\s+'
    rb'(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+'
    rb'(?:"(?:[^"\\]|\\.)*"|NIL)\s+(\d+)', re.IGNORECASE)


def text_part_size(bodystructure: bytes):
    """Размер первой текстовой части в октетах, либо None.

    Это и есть замена «загрузить тело и посмотреть, пустое ли оно»:
    IMAP сообщает размер каждой части прямо в BODYSTRUCTURE.
    """
    match = _TEXT_SIZE_RE.search(bodystructure or b"")
    return int(match.group(1)) if match else None


def parse_meta_item(prefix: bytes, header_bytes: bytes) -> dict:
    """Собирает признаки письма из ответа FETCH, не скачивая тело."""
    import email.parser

    uid_match = _UID_RE.search(prefix or b"")
    bs_match = _BODYSTRUCTURE_RE.search(prefix or b"")
    bodystructure = (mail_tools_patch._balanced_slice(prefix, bs_match.end())
                     if bs_match else b"")
    attachments = mail_tools_patch.parse_bodystructure(bodystructure)

    msg = email.parser.BytesParser().parsebytes(header_bytes or b"",
                                                headersonly=True)
    return {
        "uid": uid_match.group(1).decode() if uid_match else "",
        "flags": mail_read.parse_flags_list(prefix),
        "subject": mail_read.decode_mime_header(msg.get("Subject", "")),
        "from": mail_read.decode_mime_header(msg.get("From", "")),
        "date": msg.get("Date", ""),
        "has_reply_header": bool(msg.get("In-Reply-To") or msg.get("References")),
        "attachments": attachments["attachments"],
        "text_size": text_part_size(bodystructure),
    }


def score_meta(meta: dict) -> dict:
    """Балл письма по метаданным — быстрый путь для прохода по всему ящику."""
    doc_names = [n for n in meta["attachments"]
                 if n.lower().endswith(DOC_EXTENSIONS)]
    size = meta.get("text_size")
    body_is_short = None if size is None else size < SHORT_BODY

    score, reasons = score_features(
        meta["subject"], meta["from"], meta["has_reply_header"],
        doc_names, body_is_short,
    )
    return {
        "uid": meta["uid"],
        "score": score,
        "reasons": reasons,
        "subject": meta["subject"] or "(без темы)",
        "from": meta["from"],
        "date": meta["date"],
        "attachments": meta["attachments"],
        "has_document": bool(doc_names),
        "text_size": size,
        "flags": meta["flags"],
    }


def _scan_metadata(imap, uids: list[str]) -> list[dict]:
    """Пачечный FETCH метаданных: тела писем не скачиваются."""
    items = []
    for start in range(0, len(uids), FETCH_BATCH):
        batch = uids[start:start + FETCH_BATCH]
        typ, data = imap.uid("FETCH", ",".join(batch), FETCH_ITEMS)
        if typ != "OK":
            log.warning(f"triage: FETCH вернул {typ} для {len(batch)} UID")
            continue
        for chunk in data or []:
            if not isinstance(chunk, tuple) or len(chunk) < 2:
                continue
            prefix = chunk[0] if isinstance(chunk[0], bytes) else b""
            body = chunk[1] if isinstance(chunk[1], bytes) else b""
            items.append(parse_meta_item(bytes(prefix), bytes(body)))
    return items


def _find_candidates_impl(folder: str = "INBOX", date_from: str = "",
                          date_to: str = "", min_score: int = DEFAULT_MIN_SCORE,
                          limit: int = 50, unseen_only: bool = False,
                          max_scan: int = DEFAULT_MAX_SCAN) -> dict:
    try:
        criteria = build_criteria(None, "both", date_from, date_to, "",
                                  False, unseen_only)
    except ValueError as exc:
        return {"error": f"Некорректная дата (ожидается YYYY-MM-DD): {exc}"}

    candidates = []
    histogram: dict[int, int] = {}
    with _imap_connection() as imap:
        _select(imap, folder)
        uids, mode = _search_uids(imap, criteria)
        if mode == "client-filter":
            typ, data = imap.uid("SEARCH", "ALL")
            uids = ([u.decode() for u in data[0].split()]
                    if typ == "OK" and data and data[0] else [])

        total_matched = len(uids)
        # Если писем больше, чем готовы просмотреть, берём самые свежие —
        # но об отброшенном честно говорим в ответе, а не молчим
        skipped = max(0, total_matched - max_scan) if max_scan > 0 else 0
        if skipped:
            uids = uids[-max_scan:]

        for meta in _scan_metadata(imap, uids):
            item = score_meta(meta)
            histogram[item["score"]] = histogram.get(item["score"], 0) + 1
            if item["score"] >= min_score:
                candidates.append(item)

    candidates.sort(key=lambda c: (c["score"], c["date"]), reverse=True)
    trimmed = candidates[:limit] if limit and limit > 0 else candidates

    result = {
        "folder": folder,
        "total_in_folder": total_matched,
        "scanned": total_matched - skipped,
        "found": len(candidates),
        "min_score": min_score,
        # Сколько писем набрало каждый балл — видно, куда двигать порог,
        # не перебирая значения вызовами
        "score_histogram": dict(sorted(histogram.items())),
        "candidates": trimmed,
        "uids": [c["uid"] for c in trimmed],
        "hint": ("Письма не перемещены. Проверьте список и вызовите "
                 "move_emails с нужными UID и target_folder «СОГЛАСОВАНИЯ»."),
    }
    if skipped:
        result["skipped_older"] = skipped
        result["note"] = (
            f"Просмотрены {max_scan} самых свежих писем, ещё {skipped} "
            f"остались за пределами выборки. Поднимите max_scan или сузьте "
            f"период через date_from/date_to, чтобы разобрать остальные.")
    if len(candidates) > len(trimmed):
        result["note_limit"] = (
            f"Кандидатов {len(candidates)}, показаны первые {len(trimmed)} — "
            f"поднимите limit, чтобы увидеть остальные.")
    return result


def register_tools(mcp):
    """Регистрирует инструмент отбора писем-согласований."""

    @mcp.tool()
    def find_approval_candidates(folder: str = "INBOX", date_from: str = "",
                                 date_to: str = "",
                                 min_score: int = DEFAULT_MIN_SCORE,
                                 limit: int = 50,
                                 unseen_only: bool = False,
                                 max_scan: int = DEFAULT_MAX_SCAN) -> dict:
        """Найти письма, похожие на ответы-согласования от организаций.

        Отбирает не по слову «согласование» (в таких письмах его обычно нет),
        а по совокупности признаков: письмо является ответом на нашу
        переписку, тема вида «в ответ на» / «исх. № N» либо темы нет вовсе,
        документ во вложении при пустом теле, отправитель — организация,
        а не рассылка. По каждому письму возвращается балл и причины, чтобы
        решение можно было проверить глазами.

        Письма НЕ перемещаются: получив список, вызовите move_emails с
        нужными UID и папкой «СОГЛАСОВАНИЯ».

        Args:
            folder: Где искать, обычное имя папки
            date_from: С какой даты, YYYY-MM-DD
            date_to: По какую дату включительно, YYYY-MM-DD
            min_score: Порог балла. При 5 остаётся короткий проверяемый
                список; 3 и ниже захватывает всю деловую переписку с
                вложениями. Поле score_histogram в ответе показывает,
                сколько писем набрало каждый балл
            limit: Сколько кандидатов вернуть
            unseen_only: Смотреть только непрочитанные
            max_scan: Сколько писем просмотреть, начиная со свежих. Если в
                папке их больше, в ответе придут skipped_older и note —
                отброшенное не замалчивается
        """
        try:
            return _find_candidates_impl(folder, date_from, date_to,
                                         min_score, limit, unseen_only,
                                         max_scan)
        except Exception as exc:
            log.error(f"find_approval_candidates: {exc}")
            return {"error": str(exc)}

    log.info("Зарегистрирован инструмент отбора: find_approval_candidates")
