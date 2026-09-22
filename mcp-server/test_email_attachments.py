# -*- coding: utf-8 -*-
"""Тесты вложений из существующих писем (email_attachments).

Без сети: IMAP — сервер в памяти, SMTP — заглушка, которая только
записывает отправленное. Тестовые письма собираются через EmailMessage.

Запуск:  python -m pytest test_email_attachments.py -v
"""

import base64
import email
import json
import unicodedata
from email.message import EmailMessage

import pytest

import imap_client
import mail_attachments
import mail_read
import tools
from imap_client import IMAPClient
from imap_utf7 import encode_mutf7
from mail_attachments import AttachmentError

INVOICE = "Счет № 245 от 22.09.2026.pdf"
BUH = "Бухгалтерия Наталья"
SENT = "Отправленные"


def make_message(subject: str, attachments=(), extra=()) -> bytes:
    """Письмо с вложениями: attachments — (имя, байты, mime),
    extra — готовые kwargs для add_attachment (inline-картинки и т.п.)."""
    msg = EmailMessage()
    msg["From"] = "Наталья <buh@example.ru>"
    msg["To"] = "stavgeo26@mail.ru"
    msg["Subject"] = subject
    msg.set_content("Добрый день! Файлы во вложении.")
    for filename, content, mime in attachments:
        maintype, subtype = mime.split("/")
        msg.add_attachment(content, maintype=maintype, subtype=subtype,
                           filename=filename)
    for kwargs in extra:
        kwargs = dict(kwargs)
        msg.add_attachment(kwargs.pop("data"), **kwargs)
    return msg.as_bytes()


SIGNATURE_IMAGES = (
    dict(data=b"\x89PNG-sig", maintype="image", subtype="png",
         filename="image001.png", disposition="inline", cid="<sig1>"),
    dict(data=b"\x89PNG-logo", maintype="image", subtype="png",
         filename="mailrusigimg_7f3a.png", disposition="inline"),
    # Встроенная картинка по Content-ID с человеческим именем
    dict(data=b"\x89PNG-banner", maintype="image", subtype="png",
         filename="banner.png", disposition="inline", cid="<banner>"),
)


class FakeIMAP:
    """IMAP-сервер в памяти: папки по raw-имени, письма по UID.

    Как настоящий сервер, FETCH BODY[] без PEEK ставит письму \\Seen,
    а BODY.PEEK[] — нет.
    """

    def __init__(self, folders: dict):
        self.folders = {}
        for name, messages in folders.items():
            raw = "INBOX" if name == "INBOX" else encode_mutf7(name)
            self.folders[raw] = messages
        self.sent_raw = encode_mutf7(SENT)
        self.folders.setdefault(self.sent_raw, {})
        self.selected = None
        self.commands = []
        self.seen = set()
        self.appended = []

    def list(self):
        lines = []
        for raw in self.folders:
            flags = "\\HasNoChildren \\Sent" if raw == self.sent_raw else "\\HasNoChildren"
            lines.append(f'({flags}) "/" "{raw}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        raw = mailbox[1:-1] if mailbox.startswith('"') else mailbox
        if raw not in self.folders:
            return "NO", [b"Folder not found"]
        self.selected = raw
        return "OK", [str(len(self.folders[raw])).encode()]

    def noop(self):
        return "OK", [b""]

    def uid(self, command, *args):
        command = command.upper()
        self.commands.append((command,) + args)
        if command == "FETCH":
            uid, items = args
            raw = self.folders[self.selected].get(uid)
            if raw is None:
                return "OK", [None]
            key = (self.selected, uid)
            if "BODY[" in items.replace("BODY.PEEK[", ""):
                self.seen.add(key)
            flags = "\\Seen" if key in self.seen else ""
            prefix = f"1 (UID {uid} FLAGS ({flags}) BODY[] {{{len(raw)}}}"
            return "OK", [(prefix.encode(), raw), b")"]
        if command == "STORE":
            if "\\Seen" in " ".join(str(a) for a in args):
                self.seen.add((self.selected, args[0]))
            return "OK", []
        return "NO", [b"unsupported"]

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, message))
        return "OK", []

    def logout(self):
        return "BYE", []


class FakeSMTP:
    """SMTP-заглушка: ничего не шлёт, записывает (from, rcpt, письмо)."""

    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def ehlo(self):
        return 250, b"ok"

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        FakeSMTP.sent.append((from_addr, list(to_addrs), msg))

    def quit(self):
        pass


class FakeMCP:
    """Собирает зарегистрированные инструменты по имени."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def mailbox(monkeypatch):
    imap = FakeIMAP({
        BUH: {
            "179012": make_message(
                "Счёт на оплату",
                [(INVOICE, b"%PDF-1.4 invoice", "application/pdf")],
                SIGNATURE_IMAGES),
        },
        "INBOX": {
            "178070": make_message(
                "Договор",
                [("Договор №57.docx", b"PK\x03\x04docx",
                  "application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document")]),
            "178071": make_message(
                "Второй счёт",
                [(INVOICE, b"%PDF-1.4 other invoice", "application/pdf"),
                 ("Схема.qqz", b"QQZ", "application/octet-stream"),
                 ("Отчёт.pdf", b"%PDF-1.4 report", "application/octet-stream")]),
            "500": make_message("Запрос документов"),
        },
    })
    monkeypatch.setattr(IMAPClient, "connect",
                        lambda self: setattr(self, "conn", imap))
    FakeSMTP.sent = []
    monkeypatch.setattr(imap_client.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(imap_client.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(tools, "_render_letter_pdf",
                        lambda **kw: b"%PDF-letter")
    monkeypatch.delenv("INTERNAL_EMAILS", raising=False)

    mcp = FakeMCP()
    tools.register_tools(mcp)
    return mcp.tools, imap


def spec(*entries) -> str:
    return json.dumps([dict(zip(("uid", "folder", "filename"), e))
                       for e in entries], ensure_ascii=False)


LETTER = dict(subject="О направлении счёта", addressee="Директору",
              isx_number="151", date_str="«22» сентября 2026 г.",
              body='[{"text": "Направляем счёт."}]')


def sent_attachments(index=-1) -> list[str]:
    raw = FakeSMTP.sent[index][2]
    return [a["filename"]
            for a in mail_read.list_attachments(email.message_from_string(raw))]


def sent_payload(filename: str, index=-1) -> bytes:
    msg = email.message_from_string(FakeSMTP.sent[index][2])
    for part, name in mail_read.attachment_parts(msg):
        if name == filename:
            return part.get_payload(decode=True)
    raise AssertionError(f"вложения {filename} нет в письме")


def parse(raw: bytes):
    return email.message_from_bytes(raw)


# --------------------------------------------------------- 1. картинки подписи

def test_empty_filename_takes_documents_without_signature_images():
    raw = make_message(
        "Счёт",
        [(INVOICE, b"%PDF-1.4", "application/pdf"),
         ("Фото участка.png", b"\x89PNG-photo", "image/png")],
        SIGNATURE_IMAGES)
    names = [a["filename"] for a in mail_attachments.extract_attachments(parse(raw))]
    # Картинка, приложенная обычным вложением, — документ; подпись — нет
    assert names == [INVOICE, "Фото участка.png"]


def test_signature_image_can_be_taken_explicitly():
    raw = make_message("Скан", [], SIGNATURE_IMAGES)
    found = mail_attachments.extract_attachments(parse(raw), "image001.png")
    assert [a["filename"] for a in found] == ["image001.png"]


def test_only_signature_images_gives_hint():
    raw = make_message("Подпись", [], SIGNATURE_IMAGES)
    with pytest.raises(AttachmentError, match="кроме картинок подписи"):
        mail_attachments.extract_attachments(parse(raw))


def test_inline_pdf_with_content_id_is_still_a_document():
    raw = make_message("PDF inline", [], [dict(
        data=b"%PDF-inline", maintype="application", subtype="pdf",
        filename="Акт.pdf", disposition="inline", cid="<act>")])
    found = mail_attachments.extract_attachments(parse(raw))
    assert [a["filename"] for a in found] == ["Акт.pdf"]


# --------------------------------------------------------- 2. RFC 2231 / 2047

def test_rfc2231_cyrillic_name_found_by_plain_name():
    raw = make_message("Счёт", [(INVOICE, b"%PDF-1.4", "application/pdf")])
    assert b"filename*" in raw  # имя действительно закодировано по RFC 2231
    msg = parse(raw)
    for wanted in (INVOICE, INVOICE.upper(), f"  {INVOICE} ",
                   unicodedata.normalize("NFD", INVOICE)):
        found = mail_attachments.extract_attachments(msg, wanted)
        assert [a["filename"] for a in found] == [INVOICE]


def test_rfc2047_encoded_name_found_by_plain_name():
    encoded = "=?UTF-8?B?" + base64.b64encode(INVOICE.encode()).decode() + "?="
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nтекст\r\n"
        f'--B\r\nContent-Type: application/pdf; name="{encoded}"\r\n'
        f'Content-Disposition: attachment; filename="{encoded}"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{base64.b64encode(b'%PDF-1.4').decode()}\r\n--B--\r\n"
    ).encode()
    found = mail_attachments.extract_attachments(parse(raw), INVOICE)
    assert found[0]["filename"] == INVOICE
    assert found[0]["content"] == b"%PDF-1.4"


def test_octet_stream_mime_guessed_by_extension():
    raw = make_message("", [("Отчёт.pdf", b"%PDF", "application/octet-stream"),
                            ("Схема.qqz", b"QQZ", "application/octet-stream")])
    found = mail_attachments.extract_attachments(parse(raw))
    assert found[0]["mime"] == "application/pdf"
    # неизвестное расширение — остаётся octet-stream
    assert found[1]["mime"] == "application/octet-stream"


# -------------------------------------------------------- 3. имя не найдено

def test_unknown_filename_lists_available_names():
    raw = make_message("Счёт", [(INVOICE, b"%PDF", "application/pdf"),
                                ("Акт.pdf", b"%PDF", "application/pdf")])
    with pytest.raises(AttachmentError) as exc:
        mail_attachments.extract_attachments(parse(raw), "Счет № 999.pdf")
    text = str(exc.value)
    assert "Счет № 999.pdf" in text
    assert INVOICE in text and "Акт.pdf" in text


def test_message_without_attachments():
    with pytest.raises(AttachmentError, match="нет вложений"):
        mail_attachments.extract_attachments(parse(make_message("Пусто")))


# ------------------------------------------------ 4. нет UID — нет отправки

def test_unknown_uid_is_error_and_smtp_not_called(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("999999", BUH, INVOICE))))
    assert result["sent"] is False
    assert "999999" in result["error"] and "не найдено" in result["error"]
    assert FakeSMTP.sent == []


def test_partial_failure_sends_nothing(mailbox):
    """Первый файл нашёлся, второй нет — письмо не уходит вовсе."""
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", BUH, INVOICE),
                               ("178071", "INBOX", "Нет такого.pdf"))))
    assert result["sent"] is False
    assert "uid 178071 / INBOX" in result["error"]
    assert FakeSMTP.sent == []


@pytest.mark.parametrize("bad", [
    "не json", '{"uid": "abc"}', '[{"uid": ""}]', '["179012"]', '"строка"'])
def test_bad_spec_is_rejected_before_imap(mailbox, bad):
    handlers, imap = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER, email_attachments=bad))
    assert result["sent"] is False
    assert FakeSMTP.sent == []
    assert imap.commands == []


def test_unknown_folder_is_error(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", "Нет такой папки", ""))))
    assert result["sent"] is False
    assert "Нет такой папки" in result["error"]
    assert FakeSMTP.sent == []


# ------------------------------------------------ 5. офисные форматы наружу

def test_docx_to_external_address_is_blocked(mailbox):
    handlers, _ = mailbox
    # Контрагент на mail.ru — всё равно внешний: сверка по полному адресу
    result = json.loads(handlers["send_letter"](
        to="client@mail.ru", **LETTER,
        email_attachments=spec(("178070", "INBOX", ""))))
    assert result["sent"] is False
    assert "Договор №57.docx" in result["error"]
    assert "client@mail.ru" in result["error"]
    assert FakeSMTP.sent == []


def test_docx_to_internal_address_passes(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="stavgeo26@mail.ru", **LETTER,
        email_attachments=spec(("178070", "INBOX", ""))))
    assert result["status"] == "sent"
    assert sent_attachments() == ["Письмо.pdf", "Договор №57.docx"]


def test_docx_blocked_when_external_address_only_in_cc(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="stavgeo26@mail.ru", cc="client@example.ru", **LETTER,
        email_attachments=spec(("178070", "INBOX", ""))))
    assert result["sent"] is False
    assert FakeSMTP.sent == []


def test_internal_list_by_full_address(monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAILS", "Buh@Example.ru, ")
    monkeypatch.setenv("MAIL_USERNAME", "stavgeo26@mail.ru")
    docx = [{"filename": "a.docx", "mime": "", "size_bytes": 1}]
    for ok in ("stavgeo26@mail.ru", "business_mail.24@mail.ru",
               "buh@example.ru", "STAVGEO26@MAIL.RU"):
        mail_attachments.validate_outgoing(docx, [ok], 1)
    with pytest.raises(AttachmentError):
        mail_attachments.validate_outgoing(docx, ["other@mail.ru"], 1)


def test_office_detected_by_mime_without_extension():
    item = {"filename": "Договор", "size_bytes": 1,
            "mime": "application/msword"}
    with pytest.raises(AttachmentError, match="офисные"):
        mail_attachments.validate_outgoing([item], ["a@example.ru"], 1)
    pdf = {"filename": "Договор.pdf", "size_bytes": 1, "mime": "application/pdf"}
    mail_attachments.validate_outgoing([pdf], ["a@example.ru"], 1)


# --------------------------------------------------------- 6. дубли имён

def test_same_name_from_two_letters_gets_suffix(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", BUH, INVOICE),
                               ("178071", "INBOX", INVOICE))))
    assert result["status"] == "sent"
    assert sent_attachments() == [
        "Письмо.pdf", INVOICE, "Счет № 245 от 22.09.2026 (2).pdf"]
    # файлы не перепутаны: под (2) лежит счёт из второго письма
    assert sent_payload("Счет № 245 от 22.09.2026 (2).pdf") == \
        b"%PDF-1.4 other invoice"


def test_name_equal_to_letter_pdf_gets_suffix(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER, pdf_filename=INVOICE,
        email_attachments=spec(("179012", BUH, INVOICE))))
    assert result["email_attachments"][0]["filename"] == \
        "Счет № 245 от 22.09.2026 (2).pdf"
    assert sent_attachments() == [INVOICE, "Счет № 245 от 22.09.2026 (2).pdf"]


def test_dedupe_skips_taken_suffixes():
    items = [{"filename": n} for n in ("a.pdf", "A.PDF", "a (2).pdf", "README")]
    names = [i["filename"] for i in
             mail_attachments.dedupe_names(items, reserved=["README"])]
    assert names == ["a.pdf", "A (2).PDF", "a (2) (2).pdf", "README (2)"]


# ------------------------------------------------------------ 7. лимит 24 МБ

def test_real_limit_is_24_mb():
    mail_attachments.check_total_size(24 * 1024 * 1024)
    with pytest.raises(AttachmentError, match="24 МБ"):
        mail_attachments.check_total_size(24 * 1024 * 1024 + 1)


def test_oversized_attachments_are_refused(mailbox, monkeypatch):
    handlers, _ = mailbox
    monkeypatch.setattr(mail_attachments, "MAX_TOTAL_BYTES", 10)
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", BUH, INVOICE))))
    assert result["sent"] is False
    assert "лимит" in result["error"]
    assert FakeSMTP.sent == []


def test_limit_counts_letter_pdf_too(mailbox, monkeypatch):
    """Сам счёт в лимит влезает, но вместе с PDF бланка — уже нет."""
    handlers, _ = mailbox
    invoice_size = len(b"%PDF-1.4 invoice")
    monkeypatch.setattr(mail_attachments, "MAX_TOTAL_BYTES", invoice_size + 5)
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", BUH, INVOICE))))
    assert result["sent"] is False
    assert FakeSMTP.sent == []


# ---------------------------------------------------- 8. превью не отправляет

def test_preview_returns_attachments_plan_and_sends_nothing(mailbox):
    handlers, imap = mailbox
    result = json.loads(handlers["preview_letter"](
        **LETTER, email_attachments=spec(("179012", BUH, ""),
                                         ("178071", "INBOX", "Отчёт.pdf"))))
    assert result["ok"] is True
    assert result["preview_url"].endswith(".pdf")
    assert result["attachments_plan"] == [
        {"filename": INVOICE, "size_bytes": len(b"%PDF-1.4 invoice"),
         "mime": "application/pdf", "source": f"uid 179012 / {BUH}"},
        {"filename": "Отчёт.pdf", "size_bytes": len(b"%PDF-1.4 report"),
         "mime": "application/pdf", "source": "uid 178071 / INBOX"},
    ]
    assert "warnings" not in result
    assert FakeSMTP.sent == []
    assert imap.appended == []  # и в «Отправленные» ничего не легло


def test_preview_validation_error_returns_ok_false(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["preview_letter"](
        **LETTER, email_attachments=spec(("179012", BUH, "Нет.pdf"))))
    assert result["ok"] is False
    assert INVOICE in result["reason"]  # список доступных имён
    assert "preview_url" not in result


def test_preview_warns_about_office_files(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["preview_letter"](
        **LETTER, email_attachments=spec(("178070", "INBOX", ""))))
    assert result["ok"] is True
    assert "Договор №57.docx" in result["warnings"][0]


def test_preview_without_email_attachments_does_not_touch_imap(mailbox):
    handlers, imap = mailbox
    result = json.loads(handlers["preview_letter"](**LETTER))
    assert result["ok"] is True
    assert "attachments_plan" not in result
    assert imap.commands == []


# ------------------------------------------ 9. письмо-источник непрочитанное

def test_source_message_stays_unseen(mailbox):
    handlers, imap = mailbox
    handlers["preview_letter"](**LETTER,
                               email_attachments=spec(("179012", BUH, "")))
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("179012", BUH, ""))))
    assert result["status"] == "sent"

    fetches = [c for c in imap.commands if c[0] == "FETCH"]
    assert fetches and all("BODY.PEEK[]" in c[2] for c in fetches)
    assert not [c for c in imap.commands if c[0] == "STORE"]
    assert imap.seen == set()


def test_fake_server_would_notice_seen():
    """Проверка самого теста: без PEEK заглушка ставит \\Seen."""
    imap = FakeIMAP({"INBOX": {"1": make_message("x")}})
    imap.select('"INBOX"')
    imap.uid("FETCH", "1", "(BODY[])")
    assert imap.seen == {("INBOX", "1")}


# ------------------------------------------------ порядок и другие инструменты

def test_send_letter_order_and_result(mailbox):
    handlers, imap = mailbox
    result = json.loads(handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("178071", "INBOX", "Отчёт.pdf"),
                               ("179012", "бухгалтерия наталья", INVOICE))))
    assert result["status"] == "sent"
    # PDF бланка первым, дальше — в порядке списка
    assert sent_attachments() == ["Письмо.pdf", "Отчёт.pdf", INVOICE]
    assert sent_payload(INVOICE) == b"%PDF-1.4 invoice"
    assert [a["filename"] for a in result["email_attachments"]] == \
        ["Отчёт.pdf", INVOICE]
    assert "content" not in result["email_attachments"][0]
    assert len(imap.appended) == 1  # копия в «Отправленные»


def test_one_letter_fetched_once_for_several_files(mailbox):
    handlers, imap = mailbox
    handlers["send_letter"](
        to="client@example.ru", **LETTER,
        email_attachments=spec(("178071", "INBOX", INVOICE),
                               ("178071", "INBOX", "Отчёт.pdf")))
    assert len([c for c in imap.commands if c[0] == "FETCH"]) == 1


def test_attach_pdf_false_sends_only_email_files(mailbox):
    handlers, _ = mailbox
    handlers["send_letter"](
        to="client@example.ru", **LETTER, attach_pdf=False,
        pdf_filename=INVOICE,
        email_attachments=spec(("179012", BUH, INVOICE)))
    assert sent_attachments() == [INVOICE]


def test_several_to_addresses_go_as_separate_recipients(mailbox):
    handlers, _ = mailbox
    handlers["send_letter"](to="a@example.ru, Иван <b@example.ru>",
                            cc="c@example.ru", **LETTER)
    assert FakeSMTP.sent[-1][1] == ["a@example.ru", "b@example.ru",
                                    "c@example.ru"]


def test_send_new_email_appends_email_files_after_others(mailbox):
    handlers, _ = mailbox
    b64 = [{"filename": "Смета.pdf", "mime_type": "application/pdf",
            "content_base64": base64.b64encode(b"%PDF-smeta").decode()}]
    result = json.loads(handlers["send_new_email"](
        to="client@example.ru", subject="Документы", body="Добрый день",
        attachments=json.dumps(b64, ensure_ascii=False),
        email_attachments=spec(("179012", BUH, INVOICE))))
    assert result["status"] == "sent"
    assert sent_attachments() == ["Смета.pdf", INVOICE]


def test_send_new_email_office_block(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_new_email"](
        to="client@example.ru", subject="Договор", body="Добрый день",
        email_attachments=spec(("178070", "INBOX", ""))))
    assert result["sent"] is False
    assert FakeSMTP.sent == []


def test_send_reply_with_email_attachments(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_reply"](
        email_uid="500", body="Счёт во вложении",
        email_attachments=spec(("179012", BUH, ""))))
    assert result["status"] == "sent"
    assert result["to"] == "buh@example.ru"
    assert sent_attachments() == [INVOICE]


def test_send_reply_office_to_external_blocked(mailbox):
    handlers, _ = mailbox
    result = json.loads(handlers["send_reply"](
        email_uid="500", body="Договор",
        email_attachments=spec(("178070", "INBOX", ""))))
    assert result["sent"] is False
    assert "buh@example.ru" in result["error"]
    assert FakeSMTP.sent == []


def test_audit_log_has_source_and_recipient_but_no_content(mailbox, caplog):
    handlers, _ = mailbox
    with caplog.at_level("INFO", logger="imap_client"):
        handlers["send_letter"](to="client@example.ru", **LETTER,
                                email_attachments=spec(("179012", BUH, "")))
    audit = [r.getMessage() for r in caplog.records
             if "Вложение из письма" in r.getMessage()]
    assert len(audit) == 1
    line = audit[0]
    for part in ("uid=179012", f"folder={BUH}", f"filename={INVOICE}",
                 f"size={len(b'%PDF-1.4 invoice')}", "to=client@example.ru"):
        assert part in line
    assert "%PDF" not in line
