# -*- coding: utf-8 -*-
"""Произвольный HTML -> PDF на сервере (WeasyPrint) для preview_html_pdf / send_html_pdf.

Клиент передаёт только HTML; PDF собирается здесь. Печать с подписью
(assets/stamp.jpg — тот же файл, что и в send_letter) подставляется вместо
маркера <img data-seal>.
"""
import datetime
import hashlib
import html as html_lib
import re

from letter_render import STAMP_PATH, _data_uri

STAMP_WIDTH_MM = 46  # как в бланке письма (letter_template.html)

# Базовые стили; стили самого документа идут ПОСЛЕ и могут их переопределить.
BASE_CSS = """
@page { size: A4; margin: 20mm 15mm 20mm 30mm; }
body { font-family: "Liberation Serif", "Times New Roman", serif;
       font-size: 12pt; color: #000; line-height: 1.3; }
table { border-collapse: collapse; page-break-inside: avoid; break-inside: avoid; }
tr, td, th, img { page-break-inside: avoid; break-inside: avoid; }
thead { display: table-header-group; }
img.__seal { position: absolute; width: %dmm; height: auto; z-index: -1; }
""" % STAMP_WIDTH_MM

_SEAL_TAG = re.compile(r"<img\b[^>]*\bdata-seal\b[^>]*>", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_HAS_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HAS_HTML = re.compile(r"<html\b", re.IGNORECASE)
_HAS_CREATED = re.compile(r"""name\s*=\s*["']?dcterms\.created""", re.IGNORECASE)


class HtmlPdfError(Exception):
    """Понятная пользователю причина, по которой PDF не собран."""


def _seal_img(tag: str, stamp: str) -> str:
    """Маркер -> <img> печати. Свой style маркера (отступы top/left и т.п.)
    сохраняется: так в HTML задают положение над строкой подписи."""
    m = _STYLE_ATTR.search(tag)
    raw = (m.group(1) or m.group(2) or "") if m else ""
    style = html_lib.escape(html_lib.unescape(raw), quote=True)
    return f'<img class="__seal" alt="" src="{stamp}" style="{style}">'


def apply_seal(html: str, seal: bool) -> str:
    """seal=True: каждый маркер <img data-seal> -> файл печати.
    seal=False: маркеры удаляются (битая картинка в PDF не нужна)."""
    markers = _SEAL_TAG.findall(html)
    if not seal:
        return _SEAL_TAG.sub("", html)
    if not markers:
        raise HtmlPdfError(
            "seal=true, но в HTML нет маркера <img data-seal> — "
            "непонятно, где ставить печать")
    stamp = _data_uri(STAMP_PATH)
    if not stamp:
        raise HtmlPdfError("Файл печати assets/stamp.jpg не найден на сервере")
    return _SEAL_TAG.sub(lambda m: _seal_img(m.group(0), stamp), html)


def build_document(html: str, seal: bool) -> str:
    """Полный HTML-документ: фрагмент оборачивается, базовые стили и
    фиксированная дата создания (детерминированный PDF) добавляются."""
    if not html or not html.strip():
        raise HtmlPdfError("html пустой")
    body = apply_seal(html, seal)
    created = "" if _HAS_CREATED.search(body) else (
        '<meta name="dcterms.created" content="%s">'
        % datetime.datetime.now(datetime.timezone.utc).date().isoformat())
    head_add = f'<meta charset="utf-8">{created}<style>{BASE_CSS}</style>'
    if not _HAS_HTML.search(body):
        return f'<!DOCTYPE html><html lang="ru"><head>{head_add}</head><body>{body}</body></html>'
    m = _HAS_HEAD.search(body)
    if m:  # вставляем сразу после <head>: стили документа ниже и побеждают
        return body[:m.end()] + head_add + body[m.end():]
    m = _HAS_HTML.search(body)
    end = body.find(">", m.start()) + 1
    return body[:end] + f"<head>{head_add}</head>" + body[end:]


def _allow_only_data(url, *args, **kwargs):
    raise HtmlPdfError(f"Внешние ресурсы запрещены: {url[:80]}")


def render_html_pdf(html: str, seal: bool = False) -> bytes:
    """HTML -> байты PDF. Любая проблема — исключение (письмо не уйдёт)."""
    doc = build_document(html, seal)
    # WeasyPrint лениво: тесты шаблонов не требуют системных библиотек.
    from weasyprint import HTML
    try:
        from weasyprint.urls import URLFetcher  # WeasyPrint >= 68

        class _DataOnly(URLFetcher):
            def fetch(self, url, headers=None):
                if url.startswith("data:"):
                    return super().fetch(url, headers)
                _allow_only_data(url)

        fetcher = _DataOnly()
    except ImportError:  # старые версии: url_fetcher — функция
        from weasyprint import default_url_fetcher

        def fetcher(url, *a, **kw):
            if url.startswith("data:"):
                return default_url_fetcher(url, *a, **kw)
            _allow_only_data(url)

    pdf = HTML(string=doc, url_fetcher=fetcher).write_pdf()
    if not pdf or not pdf.startswith(b"%PDF"):
        raise HtmlPdfError("Рендер вернул пустой или некорректный PDF")
    return pdf


def pdf_facts(pdf: bytes) -> dict:
    return {"pdf_size_bytes": len(pdf), "sha256": hashlib.sha256(pdf).hexdigest()}
