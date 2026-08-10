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
            ("О согласовании топографической съёмки", "в теме согласование"),
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
        self.assertIn("в теме согласование", result["reasons"])

    def test_min_score_threshold_is_respected(self):
        """Порог решает, что считать кандидатом."""
        msg = make(subject="Информация", sender="x@kochubgaz.ru",
                   body="короткий текст")
        low = mail_triage.score_message(msg, "1")
        self.assertLess(low["score"], 3)
        self.assertGreaterEqual(low["score"], 1)


class TestMetadataScoring(unittest.TestCase):
    """Быстрый путь: скоринг по метаданным, без загрузки писем."""

    HEADERS = (
        b"Date: Mon, 08 Jun 2026 09:45:00 +0300\r\n"
        b"From: =?utf-8?B?0J/QotCe?= <pto@kochubgaz.ru>\r\n"
        b"Subject: =?utf-8?B?0J4g0YHQvtCz0LvQsNGB0L7QstCw0L3QuNC4?=\r\n"
        b"In-Reply-To: <our-outgoing@mail.ru>\r\n"
        b"Message-ID: <abc@kochubgaz.ru>\r\n\r\n"
    )

    PREFIX = (
        b'12 (UID 177024 FLAGS (\\Seen) BODYSTRUCTURE '
        b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 12 1)'
        b'("APPLICATION" "PDF" ("NAME" "Otvet.pdf") NIL NIL "BASE64" 90000 '
        b'NIL ("ATTACHMENT" ("FILENAME" "Otvet.pdf")) NIL) "MIXED") '
        b'BODY[HEADER.FIELDS (DATE FROM SUBJECT)] {200}'
    )

    def test_text_part_size_from_bodystructure(self):
        bs = (b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 12 1)'
              b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 90000 NIL NIL NIL))')
        self.assertEqual(mail_triage.text_part_size(bs), 12)

    def test_text_part_size_large_body(self):
        bs = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 4096 60)'
        self.assertEqual(mail_triage.text_part_size(bs), 4096)

    def test_text_part_size_absent(self):
        self.assertIsNone(mail_triage.text_part_size(b'("IMAGE" "PNG" NIL)'))
        self.assertIsNone(mail_triage.text_part_size(b""))

    def test_parse_meta_item(self):
        meta = mail_triage.parse_meta_item(self.PREFIX, self.HEADERS)
        self.assertEqual(meta["uid"], "177024")
        self.assertEqual(meta["subject"], "О согласовании")
        self.assertIn("pto@kochubgaz.ru", meta["from"])
        self.assertTrue(meta["has_reply_header"])
        self.assertEqual(meta["attachments"], ["Otvet.pdf"])
        self.assertEqual(meta["text_size"], 12)
        self.assertEqual(meta["flags"], ["\\Seen"])

    def test_score_meta_matches_full_scoring(self):
        """Быстрый путь должен давать тот же балл, что и разбор письма."""
        meta = mail_triage.parse_meta_item(self.PREFIX, self.HEADERS)
        fast = mail_triage.score_meta(meta)

        full_msg = make(subject="О согласовании",
                        sender="ПТО <pto@kochubgaz.ru>", body="",
                        attachments=["Otvet.pdf"], reply=True)
        full = mail_triage.score_message(full_msg, "177024")
        self.assertEqual(fast["score"], full["score"])
        self.assertGreaterEqual(fast["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_unknown_body_size_does_not_add_points(self):
        """Размер тела неизвестен — балл за «пустое тело» не выдаётся."""
        with_size, _ = mail_triage.score_features(
            "Информация", "x@kochubgaz.ru", False, ["a.pdf"], True)
        unknown, reasons = mail_triage.score_features(
            "Информация", "x@kochubgaz.ru", False, ["a.pdf"], None)
        self.assertEqual(with_size - unknown, 2)
        self.assertNotIn("тело пустое или в одну строку — суть во вложении",
                         reasons)

    def test_long_text_part_is_not_short_body(self):
        prefix = self.PREFIX.replace(b'"8BIT" 12 1', b'"8BIT" 4000 80')
        meta = mail_triage.parse_meta_item(prefix, self.HEADERS)
        self.assertEqual(meta["text_size"], 4000)
        item = mail_triage.score_meta(meta)
        self.assertNotIn("тело пустое или в одну строку — суть во вложении",
                         item["reasons"])


class TestRecallFeatures(unittest.TestCase):
    """Признаки, добавленные после проверки на папке СОГЛАСОВАНИЯ."""

    def test_subject_without_preposition(self):
        """«Согласование топосъемки» без предлога «о» — тоже согласование."""
        for subject in ("Согласование топосъемки", "согласования по объекту",
                        "Re: Согласование топосъемки по санаторию"):
            with self.subTest(subject=subject):
                result = mail_triage.score_message(make(subject=subject), "1")
                self.assertIn("в теме согласование", result["reasons"])

    def test_subject_topo_words(self):
        result = mail_triage.score_message(
            make(subject="Топографическая съёмка участка"), "1")
        self.assertIn("в теме топосъёмка или топоплан", result["reasons"])

    def test_document_name_counts_as_signal(self):
        """У писем без темы вся подсказка — в имени файла."""
        result = mail_triage.score_message(
            make(subject="", body="",
                 attachments=["Согласовано_топосъемка_Факел.pdf"]), "1")
        joined = " ".join(result["reasons"])
        self.assertIn("в имени файла согласование или топосъёмка", joined)
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_organization_on_free_mail_is_not_a_robot(self):
        """Организация с Яндекс-почты не должна получать штраф как робот."""
        result = mail_triage.score_message(
            make(subject="Согласование топосъемки",
                 sender="КСК АО <kskkmv.pto@yandex.ru>", body="",
                 attachments=["Согласование топосъемки.pdf"]), "1")
        joined = " ".join(result["reasons"])
        self.assertNotIn("рассылку или робота", joined)
        self.assertIn("бесплатная почта", joined)
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_robot_still_penalised_hard(self):
        result = mail_triage.score_message(
            make(subject="Согласование заказа",
                 sender="Робот <no-reply@telko.ru>",
                 body="текст " * 100, attachments=["catalog.pdf"]), "1")
        self.assertIn("похоже на рассылку или робота", result["reasons"])
        self.assertLess(result["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_free_mail_personal_chat_still_low(self):
        """Смягчение штрафа не должно тащить наверх личную переписку."""
        result = mail_triage.score_message(
            make(subject="привет, как дела", sender="Друг <friend@gmail.com>",
                 body="созвонимся завтра"), "1")
        self.assertLess(result["score"], mail_triage.DEFAULT_MIN_SCORE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
