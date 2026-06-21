#!/usr/bin/env python3
"""Локальный визуальный превью WebApp — без БД, бота и Telegram.

Запуск:  python preview.py   →  открой http://localhost:8090

Отдаёт index.html на `/` и `webapp/static/*` (чтобы абсолютные пути /static/... грузились).
Запросы к /api/* отдают 404 — приложение показывает пустое/дефолтное состояние, но это
достаточно, чтобы оценить вёрстку, палитру и типографику. Telegram-SDK грузится с
telegram.org и работает заглушкой вне Telegram.
"""
import os
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

WEBAPP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
PORT = 8090


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split("?")[0].split("#")[0]
        if p in ("/", "/index.html"):
            return os.path.join(WEBAPP, "templates", "index.html")
        return super().translate_path(path)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    handler = functools.partial(Handler, directory=WEBAPP)
    print(f"Preview: http://localhost:{PORT}  (Ctrl+C — остановить)")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
