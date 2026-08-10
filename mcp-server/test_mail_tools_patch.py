"""Тесты для mail_tools_patch и imap_utf7 — без сети и без кредов.

Запуск:  python test_mail_tools_patch.py
         python -m unittest test_mail_tools_patch -v
"""

import unittest

from imap_utf7 import (decode_mutf7, encode_mutf7, parse_list_line,
                       resolve_folder)
import mail_tools_patch as mtp


class TestMutf7(unittest.TestCase):
    def test_rfc3501_vector(self):
        self.assertEqual(
            decode_mutf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
            "~peter/mail/台北/日本語",
        )
        self.assertEqual(
            encode_mutf7("~peter/mail/台北/日本語"),
            "~peter/mail/&U,BTFw-/&ZeVnLIqe-",
        )

    def test_round_trip(self):
        for name in ("Согласование", "Отправленные", "R&D", "INBOX",
                     "INBOX/Согласование", "Корзина", "Черновики"):
            with self.subTest(name=name):
                self.assertEqual(decode_mutf7(encode_mutf7(name)), name)

    def test_ampersand_escaping(self):
        self.assertEqual(encode_mutf7("R&D"), "R&-D")
        self.assertEqual(decode_mutf7("R&-D"), "R&D")

    def test_ascii_names_unchanged(self):
        self.assertEqual(encode_mutf7("INBOX"), "INBOX")
        self.assertEqual(encode_mutf7("INBOX/Drafts"), "INBOX/Drafts")

    def test_cyrillic_encoding_is_ascii(self):
        raw = encode_mutf7("Согласование")
        self.assertTrue(raw.isascii(), raw)
        self.assertTrue(raw.startswith("&") and raw.endswith("-"))

    def test_broken_sequence_survives(self):
        # Незакрытая последовательность не должна ронять разбор
        self.assertEqual(decode_mutf7("&BAD"), "&BAD")


class TestParseListLine(unittest.TestCase):
    def test_quoted_cyrillic_folder(self):
        raw = encode_mutf7("Согласование")
        line = f'(\\HasNoChildren) "/" "{raw}"'.encode()
        parsed = parse_list_line(line)
        self.assertEqual(parsed["name"], "Согласование")
        self.assertEqual(parsed["raw"], raw)
        self.assertEqual(parsed["delimiter"], "/")
        self.assertEqual(parsed["flags"], ["\\HasNoChildren"])

    def test_unquoted_name_and_multiple_flags(self):
        parsed = parse_list_line(b'(\\HasChildren \\Noselect) "|" INBOX')
        self.assertEqual(parsed["name"], "INBOX")
        self.assertEqual(parsed["delimiter"], "|")
        self.assertEqual(parsed["flags"], ["\\HasChildren", "\\Noselect"])

    def test_nested_path(self):
        parsed = parse_list_line(b'(\\HasNoChildren) "/" "INBOX/&BCE-"')
        self.assertEqual(parsed["raw"], "INBOX/&BCE-")
        self.assertTrue(parsed["name"].startswith("INBOX/"))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_list_line(b""))
        self.assertIsNone(parse_list_line(b"* OK nonsense"))


class FakeIMAP:
    """Минимальная заглушка IMAP: только list() и capabilities."""

    def __init__(self, folders, capabilities=("IMAP4REV1", "MOVE", "UIDPLUS")):
        self._folders = folders
        self.capabilities = capabilities
        self.calls = []

    def list(self):
        lines = [f'(\\HasNoChildren) "/" "{raw}"'.encode() for raw in self._folders]
        return "OK", lines


class TestResolveFolder(unittest.TestCase):
    def setUp(self):
        self.imap = FakeIMAP([
            "INBOX",
            encode_mutf7("Согласование"),
            "INBOX/" + encode_mutf7("Архив"),
            encode_mutf7("Отправленные"),
        ])

    def test_inbox(self):
        self.assertEqual(resolve_folder(self.imap, "INBOX"), "INBOX")
        self.assertEqual(resolve_folder(self.imap, "inbox"), "INBOX")

    def test_cyrillic_folder_found(self):
        self.assertEqual(resolve_folder(self.imap, "Согласование"),
                         encode_mutf7("Согласование"))

    def test_case_insensitive(self):
        self.assertEqual(resolve_folder(self.imap, "сОГЛАСОВАНИЕ"),
                         encode_mutf7("Согласование"))

    def test_last_segment_match(self):
        self.assertEqual(resolve_folder(self.imap, "Архив"),
                         "INBOX/" + encode_mutf7("Архив"))

    def test_unknown_folder_falls_back_to_encoding(self):
        self.assertEqual(resolve_folder(self.imap, "Новая"),
                         encode_mutf7("Новая"))


class TestCriteria(unittest.TestCase):
    def test_keyword_is_utf8_bytes(self):
        criteria = mtp.build_criteria(["согласовани"], scope="subject")
        self.assertEqual(criteria[0], "SUBJECT")
        self.assertIsInstance(criteria[1], bytes)
        self.assertEqual(criteria[1], '"согласовани"'.encode("utf-8"))
        self.assertTrue(mtp.needs_utf8(criteria))

    def test_scope_both_expands_to_or(self):
        criteria = mtp.build_criteria(["pdf"], scope="both")
        self.assertEqual(criteria, ["OR", "SUBJECT", b'"pdf"', "BODY", b'"pdf"'])

    def test_scope_body(self):
        self.assertEqual(mtp.build_criteria(["pdf"], scope="body"),
                         ["BODY", b'"pdf"'])

    def test_or_chain_prefix_form(self):
        chain = mtp._or_chain([["A"], ["B"], ["C"]])
        self.assertEqual(chain, ["OR", "OR", "A", "B", "C"])

    def test_three_keywords_subject_scope(self):
        criteria = mtp.build_criteria(["a", "b", "c"], scope="subject")
        self.assertEqual(criteria, ["OR", "OR", "SUBJECT", b'"a"',
                                    "SUBJECT", b'"b"', "SUBJECT", b'"c"'])

    def test_since_and_before_inclusive(self):
        criteria = mtp.build_criteria(None, date_from="2026-01-01",
                                      date_to="2026-06-08")
        self.assertIn("SINCE", criteria)
        self.assertIn("01-Jan-2026", criteria)
        # date_to включительно -> BEFORE следующего дня
        self.assertIn("BEFORE", criteria)
        self.assertIn("09-Jun-2026", criteria)

    def test_before_crosses_month_boundary(self):
        criteria = mtp.build_criteria(None, date_to="2026-01-31")
        self.assertIn("01-Feb-2026", criteria)

    def test_imap_date_format(self):
        self.assertEqual(mtp.imap_date("2026-06-08"), "08-Jun-2026")
        self.assertEqual(mtp.imap_date("2026-12-31"), "31-Dec-2026")

    def test_quote_escaping(self):
        self.assertEqual(mtp._quoted('он сказал "да"'),
                         '"он сказал \\"да\\""'.encode("utf-8"))
        self.assertEqual(mtp._quoted("back\\slash"), b'"back\\\\slash"')

    def test_flags_and_sender(self):
        criteria = mtp.build_criteria(None, sender="ivanov@example.com",
                                      flagged_only=True, unseen_only=True)
        self.assertIn("FLAGGED", criteria)
        self.assertIn("UNSEEN", criteria)
        self.assertIn("FROM", criteria)
        self.assertFalse(mtp.needs_utf8(criteria))

    def test_empty_criteria_is_all(self):
        self.assertEqual(mtp.build_criteria(), ["ALL"])

    def test_bad_date_raises(self):
        with self.assertRaises(ValueError):
            mtp.build_criteria(None, date_from="08.06.2026")


class TestBodystructure(unittest.TestCase):
    PLAIN = b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 12 1)'

    WITH_PDF = (
        b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 120 4)'
        b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 8000 '
        b'NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL) "MIXED")'
    )

    WITH_MIME_NAME = (
        b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 10 1)'
        b'("APPLICATION" "OCTET-STREAM" '
        b'("NAME" "=?utf-8?B?0J/QuNGB0YzQvNC+LmRvY3g=?=") NIL NIL "BASE64" 100 '
        b'NIL ("ATTACHMENT" NIL) NIL) "MIXED")'
    )

    def test_plain_has_nothing(self):
        info = mtp.parse_bodystructure(self.PLAIN)
        self.assertEqual(info["attachments"], [])
        self.assertFalse(info["has_attachment"])
        self.assertFalse(info["has_pdf"])

    def test_pdf_attachment(self):
        info = mtp.parse_bodystructure(self.WITH_PDF)
        self.assertEqual(info["attachments"], ["report.pdf"])
        self.assertTrue(info["has_attachment"])
        self.assertTrue(info["has_pdf"])
        self.assertEqual(info["attachment_count"], 1)

    def test_mime_encoded_filename(self):
        info = mtp.parse_bodystructure(self.WITH_MIME_NAME)
        self.assertEqual(info["attachments"], ["Письмо.docx"])
        self.assertTrue(info["has_attachment"])
        self.assertFalse(info["has_pdf"])

    def test_pdf_detected_by_subtype_without_name(self):
        bs = b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100 NIL NIL NIL)'
        self.assertTrue(mtp.parse_bodystructure(bs)["has_pdf"])


class TestFetchParsing(unittest.TestCase):
    HEADERS = (
        b"Date: Mon, 08 Jun 2026 09:45:00 +0300\r\n"
        b"From: =?utf-8?B?0J3QtdCy0LjQvdC90L7QvNGL0YHRgdC60LjQuQ==?= "
        b"<nevinnomyssk@gupsktek.ru>\r\n"
        b"To: stavgeo26@mail.ru\r\n"
        b"Subject: =?utf-8?B?0J4g0YHQvtCz0LvQsNGB0L7QstCw0L3QuNC4?=\r\n"
        b"Message-ID: <abc@mail.ru>\r\n\r\n"
    )

    PREFIX = (
        b'123 (UID 18357 FLAGS (\\Seen \\Flagged) BODYSTRUCTURE '
        b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "8BIT" 120 4)'
        b'("APPLICATION" "PDF" ("NAME" "topo.pdf") NIL NIL "BASE64" 8000 '
        b'NIL ("ATTACHMENT" ("FILENAME" "topo.pdf")) NIL) "MIXED") '
        b'BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)] {5841}'
    )

    def setUp(self):
        self.item = mtp.parse_fetch_item(self.PREFIX, self.HEADERS)

    def test_uid_extracted_not_raw_prefix(self):
        self.assertEqual(self.item["uid"], "18357")

    def test_flags_parsed_into_list(self):
        self.assertEqual(self.item["flags"], ["\\Seen", "\\Flagged"])
        self.assertTrue(self.item["flagged"])
        self.assertTrue(self.item["seen"])
        self.assertFalse(self.item["answered"])

    def test_headers_decoded(self):
        self.assertEqual(self.item["subject"], "О согласовании")
        self.assertEqual(self.item["from_name"], "Невинномысский")
        self.assertEqual(self.item["from_email"], "nevinnomyssk@gupsktek.ru")
        self.assertEqual(self.item["date"], "2026-06-08 09:45")

    def test_attachments_from_bodystructure(self):
        self.assertEqual(self.item["attachments"], ["topo.pdf"])
        self.assertTrue(self.item["has_pdf"])

    def test_compact_line(self):
        line = mtp.format_compact(self.item)
        self.assertTrue(line.startswith("18357 | 2026-06-08 09:45 | "))
        self.assertIn("О согласовании", line)
        self.assertIn("[флаг, pdf]", line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_compact_marks_unseen(self):
        item = dict(self.item, seen=False, flagged=False, has_pdf=False,
                    has_attachment=False)
        self.assertIn("[непроч]", mtp.format_compact(item))

    def test_compact_without_marks(self):
        item = dict(self.item, seen=True, flagged=False, has_pdf=False,
                    has_attachment=False)
        self.assertNotIn("[", mtp.format_compact(item))

    def test_parse_full_response(self):
        data = [(self.PREFIX, self.HEADERS), b")"]
        items = mtp.parse_fetch_response(data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["uid"], "18357")

    def test_missing_flags_and_bodystructure(self):
        item = mtp.parse_fetch_item(b"1 (UID 42 BODY[HEADER.FIELDS (DATE)] {10}",
                                    b"Subject: hi\r\n\r\n")
        self.assertEqual(item["uid"], "42")
        self.assertEqual(item["flags"], [])
        self.assertFalse(item["has_attachment"])


class TestUidValidation(unittest.TestCase):
    def test_valid_uids(self):
        valid, invalid = mtp.validate_uids(["1", "18357", 42])
        self.assertEqual(valid, ["1", "18357", "42"])
        self.assertEqual(invalid, [])

    def test_rejects_injection(self):
        valid, invalid = mtp.validate_uids(["1", '1:* "INBOX"', "abc"])
        self.assertEqual(valid, ["1"])
        self.assertEqual(invalid, ['1:* "INBOX"', "abc"])

    def test_move_rejects_bad_uid_without_network(self):
        # Кредов нет и сети нет: если бы функция подключалась, был бы не тот текст
        result = mtp._move_impl(["18357", "не-uid"], "Согласование")
        self.assertIn("error", result)
        self.assertIn("только из цифр", result["error"])
        self.assertEqual(result["moved"], [])

    def test_move_rejects_empty_list(self):
        result = mtp._move_impl([], "Согласование")
        self.assertIn("error", result)
        self.assertEqual(result["moved"], [])


class TestSearchModes(unittest.TestCase):
    """Три ступени фолбэка на синтетических ответах SEARCH."""

    class SearchIMAP:
        def __init__(self, behaviour):
            self.behaviour = behaviour
            self.commands = []

        def uid(self, command, *args):
            self.commands.append((command, args))
            has_charset = "CHARSET" in args
            if self.behaviour == "ok":
                return "OK", [b"1 2 3"]
            if self.behaviour == "no-charset" and has_charset:
                raise Exception("BAD Invalid charset")
            if self.behaviour == "no-charset":
                return "OK", [b"4 5"]
            raise Exception("BAD Cannot search")

    def test_mode_server(self):
        imap = self.SearchIMAP("ok")
        uids, mode = mtp._search_uids(imap, ["SUBJECT", b'"\xd1\x81"'])
        self.assertEqual(mode, "server")
        self.assertEqual(uids, ["1", "2", "3"])
        self.assertIn("CHARSET", imap.commands[0][1])

    def test_mode_server_nocharset(self):
        imap = self.SearchIMAP("no-charset")
        uids, mode = mtp._search_uids(imap, ["SUBJECT", b'"\xd1\x81"'])
        self.assertEqual(mode, "server-nocharset")
        self.assertEqual(uids, ["4", "5"])

    def test_mode_client_filter(self):
        imap = self.SearchIMAP("fail")
        uids, mode = mtp._search_uids(imap, ["SUBJECT", b'"\xd1\x81"'])
        self.assertEqual(mode, "client-filter")
        self.assertEqual(uids, [])

    def test_ascii_query_skips_charset(self):
        imap = self.SearchIMAP("ok")
        uids, mode = mtp._search_uids(imap, ["SUBJECT", b'"pdf"'])
        self.assertEqual(mode, "server")
        self.assertNotIn("CHARSET", imap.commands[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
