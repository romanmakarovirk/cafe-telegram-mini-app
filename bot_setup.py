from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, Router

from config import INITIAL_ADMIN_CHAT_ID

bot: Bot | None = None
bot_polling_task: asyncio.Task[None] | None = None
dispatcher = Dispatcher()
router = Router()
dispatcher.include_router(router)
ADMIN_CHAT_ID: int | None = INITIAL_ADMIN_CHAT_ID
