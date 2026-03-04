"""
Классификатор писем по важности.

Категории:
- URGENT (🔴) — госорганы
- IMPORTANT (🟡) — финансовые документы, флаг Important
- LEAD (🟢) — коммерческие запросы/КП
- SKIP — всё остальное (не отправляем в Telegram)
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Паттерны для классификации ───────────────────────────────────────────────

# Госорганы: домены и ключевые слова в отправителе
GOV_DOMAINS = {
    "rosreestr.ru", "nalog.ru", "sudrf.ru", "gosuslugi.ru",
    "fss.ru", "pfr.ru", "mos.ru", "gov.ru", "fns.ru",
    "mvd.ru", "mchs.ru", "minstroy.ru", "rosprirodnadzor.ru",
    "rostechnadzor.ru", "rospotrebnadzor.ru",
}

GOV_KEYWORDS_SENDER = re.compile(
    r"росреестр|налогов|суд|администрац|мвд|фсс|пфр|минстрой|"
    r"прокуратур|пристав|инспекц|кадастр|госуслуг|роспотребнадзор|"
    r"ростехнадзор|росприроднадзор|пенсионн",
    re.IGNORECASE,
)

# Финансовые документы: ключевые слова в теме/теле
FINANCE_KEYWORDS = re.compile(
    r"договор|счёт|счет|акт\b|оплат|задолженност|"
    r"дебиторск|кредиторск|штраф|пени|неустойк|"
    r"бухгалтер|накладн|счёт-фактур|счет-фактур|"
    r"платёж|платеж|взыскан|долг\b|просрочк",
    re.IGNORECASE,
)

# Суммы денег
MONEY_PATTERN = re.compile(
    r"\d[\d\s.,]*\s*(?:руб|₽|рублей|р\.)",
    re.IGNORECASE,
)

# Коммерческие запросы / лиды
LEAD_KEYWORDS = re.compile(
    r"коммерческ\w*\s+предложен|(?:^|\s)КП(?:\s|$|,|\.)|\bкп\b|"
    r"стоимость\s+работ|сколько\s+стоит|запрос\s+(?:цен|стоимост)|"
    r"ценовое\s+предложен|тендер|конкурс|закупк|котировк|"
    r"заявк\w+\s+на\s+(?:участ|выполнен)|техническ\w+\s+задани",
    re.IGNORECASE,
)


class EmailCategory:
    URGENT = "URGENT"       # 🔴 Госорганы
    IMPORTANT = "IMPORTANT" # 🟡 Финансы / флаг Important
    LEAD = "LEAD"           # 🟢 Коммерческий запрос
    SKIP = "SKIP"           # Не отправлять


def classify_email(email_data: dict) -> tuple[str, str]:
    """
    Классифицирует письмо.
    Возвращает (категория, причина).
    """
    sender_email = email_data.get("sender_email", "").lower()
    sender_name = email_data.get("sender_name", "").lower()
    subject = email_data.get("subject", "")
    body = email_data.get("body", "")[:3000]
    is_flagged = email_data.get("is_flagged", False)
    text = f"{subject} {body}"

    # 1. Госорганы — СРОЧНО 🔴
    sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""
    if sender_domain in GOV_DOMAINS:
        return EmailCategory.URGENT, f"Госорган: {sender_domain}"

    if GOV_KEYWORDS_SENDER.search(sender_name):
        return EmailCategory.URGENT, f"Госорган: {sender_name}"

    if GOV_KEYWORDS_SENDER.search(sender_email):
        return EmailCategory.URGENT, f"Госорган: {sender_email}"

    # 2. Финансовые документы — ВАЖНО 🟡
    finance_match = FINANCE_KEYWORDS.search(text)
    money_match = MONEY_PATTERN.search(text)

    if finance_match and money_match:
        return (EmailCategory.IMPORTANT,
                f"Финансы: {finance_match.group()} + сумма")

    if finance_match:
        return EmailCategory.IMPORTANT, f"Документ: {finance_match.group()}"

    if money_match:
        return EmailCategory.IMPORTANT, f"Сумма: {money_match.group().strip()}"

    # 3. Коммерческие запросы — ЛИД 🟢
    lead_match = LEAD_KEYWORDS.search(text)
    if lead_match:
        return EmailCategory.LEAD, f"Лид: {lead_match.group().strip()}"

    # 4. Флаг Important — ВАЖНО 🟡
    if is_flagged:
        return EmailCategory.IMPORTANT, "Флаг Important"

    # 5. Всё остальное — пропускаем
    return EmailCategory.SKIP, ""
