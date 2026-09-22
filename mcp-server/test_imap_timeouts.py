# -*- coding: utf-8 -*-
"""Тесты таймаутов IMAP: Mail.ru молчит — инструмент быстро и понятно падает.

Главный сценарий проверяется на настоящем imaplib: локальный сервер
принимает соединение и молчит, как Mail.ru во время сбоя 22.09.2026.

Запуск:  python -m pytest test_imap_timeouts.py -v
"""

import errno
import imaplib
import json
import socket
import threading
import time

import pytest

import imap_client
import mail_errors
import mail_tools_patch
import tools
from test_email_attachments import FakeMCP, FakeSMTP


@pytest.fixture
def silent_server():
    """TCP-сервер, который принимает подключение и ничего не отвечает."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()
    clients = []

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                clients.append(conn)
            except OSError:
                continue

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    for conn in clients:
        conn.close()
    srv.close()


@pytest.fixture
def handlers(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(imap_client.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(imap_client.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(tools, "_render_letter_pdf", lambda **kw: b"%PDF-letter")
    mcp = FakeMCP()
    tools.register_tools(mcp)
    return mcp.tools


# ------------------------------------------------ настоящий imaplib

def test_silent_server_send_fails_fast_and_smtp_not_called(
        handlers, silent_server, monkeypatch):
    monkeypatch.setattr(imap_client, "IMAP_HOST", "127.0.0.1")
    monkeypatch.setattr(imap_client, "IMAP_PORT", silent_server)
    monkeypatch.setattr(mail_errors, "IMAP_TIMEOUT", 0.5)

    started = time.monotonic()
    result = json.loads(handlers["send_new_email"](
        to="client@example.ru", subject="Тест", body="Текст"))
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"ждали {elapsed:.1f} с — таймаут не сработал"
    assert "Mail.ru не отвечает" in result["error"]
    assert result.get("status") != "sent"
    assert FakeSMTP.sent == []


def test_silent_server_search_mail_fails_fast(silent_server, monkeypatch):
    monkeypatch.setenv("MAIL_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("MAIL_IMAP_PORT", str(silent_server))
    monkeypatch.setenv("MAIL_USERNAME", "stavgeo26@mail.ru")
    monkeypatch.setenv("MAIL_PASSWORD", "x")
    monkeypatch.setattr(mail_errors, "IMAP_TIMEOUT", 0.5)
    mcp = FakeMCP()
    mail_tools_patch.register_tools(mcp)

    started = time.monotonic()
    result = mcp.tools["search_mail"](keywords=["счёт"])
    assert time.monotonic() - started < 5
    assert "Mail.ru не отвечает" in result["error"]


# ------------------------------------------------ таймаут передаётся

class RecordingIMAP:
    calls = []

    def __init__(self, host, port, **kwargs):
        RecordingIMAP.calls.append((host, port, kwargs))

    def login(self, user, password):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def test_imap_client_passes_timeout(monkeypatch):
    RecordingIMAP.calls = []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", RecordingIMAP)
    monkeypatch.setattr(mail_errors, "IMAP_TIMEOUT", 17.0)
    imap_client.IMAPClient().connect()
    assert RecordingIMAP.calls[-1][2] == {"timeout": 17.0}


def test_tools_patch_connection_passes_timeout(monkeypatch):
    RecordingIMAP.calls = []
    monkeypatch.setattr(mail_tools_patch.imaplib, "IMAP4_SSL", RecordingIMAP)
    monkeypatch.setattr(mail_errors, "IMAP_TIMEOUT", 17.0)
    monkeypatch.setenv("MAIL_USERNAME", "stavgeo26@mail.ru")
    monkeypatch.setenv("MAIL_PASSWORD", "x")
    with mail_tools_patch._imap_connection():
        pass
    assert RecordingIMAP.calls[-1][2] == {"timeout": 17.0}


def test_default_timeout_is_30_seconds():
    assert mail_errors.IMAP_TIMEOUT == 30


# ------------------------------------------------ тексты ошибок

@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    imaplib.IMAP4.abort("socket error: EOF"),
    ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
    OSError(errno.ETIMEDOUT, "Connection timed out"),
])
def test_connection_errors_get_clear_text(exc):
    assert mail_errors.is_connection_error(exc)
    assert "Mail.ru не отвечает" in mail_errors.describe(exc)


@pytest.mark.parametrize("exc", [
    ValueError("Письмо с UID 1 не найдено"),
    imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"),
    RuntimeError("Не удалось открыть папку: X"),
])
def test_other_errors_unchanged(exc):
    assert not mail_errors.is_connection_error(exc)
    assert mail_errors.describe(exc) == str(exc)
