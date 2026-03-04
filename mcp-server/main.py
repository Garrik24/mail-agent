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
    else:
        await mcp_asgi(scope, receive, send)


if __name__ == "__main__":
    log.info(f"Запуск Mail MCP сервера на порту {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
