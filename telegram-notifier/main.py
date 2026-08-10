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
from db import (init_db, is_processed, mark_processed, get_all_processed_ids,
                cleanup_old, get_meta, set_meta)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Интервал проверки (секунды)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))  # 15 мин
# Глубина поиска при первом запуске (часы)
INITIAL_LOOKBACK_HOURS = int(os.environ.get("INITIAL_LOOKBACK_HOURS", "24"))
# Сообщение «Mail Agent запущен» в Telegram. По умолчанию выключено: если
# сервис перезапускается, оно превращается в поток одинаковых уведомлений.
# Работа воркера от него не зависит — старт всегда виден в логах.
STARTUP_NOTIFY = os.environ.get("STARTUP_NOTIFY", "false").lower() in (
    "1", "true", "yes", "да")
# Даже когда уведомление включено, повторять его не чаще этого интервала
STARTUP_NOTICE_COOLDOWN_HOURS = int(
    os.environ.get("STARTUP_NOTICE_COOLDOWN_HOURS", "6"))


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


def maybe_notify_startup():
    """Сообщает в Telegram о запуске, если это включено и не слишком часто.

    Сообщение полезно после деплоя, но при перезапусках воркера оно
    превращается в поток одинаковых уведомлений, поэтому по умолчанию
    выключено, а при включении ограничено по частоте.
    """
    if not STARTUP_NOTIFY:
        log.info("Уведомление о запуске отключено (STARTUP_NOTIFY)")
        return

    now = datetime.now(timezone.utc)
    last = get_meta("last_startup_notice")
    if last:
        try:
            previous = datetime.fromisoformat(last)
            passed = (now - previous).total_seconds() / 3600
            if passed < STARTUP_NOTICE_COOLDOWN_HOURS:
                log.info(f"Уведомление о запуске пропущено: прошло "
                         f"{passed:.1f} ч из {STARTUP_NOTICE_COOLDOWN_HOURS}")
                return
        except ValueError:
            log.warning(f"Непонятная отметка последнего запуска: {last!r}")

    send_telegram(
        f"🤖 <b>Mail Agent запущен</b>\n"
        f"Проверяю почту каждые {CHECK_INTERVAL // 60} минут."
    )
    set_meta("last_startup_notice", now.isoformat())


def main():
    log.info("=" * 50)
    log.info("🚀 Telegram нотификатор запущен")
    log.info(f"Интервал проверки: {CHECK_INTERVAL // 60} мин")
    log.info("=" * 50)

    # Инициализация БД
    init_db()

    # Очистка старых записей при старте
    cleanup_old(days=30)

    # Уведомление о запуске — только если его явно попросили
    maybe_notify_startup()

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
