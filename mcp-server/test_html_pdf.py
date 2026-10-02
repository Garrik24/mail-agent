# -*- coding: utf-8 -*-
"""Тесты preview_html_pdf / send_html_pdf.

Запуск:  python -m pytest test_html_pdf.py -v
Тесты реального рендера пропускаются, если WeasyPrint не может загрузиться.
"""
import json

import pytest

import html_pdf
import html_pdf_tools
from test_send_body_normalization import FakeClient, FakeMCP

SEAL_HTML = ('<div style="position:relative">Директор<img data-seal '
             'style="left:60mm; top:-8mm"></div>')


def weasyprint_ok() -> bool:
    try:
        from weasyprint import HTML  # noqa: F401
        HTML(string="<p>x</p>").write_pdf()
        return True
    except Exception:
        return False


needs_wp = pytest.mark.skipif(not weasyprint_ok(), reason="WeasyPrint недоступен")


# ---------- сборка документа (без WeasyPrint) ----------

def test_seal_marker_replaced_keeping_style():
    doc = html_pdf.build_document(SEAL_HTML, True)
    assert "data-seal" not in doc
    assert 'class="__seal"' in doc and "data:image/" in doc
    assert "left:60mm; top:-8mm" in doc


def test_seal_false_removes_marker():
    doc = html_pdf.build_document(SEAL_HTML, False)
    assert "data-seal" not in doc and "__seal\"" not in doc


def test_seal_true_without_marker_fails():
    with pytest.raises(html_pdf.HtmlPdfError):
        html_pdf.build_document("<p>текст</p>", True)


def test_empty_html_fails():
    with pytest.raises(html_pdf.HtmlPdfError):
        html_pdf.build_document("  ", False)


def test_fragment_and_full_page_get_base_css():
    frag = html_pdf.build_document("<p>a</p>", False)
    full = html_pdf.build_document(
        "<html><head><title>t</title></head><body>b</body></html>", False)
    full_nohead = html_pdf.build_document("<html><body>b</body></html>", False)
    for doc in (frag, full, full_nohead):
        assert "size: A4" in doc and "Liberation Serif" in doc
        assert doc.count("<head") == 1


# ---------- реальный рендер ----------

@needs_wp
def test_render_is_pdf_and_deterministic():
    html = "<table><tr><td>Кириллица</td><td>1</td></tr></table>" + SEAL_HTML
    a = html_pdf.render_html_pdf(html, True)
    b = html_pdf.render_html_pdf(html, True)
    assert a.startswith(b"%PDF") and a == b


@needs_wp
def test_external_resources_blocked():
    # внешняя картинка не должна ни грузиться, ни ронять рендер
    pdf = html_pdf.render_html_pdf('<img src="file:///etc/hosts"><p>ok</p>', False)
    assert pdf.startswith(b"%PDF")


@needs_wp
def test_table_not_split_across_pages():
    from pypdf import PdfReader
    import io
    filler = "<p>строка</p>" * 38
    table = "<table border=1>" + "<tr><td>Ячейка</td></tr>" * 8 + "</table>"
    pdf = html_pdf.render_html_pdf(filler + table, False)
    pages = PdfReader(io.BytesIO(pdf)).pages
    assert len(pages) == 2
    # вся таблица на одной странице
    assert [p.extract_text().count("Ячейка") for p in pages] in ([0, 8], [8, 0])


# ---------- инструменты ----------

@pytest.fixture
def tools_(monkeypatch):
    FakeClient.calls = []
    renders = []

    def fake_render(html, seal=False):
        renders.append((html, seal))
        return b"%PDF-fake"

    monkeypatch.setattr(html_pdf, "render_html_pdf", fake_render)
    monkeypatch.setattr(html_pdf_tools, "IMAPClient", FakeClient)
    monkeypatch.setattr(FakeClient, "send_letter_email",
                        lambda self, **kw: (FakeClient.calls.append(kw) or
                                            {"status": "sent",
                                             "attachment": kw["pdf_filename"]}),
                        raising=False)
    mcp = FakeMCP()
    html_pdf_tools.register_tools(mcp)
    return mcp.tools, FakeClient.calls, renders


def test_preview_returns_url_size_hash(tools_):
    t, calls, _ = tools_
    out = json.loads(t["preview_html_pdf"]("<p>x</p>", "Акт"))
    assert out["ok"] and out["preview_url"].endswith(".pdf")
    assert out["pdf_size_bytes"] == len(b"%PDF-fake") and len(out["sha256"]) == 64
    assert out["expires_in_min"] == 60 and calls == []


def test_send_normalizes_body_and_attaches(tools_):
    t, calls, _ = tools_
    out = json.loads(t["send_html_pdf"](
        html="<p>x</p>", pdf_filename="Акт", to="a@b.ru", subject="Т",
        email_body="Первая\n\nВторая", cc="c@d.ru, e@f.ru"))
    kw = calls[0]
    assert "<p>" in kw["html_body"]
    assert kw["pdf_bytes"] == b"%PDF-fake" and kw["pdf_filename"] == "Акт.pdf"
    assert kw["cc"] == ["c@d.ru", "e@f.ru"]
    assert kw["read_receipt"] is False and kw["urgent"] is False
    assert out["pdf_size_bytes"] == 9 and "sha256" in out


def test_send_rejects_multiple_to(tools_):
    t, calls, renders = tools_
    out = json.loads(t["send_html_pdf"](
        html="<p>x</p>", pdf_filename="a", to="a@b.ru, c@d.ru",
        subject="Т", email_body="т"))
    assert "error" in out and calls == [] and renders == []


def test_render_failure_does_not_send(tools_, monkeypatch):
    t, calls, _ = tools_

    def boom(html, seal=False):
        raise RuntimeError("упал рендер")

    monkeypatch.setattr(html_pdf, "render_html_pdf", boom)
    out = json.loads(t["send_html_pdf"](
        html="<p>x</p>", pdf_filename="a", to="a@b.ru",
        subject="Т", email_body="т"))
    assert out["sent"] is False and "упал рендер" in out["error"] and calls == []


def test_filename_sanitized():
    assert html_pdf_tools._clean_filename("../x\r\ny") == "..xy.pdf"
