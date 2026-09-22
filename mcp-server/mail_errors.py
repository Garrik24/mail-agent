# -*- coding: utf-8 -*-
"""Таймаут IMAP и понятный текст ошибок связи с почтовым сервером.

Без таймаута imaplib ждёт сколько угодно: при сбое Mail.ru подключение
висело ~2 минуты до системного [Errno 110], а зависшее чтение после
подключения не закончилось бы никогда. Таймаут из конструктора
IMAP4_SSL остаётся на сокете и действует и на подключение, и на каждое
чтение. Считается по молчанию сервера, а не по длине операции, поэтому
большое вложение скачивается, пока идут данные.
"""

import errno
import imaplib
import os

IMAP_TIMEOUT = float(os.environ.get("MAIL_IMAP_TIMEOUT", "30"))

_NETWORK_ERRNOS = {errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET,
                   errno.EHOSTUNREACH, errno.ENETUNREACH}


def is_connection_error(exc: BaseException) -> bool:
    """Сервер не отвечает или оборвал связь — в отличие от ошибки команды.

    Зависшее чтение даёт голый TimeoutError, обрыв при записи imaplib
    заворачивает в IMAP4.abort.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, imaplib.IMAP4.abort)):
        return True
    return isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS


def describe(exc: BaseException) -> str:
    """Текст ошибки для ответа инструмента."""
    if is_connection_error(exc):
        return (f"Почтовый сервер Mail.ru не отвечает или оборвал соединение "
                f"(таймаут {IMAP_TIMEOUT:g} с): {type(exc).__name__}: {exc}. "
                "Повтори запрос позже")
    return str(exc)
