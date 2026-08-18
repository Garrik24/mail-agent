# -*- coding: utf-8 -*-
"""Тесты нормализации тела письма (sanitize.prepare_body)."""

from sanitize import prepare_body, unescape_tags, has_markup, plain_to_html


def test_escaped_tags_fixed():
    """Экранированные сущности превращаются в реальные теги."""
    src = '&lt;p&gt;Добрый день&lt;/p&gt;&lt;p&gt;Текст&lt;/p&gt;'
    assert prepare_body(src) == '<p>Добрый день</p><p>Текст</p>'


def test_double_escaped_tags_fixed():
    """Двойное экранирование тоже раскручивается."""
    src = '&amp;lt;p&amp;gt;Текст&amp;lt;/p&amp;gt;'
    assert prepare_body(src) == '<p>Текст</p>'


def test_plain_text_wrapped_in_paragraphs():
    """Обычный текст с пустыми строками -> абзацы."""
    src = 'Первый абзац.\n\nВторой абзац.'
    assert prepare_body(src) == '<p>Первый абзац.</p><p>Второй абзац.</p>'


def test_single_newline_becomes_br():
    """Одиночный перенос внутри абзаца -> <br>."""
    src = 'Строка один\nСтрока два'
    assert prepare_body(src) == '<p>Строка один<br>Строка два</p>'


def test_real_html_untouched():
    """Готовый HTML не трогаем."""
    src = '<p>Уже размечено</p><ul><li>Пункт</li></ul>'
    assert prepare_body(src) == src


def test_special_chars_escaped_in_plain_text():
    """Спецсимволы в plain text экранируются и не ломают письмо."""
    src = 'Допуск 1/25000 & точность < 5"'
    result = prepare_body(src)
    assert '&amp;' in result
    assert '&lt;' in result


def test_empty_input():
    assert prepare_body('') == ''
    assert prepare_body(None) is None


def test_real_incident_case():
    """Реальный кейс 18.08.2026 — задание на выезд."""
    src = ('Александр, добрый день.\n\nЗадание на выезд 18.08.2026.\n\n'
           'Состав работ:\n1. Отыскать центры\n2. Промер базиса')
    result = prepare_body(src)
    assert result.count('<p>') == 3
    assert '<br>' in result


# --- дополнительные кейсы ---

def test_inline_tags_survive_in_plain_text():
    """Строчные теги в plain text остаются рабочими, а не экранируются."""
    src = 'Добрый день.\n\nС уважением, <b>Коровко И.А.</b>'
    result = prepare_body(src)
    assert result == '<p>Добрый день.</p><p>С уважением, <b>Коровко И.А.</b></p>'


def test_inline_link_survives():
    """Ссылка в plain text остаётся кликабельной."""
    src = 'Материалы: <a href="https://disk.yandex.ru/d/abc">Я.Диск</a>'
    result = prepare_body(src)
    assert '<a href="https://disk.yandex.ru/d/abc">Я.Диск</a>' in result


def test_unknown_tag_still_escaped():
    """Теги вне белого списка экранируются — письмо не ломается."""
    result = prepare_body('Условие: <script>alert(1)</script>')
    assert '<script>' not in result
    assert '&lt;script&gt;' in result


def test_escaped_tags_then_no_double_wrapping():
    """После расэкранирования абзацы не оборачиваются повторно."""
    src = '&lt;p&gt;Раз&lt;/p&gt;\n\n&lt;p&gt;Два&lt;/p&gt;'
    result = prepare_body(src)
    assert result.count('<p>') == 2


def test_trailing_whitespace_only():
    """Строка из пробелов/переносов не превращается в пустой абзац."""
    assert prepare_body('   \n  ') == '   \n  '


def test_idempotent():
    """Повторный прогон ничего не меняет."""
    for src in ('Текст\n\nЕщё текст',
                '&lt;p&gt;Текст&lt;/p&gt;',
                '<p>Готово</p>'):
        once = prepare_body(src)
        assert prepare_body(once) == once


def test_helpers():
    assert has_markup('<div>x</div>') is True
    assert has_markup('просто текст') is False
    assert has_markup(None) is False
    assert unescape_tags('&lt;br&gt;') == '<br>'
    assert plain_to_html('А\n\nБ') == '<p>А</p><p>Б</p>'
