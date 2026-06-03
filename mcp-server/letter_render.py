# -*- coding: utf-8 -*-
"""Сборка письма на фирменном бланке: HTML-шаблон -> PDF (WeasyPrint)."""
import os
import base64
import mimetypes
from jinja2 import Template
from weasyprint import HTML

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


def render_letter_pdf(*, addressee: str, isx_number: str, date_str: str, subject: str,
                      salutation: str, paragraphs: list, yadisk_url: str = "",
                      appendix: str = "", executor: str = "Виктория",
                      executor_phone: str = "8 (938) 350-74-00") -> bytes:
    """
    paragraphs — список словарей вида {"text": "...", "italic": False}.
    Возвращает байты готового PDF.
    """
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
