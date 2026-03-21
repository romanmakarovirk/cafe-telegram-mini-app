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
    "config", "models", "menu_data", "database", "security",
    "serializers", "bot_setup", "bot_handlers", "workers", "routes",
]
for _mod_name in _SUBMODULES:
    if _mod_name in sys.modules:
        importlib.reload(sys.modules[_mod_name])

# ── Ре-экспорт всего для совместимости с тестами ─────────────────────────
from config import *  # noqa: F401,F403
from models import *  # noqa: F401,F403
from menu_data import *  # noqa: F401,F403
from database import *  # noqa: F401,F403
from security import *  # noqa: F401,F403
from serializers import *  # noqa: F401,F403
from bot_setup import bot, bot_polling_task, dispatcher, router, ADMIN_CHAT_ID  # noqa: F401
from bot_handlers import *  # noqa: F401,F403
from workers import *  # noqa: F401,F403
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
    _fiscal_retry_worker,
    _sbp_payment_polling_worker,
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


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    validate_production_config()

    # Startup self-test: проверяем что ATOL credentials валидны
    if not DEV_MODE:
        try:
            from payments.fiscal import atol_client
            if atol_client.is_configured:
                token = await atol_client._get_token()
                if token:
                    logging.info("ATOL startup check: OK (token получен)")
                else:
                    logging.error("ATOL startup check: FAILED (не удалось получить токен)")
        except Exception as e:
            logging.error("ATOL startup check error: %s", e)

    # Фоновые задачи
    stoplist_task = asyncio.create_task(_stoplist_auto_enable_worker())
    timeout_task = asyncio.create_task(_order_timeout_worker())
    fiscal_retry_task = asyncio.create_task(_fiscal_retry_worker())
    sbp_polling_task = asyncio.create_task(_sbp_payment_polling_worker())
    logging.info("Background workers started: stoplist auto-enable, order timeout, fiscal retry, sbp polling")

    # Keep-alive self-ping (only when deployed with a real URL)
    keep_alive_task = None
    if APP_BASE_URL and not APP_BASE_URL.startswith("http://127.0.0.1"):
        keep_alive_task = asyncio.create_task(_keep_alive_ping())

    if BOT_TOKEN:
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
    else:
        logging.warning("BOT_TOKEN is empty. FastAPI will run without Telegram bot.")

    # Bot polling watchdog — restarts polling if it crashes
    watchdog_task = None
    if BOT_TOKEN:
        async def _bot_polling_watchdog():
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
        watchdog_task = asyncio.create_task(_bot_polling_watchdog())

    try:
        yield
    finally:
        for task in (stoplist_task, timeout_task, fiscal_retry_task, sbp_polling_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if keep_alive_task is not None:
            keep_alive_task.cancel()
            with suppress(asyncio.CancelledError):
                await keep_alive_task
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
        from payments.fiscal import atol_client
        from payments.sbp import sbp_client
        await atol_client.close()
        await sbp_client.close()


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
