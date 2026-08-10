"""Modified UTF-7 для имён IMAP-папок (RFC 3501 §5.1.3).

imaplib кодирует строковые аргументы команд в ascii (IMAP4._encoding), поэтому
имя папки «Согласование» нельзя отдать серверу как есть — нужен modified UTF-7.

Только стандартная библиотека.
"""

import base64
import re

__all__ = ["encode_mutf7", "decode_mutf7", "parse_list_line", "resolve_folder",
           "quote_folder"]


def _b64_chunk(chars: list[str]) -> str:
    """Кодирует накопленные не-ASCII символы в &<base64>- (UTF-16BE, ',' вместо '/')."""
    data = "".join(chars).encode("utf-16-be")
    b64 = base64.b64encode(data).decode("ascii").rstrip("=")
    return "&" + b64.replace("/", ",") + "-"


def encode_mutf7(name: str) -> str:
    """Обычное имя папки -> modified UTF-7 (чистый ASCII)."""
    out: list[str] = []
    buf: list[str] = []
    for ch in name:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            if buf:
                out.append(_b64_chunk(buf))
                buf = []
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    if buf:
        out.append(_b64_chunk(buf))
    return "".join(out)


def decode_mutf7(name: str) -> str:
    """modified UTF-7 -> обычное имя папки.

    Битые последовательности отдаются как есть, без исключения: имя папки из
    чужого ящика не должно ронять инструмент.
    """
    out: list[str] = []
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$'
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        value = value.replace('\\"', '"').replace("\\\\", "\\")
    return value


def parse_list_line(line) -> dict | None:
    """Разбирает строку ответа LIST.

    Пример: (\\HasNoChildren) "/" "&BCE-..." ->
    {"name": "Согласование", "raw": "&BCE-...", "delimiter": "/",
     "flags": ["\\HasNoChildren"]}
    """
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("utf-8", errors="replace")
    elif isinstance(line, tuple):  # literal-форма ответа imaplib
        head = line[0].decode("utf-8", errors="replace")
        tail = line[1].decode("utf-8", errors="replace") if len(line) > 1 else ""
        line = re.sub(r"\{\d+\}\s*$", "", head) + tail
    line = line.strip()
    if not line:
        return None
    m = _LIST_RE.match(line)
    if not m:
        return None
    raw = _unquote(m.group("name"))
    delim = _unquote(m.group("delim")) if m.group("delim") != "NIL" else ""
    flags = [f for f in m.group("flags").split() if f]
    return {"name": decode_mutf7(raw), "raw": raw, "delimiter": delim,
            "flags": flags}


def quote_folder(raw_name: str) -> str:
    """Оборачивает raw-имя папки в кавычки для команд IMAP."""
    escaped = raw_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def list_folders_raw(imap) -> list[dict]:
    """LIST "" "*" -> список разобранных папок."""
    typ, data = imap.list()
    if typ != "OK" or not data:
        return []
    folders = []
    for line in data:
        parsed = parse_list_line(line)
        if parsed:
            folders.append(parsed)
    return folders


def resolve_folder(imap, name: str) -> str:
    """Ищет папку по человекочитаемому имени без учёта регистра.

    Возвращает raw-имя IMAP (modified UTF-7). Если папки в LIST нет —
    отдаёт encode_mutf7(name), чтобы вызывающий получил внятную ошибку от
    сервера, а не ascii-исключение внутри imaplib.
    """
    if not name:
        return "INBOX"
    if name.upper() == "INBOX":
        return "INBOX"
    target = name.strip().casefold()
    folders = list_folders_raw(imap)
    for f in folders:
        if f["name"].casefold() == target or f["raw"].casefold() == target:
            return f["raw"]
    # Поиск по последнему сегменту пути: «Согласование» -> «INBOX/Согласование»
    for f in folders:
        delim = f["delimiter"] or "/"
        if f["name"].split(delim)[-1].casefold() == target:
            return f["raw"]
    return encode_mutf7(name)
