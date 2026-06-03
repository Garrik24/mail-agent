# -*- coding: utf-8 -*-
"""Краткосрочное in-memory хранилище отрендеренных PDF для предпросмотра письма.

preview_letter кладёт сюда готовый PDF и получает случайный токен; ASGI-маршрут
/preview/<token>.pdf в main.py читает PDF обратно и отдаёт в браузер. Работает в
пределах одного процесса (tool-вызовы и HTTP-обработчик исполняются в общем
процессе uvicorn), поэтому достаточно обычного словаря с TTL — без диска.
"""
import os
import secrets
import threading
import time

TTL_SECONDS = int(os.environ.get("PREVIEW_TTL_SECONDS", str(60 * 60)))  # 1 час
MAX_ITEMS = 50

_lock = threading.Lock()
_store: dict[str, dict] = {}  # token -> {"pdf": bytes, "filename": str, "expires": float}


def _purge_locked() -> None:
    """Удаляет просроченные записи и держит размер в пределах MAX_ITEMS.
    Вызывать только под _lock."""
    now = time.time()
    for token in [t for t, v in _store.items() if v["expires"] < now]:
        _store.pop(token, None)
    if len(_store) > MAX_ITEMS:
        oldest = sorted(_store.items(), key=lambda kv: kv[1]["expires"])
        for token, _ in oldest[: len(_store) - MAX_ITEMS]:
            _store.pop(token, None)


def save_preview(pdf_bytes: bytes, filename: str) -> str:
    """Сохраняет PDF, возвращает случайный URL-safe токен."""
    token = secrets.token_urlsafe(24)
    with _lock:
        _purge_locked()
        _store[token] = {
            "pdf": pdf_bytes,
            "filename": filename or "Письмо.pdf",
            "expires": time.time() + TTL_SECONDS,
        }
    return token


def load_preview(token: str):
    """Возвращает (pdf_bytes, filename) или None, если токен неизвестен/просрочен."""
    if not token:
        return None
    with _lock:
        _purge_locked()
        item = _store.get(token)
        if not item:
            return None
        return item["pdf"], item["filename"]
