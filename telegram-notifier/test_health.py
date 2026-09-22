"""Тесты: таймауты IMAP и предупреждение в Telegram о сбоях проверки почты.

Без сети: Telegram — заглушка, IMAP — поддельный или локальный молчащий
сервер, SQLite — временный файл.

Запуск:  python -m pytest test_health.py -v
"""

import imaplib
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

import db
import health
import imap_client
import main


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Чистая БД и перехват сообщений в Telegram."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    messages = []
    monkeypatch.setattr(health, "_send", lambda text: messages.append(text) or True)
    monkeypatch.setattr(health, "ALERT_AFTER_FAILURES", 3)
    return messages


# ------------------------------------------------ предупреждение о сбоях

def test_alert_once_after_three_failures(sent):
    for _ in range(2):
        health.record_failure(TimeoutError("timed out"))
    assert sent == []
    health.record_failure(TimeoutError("timed out"))
    assert len(sent) == 1
    assert "не могу проверить почту" in sent[0]
    assert "3 раза подряд" in sent[0]
    for _ in range(5):
        health.record_failure(TimeoutError("timed out"))
    assert len(sent) == 1, "предупреждение не должно повторяться"


def test_recovery_message_once_and_counter_reset(sent):
    for _ in range(3):
        health.record_failure(TimeoutError("timed out"))
    health.record_success(24)
    assert len(sent) == 2
    assert "почта снова проверяется" in sent[1]
    assert "уже проверены" in sent[1]
    health.record_success(24)
    assert len(sent) == 2
    # после восстановления счёт начинается заново
    for _ in range(2):
        health.record_failure(TimeoutError("timed out"))
    assert len(sent) == 2


def test_short_glitch_is_silent(sent):
    health.record_failure(TimeoutError("timed out"))
    health.record_failure(TimeoutError("timed out"))
    health.record_success(24)
    assert sent == []
    assert db.get_meta("check_failures") == "0"


def test_alert_retried_if_telegram_was_down(sent, monkeypatch):
    attempts = []
    monkeypatch.setattr(health, "_send",
                        lambda text: attempts.append(text) or len(attempts) > 1)
    for _ in range(3):
        health.record_failure(TimeoutError("timed out"))
    assert len(attempts) == 1  # не ушло
    health.record_failure(TimeoutError("timed out"))
    assert len(attempts) == 2  # повтор ушёл
    health.record_failure(TimeoutError("timed out"))
    assert len(attempts) == 2


def test_state_survives_restart(sent):
    """Счётчик и отметка о предупреждении — в SQLite, не в памяти."""
    for _ in range(3):
        health.record_failure(TimeoutError("timed out"))
    assert db.get_meta("check_failures") == "3"
    assert db.get_meta("failure_alert_sent_at")


def test_error_text_is_html_escaped(sent):
    for _ in range(3):
        health.record_failure(imaplib.IMAP4.error("<b>AUTH</b> failed & denied"))
    assert "&lt;b&gt;AUTH&lt;/b&gt; failed &amp; denied" in sent[0]


def test_long_outage_warns_about_unchecked_mail(sent):
    for _ in range(3):
        health.record_failure(TimeoutError("timed out"))
    long_ago = datetime.now(timezone.utc) - timedelta(hours=30)
    db.set_meta("first_failure_at", long_ago.isoformat())
    health.record_success(24)
    assert "не проверены" in sent[-1]


def test_run_check_wires_failures_and_success(sent, monkeypatch):
    def broken():
        raise TimeoutError("timed out")
    monkeypatch.setattr(main, "check_emails", broken)
    for _ in range(3):
        main.run_check()
    assert len(sent) == 1
    monkeypatch.setattr(main, "check_emails", lambda: None)
    main.run_check()
    assert len(sent) == 2 and "снова проверяется" in sent[1]


# ------------------------------------------------ IMAP: ошибки связи не глотаются

def raw_message(subject: str, sender: str = "client@example.ru") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{subject.encode().hex()}@example.ru>"
    msg.set_content("Текст")
    return msg.as_bytes()


class FakeIMAP:
    """Поддельный IMAP: папки, письма и точка, где «ломается связь»."""
    instances = []

    def __init__(self, host, port, timeout=None, fail_on=None, messages=None):
        self.timeout = timeout
        self.fail_on = fail_on or {}
        self.messages = messages or {b"1": raw_message("первое")}
        FakeIMAP.instances.append(self)

    def _maybe_fail(self, step):
        if step in self.fail_on:
            raise self.fail_on[step]

    def login(self, user, password):
        self._maybe_fail("login")
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren) "/" "bad\\"name"']

    def select(self, mailbox, readonly=False):
        if "bad" in mailbox:
            raise imaplib.IMAP4.error("SELECT command error: BAD")
        return "OK", [b"1"]

    def search(self, charset, criteria):
        return "OK", [b" ".join(self.messages)]

    def fetch(self, uid, items):
        self._maybe_fail("fetch")
        raw = self.messages[uid]
        if raw is None:
            raise ValueError("битое письмо")
        return "OK", [(b"1 (FLAGS ())", raw)]

    def logout(self):
        return "BYE", [b""]


def use_fake(monkeypatch, **kwargs):
    FakeIMAP.instances = []
    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL",
                        lambda host, port, timeout=None: FakeIMAP(
                            host, port, timeout=timeout, **kwargs))


def test_timeout_passed_to_imap(monkeypatch):
    use_fake(monkeypatch)
    monkeypatch.setattr(imap_client, "IMAP_TIMEOUT", 17.0)
    imap_client.fetch_recent_emails("22-Sep-2026", set())
    assert FakeIMAP.instances[-1].timeout == 17.0


@pytest.mark.parametrize("step, error", [
    ("login", imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")),
    ("login", TimeoutError("timed out")),
    ("fetch", TimeoutError("timed out")),
    ("fetch", imaplib.IMAP4.abort("socket error: EOF")),
])
def test_connection_and_login_errors_propagate(monkeypatch, step, error):
    use_fake(monkeypatch, fail_on={step: error})
    with pytest.raises(type(error)):
        imap_client.fetch_recent_emails("22-Sep-2026", set())


def test_broken_single_message_and_bad_folder_are_skipped(monkeypatch):
    use_fake(monkeypatch, messages={b"1": raw_message("первое"), b"2": None,
                                    b"3": raw_message("третье")})
    emails = imap_client.fetch_recent_emails("22-Sep-2026", set())
    assert [e["subject"] for e in emails] == ["первое", "третье"]


def test_silent_server_does_not_hang(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    clients = []
    threading.Thread(target=lambda: clients.append(srv.accept()[0]),
                     daemon=True).start()
    monkeypatch.setattr(imap_client, "IMAP_HOST", "127.0.0.1")
    monkeypatch.setattr(imap_client, "IMAP_PORT", srv.getsockname()[1])
    monkeypatch.setattr(imap_client, "IMAP_TIMEOUT", 0.5)
    started = time.monotonic()
    try:
        with pytest.raises(OSError):
            imap_client.fetch_recent_emails("22-Sep-2026", set())
        assert time.monotonic() - started < 5
    finally:
        for conn in clients:
            conn.close()
        srv.close()
