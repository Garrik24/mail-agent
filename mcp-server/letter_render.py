# -*- coding: utf-8 -*-
"""Сборка письма на фирменном бланке: HTML-шаблон -> PDF (WeasyPrint)."""
import os
import json
import base64
import mimetypes
from jinja2 import Template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAMP_PATH = os.path.join(BASE_DIR, "assets", "stamp.jpg")          # файл печати/подписи
TEMPLATE_PATH = os.path.join(BASE_DIR, "letter_template.html")


def _data_uri(path: str) -> str:
    """Картинку — в data-URI, чтобы не зависеть от base_url при рендере.
    Если файла нет (печать ещё не загружена) — вернём пустую строку,
    шаблон в этом случае просто не выводит <img>."""
    if not os.path.exists(path):
        return ""
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


with open(TEMPLATE_PATH, encoding="utf-8") as _f:
    _TEMPLATE = Template(_f.read())


def parse_paragraphs(body: str) -> list:
    """Разбирает поле body в список абзацев [{"text": ..., "italic": bool}].

    Принимает: JSON-список объектов/строк, JSON-строку или обычный текст
    (в последнем случае абзацы делятся по пустой строке). Единая точка разбора —
    чтобы preview_letter и send_letter собирали идентичный PDF.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return [{"text": t} for t in body.split("\n\n") if t.strip()]
    if isinstance(parsed, list):
        return [p if isinstance(p, dict) else {"text": str(p)} for p in parsed]
    if isinstance(parsed, str):
        return [{"text": t} for t in parsed.split("\n\n") if t.strip()]
    return [{"text": str(parsed)}]


def render_letter_pdf(*, addressee: str, isx_number: str, date_str: str, subject: str,
                      salutation: str, paragraphs: list, yadisk_url: str = "",
                      appendix: str = "", executor: str = "Виктория",
                      executor_phone: str = "8 (938) 350-74-00") -> bytes:
    """
    paragraphs — список словарей вида {"text": "...", "italic": False}.
    Возвращает байты готового PDF.
    """
    # WeasyPrint лениво: модуль импортируется и без системных библиотек
    # рендера (например, в тестах прописи/шаблонов на локальной машине).
    from weasyprint import HTML
    html = _TEMPLATE.render(
        addressee_lines=[ln for ln in addressee.split("\n") if ln.strip()],
        isx_number=isx_number,
        date_str=date_str,
        subject=subject,
        salutation=salutation,
        paragraphs=paragraphs,
        yadisk_url=yadisk_url,
        appendix=appendix,
        executor=executor,
        executor_phone=executor_phone,
        stamp=_data_uri(STAMP_PATH),
    )
    return HTML(string=html, base_url=BASE_DIR).write_pdf()
