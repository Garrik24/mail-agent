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
        with_size, _, _ = mail_triage.score_features(
            "Информация", "x@kochubgaz.ru", False, ["a.pdf"], True)
        unknown, reasons, _ = mail_triage.score_features(
            "Информация", "x@kochubgaz.ru", False, ["a.pdf"], None)
        self.assertEqual(with_size - unknown, 1)
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
        self.assertIn("в имени файла согласование, топосъёмка", joined)
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


class TestTopicGate(unittest.TestCase):
    """Тематический признак отделяет согласования от прочей переписки."""

    def test_business_reply_without_topic_is_not_a_candidate(self):
        """Договор с корпоративного домена набирает баллы, но темы нет."""
        msg = make(subject="RE: Ставропольгеодезия_Вольфрам",
                   sender="Астафьева <aal@omsk.tavrida.ru>",
                   body="во вложении договор, посмотрите " * 10,
                   attachments=["Договор №57.docx"], reply=True)
        result = mail_triage.score_message(msg, "1")
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)
        self.assertFalse(result["has_topic_signal"])

    def test_approval_reply_has_topic_signal(self):
        msg = make(subject="RE: О согласовании топосъёмки",
                   sender="ПТО <pto@kochubgaz.ru>", body="", reply=True)
        self.assertTrue(mail_triage.score_message(msg, "1")["has_topic_signal"])

    def test_outgoing_number_in_filename_counts(self):
        """Единственное письмо эталона без темы: исх. номер в имени файла."""
        msg = make(subject="RE: Письмо_Теплоэнерго_Кисловодск.pdf",
                   sender="<gpte@gpte26.ru>", body="текст " * 80,
                   attachments=["Исх_2026_668-08.pdf"], reply=True)
        result = mail_triage.score_message(msg, "1")
        self.assertTrue(result["has_topic_signal"])
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_empty_body_with_document_is_not_a_topic_signal(self):
        """Пустое тело плюс документ — это ещё и УПД, и акт сверки."""
        msg = make(subject="Информация", sender="x@gupsktek.ru", body="",
                   attachments=["1108.pdf"])
        self.assertFalse(mail_triage.score_message(msg, "1")["has_topic_signal"])

    def test_score_meta_reports_topic_signal(self):
        meta = mail_triage.parse_meta_item(TestMetadataScoring.PREFIX,
                                           TestMetadataScoring.HEADERS)
        self.assertTrue(mail_triage.score_meta(meta)["has_topic_signal"])


class TestRulesFromEtalon(unittest.TestCase):
    """Правила, выведенные из 29 писем папки СОГЛАСОВАНИЯ."""

    def test_own_outgoing_address_detected(self):
        import os
        os.environ["MAIL_USERNAME"] = "stavgeo26@mail.ru"
        try:
            self.assertIn("stavgeo26@mail.ru", mail_triage.self_addresses())
        finally:
            os.environ.pop("MAIL_USERNAME", None)

    def test_accounting_documents_rejected(self):
        """УПД и акты сверки приходят с пустым телом и PDF — но это не то."""
        for subject, filename in [
            ("Документы подтверждающие выход рекламы", "УПД (статус 1).pdf"),
            ("Документы за январь", "Акт сверки взаиморасчетов.pdf"),
            ("Закрывающие документы", "Договор Акты обследования.pdf"),
        ]:
            with self.subTest(subject=subject):
                msg = make(subject=subject, sender="s@stavropol.2gis.ru",
                           body="", attachments=[filename])
                result = mail_triage.score_message(msg, "1")
                self.assertFalse(result["has_topic_signal"])

    def test_hr_and_ads_rejected(self):
        for subject, filename in [("Трудоустройство", "резюме1.docx"),
                                  ("Платформа для бизнеса", "Презентация.pdf")]:
            with self.subTest(subject=subject):
                result = mail_triage.score_message(
                    make(subject=subject, body="", attachments=[filename]), "1")
                self.assertFalse(result["has_topic_signal"])

    def test_project_documentation_rejected(self):
        """Школа №54 шлёт разделы ПД с того же домена, что и согласования."""
        result = mail_triage.score_message(
            make(subject="школа 54 г. Ставрополь", sender="sch_54@stavadm.ru",
                 body="", attachments=["Раздел ПД №3 (127.22-АР).pdf"]), "1")
        self.assertFalse(result["has_topic_signal"])

    def test_empty_body_alone_is_not_enough(self):
        """Пустое тело с документом больше не открывает гейт само по себе."""
        result = mail_triage.score_message(
            make(subject="Документы", sender="x@some-company.ru", body="",
                 attachments=["file.pdf"]), "1")
        self.assertFalse(result["has_topic_signal"])

    def test_no_subject_from_profile_org_passes(self):
        """А вот письмо без темы от профильной организации — проходит."""
        result = mail_triage.score_message(
            make(subject="", sender="Петрова <petrovaap@stavkraygaz.ru>",
                 body="", attachments=["39-06-07-768.pdf"]), "1")
        self.assertTrue(result["has_topic_signal"])
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)

    def test_normal_body_size_does_not_block_approval(self):
        """У согласований тела обычные — медиана эталона около 2700 байт."""
        result = mail_triage.score_message(
            make(subject="О согласовании топографической съёмки",
                 sender="o.cymbalova@skvk.ru", body="текст письма " * 200,
                 attachments=["Ответ.pdf"], reply=True), "1")
        self.assertTrue(result["has_topic_signal"])
        self.assertGreaterEqual(result["score"], mail_triage.DEFAULT_MIN_SCORE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
