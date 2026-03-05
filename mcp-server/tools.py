"""
MCP инструменты для работы с почтой Mail.ru.
"""

import json
import logging
import smtplib
import os
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
    def prepare_reply(email_uid: str, folder: str = "INBOX") -> str:
        """Подготовить ответ: показать всех получателей ПЕРЕД отправкой.
        ОБЯЗАТЕЛЬНО вызови перед send_reply, чтобы показать пользователю
        кому пойдёт письмо (To, CC) и дать возможность изменить список.

        Args:
            email_uid: UID письма, на которое хотим ответить
            folder: Папка, где находится оригинальное письмо
        """
        @_with_imap
        def _run(client: IMAPClient):
            result = client.get_reply_info(
                email_uid=email_uid, folder=folder,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        return _run()

    @mcp.tool()
    def send_reply(email_uid: str, body: str,
                   folder: str = "INBOX",
                   reply_all: bool = False,
                   cc_override: str = "") -> str:
        """Ответить на письмо. СНАЧАЛА вызови prepare_reply чтобы показать
        пользователю получателей и получить подтверждение.

        Письмо сохраняется в папку Отправленные автоматически.

        Args:
            email_uid: UID письма, на которое отвечаем
            body: Текст ответа
            folder: Папка, где находится оригинальное письмо
            reply_all: True = ответить всем (отправитель + CC + другие To).
                       False = ответить только отправителю.
            cc_override: Если нужно изменить CC вручную — список email через запятую.
                         Пример: "a@mail.ru, b@mail.ru". Пустая строка = авто.
        """
        @_with_imap
        def _run(client: IMAPClient):
            cc_list = None
            if cc_override.strip():
                cc_list = [e.strip() for e in cc_override.split(",") if e.strip()]
            result = client.send_reply(
                email_uid=email_uid, body=body, folder=folder,
                reply_all=reply_all, cc_override=cc_list,
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

    @mcp.tool()
    def test_smtp() -> str:
        """Диагностика SMTP подключения к Mail.ru.
        Проверяет соединение, SSL, EHLO, авторизацию.
        Не отправляет писем — только тестирует подключение.
        """
        smtp_host = os.environ.get("MAIL_SMTP_HOST", "smtp.mail.ru")
        smtp_port = int(os.environ.get("MAIL_SMTP_PORT", "465"))
        mail_user = os.environ.get("MAIL_USERNAME", "")
        mail_pass = os.environ.get("MAIL_PASSWORD", "")

        steps = []
        try:
            # Шаг 1: подключение
            steps.append(f"1. Подключение к {smtp_host}:{smtp_port}...")
            if smtp_port == 465:
                smtp = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            else:
                smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            steps.append("   OK: TCP+SSL соединение установлено")

            # Шаг 2: EHLO
            steps.append("2. Отправка EHLO...")
            code, ehlo_msg = smtp.ehlo()
            ehlo_str = ehlo_msg.decode("utf-8", errors="replace")
            steps.append(f"   OK: код {code}")
            # Извлекаем поддерживаемые методы AUTH
            auth_line = ""
            for line in ehlo_str.split("\n"):
                if "AUTH" in line.upper():
                    auth_line = line.strip()
                    break
            steps.append(f"   Методы авторизации: {auth_line}")

            # Шаг 3: STARTTLS (только для порта 587)
            if smtp_port != 465:
                steps.append("3. STARTTLS...")
                smtp.starttls()
                smtp.ehlo()
                steps.append("   OK: TLS установлен")

            # Шаг 4: авторизация
            steps.append(f"4. Авторизация как {mail_user}...")
            smtp.login(mail_user, mail_pass)
            steps.append("   OK: Авторизация успешна!")

            smtp.quit()
            steps.append("5. SMTP готов к отправке!")
            return json.dumps(
                {"status": "ok", "steps": steps},
                ensure_ascii=False, indent=2,
            )
        except smtplib.SMTPAuthenticationError as e:
            steps.append(f"   ОШИБКА АВТОРИЗАЦИИ: {e.smtp_code} {e.smtp_error}")
            error_msg = e.smtp_error.decode("utf-8", errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            return json.dumps(
                {
                    "status": "auth_error",
                    "error": error_msg,
                    "steps": steps,
                    "fix": (
                        "Ошибка авторизации SMTP. Нужен пароль для внешнего приложения Mail.ru. "
                        "Зайдите в mail.ru → Настройки → Безопасность → "
                        "Пароли для внешних приложений → Создать → "
                        "Тип: 'Полный доступ к Почте'. "
                        "Затем обновите MAIL_PASSWORD в Railway."
                    ),
                },
                ensure_ascii=False, indent=2,
            )
        except Exception as e:
            steps.append(f"   ОШИБКА: {type(e).__name__}: {e}")
            return json.dumps(
                {"status": "error", "error": str(e), "steps": steps},
                ensure_ascii=False, indent=2,
            )
