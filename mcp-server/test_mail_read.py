"""Тесты чтения писем — без сети и без кредов, на синтетических письмах.

Запуск:  python test_mail_read.py
         python -m unittest test_mail_read -v
"""

import base64
import email
import io
import unittest
import zipfile
from email.header import Header
from quopri import encodestring

import mail_read
import mail_read_tools


def build_raw(headers: dict, parts: list[tuple], boundary="BOUND1") -> bytes:
    """Собирает сырое MIME-письмо из (content_type, extra_headers, тело-bytes)."""
    lines = [f"{k}: {v}" for k, v in headers.items()]
    if len(parts) == 1 and not parts[0][1].get("_multipart", True):
        ctype, extra, body = parts[0]
        lines.append(f"Content-Type: {ctype}")
        for k, v in extra.items():
            if not k.startswith("_"):
                lines.append(f"{k}: {v}")
        head = "\r\n".join(lines).encode("utf-8")
        return head + b"\r\n\r\n" + body

    lines.append("MIME-Version: 1.0")
    lines.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')
    out = ["\r\n".join(lines).encode("utf-8"), b"\r\n\r\n"]
    for ctype, extra, body in parts:
        section = [f"--{boundary}", f"Content-Type: {ctype}"]
        for k, v in extra.items():
            if not k.startswith("_"):
                section.append(f"{k}: {v}")
        out.append("\r\n".join(section).encode("utf-8"))
        out.append(b"\r\n\r\n")
        out.append(body)
        out.append(b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out)


def parse(raw: bytes):
    return email.message_from_bytes(raw)


class TestBodyExtraction(unittest.TestCase):
    def test_multipart_alternative_prefers_plain(self):
        raw = (b"Subject: test\r\nMIME-Version: 1.0\r\n"
               b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
               b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               b"\xd0\xa2\xd0\xb5\xd0\xba\xd1\x81\xd1\x82 plain\r\n"
               b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<p>HTML \xd0\xb2\xd0\xb0\xd1\x80\xd0\xb8\xd0\xb0\xd0\xbd\xd1\x82</p>\r\n"
               b"--B--\r\n")
        body = mail_read.extract_body(parse(raw))
        self.assertEqual(body["format"], "plain")
        self.assertIn("Текст plain", body["text"])
        self.assertNotIn("HTML вариант", body["text"])

    def test_html_only_becomes_readable_text(self):
        html = ("<html><head><style>p{color:red}</style></head><body>"
                "<p>Первый абзац</p><p>Второй&nbsp;абзац</p>"
                "<br><div>Третий</div><script>alert(1)</script>"
                "</body></html>")
        raw = build_raw({"Subject": "html"},
                        [("text/html; charset=utf-8", {"_multipart": False},
                          html.encode("utf-8"))])
        body = mail_read.extract_body(parse(raw))
        self.assertEqual(body["format"], "html")
        self.assertNotIn("<", body["text"])
        self.assertNotIn("alert", body["text"])
        self.assertNotIn("color:red", body["text"])
        self.assertIn("Первый абзац", body["text"])
        self.assertIn("Второй абзац", body["text"])
        self.assertIn("Третий", body["text"])

    def test_windows1251_base64_body(self):
        text = "Уважаемый Игорь Владимирович, направляем ответ на запрос."
        payload = base64.b64encode(text.encode("cp1251"))
        raw = build_raw({"Subject": "cp1251"},
                        [("text/plain; charset=windows-1251",
                          {"Content-Transfer-Encoding": "base64",
                           "_multipart": False}, payload)])
        body = mail_read.extract_body(parse(raw))
        self.assertEqual(body["text"], text)

    def test_windows1251_quoted_printable(self):
        text = "Согласование топосъёмки"
        payload = encodestring(text.encode("cp1251"))
        raw = build_raw({"Subject": "qp"},
                        [("text/plain; charset=windows-1251",
                          {"Content-Transfer-Encoding": "quoted-printable",
                           "_multipart": False}, payload)])
        self.assertEqual(mail_read.extract_body(parse(raw))["text"], text)

    def test_undeclared_cp1251_does_not_crash(self):
        raw = build_raw({"Subject": "no charset"},
                        [("text/plain", {"_multipart": False},
                          "Ответ на запрос".encode("cp1251"))])
        text = mail_read.extract_body(parse(raw))["text"]
        self.assertIn("Ответ", text)

    def test_empty_body(self):
        raw = build_raw({"Subject": "empty"},
                        [("text/plain; charset=utf-8", {"_multipart": False}, b"")])
        self.assertEqual(mail_read.extract_body(parse(raw))["text"], "")


class TestHeadersAndAttachments(unittest.TestCase):
    def test_encoded_words_in_subject_and_filename(self):
        subject = Header("О согласовании топосъёмки", "utf-8").encode()
        filename = Header("Письмо №123.pdf", "utf-8").encode()
        raw = build_raw(
            {"Subject": subject,
             "From": Header("Невинномысский филиал", "utf-8").encode()
                     + " <nevinnomyssk@gupsktek.ru>"},
            [("text/plain; charset=utf-8", {}, "тело".encode("utf-8")),
             ("application/pdf",
              {"Content-Disposition": f'attachment; filename="{filename}"'},
              b"%PDF-1.4 fake")],
        )
        msg = parse(raw)
        self.assertEqual(mail_read.decode_mime_header(msg.get("Subject")),
                         "О согласовании топосъёмки")
        attachments = mail_read.list_attachments(msg)
        self.assertEqual(attachments[0]["filename"], "Письмо №123.pdf")

    def test_rfc2231_filename(self):
        raw = build_raw(
            {"Subject": "rfc2231"},
            [("text/plain; charset=utf-8", {}, b"body"),
             ("application/pdf",
              {"Content-Disposition":
               "attachment; filename*=UTF-8''%D0%9E%D1%82%D0%B2%D0%B5%D1%82.pdf"},
              b"%PDF-1.4 fake")],
        )
        attachments = mail_read.list_attachments(parse(raw))
        self.assertEqual(attachments[0]["filename"], "Ответ.pdf")

    def test_two_attachments_names_types_sizes(self):
        pdf_data = b"%PDF-1.4" + b"x" * 100
        docx_data = b"PK\x03\x04" + b"y" * 50
        raw = build_raw(
            {"Subject": "two"},
            [("text/plain; charset=utf-8", {}, b"body"),
             ("application/pdf",
              {"Content-Disposition": 'attachment; filename="a.pdf"'}, pdf_data),
             ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              {"Content-Disposition": 'attachment; filename="b.docx"'}, docx_data)],
        )
        attachments = mail_read.list_attachments(parse(raw))
        self.assertEqual(len(attachments), 2)
        self.assertEqual(attachments[0]["filename"], "a.pdf")
        self.assertEqual(attachments[0]["content_type"], "application/pdf")
        self.assertEqual(attachments[0]["size_bytes"], len(pdf_data))
        self.assertEqual(attachments[1]["filename"], "b.docx")
        self.assertEqual(attachments[1]["size_bytes"], len(docx_data))

    def test_attachment_body_not_mixed_into_text(self):
        raw = build_raw(
            {"Subject": "mix"},
            [("text/plain; charset=utf-8", {}, "настоящее тело".encode()),
             ("text/plain; charset=utf-8",
              {"Content-Disposition": 'attachment; filename="note.txt"'},
              "текст вложения".encode())],
        )
        body = mail_read.extract_body(parse(raw))
        self.assertIn("настоящее тело", body["text"])
        self.assertNotIn("текст вложения", body["text"])


class TestQuotesAndTruncation(unittest.TestCase):
    def test_strip_quotes_removes_original_message_block(self):
        text = ("Добрый день!\nНаправляем ответ на ваш запрос.\n\n"
                "-----Original Message-----\n"
                "From: someone@example.com\nОчень длинная старая переписка")
        own, quoted = mail_read.split_quotes(text)
        self.assertIn("Направляем ответ", own)
        self.assertNotIn("Очень длинная", own)
        self.assertIn("Очень длинная", quoted)

    def test_strip_quotes_removes_angle_lines(self):
        own, quoted = mail_read.split_quotes(
            "Мой ответ\n> старая строка\n> ещё старая")
        self.assertEqual(own, "Мой ответ")
        self.assertIn("старая строка", quoted)

    def test_strip_quotes_russian_header_block(self):
        text = "Ответ по существу\n\nОт кого: Иванов <i@x.ru>\nстарое тело"
        own, quoted = mail_read.split_quotes(text)
        self.assertEqual(own.strip(), "Ответ по существу")
        self.assertIn("старое тело", quoted)

    def test_wrote_marker(self):
        own, _ = mail_read.split_quotes(
            "Согласны\n12.05.2026 10:00, Иванов написал:\nстарый текст")
        self.assertEqual(own.strip(), "Согласны")

    def test_no_quotes_keeps_everything(self):
        text = "Просто письмо\nбез цитат"
        own, quoted = mail_read.split_quotes(text)
        self.assertEqual(own, text)
        self.assertEqual(quoted, "")

    def test_truncate_sets_flag(self):
        text, truncated = mail_read.truncate("a" * 100, 10)
        self.assertEqual(len(text), 10)
        self.assertTrue(truncated)
        text, truncated = mail_read.truncate("short", 100)
        self.assertEqual(text, "short")
        self.assertFalse(truncated)

    def test_build_message_dict_applies_max_chars(self):
        raw = build_raw({"Subject": "long", "From": "a@b.ru", "Date": "x"},
                        [("text/plain; charset=utf-8", {"_multipart": False},
                          ("длинно " * 500).encode("utf-8"))])
        result = mail_read.build_message_dict(parse(raw), uid="1",
                                              flags=["\\Seen"], max_chars=50)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["body"]), 50)
        self.assertTrue(result["seen"])
        self.assertFalse(result["flagged"])


class TestFlags(unittest.TestCase):
    def test_parse_flags_from_fetch_prefix(self):
        prefix = b'1 (UID 174654 FLAGS (\\Seen \\Flagged) BODY[] {100}'
        flags = mail_read.parse_flags_list(prefix)
        self.assertEqual(flags, ["\\Seen", "\\Flagged"])

    def test_empty_flags(self):
        self.assertEqual(mail_read.parse_flags_list(b"1 (FLAGS () BODY[]"), [])
        self.assertEqual(mail_read.parse_flags_list(b""), [])

    def test_answered(self):
        raw = build_raw({"Subject": "s", "From": "a@b.ru"},
                        [("text/plain; charset=utf-8", {"_multipart": False},
                          b"body")])
        result = mail_read.build_message_dict(parse(raw), flags=["\\Answered"])
        self.assertTrue(result["answered"])
        self.assertFalse(result["seen"])


class TestAttachmentText(unittest.TestCase):
    def _docx(self, paragraphs) -> bytes:
        body = "".join(
            f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        xml = ('<?xml version="1.0"?><w:document '
               'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               f"<w:body>{body}</w:body></w:document>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return buf.getvalue()

    def test_docx_text(self):
        data = self._docx(["Уважаемый Игорь Владимирович!",
                           "Направляем согласование."])
        result = mail_read.extract_docx_text(data)
        self.assertTrue(result["ok"])
        self.assertIn("Уважаемый Игорь Владимирович!", result["text"])
        self.assertIn("Направляем согласование.", result["text"])

    def test_docx_truncation(self):
        result = mail_read.extract_docx_text(self._docx(["слово " * 200]),
                                             max_chars=20)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])

    def test_broken_docx(self):
        result = mail_read.extract_docx_text(b"not a zip")
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_pdf_without_text_layer(self):
        """Валидный PDF с пустой страницей = скан без текстового слоя."""
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf не установлен")
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        buf = io.BytesIO()
        writer.write(buf)

        result = mail_read.extract_pdf_text(buf.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("скан", result["reason"])

    def _scan_pdf(self, text: str = "Направляем согласование трассы") -> bytes:
        """PDF-«скан»: страница-картинка с текстом, без текстового слоя."""
        from PIL import Image, ImageDraw
        from pypdf import PdfWriter

        image = Image.new("RGB", (1240, 400), "white")
        draw = ImageDraw.Draw(image)
        # Крупный дефолтный шрифт: мелкий bitmap-шрифт tesseract не берёт
        draw.text((40, 150), text, fill="black", font_size=48)
        buf = io.BytesIO()
        image.save(buf, format="PDF")

        # Сверяем, что текстового слоя в получившемся PDF действительно нет
        writer = PdfWriter(clone_from=io.BytesIO(buf.getvalue()))
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    def test_scan_pdf_has_no_text_layer(self):
        """Без OCR скан честно помечается, а не отдаёт мусор."""
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow не установлен")
        result = mail_read.extract_pdf_text(self._scan_pdf(), ocr="off")
        self.assertFalse(result["ok"])
        self.assertIn("скан", result["reason"])

    def test_ocr_reads_russian_scan(self):
        available, reason = mail_read.ocr_available()
        if not available:
            self.skipTest(f"OCR недоступен локально: {reason}")
        result = mail_read.extract_pdf_text(
            self._scan_pdf("Направляем согласование трассы"), ocr="auto")
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(result["source"], "ocr")
        self.assertIn("огласовани", result["text"])

    def test_ocr_unavailable_gives_reason_not_crash(self):
        """Без tesseract инструмент возвращает причину, а не падает."""
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow не установлен")
        result = mail_read.extract_pdf_text(self._scan_pdf(), ocr="auto")
        self.assertIn("ok", result)
        if not result["ok"]:
            self.assertIn("reason", result)
            self.assertIsInstance(result["reason"], str)

    def test_blank_image_detected(self):
        """Чёрное полотно от pypdf не должно уходить в OCR как страница."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow не установлен")
        self.assertTrue(mail_read.is_blank_image(
            Image.new("L", (100, 100), 0)))
        self.assertTrue(mail_read.is_blank_image(
            Image.new("RGB", (100, 100), "white")))

        image = Image.new("L", (100, 100), 255)
        image.putpixel((10, 10), 0)
        self.assertFalse(mail_read.is_blank_image(image))

    def test_page_images_extracted_from_scan(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow не установлен")
        images = mail_read.pdf_page_images(self._scan_pdf())
        self.assertGreaterEqual(len(images), 1)

    def test_ocr_off_is_respected_for_attachments(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow не установлен")
        result = mail_read.extract_attachment_text(
            "скан.pdf", "application/pdf", self._scan_pdf(), ocr="off")
        self.assertFalse(result["ok"])
        self.assertIn("скан", result["reason"])

    def test_broken_pdf_gives_reason_not_exception(self):
        result = mail_read.extract_pdf_text(b"definitely not a pdf")
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_unsupported_format(self):
        result = mail_read.extract_attachment_text("chart.xlsx", "application/vnd.ms-excel", b"data")
        self.assertFalse(result["ok"])
        self.assertIn("не поддержан", result["reason"])

    def test_txt_attachment_cp1251(self):
        result = mail_read.extract_attachment_text(
            "note.txt", "text/plain", "Ответ на запрос".encode("cp1251"))
        self.assertTrue(result["ok"])
        self.assertIn("Ответ", result["text"])

    def test_find_attachment_by_name_and_default(self):
        raw = build_raw(
            {"Subject": "s"},
            [("text/plain; charset=utf-8", {}, b"body"),
             ("image/png",
              {"Content-Disposition": 'attachment; filename="logo.png"'}, b"PNG"),
             ("application/pdf",
              {"Content-Disposition": 'attachment; filename="Ответ.pdf"'},
              b"%PDF-1.4")],
        )
        msg = parse(raw)
        # без имени берётся первый PDF/DOCX, а не первое вложение вообще
        self.assertEqual(mail_read.find_attachment(msg)["filename"], "Ответ.pdf")
        # поиск по подстроке без учёта регистра
        self.assertEqual(mail_read.find_attachment(msg, "ответ")["filename"],
                         "Ответ.pdf")
        self.assertIsNone(mail_read.find_attachment(msg, "нет-такого"))


class FakeIMAP:
    """Заглушка IMAP: отдаёт заранее заданные письма по UID."""

    def __init__(self, messages: dict, capabilities=("IMAP4REV1", "MOVE")):
        self.messages = messages
        self.capabilities = capabilities
        self.fetch_commands = []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren) "/" "&BCEEHgQTBBsEEAQhBB4EEgQQBB0EGAQv-"']

    def select(self, mailbox):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command.upper() != "FETCH":
            return "NO", [None]
        self.fetch_commands.append(args)
        uid = args[0]
        if uid not in self.messages:
            return "OK", [None]
        raw, flags = self.messages[uid]
        prefix = f"1 (UID {uid} FLAGS ({flags}) BODY[] {{{len(raw)}}}".encode()
        return "OK", [(prefix, raw), b")"]


class TestReadMessages(unittest.TestCase):
    def setUp(self):
        plain = build_raw(
            {"Subject": "Тема письма", "From": "Иванов <i@gupsktek.ru>",
             "Date": "Mon, 08 Jun 2026 09:45:00 +0300"},
            [("text/plain; charset=utf-8", {"_multipart": False},
              ("Добрый день! " * 20).encode("utf-8"))])
        empty_with_docx = build_raw(
            {"Subject": "", "From": "ГУП СК <info@gupsktek.ru>",
             "Date": "Tue, 09 Jun 2026 10:00:00 +0300"},
            [("text/plain; charset=utf-8", {}, b" "),
             ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              {"Content-Disposition": 'attachment; filename="Ответ.docx"'},
              TestAttachmentText()._docx(["Направляем согласование трассы."]))])
        self.imap = FakeIMAP({
            "174654": (plain, "\\Seen"),
            "174655": (empty_with_docx, ""),
        })

    def test_uses_body_when_present(self):
        msg, flags = mail_read_tools._fetch_message(self.imap, "174654", "INBOX")
        summary = mail_read_tools._summarize(msg, "174654", flags, 800, True)
        self.assertEqual(summary["source"], "body")
        self.assertIn("Добрый день!", summary["text"])
        self.assertEqual(summary["subject"], "Тема письма")
        self.assertTrue(summary["seen"])

    def test_falls_back_to_attachment_when_body_empty(self):
        msg, flags = mail_read_tools._fetch_message(self.imap, "174655", "INBOX")
        summary = mail_read_tools._summarize(msg, "174655", flags, 800, True)
        self.assertEqual(summary["source"], "attachment:Ответ.docx")
        self.assertIn("Направляем согласование трассы.", summary["text"])
        self.assertEqual(summary["attachments"], ["Ответ.docx"])
        self.assertFalse(summary["seen"])

    def test_attachment_text_can_be_disabled(self):
        msg, flags = mail_read_tools._fetch_message(self.imap, "174655", "INBOX")
        summary = mail_read_tools._summarize(msg, "174655", flags, 800, False)
        self.assertEqual(summary["source"], "body")

    def test_max_chars_each(self):
        msg, flags = mail_read_tools._fetch_message(self.imap, "174654", "INBOX")
        summary = mail_read_tools._summarize(msg, "174654", flags, 30, True)
        self.assertTrue(summary["truncated"])
        self.assertLessEqual(len(summary["text"]), 30)

    def test_peek_is_used_so_unread_stays_unread(self):
        mail_read_tools._fetch_message(self.imap, "174654", "INBOX")
        items = self.imap.fetch_commands[-1][1]
        self.assertIn("BODY.PEEK[]", items)
        self.assertNotIn("BODY[]", items.replace("BODY.PEEK[]", ""))

    def test_missing_uid_gives_readable_error(self):
        with self.assertRaises(ValueError) as ctx:
            mail_read_tools._fetch_message(self.imap, "999999", "СОГЛАСОВАНИЯ")
        message = str(ctx.exception)
        self.assertIn("999999", message)
        self.assertIn("СОГЛАСОВАНИЯ", message)
        self.assertIn("search_mail", message)

    def test_batch_rejects_non_numeric_uid(self):
        result = mail_read_tools._read_messages_impl(["174654", "abc"])
        self.assertIn("error", result)
        self.assertIn("только из цифр", result["error"])

    def test_batch_rejects_empty_list(self):
        self.assertIn("error", mail_read_tools._read_messages_impl([]))

    def test_folder_resolved_by_human_name(self):
        raw = mail_read_tools._select(self.imap, "СОГЛАСОВАНИЯ")
        self.assertEqual(raw, "&BCEEHgQTBBsEEAQhBB4EEgQQBB0EGAQv-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
