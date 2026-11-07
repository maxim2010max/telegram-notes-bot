# 🌐 Маленький сервер для мини-приложения
# Отдаёт файлы из папки webapp/ по адресу http://localhost:8080
#
# Запуск:  python webapp_server.py
# Порт можно поменять:  set PORT=9000  (Windows CMD)

import os
import sys

from aiohttp import web

# Чтобы emoji в print не роняли сервер, если вывод перенаправлен (Windows, cp1251)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Папка, где лежат index.html, style.css, app.js
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


async def index(request):
    """Главная страница мини-приложения."""
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


@web.middleware
async def no_store(request, handler):
    """Запрещаем кэш: во время разработки правки видны сразу."""
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app():
    app = web.Application(middlewares=[no_store])
    app.router.add_get("/", index)
    # Всё остальное (style.css, app.js) — обычная статика из webapp/
    app.router.add_static("/", WEB_DIR, show_index=False)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    print("🌐 Мини-приложение запущено:")
    print(f"   http://localhost:{port}")
    print("   Для остановки — Ctrl+C")
    web.run_app(create_app(), host="0.0.0.0", port=port)
