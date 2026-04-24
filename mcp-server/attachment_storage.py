"""
Chunked upload хранилище для вложений.

Клиент режет base64-содержимое на куски и загружает через несколько tool-вызовов;
сервер накапливает куски на диске, при finish декодирует в бинарь и затем
прикрепляет по upload_id к письму. Однократное использование: после отправки
файл удаляется; не использованные сессии чистятся по TTL.
"""

import base64
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/mail-agent-uploads"))
TTL_SECONDS = 60 * 60  # 1 час
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 МБ (SMTP-лимит Mail.ru)
MAX_ACTIVE_SESSIONS = 50
CLEANUP_INTERVAL = 5 * 60  # 5 минут

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _session_path(upload_id: str) -> Path:
    """Возвращает путь к сессии, предварительно валидируя upload_id как UUID.

    Любой не-UUID вход отвергается, что исключает path traversal: строка может
    содержать только hex-символы и дефисы.
    """
    try:
        uuid.UUID(upload_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Невалидный upload_id: {upload_id!r}")
    return UPLOAD_DIR / upload_id


def start_upload(filename: str, mime_type: str) -> dict:
    """Создаёт новую сессию загрузки."""
    if not filename:
        raise ValueError("filename не может быть пустым")

    with _lock:
        active = [p for p in UPLOAD_DIR.iterdir() if p.is_dir()]
        if len(active) >= MAX_ACTIVE_SESSIONS:
            raise RuntimeError(
                f"Слишком много активных сессий ({len(active)}), "
                f"лимит {MAX_ACTIVE_SESSIONS}. Попробуй позже."
            )

        upload_id = str(uuid.uuid4())
        session_dir = _session_path(upload_id)
        session_dir.mkdir(parents=True, exist_ok=False)

        meta = {
            "filename": filename,
            "mime_type": mime_type or "application/octet-stream",
            "created_at": time.time(),
        }
        (session_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
        (session_dir / "content.b64").write_text("", encoding="utf-8")

        log.info(f"Upload создан: {upload_id} ({filename}, {mime_type})")
        return {"upload_id": upload_id, "chunk_size_recommended": 50000}


def append_chunk(upload_id: str, chunk_base64: str) -> dict:
    """Дописывает кусок base64 к сессии."""
    session_dir = _session_path(upload_id)
    if not session_dir.exists():
        raise ValueError(f"Сессия {upload_id} не найдена или истекла")

    b64_file = session_dir / "content.b64"
    with open(b64_file, "a", encoding="utf-8") as f:
        f.write(chunk_base64 or "")

    received_b64_size = b64_file.stat().st_size
    # Приблизительная оценка размера после декодирования: 3/4 от base64
    estimated_bytes = int(received_b64_size * 3 / 4)

    if estimated_bytes > MAX_ATTACHMENT_SIZE:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise RuntimeError(
            f"Превышен лимит размера {MAX_ATTACHMENT_SIZE} байт "
            f"(примерно {estimated_bytes})"
        )

    return {
        "upload_id": upload_id,
        "received_bytes": estimated_bytes,
        "status": "ok",
    }


def finish_upload(upload_id: str) -> dict:
    """Декодирует накопленный base64 в бинарь."""
    session_dir = _session_path(upload_id)
    if not session_dir.exists():
        raise ValueError(f"Сессия {upload_id} не найдена или истекла")

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    b64_file = session_dir / "content.b64"
    if not b64_file.exists():
        return {
            "upload_id": upload_id,
            "status": "error",
            "error": "Сессия уже финализирована или повреждена",
        }

    b64_content = b64_file.read_text(encoding="utf-8")

    try:
        # validate=True бракует любые левые символы
        binary = base64.b64decode(b64_content, validate=True)
    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        return {
            "upload_id": upload_id,
            "status": "error",
            "error": f"base64 decode: {e}",
        }

    if len(binary) > MAX_ATTACHMENT_SIZE:
        shutil.rmtree(session_dir, ignore_errors=True)
        return {
            "upload_id": upload_id,
            "status": "error",
            "error": f"Файл {len(binary)} байт > лимит {MAX_ATTACHMENT_SIZE}",
        }

    (session_dir / "content.bin").write_bytes(binary)
    b64_file.unlink()  # base64 больше не нужен — экономим место

    log.info(
        f"Upload готов: {upload_id} ({meta['filename']}, {len(binary)} байт)"
    )
    return {
        "upload_id": upload_id,
        "filename": meta["filename"],
        "size_bytes": len(binary),
        "status": "ready",
    }


def read_attachment(upload_id: str) -> tuple[bytes, str, str]:
    """Возвращает (content, filename, mime_type) для финализированного upload."""
    session_dir = _session_path(upload_id)
    if not session_dir.exists():
        raise ValueError(f"Вложение {upload_id} не найдено или истекло")

    bin_file = session_dir / "content.bin"
    if not bin_file.exists():
        raise ValueError(
            f"Вложение {upload_id} не финализировано "
            f"(нужно вызвать attachment_upload_finish)"
        )

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    return bin_file.read_bytes(), meta["filename"], meta["mime_type"]


def delete_attachment(upload_id: str) -> None:
    """Удаляет сессию. Используется после успешной отправки письма."""
    try:
        session_dir = _session_path(upload_id)
    except ValueError:
        return
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
        log.info(f"Upload удалён: {upload_id}")


def cleanup_expired() -> int:
    """Удаляет сессии старше TTL. Возвращает количество удалённых."""
    now = time.time()
    removed = 0
    if not UPLOAD_DIR.exists():
        return 0
    for session_dir in UPLOAD_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        meta_file = session_dir / "meta.json"
        if not meta_file.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if now - meta.get("created_at", 0) > TTL_SECONDS:
                shutil.rmtree(session_dir, ignore_errors=True)
                removed += 1
        except Exception:
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
    if removed:
        log.info(f"Cleanup: удалено {removed} просроченных сессий")
    return removed


def _cleanup_loop():
    while True:
        try:
            cleanup_expired()
        except Exception as e:
            log.warning(f"Ошибка cleanup: {e}")
        time.sleep(CLEANUP_INTERVAL)


_cleanup_thread = threading.Thread(
    target=_cleanup_loop, daemon=True, name="attachment-cleanup"
)
_cleanup_thread.start()
