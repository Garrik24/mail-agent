# -*- coding: utf-8 -*-
"""Сборка коммерческого предложения (КП): HTML-шаблон -> PDF (WeasyPrint).

Тот же рендер-пайплайн, что у писем (letter_render.py): Jinja2-шаблон,
картинки data-URI, WeasyPrint. Сумма прописью генерируется здесь из
price_total — ассистент передаёт только число, расхождение цифры и
прописи исключено.
"""
import glob
import json
import os
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from jinja2 import Template

from letter_render import _data_uri

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Печать ООО + подпись — тот же файл, что на бланке писем (не дублируем).
STAMP_OOO_PATH = os.path.join(BASE_DIR, "assets", "stamp.jpg")
BANNER_PATH = os.path.join(BASE_DIR, "assets", "kp", "header_banner.png")
CLIENTS_DIR = os.path.join(BASE_DIR, "assets", "kp", "clients")
TEMPLATE_PATH = os.path.join(BASE_DIR, "kp_template.html")

DEFAULT_PAYMENT_TERMS = (
    "50% аванс в течение 5 рабочих дней с даты подписания договора "
    "на основании выставленного счёта, 50% — после подписания "
    "Акта выполненных работ"
)

with open(TEMPLATE_PATH, encoding="utf-8") as _f:
    _TEMPLATE = Template(_f.read())


# --- Сумма прописью -------------------------------------------------------

_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь",
            "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь",
            "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать",
          "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
          "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# Разряды по тройкам: тысячи — женского рода («одна тысяча»).
_SCALES = [
    (None, False),
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = n % 100
    if 11 <= n <= 14:
        return many
    n = n % 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _triple_words(n: int, feminine: bool) -> list:
    units = _UNITS_F if feminine else _UNITS_M
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
    if 10 <= n <= 19:
        words.append(_TEENS[n - 10])
        n = 0
    if n:
        words.append(units[n])
    return words


def number_in_words(n: int) -> str:
    """Целое число прописью, мужской род конечных единиц (для рублей)."""
    if n == 0:
        return "ноль"
    groups = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    if len(groups) > len(_SCALES):
        raise ValueError("Сумма слишком велика для прописи")
    words = []
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if not g:
            continue
        scale, feminine = _SCALES[idx]
        words.extend(_triple_words(g, feminine))
        if scale:
            words.append(_plural(g, *scale))
    return " ".join(words)


def format_money(price_total) -> dict:
    """Из числа — цифровая запись и пропись: одна точка истины для обеих.

    Возвращает {"digits": "1 234 567 руб. 50 коп.",
                "words": "Один миллион ... рублей 50 копеек"}.
    """
    try:
        d = Decimal(str(price_total)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"price_total не является числом: {price_total!r}")
    if d <= 0:
        raise ValueError("price_total должен быть положительным числом")
    rub = int(d)
    kop = int((d - rub) * 100)
    digits = "{:,}".format(rub).replace(",", " ") + \
        f" руб. {kop:02d} коп."
    words = number_in_words(rub)
    words = words[0].upper() + words[1:]
    rub_word = _plural(rub, "рубль", "рубля", "рублей")
    kop_word = _plural(kop, "копейка", "копейки", "копеек")
    return {"digits": digits,
            "words": f"{words} {rub_word} {kop:02d} {kop_word}"}


# --- Разбор параметров-списков --------------------------------------------

def parse_body(body: str) -> list:
    """body -> список абзацев [{"text": ..., "bold": bool}].

    Принимает JSON-список объектов/строк или обычный текст (абзацы по
    пустой строке) — так же терпимо, как parse_paragraphs у писем.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return [{"text": t} for t in body.split("\n\n") if t.strip()]
    if isinstance(parsed, list):
        return [p if isinstance(p, dict) else {"text": str(p)}
                for p in parsed]
    if isinstance(parsed, str):
        return [{"text": t} for t in parsed.split("\n\n") if t.strip()]
    return [{"text": str(parsed)}]


def parse_works_table(works_table: str) -> list:
    """works_table -> список строк [{"name", "volume", "price"}]."""
    if not works_table or not works_table.strip():
        return []
    rows = json.loads(works_table)
    if not isinstance(rows, list):
        raise ValueError("works_table должен быть JSON-списком строк таблицы")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Строка works_table должна быть объектом "
                             '{"name": ..., "volume": ..., "price": ...}')
        result.append({
            "name": str(row.get("name", "")),
            "volume": str(row.get("volume", "")),
            "price": str(row.get("price", "")),
        })
    return result


def _strip_trailing_dot(text: str) -> str:
    """Шаблон сам ставит точку в конце блока — одну завершающую убираем,
    чтобы не получалось «..» (многоточие не трогаем)."""
    text = text.strip()
    if text.endswith(".") and not text.endswith(".."):
        return text[:-1]
    return text


def _ip_stamp_uri() -> str:
    """Печать ИП: assets/kp/stamp_ip.* — если файла нет, КП идёт без печати."""
    for path in sorted(glob.glob(os.path.join(BASE_DIR, "assets", "kp",
                                              "stamp_ip.*"))):
        uri = _data_uri(path)
        if uri:
            return uri
    return ""


def _client_logo_uris() -> list:
    if not os.path.isdir(CLIENTS_DIR):
        return []
    uris = []
    for name in sorted(os.listdir(CLIENTS_DIR)):
        path = os.path.join(CLIENTS_DIR, name)
        if os.path.isfile(path):
            uri = _data_uri(path)
            if uri:
                uris.append(uri)
    return uris


def render_kp_html(*, entity: str = "ooo", kp_number: str = "",
                   date_str: str, addressee: str, object_name: str,
                   object_address: str = "", cadastral_number: str = "",
                   body: str, works_table: str = "", price_total,
                   payment_terms: str = "", result: str, timeline: str,
                   validity: str = "") -> str:
    """Собирает HTML КП (общая часть preview_kp и send_kp)."""
    if entity not in ("ooo", "ip"):
        raise ValueError(f'entity должен быть "ooo" или "ip", а не {entity!r}')
    money = format_money(price_total)
    is_ooo = entity == "ooo"
    # По 5 логотипов в ряд — сетка как в эталонных КП.
    logos = _client_logo_uris() if is_ooo else []
    logo_rows = [logos[i:i + 5] for i in range(0, len(logos), 5)]
    return _TEMPLATE.render(
        is_ooo=is_ooo,
        banner=_data_uri(BANNER_PATH) if is_ooo else "",
        stamp=_data_uri(STAMP_OOO_PATH) if is_ooo else _ip_stamp_uri(),
        logo_rows=logo_rows,
        kp_number=kp_number.strip(),
        date_str=date_str,
        addressee_lines=[ln for ln in addressee.split("\n") if ln.strip()],
        object_name=object_name,
        object_address=object_address,
        cadastral_number=cadastral_number,
        paragraphs=parse_body(body),
        works_rows=parse_works_table(works_table),
        money=money,
        payment_terms=_strip_trailing_dot(payment_terms) or DEFAULT_PAYMENT_TERMS,
        result=_strip_trailing_dot(result),
        timeline=_strip_trailing_dot(timeline),
        validity=_strip_trailing_dot(validity),
    )


def render_kp_pdf(**kwargs) -> bytes:
    """HTML КП -> байты PDF."""
    from weasyprint import HTML
    html = render_kp_html(**kwargs)
    return HTML(string=html, base_url=BASE_DIR).write_pdf()
