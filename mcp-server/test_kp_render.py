# -*- coding: utf-8 -*-
"""Тесты рендера КП: сумма прописью, разбор параметров, сборка HTML."""
import json

import pytest

from kp_render import (
    DEFAULT_PAYMENT_TERMS,
    format_money,
    number_in_words,
    parse_body,
    parse_works_table,
    render_kp_html,
)


# --- Сумма прописью: приёмочные значения из ТЗ ----------------------------

def test_money_85000():
    m = format_money(85000)
    assert m["digits"] == "85 000 руб. 00 коп."
    assert m["words"] == "Восемьдесят пять тысяч рублей 00 копеек"


def test_money_1234567_50():
    m = format_money(1234567.50)
    assert m["digits"] == "1 234 567 руб. 50 коп."
    assert m["words"] == ("Один миллион двести тридцать четыре тысячи "
                          "пятьсот шестьдесят семь рублей 50 копеек")


def test_money_990000():
    m = format_money(990000)
    assert m["digits"] == "990 000 руб. 00 коп."
    assert m["words"] == "Девятьсот девяносто тысяч рублей 00 копеек"


# --- Сверка с формулировками из эталонных КП ------------------------------

def test_money_matches_rzd_reference():
    # Эталон РЖД: «2 350 000 (Два миллиона триста пятьдесят тысяч) рублей»
    m = format_money(2350000)
    assert m["words"] == "Два миллиона триста пятьдесят тысяч рублей 00 копеек"


def test_money_matches_enisey_reference():
    # Эталон Енисей: «1 956 500 (Один миллион девятьсот пятьдесят шесть
    # тысяч пятьсот) рублей» и «5 772 000 (Пять миллионов семьсот семьдесят
    # две тысячи) рублей»
    assert format_money(1956500)["words"] == (
        "Один миллион девятьсот пятьдесят шесть тысяч пятьсот "
        "рублей 00 копеек")
    assert format_money(5772000)["words"] == (
        "Пять миллионов семьсот семьдесят две тысячи рублей 00 копеек")


# --- Краевые случаи прописи ------------------------------------------------

@pytest.mark.parametrize("n, words", [
    (1, "один"),
    (2, "два"),
    (11, "одиннадцать"),
    (21, "двадцать один"),
    (100, "сто"),
    (1000, "одна тысяча"),
    (2000, "две тысячи"),
    (5000, "пять тысяч"),
    (21000, "двадцать одна тысяча"),
    (1000000, "один миллион"),
    (1407900, "один миллион четыреста семь тысяч девятьсот"),
    (601900, "шестьсот одна тысяча девятьсот"),
])
def test_number_in_words(n, words):
    assert number_in_words(n) == words


def test_ruble_declension():
    assert format_money(1)["words"] == "Один рубль 00 копеек"
    assert format_money(2)["words"] == "Два рубля 00 копеек"
    assert format_money(21)["words"] == "Двадцать один рубль 00 копеек"


def test_kopeck_declension():
    assert format_money(10.01)["words"].endswith("01 копейка")
    assert format_money(10.02)["words"].endswith("02 копейки")
    assert format_money(10.11)["words"].endswith("11 копеек")


def test_money_rejects_zero_and_negative():
    with pytest.raises(ValueError):
        format_money(0)
    with pytest.raises(ValueError):
        format_money(-100)
    with pytest.raises(ValueError):
        format_money("не число")


# --- Разбор body и works_table ---------------------------------------------

def test_parse_body_json():
    body = json.dumps([{"text": "Абзац 1", "bold": True},
                       {"text": "Абзац 2"}], ensure_ascii=False)
    parsed = parse_body(body)
    assert parsed[0]["text"] == "Абзац 1" and parsed[0]["bold"] is True
    assert parsed[1]["text"] == "Абзац 2"


def test_parse_body_plain_text():
    parsed = parse_body("Первый абзац.\n\nВторой абзац.")
    assert [p["text"] for p in parsed] == ["Первый абзац.", "Второй абзац."]


def test_parse_works_table():
    rows = parse_works_table(json.dumps(
        [{"name": "Топосъёмка", "volume": "0,62 га", "price": "450 000"}],
        ensure_ascii=False))
    assert rows == [{"name": "Топосъёмка", "volume": "0,62 га",
                     "price": "450 000"}]
    assert parse_works_table("") == []
    with pytest.raises(Exception):
        parse_works_table('{"не": "список"}')


# --- Сборка HTML ------------------------------------------------------------

def _minimal_kwargs(**over):
    kwargs = dict(
        date_str="«13» августа 2026 г.",
        addressee="Руководителю\nООО «Тест»",
        object_name="Топографическая съёмка участка",
        body=json.dumps([{"text": "Состав работ по ТЗ."}],
                        ensure_ascii=False),
        price_total=85000,
        result="Технический отчёт",
        timeline="10 рабочих дней с момента поступления аванса",
    )
    kwargs.update(over)
    return kwargs


def test_html_minimal_has_no_placeholders():
    html = render_kp_html(**_minimal_kwargs())
    # Номер не передан — блока «№» нет вовсе, прочерков нет.
    assert "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ №" not in html
    assert "___" not in html
    assert "85 000 руб. 00 коп." in html
    assert "Восемьдесят пять тысяч рублей 00 копеек" in html
    assert DEFAULT_PAYMENT_TERMS in html
    assert "НАШИ КЛИЕНТЫ" in html
    assert "Контакт для связи: Игорь Александрович, тел. +7 928 821-11-40" in html


def test_html_with_number_and_optional_blocks():
    html = render_kp_html(**_minimal_kwargs(
        kp_number="15", object_address="г. Ставрополь, ул. Ленина, 1",
        cadastral_number="26:12:000000:1",
        validity="30 календарных дней",
        payment_terms="100% предоплата",
        works_table=json.dumps([{"name": "Съёмка", "volume": "1 га",
                                 "price": "85 000"}], ensure_ascii=False),
    ))
    assert "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ № 15" in html
    assert "Адрес объекта:" in html
    assert "Кадастровый номер участка:" in html
    assert "Срок действия предложения:" in html
    assert "100% предоплата" in html
    assert DEFAULT_PAYMENT_TERMS not in html
    assert "Наименование работ" in html


def test_html_ooo_has_banner_and_clients():
    html = render_kp_html(**_minimal_kwargs())
    assert 'class="banner"' in html
    assert html.count("<td><img") == 10  # 10 логотипов клиентов
    assert 'class="stamp"' in html       # печать ООО


def test_html_ip_has_no_ooo_artifacts():
    html = render_kp_html(**_minimal_kwargs(entity="ip"))
    assert 'class="banner"' not in html
    assert "НАШИ КЛИЕНТЫ" not in html
    assert "Индивидуальный предприниматель" in html
    assert "ИНН 2634103024" not in html  # реквизиты ООО не выводятся


def test_html_no_double_dot_when_fields_end_with_period():
    html = render_kp_html(**_minimal_kwargs(
        result="Технический отчёт.",
        timeline="10 рабочих дней с момента поступления аванса.",
    ))
    assert "Технический отчёт.." not in html
    assert "аванса.." not in html


def test_html_ip_signature_appears_when_file_exists(tmp_path, monkeypatch):
    import kp_render
    # без файла подписи блока нет
    html = render_kp_html(**_minimal_kwargs(entity="ip"))
    assert 'class="ip-signature"' not in html
    # файл появился — подпись выводится (печати ИП по-прежнему нет)
    kp_dir = tmp_path / "assets" / "kp"
    kp_dir.mkdir(parents=True)
    (kp_dir / "signature_ip.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(kp_render, "BASE_DIR", str(tmp_path))
    html = render_kp_html(**_minimal_kwargs(entity="ip"))
    assert 'class="ip-signature"' in html
    assert 'class="stamp"' not in html


def test_html_rejects_bad_entity():
    with pytest.raises(ValueError):
        render_kp_html(**_minimal_kwargs(entity="zao"))


def test_html_rejects_missing_price():
    with pytest.raises(ValueError):
        render_kp_html(**_minimal_kwargs(price_total=""))


# --- Рендер PDF (только если WeasyPrint доступен локально) -----------------

def test_pdf_render_smoke():
    pytest.importorskip("weasyprint")
    from kp_render import render_kp_pdf
    pdf = render_kp_pdf(**_minimal_kwargs(
        kp_number="7",
        works_table=json.dumps(
            [{"name": f"Работа {i}", "volume": f"{i} шт.",
              "price": f"{i}0 000"} for i in range(1, 13)],
            ensure_ascii=False),
    ))
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 10000
