"""
Отправка уведомлений в Telegram с анализом от Claude.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import anthropic
import httpx

import gemini_fallback

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MSK = timezone(timedelta(hours=3))

# Эмодзи и метки по категориям
CATEGORY_LABELS = {
    "URGENT": ("🔴 СРОЧНО", "🔴"),
    "IMPORTANT": ("🟡 ВАЖНО", "🟡"),
    "LEAD": ("🟢 ЛИД", "🟢"),
}


def _claude_outage(exc: Exception) -> bool:
    """Claude недоступен как сервис (баланс, блокировка организации, ключ, лимиты, сбои)."""
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return gemini_fallback.is_outage(exc.status_code, str(exc))
    return False


def analyze_with_claude(email_data: dict) -> dict:
    """
    Отправляет письмо Claude для анализа; если Claude недоступен — Gemini.
    Возвращает: {"summary": "...", "action": "...", "urgency": "..."}
    """
    if not ANTHROPIC_API_KEY and not gemini_fallback.enabled():
        return {
            "summary": email_data.get("subject", ""),
            "action": "Проверить письмо",
            "urgency": "средняя",
        }

    prompt = f"""Проанализируй деловое письмо и верни ТОЛЬКО JSON без markdown-блоков.

Письмо:
От: {email_data['sender_name']} <{email_data['sender_email']}>
Тема: {email_data['subject']}
Дата: {email_data['date']}

Текст:
{email_data['body'][:3000]}

Верни JSON:
{{
  "summary": "суть письма в 2-3 предложениях",
  "action": "конкретное действие которое нужно предпринять",
  "urgency": "высокая/средняя/низкая"
}}"""

    try:
        raw = None
        if ANTHROPIC_API_KEY:
            try:
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                message = client.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                # Первым блоком может прийти размышление — берём только текст.
                raw = "".join(b.text for b in message.content if b.type == "text").strip()
            except Exception as e:
                # Запасной канал — только при сбое самого сервиса Anthropic.
                if not (gemini_fallback.enabled() and _claude_outage(e)):
                    raise
                log.warning(f"Claude недоступен ({str(e)[:150]}) — письмо анализирует Gemini")
        if raw is None:
            raw = gemini_fallback.generate("", prompt, max_tokens=512, json_mode=True).strip()
        # Убираем markdown-обёртку если есть
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Ошибка Claude анализа: {e}")
        return {
            "summary": email_data.get("subject", "Не удалось проанализировать"),
            "action": "Проверить письмо вручную",
            "urgency": "средняя",
        }


def format_notification(email_data: dict, category: str,
                        reason: str, analysis: dict) -> str:
    """Форматирует уведомление для Telegram."""
    label, emoji = CATEGORY_LABELS.get(category, ("📧 ПИСЬМО", "📧"))

    # Время получения
    now_msk = datetime.now(MSK).strftime("%H:%M")

    # Обрезаем длинные поля
    summary = analysis.get("summary", "—")
    if len(summary) > 300:
        summary = summary[:297] + "..."
    action = analysis.get("action", "—")
    if len(action) > 200:
        action = action[:197] + "..."

    sender_display = email_data["sender_name"]
    if sender_display != email_data["sender_email"]:
        sender_display = f"{sender_display}"

    subject = email_data.get("subject", "Без темы")
    if len(subject) > 100:
        subject = subject[:97] + "..."

    text = (
        f"{label} | {reason}\n"
        f"{'─' * 30}\n"
        f"📧 <b>От:</b> {sender_display}\n"
        f"    {email_data['sender_email']}\n"
        f"📌 <b>Тема:</b> {subject}\n"
        f"💬 <b>Суть:</b> {summary}\n"
        f"⚡ <b>Действие:</b> {action}\n"
        f"🕐 <b>Получено:</b> {now_msk} МСК"
    )
    return text


def send_telegram(text: str) -> bool:
    """Отправить сообщение в Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram не настроен: нет BOT_TOKEN или CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(url, json=payload)
            if response.status_code == 200:
                log.info("Telegram уведомление отправлено")
                return True
            else:
                log.error(
                    f"Ошибка Telegram: {response.status_code} {response.text}"
                )
                return False
    except Exception as e:
        log.error(f"Ошибка отправки в Telegram: {e}")
        return False


def notify(email_data: dict, category: str, reason: str):
    """Полный цикл: анализ Claude + форматирование + отправка в Telegram."""
    log.info(
        f"Обработка [{category}]: {email_data['subject'][:60]} "
        f"от {email_data['sender_email']}"
    )
    analysis = analyze_with_claude(email_data)
    text = format_notification(email_data, category, reason, analysis)
    send_telegram(text)
