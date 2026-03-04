"""
MCP инструменты для работы с почтой Mail.ru.
"""

import json
import logging
from imap_client import IMAPClient

log = logging.getLogger(__name__)


def register_tools(mcp):
    """Регистрирует все MCP инструменты на экземпляре FastMCP."""

    def _with_imap(func):
        """Обёртка: создаёт IMAP соединение, вызывает func, закрывает."""
        def wrapper(*args, **kwargs):
            client = IMAPClient()
            try:
                client.connect()
                return func(client, *args, **kwargs)
            except Exception as e:
                log.error(f"Ошибка IMAP: {e}")
                return json.dumps({"error": str(e)}, ensure_ascii=False)
            finally:
                client.disconnect()
        return wrapper

    @mcp.tool()
    def get_new_emails(hours: int = 24, folder: str = "INBOX",
                       limit: int = 50) -> str:
        """Получить новые письма за последние N часов.

        Args:
            hours: За сколько часов показать письма (по умолчанию 24)
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            emails = client.get_emails(folder=folder, since_hours=hours,
                                       limit=limit)
            if not emails:
                return json.dumps(
                    {"message": f"Новых писем за {hours}ч нет", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def get_important_emails(folder: str = "INBOX",
                             limit: int = 50) -> str:
        """Получить письма с флагом Important/Flagged.

        Args:
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            emails = client.get_important_emails(folder=folder, limit=limit)
            if not emails:
                return json.dumps(
                    {"message": "Важных писем нет", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def search_emails(query: str = "", sender: str = "",
                      date_from: str = "", folder: str = "INBOX",
                      limit: int = 50) -> str:
        """Поиск писем по ключевым словам, отправителю или дате.

        Args:
            query: Текст для поиска в теме и теле письма
            sender: Email или имя отправителя
            date_from: Дата начала поиска в формате YYYY-MM-DD
            folder: Папка почты (по умолчанию INBOX)
            limit: Максимум писем (по умолчанию 50)
        """
        @_with_imap
        def _run(client: IMAPClient):
            emails = client.search_emails(
                query=query, sender=sender, date_from=date_from,
                folder=folder, limit=limit,
            )
            if not emails:
                return json.dumps(
                    {"message": "Ничего не найдено", "count": 0},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"count": len(emails), "emails": emails},
                ensure_ascii=False, indent=2,
            )
        return _run()

    @mcp.tool()
    def get_email_body(email_uid: str, folder: str = "INBOX") -> str:
        """Получить полное содержимое письма по его UID.

        Args:
            email_uid: UID письма (из результатов get_new_emails или search_emails)
            folder: Папка почты (по умолчанию INBOX)
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.get_email_body(email_uid=email_uid, folder=folder)
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def analyze_email(email_uid: str, folder: str = "INBOX") -> str:
        """Получить структурированные данные письма для анализа:
        отправитель, тема, тело, вложения, флаги.

        Args:
            email_uid: UID письма
            folder: Папка почты (по умолчанию INBOX)
        """
        @_with_imap
        def _run(client: IMAPClient):
            data = client.get_email_body(email_uid=email_uid, folder=folder)
            if "error" in data:
                return json.dumps(data, ensure_ascii=False)
            analysis = {
                "sender": f"{data['sender_name']} <{data['sender_email']}>",
                "subject": data["subject"],
                "date": data["date"],
                "body": data.get("body_full", "")[:5000],
                "attachments": data.get("attachments", []),
                "flags": data.get("flags", ""),
                "has_attachments": len(data.get("attachments", [])) > 0,
                "instructions": (
                    "Проанализируй это письмо и определи: "
                    "1) Суть письма (2-3 предложения) "
                    "2) Требуемое действие от получателя "
                    "3) Срочность (высокая/средняя/низкая) "
                    "4) Категория (госорган, финансы, лид, информационное, спам)"
                ),
            }
            return json.dumps(analysis, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def send_reply(email_uid: str, body: str,
                   folder: str = "INBOX") -> str:
        """Ответить на письмо. Отправляет ответ через SMTP.

        Args:
            email_uid: UID письма, на которое отвечаем
            body: Текст ответа
            folder: Папка, где находится оригинальное письмо
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.send_reply(
                email_uid=email_uid, body=body, folder=folder,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def get_folders() -> str:
        """Получить список всех папок почтового ящика."""
        @_with_imap
        def _run(client: IMAPClient):
            folders = client.get_folders()
            return json.dumps(
                {"count": len(folders), "folders": folders},
                ensure_ascii=False, indent=2,
            )
        return _run()
