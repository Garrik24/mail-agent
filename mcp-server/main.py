"""
MCP сервер для Mail.ru почты.
Подключается к Claude.ai через Streamable HTTP транспорт.
"""

import logging
import os
from mcp.server.fastmcp import FastMCP

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    log.info(f"Запуск Mail MCP сервера на порту {port}")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
