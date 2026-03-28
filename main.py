"""
Кафе «Шашлык и Плов» — Telegram Mini App.

Оркестратор: создаёт FastAPI-приложение, подключает роутеры, lifespan, middleware.
Ре-экспортирует всё для обратной совместимости с тестами (import main as m).
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, func

# ── Каскадный reload всех подмодулей (для importlib.reload(main) в тестах) ──
# При reload main.py заново исполняет from X import *, но X уже в sys.modules
# со старым состоянием. Нужно сначала reload все подмодули.
_SUBMODULES = [
    "statuses", "config", "models", "menu_data", "database", "security",
    "serializers", "metrics", "bot_setup", "bot_handlers", "services", "workers",
    "routes_middleware", "routes_payment", "routes_kitchen", "routes",
]
for _mod_name in _SUBMODULES:
    if _mod_name in sys.modules:
        importlib.reload(sys.modules[_mod_name])

# ── Ре-экспорт всего для совместимости с тестами ─────────────────────────
from statuses import *  # noqa: F401,F403
from config import *  # noqa: F401,F403
from models import *  # noqa: F401,F403
from menu_data import *  # noqa: F401,F403
from database import *  # noqa: F401,F403
from security import *  # noqa: F401,F403
from serializers import *  # noqa: F401,F403
from metrics import *  # noqa: F401,F403
from bot_setup import bot, bot_polling_task, dispatcher, router, ADMIN_CHAT_ID  # noqa: F401
from bot_handlers import *  # noqa: F401,F403
from services import *  # noqa: F401,F403
from workers import *  # noqa: F401,F403
from routes_payment import *  # noqa: F401,F403
from routes_kitchen import *  # noqa: F401,F403
from routes import (  # noqa: F401
    router as api_router,
    SecurityHeadersMiddleware,
    RequestIdMiddleware,
    ExceptionMiddleware,
    _process_paid_order,
    notify_admin_about_order,
)

# Explicit re-imports for names that tests rely on
from config import (
    APP_BASE_URL,
    BOT_TOKEN,
    DEV_MODE,
    WEBAPP_URL,
    validate_production_config,
)
from database import (
    engine,
    SessionLocal,
    db_session,
    initialize_database,
    seed_menu_items,
)
from bot_handlers import configure_bot_entrypoints
from workers import (
    _keep_alive_ping,
    _stoplist_auto_enable_worker,
    _order_timeout_worker,
    _yookassa_payment_polling_worker,
)

import database as _database_module
import bot_setup as _bot_setup_module


# ── __setattr__ hook: проброс патчей из тестов в модули ───────────────────
def __setattr__(name: str, value: Any) -> None:
    """Propagate test patches (m.db_session = X, m.SessionLocal = X) to modules."""
    if name in ("db_session", "SessionLocal", "engine", "get_cafe_schedule",
                 "now_utc", "fetch_order", "seed_menu_items", "initialize_database",
                 "next_public_order_number", "rub"):
        setattr(_database_module, name, value)
    if name in ("bot", "bot_polling_task", "ADMIN_CHAT_ID"):
        setattr(_bot_setup_module, name, value)
    # Also set on this module's namespace
    globals()[name] = value


# ── Lifespan helpers ──────────────────────────────────────────────────────

async def _startup_yookassa_check() -> None:
    """Startup self-test: проверяем что ЮKassa credentials валидны."""
    try:
        from payments.yookassa_payment import yookassa_client
        if yookassa_client.is_configured:
            logging.info("YooKassa startup check: OK (credentials настроены)")
        else:
            logging.warning("YooKassa startup check: credentials не настроены")
    except Exception as e:
        logging.error("YooKassa startup check error: %s", e)


def _start_background_workers() -> list[asyncio.Task]:
    """Запуск фоновых воркеров. Возвращает список задач."""
    worker_defs = [
        ("stoplist_worker", _stoplist_auto_enable_worker),
        ("timeout_worker", _order_timeout_worker),
        ("yookassa_polling", _yookassa_payment_polling_worker),
    ]
    if APP_BASE_URL and not APP_BASE_URL.startswith("http://127.0.0.1"):
        worker_defs.append(("keepalive", _keep_alive_ping))

    tasks = []
    for name, factory in worker_defs:
        _WORKER_FACTORIES[name] = factory
        tasks.append(asyncio.create_task(factory(), name=name))

    logging.info("Background workers started: %s", ", ".join(_WORKER_FACTORIES.keys()))
    return tasks


async def _start_bot_polling() -> None:
    """Инициализация бота и запуск polling."""
    _bot_setup_module.bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await configure_bot_entrypoints()
    _bot_setup_module.bot_polling_task = asyncio.create_task(
        dispatcher.start_polling(
            _bot_setup_module.bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    )
    logging.info("Bot polling started.")


async def _bot_polling_watchdog() -> None:
    """Watchdog: перезапускает polling бота при крэше."""
    while True:
        await asyncio.sleep(30)
        task = _bot_setup_module.bot_polling_task
        if task and task.done() and not task.cancelled():
            exc = task.exception() if not task.cancelled() else None
            logging.critical("Bot polling crashed: %s. Restarting...", exc)
            from bot_handlers import alert_admin
            await alert_admin(f"Bot polling упал: {exc}. Перезапускаю...")
            _bot_setup_module.bot_polling_task = asyncio.create_task(
                dispatcher.start_polling(
                    _bot_setup_module.bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                )
            )


# Worker name → factory function mapping for restart
_WORKER_FACTORIES: dict[str, Any] = {}


async def _workers_watchdog(tasks: list[asyncio.Task]) -> None:
    """Watchdog: перезапускает упавшие background workers."""
    while True:
        await asyncio.sleep(30)
        for i, task in enumerate(tasks):
            if task.done() and not task.cancelled():
                exc = task.exception() if not task.cancelled() else None
                worker_name = task.get_name()
                logging.critical("Worker %s crashed: %s. Restarting...", worker_name, exc)
                factory = _WORKER_FACTORIES.get(worker_name)
                if factory:
                    tasks[i] = asyncio.create_task(factory(), name=worker_name)
                    try:
                        from bot_handlers import alert_admin
                        await alert_admin(f"Worker {worker_name} упал: {exc}. Перезапущен.")
                    except Exception:
                        logging.exception("Failed to alert admin about worker restart")


async def _shutdown(tasks: list[asyncio.Task], watchdog_task: asyncio.Task | None) -> None:
    """Graceful shutdown: отмена воркеров, бота, закрытие HTTP-клиентов."""
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if watchdog_task is not None:
        watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog_task
    if _bot_setup_module.bot_polling_task is not None:
        _bot_setup_module.bot_polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bot_setup_module.bot_polling_task
    if _bot_setup_module.bot is not None:
        await _bot_setup_module.bot.session.close()
    from payments.yookassa_payment import yookassa_client
    await yookassa_client.close()


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    validate_production_config()

    if not DEV_MODE:
        await _startup_yookassa_check()

    worker_tasks = _start_background_workers()

    if BOT_TOKEN:
        await _start_bot_polling()
    else:
        logging.warning("BOT_TOKEN is empty. FastAPI will run without Telegram bot.")

    watchdog_task = None
    if BOT_TOKEN:
        watchdog_task = asyncio.create_task(_bot_polling_watchdog())

    workers_watchdog_task = asyncio.create_task(_workers_watchdog(worker_tasks))

    try:
        yield
    finally:
        workers_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await workers_watchdog_task
        await _shutdown(worker_tasks, watchdog_task)


# ── FastAPI app ───────────────────────────────────────────────────────────
app = FastAPI(
    title="Cafe Telegram Mini App",
    version="0.2.0",
    lifespan=lifespan,
)

_cors_origins = list(filter(None, [
    APP_BASE_URL,
    WEBAPP_URL if WEBAPP_URL != APP_BASE_URL else None,
]))
_extra_cors = os.getenv("EXTRA_CORS_ORIGINS", "").strip()
if _extra_cors:
    for _origin in _extra_cors.split(","):
        _origin = _origin.strip()
        if _origin and _origin not in _cors_origins:
            if _origin.startswith("https://") and " " not in _origin and "*" not in _origin:
                _cors_origins.append(_origin)
            else:
                logging.warning("EXTRA_CORS_ORIGINS: пропуск невалидного origin %r", _origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)

# Middleware order (last added = first executed):
# 1. RequestIdMiddleware → assigns X-Request-Id
# 2. ExceptionMiddleware → catches unhandled errors with request_id
# 3. SecurityHeadersMiddleware → adds security headers
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ExceptionMiddleware)
app.add_middleware(RequestIdMiddleware)
app.include_router(api_router)
