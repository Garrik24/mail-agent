"""Тесты отбора писем-согласований — без сети, на синтетических письмах.

Запуск:  python -m unittest test_mail_triage -v
"""

import unittest

import mail_triage
from test_mail_read import build_raw, parse


def make(subject="", sender="Иванов <ivanov@kochubgaz.ru>", body="тело письма",
         attachments=(), reply=False) -> object:
    headers = {"From": sender, "Date": "Mon, 08 Jun 2026 09:45:00 +0300"}
    if subject:
        headers["Subject"] = subject
    if reply:
        headers["In-Reply-To"] = "<our-outgoing@mail.ru>"
        headers["References"] = "<our-outgoing@mail.ru>"

    parts = [("text/plain; charset=utf-8", {}, body.encode("utf-8"))]
    for name in attachments:
        parts.append(("application/pdf",
                      {"Content-Disposition": f'attachment; filename="{name}"'},
                      b"%PDF-1.4 fake"))
    return parse(build_raw(headers, parts))


class TestScoring(unittest.TestCase):
    def test_classic_approval_reply_scores_high(self):
        """Ответ организации: пустое тело, PDF, ссылка на нашу переписку."""
        msg = make(subject="В ответ на ваше письмо исх. № 160 от 16.06.2026",
                   sender="ПТО <pto@kochubgaz.ru>", body=" ",
                   attachments=["Ответ.pdf"], reply=True)
        result = mail_triage.score_message(msg, "177024")
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)
        self.assertTrue(result["has_document"])
        joined = " ".join(result["reasons"])
        self.assertIn("In-Reply-To", joined)
        self.assertIn("во вложении", joined)

    def test_no_subject_with_document(self):
        """Темы нет, всё содержание в приложенном документе."""
        msg = make(subject="", sender="ourgeo@stavkraygaz.ru", body="",
                   attachments=["скан.pdf"])
        result = mail_triage.score_message(msg, "1")
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)
        self.assertIn("темы нет совсем", result["reasons"])
        self.assertEqual(result["subject"], "(без темы)")

    def test_newsletter_is_rejected(self):
        """Рассылка не должна попадать в кандидаты даже с вложением."""
        msg = make(subject="Новости и скидки июня",
                   sender="Рассылка <no-reply@telko.ru>",
                   body="Много текста " * 40, attachments=["catalog.pdf"])
        result = mail_triage.score_message(msg, "2")
        self.assertLess(result["score"], mail_triage.DEFAULT_MIN_SCORE)
        self.assertIn("похоже на рассылку или робота", result["reasons"])

    def test_personal_chat_without_documents_is_low(self):
        msg = make(subject="привет, как дела",
                   sender="Друг <friend@gmail.com>", body="созвонимся завтра")
        self.assertLess(mail_triage.score_message(msg, "3")["score"],
                        mail_triage.DEFAULT_MIN_SCORE)

    def test_subject_patterns(self):
        cases = [
            ("О согласовании топографической съёмки", "тема о согласовании"),
            ("О рассмотрении обращения", "тема о рассмотрении"),
            ("О предоставлении информации", "тема о предоставлении"),
            ("Ответ на исх. № 1005", "в теме исходящий номер"),
            ("В ответ на ваше обращение", "тема «в ответ на»"),
            ("Технические условия на подключение", "тема о технических условиях"),
        ]
        for subject, label in cases:
            with self.subTest(subject=subject):
                result = mail_triage.score_message(make(subject=subject), "1")
                self.assertIn(label, result["reasons"])

    def test_long_body_with_pdf_scores_lower_than_empty_body(self):
        """Пустое тело — более сильный признак, чем просто наличие PDF."""
        empty = mail_triage.score_message(
            make(subject="Информация", body=" ", attachments=["a.pdf"]), "1")
        chatty = mail_triage.score_message(
            make(subject="Информация", body="подробно " * 60,
                 attachments=["a.pdf"]), "2")
        self.assertGreater(empty["score"], chatty["score"])

    def test_reasons_are_human_readable(self):
        result = mail_triage.score_message(
            make(subject="О согласовании", attachments=["Ответ.pdf"],
                 reply=True), "1")
        self.assertTrue(all(isinstance(r, str) and r for r in result["reasons"]))
        self.assertIn("Ответ.pdf", " ".join(result["reasons"]))

    def test_encoded_subject_and_sender_decoded(self):
        from email.header import Header
        subject = Header("О согласовании топосъёмки", "utf-8").encode()
        msg = make(subject=subject, attachments=["Ответ.pdf"], reply=True)
        result = mail_triage.score_message(msg, "1")
        self.assertEqual(result["subject"], "О согласовании топосъёмки")
        self.assertIn("тема о согласовании", result["reasons"])

    def test_min_score_threshold_is_respected(self):
        """Порог решает, что считать кандидатом."""
        msg = make(subject="Информация", sender="x@kochubgaz.ru",
                   body="короткий текст")
        low = mail_triage.score_message(msg, "1")
        self.assertLess(low["score"], 3)
        self.assertGreaterEqual(low["score"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
