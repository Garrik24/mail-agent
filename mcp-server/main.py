"""
MCP сервер для Mail.ru почты.
Подключается к Claude.ai через Streamable HTTP транспорт.
"""

import contextlib
import logging
import os
import uvicorn
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

port = int(os.environ.get("PORT", "8000"))

# Создаём MCP сервер
mcp = FastMCP(
    "Mail.ru MCP Server",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    port=port,
)

# Регистрируем инструменты
from tools import register_tools
register_tools(mcp)

from mail_tools_patch import register_tools as register_mail_tools_patch
register_mail_tools_patch(mcp)

from mail_read_tools import register_tools as register_mail_read_tools
register_mail_read_tools(mcp)


async def health_response(scope, receive, send):
    """Минимальный ASGI ответ для /health."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"status":"ok"}',
    })


async def preview_response(scope, receive, send):
    """Отдаёт PDF предпросмотра письма (inline в браузере) по токену.

    Путь: /preview/<token>.pdf — токен выдаётся инструментом preview_letter.
    Ничего не отправляет; только показывает уже отрендеренный бланк.
    """
    import preview_store
    from urllib.parse import quote

    token = scope.get("path", "")[len("/preview/"):]
    if token.endswith(".pdf"):
        token = token[:-4]

    item = preview_store.load_preview(token)
    if not item:
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [[b"content-type", b"text/plain; charset=utf-8"]],
        })
        await send({
            "type": "http.response.body",
            "body": "Предпросмотр не найден или устарел (ссылка живёт ~1 час).".encode("utf-8"),
        })
        return

    pdf, filename = item
    disposition = f"inline; filename*=UTF-8''{quote(filename)}".encode("ascii")
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"application/pdf"],
            [b"content-disposition", disposition],
            [b"cache-control", b"no-store"],
        ],
    })
    await send({"type": "http.response.body", "body": pdf})


# Получаем MCP ASGI app
mcp_asgi = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(scope):
    async with mcp.session_manager.run():
        yield


async def app(scope, receive, send):
    """ASGI app: /health -> health, всё остальное -> MCP."""
    if scope["type"] == "lifespan":
        async with mcp.session_manager.run():
            # Обрабатываем lifespan events
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
    elif scope["type"] == "http" and scope.get("path") == "/health":
        await health_response(scope, receive, send)
    elif scope["type"] == "http" and scope.get("path", "").startswith("/preview/"):
        await preview_response(scope, receive, send)
    else:
        await mcp_asgi(scope, receive, send)


if __name__ == "__main__":
    log.info(f"Запуск Mail MCP сервера на порту {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
