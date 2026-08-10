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
    from mail_read_tools import _fetch_message, _select
    from mail_tools_patch import (_imap_connection, _search_uids,
                                  build_criteria)
except ImportError:  # пакетный импорт
    from . import mail_read
    from .mail_read_tools import _fetch_message, _select
    from .mail_tools_patch import (_imap_connection, _search_uids,
                                   build_criteria)

log = logging.getLogger(__name__)

MAX_SCAN = 200
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


def score_message(msg, uid: str = "", flags=None) -> dict:
    """Оценивает, похоже ли письмо на ответ-согласование от организации.

    Возвращает балл и список причин на человеческом языке, чтобы решение
    можно было проверить глазами, а не принимать на веру.
    """
    flags = flags or []
    subject = mail_read.decode_mime_header(msg.get("Subject", ""))
    from_raw = mail_read.decode_mime_header(msg.get("From", ""))
    sender_email = email.utils.parseaddr(from_raw)[1].lower()
    domain = sender_email.split("@")[-1] if "@" in sender_email else ""

    body = mail_read.extract_body(msg)
    own_text, _ = mail_read.split_quotes(body["text"])
    attachments = mail_read.list_attachments(msg)
    docs = [a for a in attachments
            if a["filename"].lower().endswith((".pdf", ".docx", ".doc",
                                               ".tif", ".tiff"))]

    score = 0
    reasons: list[str] = []

    if msg.get("In-Reply-To") or msg.get("References"):
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

    if docs:
        score += 1
        reasons.append(f"документ во вложении: {docs[0]['filename']}")
        if len(own_text.strip()) < SHORT_BODY:
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

    return {
        "uid": uid,
        "score": score,
        "reasons": reasons,
        "subject": subject or "(без темы)",
        "from": from_raw,
        "sender_email": sender_email,
        "date": msg.get("Date", ""),
        "attachments": [a["filename"] for a in attachments],
        "has_document": bool(docs),
        "body_preview": own_text[:200],
        "flags": flags,
    }


def _find_candidates_impl(folder: str = "INBOX", date_from: str = "",
                          date_to: str = "", min_score: int = DEFAULT_MIN_SCORE,
                          limit: int = 50, unseen_only: bool = False) -> dict:
    try:
        criteria = build_criteria(None, "both", date_from, date_to, "",
                                  False, unseen_only)
    except ValueError as exc:
        return {"error": f"Некорректная дата (ожидается YYYY-MM-DD): {exc}"}

    scanned, candidates = 0, []
    histogram: dict[int, int] = {}
    with _imap_connection() as imap:
        _select(imap, folder)
        uids, mode = _search_uids(imap, criteria)
        if mode == "client-filter":
            typ, data = imap.uid("SEARCH", "ALL")
            uids = ([u.decode() for u in data[0].split()]
                    if typ == "OK" and data and data[0] else [])

        # Берём самые свежие: старое уже разобрано руками
        uids = uids[-MAX_SCAN:]
        for uid in uids:
            try:
                msg, flags = _fetch_message(imap, uid, folder)
            except Exception as exc:
                log.warning(f"triage: UID {uid} не прочитан: {exc}")
                continue
            scanned += 1
            item = score_message(msg, uid, flags)
            histogram[item["score"]] = histogram.get(item["score"], 0) + 1
            if item["score"] >= min_score:
                candidates.append(item)

    candidates.sort(key=lambda c: (c["score"], c["date"]), reverse=True)
    trimmed = candidates[:limit] if limit and limit > 0 else candidates

    return {
        "folder": folder,
        "scanned": scanned,
        "found": len(candidates),
        "min_score": min_score,
        # Сколько писем набрало каждый балл — видно, куда двигать порог,
        # не перебирая значения вызовами
        "score_histogram": histogram,
        "candidates": trimmed,
        "uids": [c["uid"] for c in trimmed],
        "hint": ("Письма не перемещены. Проверьте список и вызовите "
                 "move_emails с нужными UID и target_folder «СОГЛАСОВАНИЯ»."),
    }


def register_tools(mcp):
    """Регистрирует инструмент отбора писем-согласований."""

    @mcp.tool()
    def find_approval_candidates(folder: str = "INBOX", date_from: str = "",
                                 date_to: str = "",
                                 min_score: int = DEFAULT_MIN_SCORE,
                                 limit: int = 50,
                                 unseen_only: bool = False) -> dict:
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
        """
        try:
            return _find_candidates_impl(folder, date_from, date_to,
                                         min_score, limit, unseen_only)
        except Exception as exc:
            log.error(f"find_approval_candidates: {exc}")
            return {"error": str(exc)}

    log.info("Зарегистрирован инструмент отбора: find_approval_candidates")
