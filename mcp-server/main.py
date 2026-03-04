"""
MCP сервер для Mail.ru почты.
Подключается к Claude.ai через Streamable HTTP транспорт.
"""

import contextlib
import logging
import os
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Создаём MCP сервер
mcp = FastMCP(
    "Mail.ru MCP Server",
    stateless_http=True,
    json_response=True,
)

# Регистрируем инструменты
from tools import register_tools
register_tools(mcp)


async def health(request):
    return JSONResponse({"status": "ok"})


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield


# ASGI приложение (используется и при прямом запуске, и через uvicorn)
app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    log.info(f"Запуск Mail MCP сервера на порту {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
