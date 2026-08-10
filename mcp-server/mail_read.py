"""Разбор писем: тело текстом, вложения, флаги, цитаты.

Чистые функции без сети — всё, что принимает уже загруженное письмо.
Используется исправленными инструментами чтения (imap_client) и новыми
read_messages / get_attachment_text (mail_read_tools).

Кодировки: уважается charset каждой части (windows-1251 в русской деловой
переписке встречается регулярно), base64 и quoted-printable разбирает
email.message.get_payload(decode=True).
"""

import email.message
import html as html_module
import io
import logging
import re
import zipfile
from email.header import decode_header, make_header

log = logging.getLogger(__name__)

__all__ = ["decode_mime_header", "html_to_text", "split_quotes", "truncate",
           "extract_body", "list_attachments", "parse_flags_list", "part_filename",
           "build_message_dict", "find_attachment", "extract_pdf_text",
           "extract_docx_text", "extract_attachment_text", "ocr_pdf",
           "ocr_available", "pdf_page_images", "render_pdf_pages",
           "is_blank_image"]

# Куски письма, после которых начинается процитированная переписка
_QUOTE_MARKERS = (
    re.compile(r"^-{2,}\s*(original message|исходное сообщение|"
               r"пересланное сообщение|forwarded message)\s*-{2,}\s*$",
               re.IGNORECASE),
    re.compile(r"^\s*(от кого|кому|from|sent|отправлено)\s*:\s*.+$",
               re.IGNORECASE),
    re.compile(r"^\s*от\s*:\s*.*[<(].*@.*[>)].*$", re.IGNORECASE),
    re.compile(r"^\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}.*(написал|wrote)\s*:?\s*$",
               re.IGNORECASE),
    re.compile(r"^\s*on\s+.+\s+wrote\s*:\s*$", re.IGNORECASE),
    re.compile(r"^_{20,}\s*$"),
)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(
    r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>", re.IGNORECASE)
_BLOCK_START_RE = re.compile(r"<\s*(p|div|tr|li|h[1-6])\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MANY_BLANKS_RE = re.compile(r"\n{3,}")


def _repair_surrogates(value: str) -> str:
    """Чинит заголовки с сырым UTF-8 вместо encoded-words.

    Такие письма встречаются: почтовик кладёт в Subject неASCII-байты как
    есть, email-парсер отдаёт их через surrogateescape (\\udcd0...). Без
    восстановления тема превращается в кашу из вопросительных знаков.
    """
    if not any("\udc80" <= ch <= "\udcff" for ch in value):
        return value
    raw = value.encode("utf-8", errors="surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _decode_chunk(data: bytes, charset: str | None) -> str:
    """Декодирует кусок заголовка, не доверяя объявленной кодировке вслепую."""
    if charset and charset.lower() not in ("unknown-8bit", "x-unknown", "unknown"):
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            log.warning(f"Неизвестная кодировка заголовка: {charset}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def decode_mime_header(value) -> str:
    """MIME encoded-words (=?UTF-8?B?...?=) -> обычная строка.

    Части собираются вручную: make_header на charset «unknown-8bit» (а его
    ставит парсер, когда в заголовке лежат сырые не-ASCII байты) портит
    русский текст в набор вопросительных знаков.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        parts = decode_header(value)
    except Exception:
        return _repair_surrogates(str(value)).strip()

    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(_decode_chunk(chunk, charset))
        else:
            out.append(chunk)
    return _repair_surrogates("".join(out)).strip()


def html_to_text(raw_html: str) -> str:
    """HTML -> читаемый текст: теги вырезаны, переводы строк сохранены."""
    if not raw_html:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_START_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return _MANY_BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def split_quotes(text: str) -> tuple[str, str]:
    """Делит тело на собственный текст и процитированную переписку.

    Цитатой считается всё после маркера ответа («-----Original Message-----»,
    «От кого:», «12.05.2026 ... написал:», длинная линия подчёркиваний), а
    также отдельные строки, начинающиеся с '>'.
    """
    if not text:
        return "", ""
    lines = text.split("\n")
    cut = len(lines)
    for i, line in enumerate(lines):
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            cut = i
            break
    own_lines = lines[:cut]
    quoted_lines = lines[cut:]

    kept = [ln for ln in own_lines if not ln.lstrip().startswith(">")]
    dropped = [ln for ln in own_lines if ln.lstrip().startswith(">")]

    own = _MANY_BLANKS_RE.sub("\n\n", "\n".join(kept)).strip()
    quoted = "\n".join(dropped + quoted_lines).strip()
    return own, quoted


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Обрезает текст до max_chars. Возвращает (текст, был ли обрезан)."""
    if not text or max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text or "", False
    return text[:max_chars].rstrip(), True


def _decode_part(part: email.message.Message) -> str:
    """Достаёт текст части с учётом charset, base64 и quoted-printable."""
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = part.get_content_charset()
    if charset:
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            log.warning(f"Неизвестная кодировка части: {charset}")
    # charset не объявлен или неизвестен: utf-8, затем cp1251
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1251", errors="replace")


_FILENAME_PARAM_RE = re.compile(
    r"""(?:file)?name\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s]+))""", re.IGNORECASE)


def part_filename(part: email.message.Message) -> str:
    """Имя вложения: encoded-words, RFC 2231 и сырые не-ASCII байты.

    get_filename() портит имя, когда почтовик положил в параметр сырой UTF-8
    (парсер помечает такой заголовок как unknown-8bit). В этом случае имя
    достаётся из самого заголовка Content-Disposition / Content-Type.
    """
    name = decode_mime_header(part.get_filename() or "")
    if name and "�" not in name:
        return name

    for header in ("Content-Disposition", "Content-Type"):
        raw = part.get(header)
        if raw is None:
            continue
        decoded = decode_mime_header(raw)
        if "�" in decoded:
            continue
        match = _FILENAME_PARAM_RE.search(decoded)
        if match:
            candidate = next(g for g in match.groups() if g)
            if candidate.strip():
                return candidate.strip()
    return name


def _is_attachment(part: email.message.Message) -> bool:
    disposition = str(part.get("Content-Disposition", "")).lower()
    if "attachment" in disposition:
        return True
    return bool(part.get_filename())


def extract_body(msg: email.message.Message) -> dict:
    """Тело письма текстом: text/plain приоритетнее, иначе HTML -> текст.

    Возвращает {"text": ..., "format": "plain"|"html"|""}.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if not msg.is_multipart():
        text = _decode_part(msg)
        if msg.get_content_type() == "text/html":
            return {"text": html_to_text(text), "format": "html"}
        return {"text": (text or "").strip(), "format": "plain" if text else ""}

    for part in msg.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain_parts.append(_decode_part(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part(part))

    if any(p.strip() for p in plain_parts):
        return {"text": "\n".join(plain_parts).strip(), "format": "plain"}
    if html_parts:
        return {"text": html_to_text("\n".join(html_parts)), "format": "html"}
    return {"text": "", "format": ""}


def list_attachments(msg: email.message.Message) -> list[dict]:
    """Вложения: имя, MIME-тип, размер в байтах. Содержимое не отдаётся."""
    attachments: list[dict] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        filename = part_filename(part)
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        attachments.append({
            "filename": filename,
            "content_type": part.get_content_type(),
            "size_bytes": len(payload) if payload else 0,
        })
    return attachments


def parse_flags_list(raw_flags) -> list[str]:
    """FLAGS из ответа IMAP -> список строк."""
    if isinstance(raw_flags, (bytes, bytearray)):
        raw_flags = bytes(raw_flags).decode("utf-8", errors="replace")
    if not raw_flags:
        return []
    match = re.search(r"FLAGS\s+\(([^)]*)\)", raw_flags, re.IGNORECASE)
    if match:
        raw_flags = match.group(1)
    return [f for f in raw_flags.split() if f]


def build_message_dict(msg: email.message.Message, uid: str = "",
                       flags: list[str] | None = None,
                       max_chars: int = 5000,
                       strip_quotes: bool = True) -> dict:
    """Собирает письмо в словарь: заголовки, тело текстом, вложения, флаги."""
    from email.utils import parseaddr

    flags = flags or []
    body = extract_body(msg)
    text = body["text"]
    quoted = ""
    if strip_quotes:
        text, quoted = split_quotes(text)
    text, truncated = truncate(text, max_chars)

    from_raw = decode_mime_header(msg.get("From", ""))
    sender_name, sender_email = parseaddr(from_raw)

    result = {
        "uid": uid,
        "message_id": (msg.get("Message-ID", "") or "").strip(),
        "date": msg.get("Date", ""),
        "subject": decode_mime_header(msg.get("Subject", "")),
        "sender_name": sender_name or sender_email,
        "sender_email": sender_email,
        "from": from_raw,
        "to": decode_mime_header(msg.get("To", "")),
        "cc": decode_mime_header(msg.get("Cc", "")),
        "reply_to": decode_mime_header(msg.get("Reply-To", "")),
        "body": text,
        "body_format": body["format"],
        "truncated": truncated,
        "attachments": list_attachments(msg),
        "flags": flags,
        "flagged": "\\Flagged" in flags,
        "seen": "\\Seen" in flags,
        "answered": "\\Answered" in flags,
    }
    if strip_quotes and quoted:
        result["quoted_text"] = quoted[:2000]
    return result


# ------------------------------------------------------- текст из вложений

def find_attachment(msg: email.message.Message, name: str = "") -> dict | None:
    """Ищет вложение по имени (без учёта регистра, допускает подстроку).

    Без имени возвращает первое вложение, из которого умеем доставать текст
    (PDF или DOCX), иначе просто первое.
    """
    candidates = []
    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        filename = part_filename(part)
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        candidates.append({
            "filename": filename,
            "content_type": part.get_content_type(),
            "data": payload or b"",
        })
    if not candidates:
        return None

    if name:
        target = name.strip().casefold()
        for item in candidates:
            if item["filename"].casefold() == target:
                return item
        for item in candidates:
            if target in item["filename"].casefold():
                return item
        return None

    for item in candidates:
        if item["filename"].lower().endswith((".pdf", ".docx")):
            return item
    return candidates[0]


def extract_pdf_text(data: bytes, max_chars: int = 5000, ocr: str = "auto",
                     ocr_max_pages: int = 5) -> dict:
    """Текст из PDF через pypdf, со скана — через OCR.

    ocr="auto" (по умолчанию): если текстового слоя нет, включается
    распознавание. ocr="off" — только текстовый слой, как раньше.
    Поле source в ответе показывает, откуда взят текст: text-layer или ocr.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        return {"ok": False, "reason": f"pypdf не установлен: {exc}"}

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return {"ok": False, "reason": "PDF зашифрован паролем"}
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
            if sum(len(p) for p in pages) > max_chars * 4:
                break
        text = "\n".join(pages).strip()
        page_count = len(reader.pages)
    except Exception as exc:
        return {"ok": False, "reason": f"не удалось прочитать PDF: {exc}"}

    if len(re.sub(r"\s", "", text)) < 20:
        if ocr == "off":
            return {"ok": False, "reason": "нет текстового слоя, вероятно скан",
                    "pages": page_count}
        result = ocr_pdf(data, max_chars=max_chars, max_pages=ocr_max_pages)
        result["pages"] = page_count
        return result

    text, truncated = truncate(_MANY_BLANKS_RE.sub("\n\n", text), max_chars)
    return {"ok": True, "text": text, "truncated": truncated,
            "pages": page_count, "source": "text-layer"}


# Скан А4 распознаётся примерно за 1–3 секунды на страницу, поэтому число
# страниц ограничено: инструмент должен отвечать, а не молчать минуту.
OCR_LANG = "rus+eng"
OCR_PAGE_TIMEOUT = 40
# Повороты пробуются только при пустом результате, поэтому лимит жёстче
OCR_ROTATED_TIMEOUT = 25
OCR_RENDER_TIMEOUT = 60
OCR_MAX_PAGES = 5


def ocr_available() -> tuple[bool, str]:
    """Проверяет, что OCR можно выполнить: библиотеки и сам tesseract."""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        return False, f"OCR недоступен, нет библиотеки: {exc}"
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
    except Exception as exc:
        return False, f"OCR недоступен, tesseract не запускается: {exc}"
    return True, ""


OCR_RENDER_DPI = 200


def render_pdf_pages(data: bytes, max_pages: int = OCR_MAX_PAGES,
                     dpi: int = OCR_RENDER_DPI) -> list:
    """Рендерит страницы PDF в картинки через poppler (pdf2image)."""
    from pdf2image import convert_from_bytes

    return convert_from_bytes(data, dpi=dpi, first_page=1, last_page=max_pages,
                              fmt="png", timeout=OCR_RENDER_TIMEOUT)


def embedded_page_images(data: bytes, max_pages: int = OCR_MAX_PAGES) -> list:
    """Достаёт картинки, вложенные в страницы, через pypdf.

    Запасной путь: работает без poppler, но для палитровых и ICC-изображений
    pypdf отдаёт чёрное полотно — такие страницы отсеиваются проверкой
    is_blank_image.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    images = []
    for page in reader.pages[:max_pages]:
        try:
            for item in page.images:
                if item.image is not None:
                    images.append(item.image)
        except Exception as exc:
            log.warning(f"Не удалось извлечь картинки страницы: {exc}")
    return images


def is_blank_image(image) -> bool:
    """True, если картинка сплошь одного цвета — распознавать там нечего."""
    try:
        extrema = image.convert("L").getextrema()
    except Exception:
        return False
    return extrema[0] == extrema[1]


def pdf_page_images(data: bytes, max_pages: int = OCR_MAX_PAGES) -> list:
    """Картинки страниц PDF: сначала честный рендер, потом запасной путь.

    Рендер через poppler даёт страницу такой, какой её видит человек. Раньше
    здесь стояло извлечение вложенных картинок через pypdf — на палитровых
    сканах оно отдавало полностью чёрные полотна (яркость 0), и OCR честно
    ничего не находил.
    """
    try:
        images = render_pdf_pages(data, max_pages)
        if images:
            return images
        log.warning("Рендер PDF вернул пустой список страниц")
    except Exception as exc:
        log.warning(f"Рендер PDF через poppler не удался: {exc}")

    images = embedded_page_images(data, max_pages)
    usable = [i for i in images if not is_blank_image(i)]
    if images and not usable:
        log.warning("Вложенные картинки страниц пустые (одноцветные)")
    return usable or images


# Картинки мельче этого — логотипы и подписи в бланке, не страница скана
OCR_MIN_SIDE = 300
# Ниже этой ширины tesseract начинает ошибаться: скан апскейлим
OCR_TARGET_WIDTH = 1600


def prepare_for_ocr(image):
    """Приводит картинку к виду, на котором tesseract работает уверенно.

    Одноцветные (1-bit) и палитровые сканы распознаются плохо, поэтому
    переводим в градации серого (палитру — через RGB, иначе теряются
    полутона) и растягиваем мелкие картинки до разумной ширины.
    """
    from PIL import Image

    if image.mode == "P":
        image = image.convert("RGB")
    if image.mode not in ("L", "RGB"):
        image = image.convert("L")
    width, height = image.size
    if width < OCR_TARGET_WIDTH:
        scale = min(3.0, OCR_TARGET_WIDTH / max(width, 1))
        image = image.resize((int(width * scale), int(height * scale)),
                             Image.LANCZOS)
    return image


def _has_text(value: str) -> bool:
    return len(re.sub(r"\s", "", value or "")) >= 20


def _ocr_image(pytesseract, image, lang: str) -> str:
    """Распознаёт одну страницу, при пустом результате пробуя повороты.

    Документы часто сканируют боком: страница приходит landscape, и при
    обычном режиме tesseract не находит на ней ни строчки. Повороты стоят
    времени, поэтому пробуются только когда прямой проход дал пустоту.
    """
    prepared = prepare_for_ocr(image)
    try:
        text = pytesseract.image_to_string(
            prepared, lang=lang, timeout=OCR_PAGE_TIMEOUT) or ""
    except RuntimeError as exc:  # таймаут tesseract
        log.warning(f"OCR страницы прерван по таймауту: {exc}")
        return ""
    except Exception as exc:
        log.warning(f"OCR страницы не удался: {exc}")
        return ""
    if _has_text(text):
        return text

    # Бланки с рамками и печатями иногда не разбираются автосегментацией:
    # psm 6 читает страницу как единый блок текста.
    try:
        candidate = pytesseract.image_to_string(
            prepared, lang=lang, config="--psm 6",
            timeout=OCR_ROTATED_TIMEOUT) or ""
        if _has_text(candidate):
            log.info("Страница распознана в режиме --psm 6")
            return candidate
    except Exception as exc:
        log.warning(f"OCR в режиме psm 6 не удался: {exc}")

    for angle in (270, 90, 180):
        try:
            rotated = prepared.rotate(angle, expand=True)
            candidate = pytesseract.image_to_string(
                rotated, lang=lang, timeout=OCR_ROTATED_TIMEOUT) or ""
        except Exception as exc:
            log.warning(f"OCR повёрнутой на {angle}° страницы не удался: {exc}")
            continue
        if _has_text(candidate):
            log.info(f"Страница распознана после поворота на {angle}°")
            return candidate
    return text


def describe_image(image) -> str:
    """Краткая характеристика картинки для диагностики нераспознанного скана.

    Средняя яркость сразу показывает главное: если страница почти белая или
    сплошь чёрная, значит из PDF извлеклась не та картинка, и распознавать
    там нечего.
    """
    info = f"{image.size[0]}x{image.size[1]} {image.mode}"
    try:
        grey = image.convert("L")
        pixels = list(grey.getdata())
        if pixels:
            mean = sum(pixels) / len(pixels)
            info += f" яркость~{mean:.0f} (мин {min(pixels)}, макс {max(pixels)})"
    except Exception as exc:
        info += f" (яркость не посчиталась: {exc})"
    return info


def ocr_pdf(data: bytes, max_chars: int = 5000,
            max_pages: int = OCR_MAX_PAGES, lang: str = OCR_LANG) -> dict:
    """Распознаёт текст со сканированного PDF (tesseract, rus+eng)."""
    available, reason = ocr_available()
    if not available:
        return {"ok": False,
                "reason": f"нет текстового слоя, вероятно скан; {reason}"}

    import pytesseract

    try:
        images = pdf_page_images(data, max_pages)
    except Exception as exc:
        return {"ok": False,
                "reason": f"нет текстового слоя; картинки страниц не извлеклись: {exc}"}

    if not images:
        return {"ok": False,
                "reason": "нет текстового слоя, и страницы не отрендерились — "
                          "проверьте, что установлен poppler-utils"}
    if all(is_blank_image(i) for i in images):
        return {"ok": False,
                "reason": "нет текстового слоя, а страницы отрендерились "
                          "пустыми — вложение, вероятно, повреждено",
                "images_info": [describe_image(i) for i in images[:4]]}

    # Крупные картинки — это страницы скана; мелочь (логотип, подпись в
    # бланке) только тратит время. Если крупных нет — распознаём всё подряд.
    big = [i for i in images if min(i.size) >= OCR_MIN_SIDE]
    targets = big or images

    chunks = []
    for image in targets:
        chunks.append(_ocr_image(pytesseract, image, lang))
        if sum(len(c) for c in chunks) > max_chars * 4:
            break

    text = _MANY_BLANKS_RE.sub("\n\n", "\n".join(chunks)).strip()
    if len(re.sub(r"\s", "", text)) < 20:
        return {"ok": False,
                "reason": "скан распознан, но текста не нашлось — "
                          "возможно, пустая или нечитаемая страница",
                "images": len(images),
                # Диагностика: по размерам и режиму видно, дошла ли до OCR
                # сама страница или только мелкие элементы бланка
                "images_info": [describe_image(i) for i in images[:4]]}

    text, truncated = truncate(text, max_chars)
    return {"ok": True, "text": text, "truncated": truncated,
            "source": "ocr", "ocr_pages": len(images), "ocr_lang": lang}


_DOCX_PARA_RE = re.compile(rb"<w:p[ >].*?</w:p>", re.DOTALL)
_DOCX_TEXT_RE = re.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_DOCX_BREAK_RE = re.compile(rb"<w:(?:br|tab)\b[^>]*/?>")


def extract_docx_text(data: bytes, max_chars: int = 5000) -> dict:
    """Текст из DOCX без внешних зависимостей: zip + word/document.xml."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except KeyError:
        return {"ok": False, "reason": "в DOCX нет word/document.xml"}
    except Exception as exc:
        return {"ok": False, "reason": f"не удалось прочитать DOCX: {exc}"}

    paragraphs = []
    for para in _DOCX_PARA_RE.findall(xml):
        para = _DOCX_BREAK_RE.sub(b" ", para)
        chunks = [m.decode("utf-8", errors="replace")
                  for m in _DOCX_TEXT_RE.findall(para)]
        line = html_module.unescape("".join(chunks)).strip()
        if line:
            paragraphs.append(line)

    text = "\n".join(paragraphs).strip()
    if not text:
        return {"ok": False, "reason": "в документе не найдено текста"}
    text, truncated = truncate(text, max_chars)
    return {"ok": True, "text": text, "truncated": truncated}


def extract_attachment_text(filename: str, content_type: str, data: bytes,
                            max_chars: int = 5000, ocr: str = "auto",
                            ocr_max_pages: int = OCR_MAX_PAGES) -> dict:
    """Текст из вложения по типу файла. Поддержаны PDF и DOCX."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return extract_pdf_text(data, max_chars, ocr=ocr,
                                ocr_max_pages=ocr_max_pages)
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return extract_docx_text(data, max_chars)
    if name.endswith(".txt") or ctype.startswith("text/"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
        text, truncated = truncate(text.strip(), max_chars)
        return {"ok": True, "text": text, "truncated": truncated}
    return {"ok": False,
            "reason": f"формат не поддержан: {filename or content_type}. "
                      "Умею PDF, DOCX и текстовые файлы"}
