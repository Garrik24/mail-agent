# Mail Agent

Два сервиса для работы с почтой Mail.ru:
1. **MCP сервер** — подключение к Claude.ai для управления почтой через чат
2. **Telegram нотификатор** — автоматические уведомления о важных письмах

## Структура

```
mail-agent/
├── mcp-server/           # MCP сервер для Claude.ai
│   ├── main.py           # FastMCP + Streamable HTTP
│   ├── imap_client.py    # IMAP/SMTP клиент
│   ├── tools.py          # 7 MCP инструментов
│   ├── requirements.txt
│   └── railway.toml
├── telegram-notifier/    # Фоновый воркер
│   ├── main.py           # Цикл проверки каждые 15 мин
│   ├── imap_client.py    # IMAP клиент
│   ├── classifier.py     # Классификация писем
│   ├── notifier.py       # Claude анализ + Telegram
│   ├── db.py             # SQLite дедупликация
│   ├── requirements.txt
│   └── railway.toml
└── README.md
```

## Деплой на Railway

### 1. Создать GitHub репозиторий

```bash
cd mail-agent
git init
git add .
git commit -m "Initial commit: Mail MCP + Telegram notifier"
git remote add origin https://github.com/YOUR_USERNAME/mail-agent.git
git push -u origin main
```

### 2. Создать сервисы в Railway

В Railway Dashboard → New Project → Deploy from GitHub repo.

**Сервис 1: mail-mcp-server**
- Root Directory: `mcp-server`
- ENV переменные:
  ```
  MAIL_IMAP_HOST=imap.mail.ru
  MAIL_IMAP_PORT=993
  MAIL_SMTP_HOST=smtp.mail.ru
  MAIL_SMTP_PORT=465
  MAIL_USERNAME=stavgeo26@mail.ru
  MAIL_PASSWORD=<пароль приложения>
  PORT=8000
  ```
- После деплоя: Settings → Networking → Generate Domain
- URL для Claude.ai: `https://<domain>.up.railway.app/mcp`

**Сервис 2: telegram-notifier**
- Root Directory: `telegram-notifier`
- ENV переменные:
  ```
  MAIL_IMAP_HOST=imap.mail.ru
  MAIL_IMAP_PORT=993
  MAIL_USERNAME=stavgeo26@mail.ru
  MAIL_PASSWORD=<пароль приложения>
  TELEGRAM_BOT_TOKEN=<токен бота>
  TELEGRAM_CHAT_ID=<ваш chat id>
  ANTHROPIC_API_KEY=<ключ API>
  CHECK_INTERVAL=900
  ```

### 3. Подключить MCP к Claude.ai

В Claude.ai → Settings → MCP Servers → Add:
- URL: `https://<domain>.up.railway.app/mcp`

## MCP инструменты

| Инструмент | Описание |
|-----------|----------|
| `get_new_emails` | Письма за последние N часов |
| `get_important_emails` | Письма с флагом Important |
| `search_emails` | Поиск по тексту/отправителю/дате |
| `get_email_body` | Полное содержимое письма |
| `analyze_email` | Структурированные данные для анализа |
| `send_reply` | Ответить на письмо через SMTP |
| `get_folders` | Список папок почтового ящика |

## Классификация писем (Telegram)

| Категория | Критерий |
|-----------|---------|
| 🔴 СРОЧНО | Госорганы: росреестр, налоговая, суд и др. |
| 🟡 ВАЖНО | Финансы: договоры, счета, суммы в рублях |
| 🟢 ЛИД | КП, запросы стоимости, тендеры |
| 🟡 ВАЖНО | Флаг Important на письме |
| ⏭ Пропуск | Всё остальное |
