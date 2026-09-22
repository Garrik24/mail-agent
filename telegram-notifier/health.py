"""
Предупреждение в Telegram, когда проверка почты раз за разом не проходит.

Если Mail.ru не отвечает или сменился пароль, уведомления о письмах
перестают приходить молча. Поэтому после ALERT_AFTER_FAILURES неудачных
проверок подряд владельцу уходит одно предупреждение, а когда проверка
снова проходит — одно сообщение о восстановлении. Повторов нет: состояние
хранится в meta (SQLite) и переживает перезапуск сервиса.
"""

import html
import logging
import os
from datetime import datetime, timezone

from db import get_meta, set_meta

log = logging.getLogger(__name__)

# 3 проверки подряд при интервале 15 мин — предупреждение через ~30–45 мин
ALERT_AFTER_FAILURES = int(os.environ.get("ALERT_AFTER_FAILURES", "3"))

_FAILURES = "check_failures"
_FIRST_FAILURE_AT = "first_failure_at"
_ALERT_SENT_AT = "failure_alert_sent_at"


def _send(text: str) -> bool:
    from notifier import send_telegram  # лениво: тянет anthropic
    return send_telegram(text)


def _minutes_since(iso: str) -> int:
    try:
        started = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - started).total_seconds() // 60)


def record_failure(error: BaseException) -> None:
    """Учесть неудачную проверку; на N-й подряд — предупредить один раз."""
    failures = int(get_meta(_FAILURES) or 0) + 1
    set_meta(_FAILURES, str(failures))
    if failures == 1:
        set_meta(_FIRST_FAILURE_AT, datetime.now(timezone.utc).isoformat())
    log.warning(f"Проверка почты не удалась ({failures} подряд): {error}")

    if failures < ALERT_AFTER_FAILURES or get_meta(_ALERT_SENT_AT):
        return
    minutes = _minutes_since(get_meta(_FIRST_FAILURE_AT))
    detail = html.escape(f"{type(error).__name__}: {error}")[:300]
    sent = _send(
        "⚠️ <b>Mail Agent: не могу проверить почту</b>\n"
        f"Проверка не проходит {failures} раза подряд (≈{minutes} мин). "
        "Уведомления о новых письмах сейчас не приходят.\n"
        f"Ошибка: <code>{detail}</code>"
    )
    # Не ушло (Telegram тоже недоступен) — попробуем на следующем сбое
    if sent:
        set_meta(_ALERT_SENT_AT, datetime.now(timezone.utc).isoformat())


def record_success(lookback_hours: int) -> None:
    """Проверка прошла: сбросить счётчик, сообщить о восстановлении.

    lookback_hours — окно, за которое проверка берёт письма: всё, что
    пришло за время сбоя короче этого окна, уже проверено этой проверкой.
    """
    if get_meta(_ALERT_SENT_AT):
        minutes = _minutes_since(get_meta(_FIRST_FAILURE_AT))
        if minutes < lookback_hours * 60:
            tail = "Письма, пришедшие за это время, уже проверены."
        else:
            tail = (f"Сбой был дольше {lookback_hours} ч — письма старше "
                    "этого срока не проверены, просмотрите ящик вручную.")
        _send(
            "✅ <b>Mail Agent: почта снова проверяется</b>\n"
            f"Сбой длился ≈{minutes} мин. {tail}"
        )
    if get_meta(_FAILURES) not in ("", "0"):
        set_meta(_FAILURES, "0")
        set_meta(_FIRST_FAILURE_AT, "")
        set_meta(_ALERT_SENT_AT, "")
