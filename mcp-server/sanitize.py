# -*- coding: utf-8 -*-
"""
Нормализация тела письма перед отправкой.

Чинит два типовых дефекта входных данных:
1. HTML-сущности вместо реальных тегов ('&lt;p&gt;' -> '<p>')
2. Plain text с переносами строк -> корректные HTML-абзацы
"""

import html
import re

# Известные HTML-теги, по которым определяем разметку
_TAGS = r'p|br|b|i|u|strong|em|div|span|table|tr|td|th|ul|ol|li|a|h[1-6]'

# Признак экранированной разметки: сущность '&lt;' перед именем тега.
# '(?:amp;)*' ловит и многократное экранирование ('&amp;lt;p&amp;gt;').
_ESCAPED_TAG = re.compile(rf'&(?:amp;)*lt;\s*/?\s*({_TAGS})\b', re.IGNORECASE)

# Признак реальной блочной разметки
_REAL_BLOCK_TAG = re.compile(r'<\s*/?\s*(p|br|div|table|ul|ol|li)\b', re.IGNORECASE)

# Строчные теги, которые сохраняем при обёртке plain text в абзацы:
# модель часто пишет обычный текст с одним-двумя <b>/<i>/<a href>.
_INLINE_TAG = re.compile(
    r'</?\s*(?:b|i|u|s|em|strong|sub|sup)\s*>'
    r'|<\s*a\s+href="[^"<>]*"\s*>'
    r'|</\s*a\s*>',
    re.IGNORECASE,
)


def unescape_tags(text: str) -> str:
    """
    Раскодирует HTML-сущности обратно в теги, если модель прислала
    '&lt;p&gt;Текст&lt;/p&gt;' вместо '<p>Текст</p>'.

    Цикл на 3 прохода: бывает двойное экранирование ('&amp;lt;p&amp;gt;'),
    одного вызова unescape мало.
    """
    if not text:
        return text

    for _ in range(3):
        if not _ESCAPED_TAG.search(text):
            break
        text = html.unescape(text)

    return text


def has_markup(text: str) -> bool:
    """True, если в тексте есть реальная блочная HTML-разметка."""
    return bool(_REAL_BLOCK_TAG.search(text or ''))


def _escape_keeping_inline(text: str) -> str:
    """Экранирует спецсимволы, но оставляет как есть строчные теги
    из белого списка (<b>, <i>, <a href="...">…).
    """
    parts = []
    pos = 0
    for m in _INLINE_TAG.finditer(text):
        parts.append(html.escape(text[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(html.escape(text[pos:]))
    return ''.join(parts)


def plain_to_html(text: str) -> str:
    """
    Оборачивает обычный текст в HTML-абзацы с сохранением структуры.

    Абзацы разделяются пустой строкой. Одиночный перенос внутри абзаца
    становится <br>. Спецсимволы экранируются, чтобы '<' и '&' в тексте
    не ломали письмо.
    """
    if not text or not text.strip():
        return text

    paragraphs = re.split(r'\n\s*\n', text.strip())

    result = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        escaped = _escape_keeping_inline(para)
        escaped = escaped.replace('\n', '<br>')
        result.append(f'<p>{escaped}</p>')

    return ''.join(result)


def prepare_body(text: str) -> str:
    """
    Полная подготовка тела письма. Вызывать первой строкой
    в каждом хендлере отправки.

    Гарантирует, что на выходе всегда корректный HTML независимо
    от того, что пришло на вход.
    """
    if not text:
        return text

    text = unescape_tags(text)

    if not has_markup(text):
        text = plain_to_html(text)

    return text
