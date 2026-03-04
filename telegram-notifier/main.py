"""
Telegram нотификатор — фоновый воркер.
Проверяет почту каждые 15 минут, отправляет важные письма в Telegram.
"""

import logging
import time
import os
from datetime import datetime, timezone, timedelta

from imap_client import fetch_recent_emails
from classifier import classify_email, EmailCategory
from notifier import notify, send_telegram
from db import init_db, is_processed, mark_processed, get_all_processed_ids, cleanup_old

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Интервал проверки (секунды)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))  # 15 мин
# Глубина поиска при первом запуске (часы)
INITIAL_LOOKBACK_HOURS = int(os.environ.get("INITIAL_LOOKBACK_HOURS", "24"))


def check_emails():
    """Один цикл проверки почты."""
    log.info("─── Начинаю проверку почты ───")

    # Дата для IMAP SINCE (проверяем за последние сутки с запасом)
    since = datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)
    since_str = since.strftime("%d-%b-%Y")

    # Получаем уже обработанные ID
    processed_ids = get_all_processed_ids()
    log.info(f"Уже обработано: {len(processed_ids)} писем")

    # Получаем письма
    emails = fetch_recent_emails(since_str, processed_ids)

    if not emails:
        log.info("Новых писем нет")
        return

    # Классифицируем и обрабатываем
    stats = {"URGENT": 0, "IMPORTANT": 0, "LEAD": 0, "SKIP": 0}

    for email_data in emails:
        message_id = email_data.get("message_id", "")

        # Повторная проверка (на случай гонки)
        if is_processed(message_id):
            continue

        category, reason = classify_email(email_data)
        stats[category] = stats.get(category, 0) + 1

        if category != EmailCategory.SKIP:
            notify(email_data, category, reason)

        # Помечаем как обработанное (даже SKIP, чтобы не проверять повторно)
        mark_processed(message_id, category)

    log.info(
        f"Итого: 🔴 срочных={stats['URGENT']} "
        f"🟡 важных={stats['IMPORTANT']} "
        f"🟢 лидов={stats['LEAD']} "
        f"⏭ пропущено={stats['SKIP']}"
    )


def main():
    log.info("=" * 50)
    log.info("🚀 Telegram нотификатор запущен")
    log.info(f"Интервал проверки: {CHECK_INTERVAL // 60} мин")
    log.info("=" * 50)

    # Инициализация БД
    init_db()

    # Очистка старых записей при старте
    cleanup_old(days=30)

    # Уведомление о запуске
    send_telegram("🤖 <b>Mail Agent запущен</b>\nПроверяю почту каждые 15 минут.")

    # Первый запуск сразу
    try:
        check_emails()
    except Exception as e:
        log.error(f"Ошибка первой проверки: {e}")

    # Основной цикл
    while True:
        log.info(f"Следующая проверка через {CHECK_INTERVAL // 60} мин...")
        time.sleep(CHECK_INTERVAL)
        try:
            check_emails()
        except Exception as e:
            log.error(f"Ошибка проверки почты: {e}")
            # Не падаем — ждём следующего цикла


if __name__ == "__main__":
    main()
