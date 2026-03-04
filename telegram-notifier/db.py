"""
SQLite хранилище обработанных email_id для дедупликации.
"""

import sqlite3
import logging
import os

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "processed_emails.db")


def init_db():
    """Инициализировать БД и создать таблицу если не существует."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            category TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"БД инициализирована: {DB_PATH}")


def is_processed(message_id: str) -> bool:
    """Проверить, обработано ли письмо."""
    if not message_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id = ?",
        (message_id,),
    )
    result = cursor.fetchone() is not None
    conn.close()
    return result


def mark_processed(message_id: str, category: str):
    """Отметить письмо как обработанное."""
    if not message_id:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO processed_emails (message_id, category) VALUES (?, ?)",
        (message_id, category),
    )
    conn.commit()
    conn.close()


def get_all_processed_ids() -> set[str]:
    """Получить все обработанные message_id (для быстрой фильтрации в IMAP)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT message_id FROM processed_emails")
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids


def cleanup_old(days: int = 30):
    """Удалить записи старше N дней (чтобы БД не росла бесконечно)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM processed_emails WHERE processed_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()
    conn.close()
    log.info(f"Очищены записи старше {days} дней")
