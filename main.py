from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as json_module
import logging
import os
import time as time_module
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import unquote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from fastapi import FastAPI, HTTPException, Path as FastPath, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", APP_BASE_URL).rstrip("/")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}").strip()
INITIAL_ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0) or None

# ── Security settings ─────────────────────────────────────────────────────
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")

ALLOWED_ADMIN_IDS: set[int] = set()
_raw_admin_ids = os.getenv("ALLOWED_ADMIN_IDS", "").strip()
if _raw_admin_ids:
    for _aid in _raw_admin_ids.split(","):
        _aid = _aid.strip()
        if _aid:
            with suppress(ValueError):
                ALLOWED_ADMIN_IDS.add(int(_aid))
if INITIAL_ADMIN_CHAT_ID:
    ALLOWED_ADMIN_IDS.add(INITIAL_ADMIN_CHAT_ID)

MAX_ITEMS_PER_ORDER = int(os.getenv("MAX_ITEMS_PER_ORDER", "100"))
MAX_ORDER_TOTAL_RUB = int(os.getenv("MAX_ORDER_TOTAL_RUB", "50000"))
ORDER_PAYMENT_TIMEOUT_MINUTES = int(os.getenv("ORDER_PAYMENT_TIMEOUT_MINUTES", "15"))


def normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://") and "+psycopg" not in raw_url:
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


SQLALCHEMY_DATABASE_URL = normalize_database_url(DATABASE_URL)
ENGINE_KWARGS: dict[str, Any] = {"future": True, "pool_pre_ping": True}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    ENGINE_KWARGS["connect_args"] = {"check_same_thread": False}

engine = create_engine(SQLALCHEMY_DATABASE_URL, **ENGINE_KWARGS)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(Integer)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    unavailable_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    available_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_order_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    total_amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), index=True)
    payment_status: Mapped[str] = mapped_column(String(30), index=True)
    payment_mode: Mapped[str] = mapped_column(String(30))
    cashier_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gateway_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    fiscal_uuid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    kitchen_printed: Mapped[bool] = mapped_column(Boolean, default=False)
    accounting_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    accounting_doc_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    customer_comment: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.id",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    name_snapshot: Mapped[str] = mapped_column(String(255))
    price_snapshot: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    subtotal: Mapped[int] = mapped_column(Integer)
    order: Mapped[Order] = relationship(back_populates="items")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FiscalQueue(Base):
    __tablename__ = "fiscal_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_number: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(20))  # "sell" or "sell_refund"
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=10)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fiscal_uuid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


CATEGORY_META = [
    {
        "slug": "grill",
        "title": "Шашлык",
        "subtitle": "Мясо на углях, люля и быстрые горячие закуски.",
        "note": "К блюдам подаётся лаваш и овощной салат — бесплатно",
        "colors": ("#4C2A1B", "#A65D35"),
    },
    {
        "slug": "first_courses",
        "title": "Первые блюда",
        "subtitle": "Супы и наваристые блюда для плотного обеда.",
        "colors": ("#6A4A2C", "#C38A44"),
    },
    {
        "slug": "second_courses",
        "title": "Вторые блюда",
        "subtitle": "Основные блюда кухни кафе для самовывоза.",
        "colors": ("#31473A", "#65806A"),
    },
    {
        "slug": "samsa",
        "title": "Самса",
        "subtitle": "Печёная самса разных видов из теста и мяса.",
        "colors": ("#6F3C2C", "#C5714F"),
    },
    {
        "slug": "salads",
        "title": "Салаты",
        "subtitle": "Классические салаты и холодные закуски.",
        "colors": ("#355C4A", "#77A089"),
    },
    {
        "slug": "drinks",
        "title": "Напитки",
        "subtitle": "Компоты и чай из основного меню.",
        "colors": ("#305A6D", "#5F9CB8"),
    },
    {
        "slug": "coffee_tea",
        "title": "Кофе и чай",
        "subtitle": "Кофейная карта и фирменные горячие напитки.",
        "colors": ("#493628", "#B08968"),
    },
]
CATEGORY_BY_SLUG = {entry["slug"]: entry for entry in CATEGORY_META}
CATEGORY_ORDER = {entry["slug"]: index for index, entry in enumerate(CATEGORY_META)}

MENU_SEED: list[dict[str, Any]] = [
    {
        "id": 1,
        "category": "grill",
        "name": "Шашлык из говядины",
        "description": "Порция 200 г. Подаётся как горячее блюдо для самовывоза.",
        "price": 600,
        "sort_order": 1,
    },
    {
        "id": 2,
        "category": "grill",
        "name": "Шашлык из баранины",
        "description": "Порция 200 г. Насыщенный вкус и плотная мясная подача.",
        "price": 600,
        "sort_order": 2,
    },
    {
        "id": 3,
        "category": "grill",
        "name": "Люля-кебаб",
        "description": "Порция 200 г. Классический люля для быстрого заказа.",
        "price": 600,
        "sort_order": 3,
    },
    {
        "id": 4,
        "category": "grill",
        "name": "Шашлык из печени бараньей",
        "description": "Порция 200 г. Горячая мясная позиция из меню кафе.",
        "price": 550,
        "sort_order": 4,
    },
    {
        "id": 5,
        "category": "grill",
        "name": "Шашлык из куриных крылышек",
        "description": "Порция 300 г. Подходит для компании или плотного перекуса.",
        "price": 600,
        "sort_order": 5,
    },
    {
        "id": 6,
        "category": "grill",
        "name": "Шашлык из куриной грудки",
        "description": "Порция 200 г. Более лёгкий вариант шашлыка.",
        "price": 600,
        "sort_order": 6,
    },
    {
        "id": 7,
        "category": "grill",
        "name": "Ролл-шашлык",
        "description": "Порция 400 г. Удобная позиция для быстрого заказа навынос.",
        "price": 300,
        "sort_order": 7,
    },
    {
        "id": 8,
        "category": "grill",
        "name": "Картофель фри",
        "description": "Порция 100 г. Хорошо дополняет мясные блюда.",
        "price": 150,
        "sort_order": 8,
    },
    {
        "id": 9,
        "category": "first_courses",
        "name": "Шурпа",
        "description": "Горячее первое блюдо для сытного обеда.",
        "price": 400,
        "sort_order": 1,
    },
    {
        "id": 10,
        "category": "first_courses",
        "name": "Мастава",
        "description": "Домашний суп из основного меню кафе.",
        "price": 400,
        "sort_order": 2,
    },
    {
        "id": 11,
        "category": "first_courses",
        "name": "Суп из чечевицы",
        "description": "Лёгкий суп на каждый день.",
        "price": 230,
        "sort_order": 3,
    },
    {
        "id": 12,
        "category": "first_courses",
        "name": "Борщ",
        "description": "Классический горячий борщ из меню кафе.",
        "price": 300,
        "sort_order": 4,
    },
    {
        "id": 13,
        "category": "first_courses",
        "name": "Мясо по-казахски",
        "description": "Сытное блюдо, размещённое в разделе первых блюд.",
        "price": 350,
        "sort_order": 5,
    },
    {
        "id": 14,
        "category": "first_courses",
        "name": "Лагман",
        "description": "Горячий лагман для полноценного обеда.",
        "price": 380,
        "sort_order": 6,
    },
    {
        "id": 15,
        "category": "second_courses",
        "name": "Микс (плов и шашлык)",
        "description": "Комбинация двух популярных позиций в одном заказе.",
        "price": 650,
        "sort_order": 1,
    },
    {
        "id": 16,
        "category": "second_courses",
        "name": "Плов",
        "description": "Основное блюдо с рисом и мясом.",
        "price": 350,
        "sort_order": 2,
    },
    {
        "id": 17,
        "category": "second_courses",
        "name": "Манты",
        "description": "Порция 5 шт. Горячее блюдо для заказа навынос.",
        "price": 350,
        "sort_order": 3,
    },
    {
        "id": 18,
        "category": "second_courses",
        "name": "Жаровня",
        "description": "Мясо и овощи, порция 250 г.",
        "price": 600,
        "sort_order": 4,
    },
    {
        "id": 19,
        "category": "second_courses",
        "name": "Мясо по-французски",
        "description": "Горячее основное блюдо для сытного заказа.",
        "price": 600,
        "sort_order": 5,
    },
    {
        "id": 42,
        "category": "second_courses",
        "name": "Лепёшка 100 г",
        "description": "Свежая лепёшка, большая порция.",
        "price": 100,
        "sort_order": 6,
    },
    {
        "id": 43,
        "category": "second_courses",
        "name": "Лепёшка 50 г",
        "description": "Свежая лепёшка, средняя порция.",
        "price": 50,
        "sort_order": 7,
    },
    {
        "id": 44,
        "category": "second_courses",
        "name": "Лепёшка 25 г",
        "description": "Свежая лепёшка, малая порция.",
        "price": 25,
        "sort_order": 8,
    },
    {
        "id": 20,
        "category": "samsa",
        "name": "Самса из курицы",
        "description": "Порция 200 г. Тёплая выпечка для быстрого перекуса.",
        "price": 150,
        "sort_order": 1,
    },
    {
        "id": 21,
        "category": "samsa",
        "name": "Самса из баранины",
        "description": "Порция 185 г. Более насыщенный мясной вариант.",
        "price": 200,
        "sort_order": 2,
    },
    {
        "id": 22,
        "category": "samsa",
        "name": "Самса из говядины",
        "description": "Порция 185 г. Классическая мясная самса.",
        "price": 150,
        "sort_order": 3,
    },
    {
        "id": 23,
        "category": "samsa",
        "name": "Самса из говядины с картофелем",
        "description": "Порция 185 г. Более мягкий вкус за счёт картофеля.",
        "price": 140,
        "sort_order": 4,
    },
    {
        "id": 24,
        "category": "samsa",
        "name": "Мини-самса с говядиной",
        "description": "Порция 4 шт. Удобный формат для компании.",
        "price": 180,
        "sort_order": 5,
    },
    {
        "id": 25,
        "category": "salads",
        "name": "Ассорти из солёных овощей",
        "description": "Холодная закуска к горячим блюдам.",
        "price": 200,
        "sort_order": 1,
    },
    {
        "id": 26,
        "category": "salads",
        "name": "Салат овощной",
        "description": "Свежий овощной салат из повседневного меню.",
        "price": 200,
        "sort_order": 2,
    },
    {
        "id": 27,
        "category": "salads",
        "name": "Салат Оливье",
        "description": "Классическая салатная позиция.",
        "price": 200,
        "sort_order": 3,
    },
    {
        "id": 28,
        "category": "salads",
        "name": "Язык салат",
        "description": "Более сытный салат из холодных блюд.",
        "price": 280,
        "sort_order": 4,
    },
    {
        "id": 29,
        "category": "drinks",
        "name": "Компот",
        "description": "300 мл. Лёгкий напиток к основному заказу.",
        "price": 50,
        "sort_order": 1,
    },
    {
        "id": 30,
        "category": "drinks",
        "name": "Компот сливовый",
        "description": "300 мл. Более насыщенный вкус компота.",
        "price": 70,
        "sort_order": 2,
    },
    {
        "id": 31,
        "category": "drinks",
        "name": "Чай чайник",
        "description": "Чайник на компанию.",
        "price": 200,
        "sort_order": 3,
    },
    {
        "id": 32,
        "category": "drinks",
        "name": "Чай с лимоном",
        "description": "200 мл. Быстрый горячий напиток.",
        "price": 25,
        "sort_order": 4,
    },
    {
        "id": 33,
        "category": "coffee_tea",
        "name": "Американо",
        "description": "200 мл. Базовый кофе из меню напитков.",
        "price": 150,
        "sort_order": 1,
    },
    {
        "id": 34,
        "category": "coffee_tea",
        "name": "Горячий шоколад",
        "description": "300 мл. Плотный сладкий горячий напиток.",
        "price": 200,
        "sort_order": 2,
    },
    {
        "id": 35,
        "category": "coffee_tea",
        "name": "Капучино 300 мл",
        "description": "Большой стакан капучино.",
        "price": 230,
        "sort_order": 3,
    },
    {
        "id": 36,
        "category": "coffee_tea",
        "name": "Капучино 200 мл",
        "description": "Стандартная порция капучино.",
        "price": 180,
        "sort_order": 4,
    },
    {
        "id": 37,
        "category": "coffee_tea",
        "name": "Латте",
        "description": "300 мл. Мягкий молочный кофе.",
        "price": 220,
        "sort_order": 5,
    },
    {
        "id": 38,
        "category": "coffee_tea",
        "name": "Раф",
        "description": "300 мл. Более сливочный кофейный напиток.",
        "price": 250,
        "sort_order": 6,
    },
    {
        "id": 39,
        "category": "coffee_tea",
        "name": "Флэт уайт",
        "description": "200 мл. Крепкий кофейный напиток с молоком.",
        "price": 220,
        "sort_order": 7,
    },
    {
        "id": 40,
        "category": "coffee_tea",
        "name": "Чай восточный",
        "description": "500 мл. Чайник фирменного чая.",
        "price": 300,
        "sort_order": 8,
    },
    {
        "id": 41,
        "category": "coffee_tea",
        "name": "Чай сибирский",
        "description": "500 мл. Чайник авторского чая.",
        "price": 300,
        "sort_order": 9,
    },
]

VARIANT_GROUPS: dict[str, dict[str, Any]] = {
    "lepeshka": {
        "name": "Лепёшка",
        "description": "Свежая лепёшка. Выберите размер.",
        "item_ids": [42, 43, 44],
        "labels": {42: "100 г", 43: "50 г", 44: "25 г"},
    },
}
ITEM_TO_VARIANT_GROUP: dict[int, str] = {}
for _group_key, _group_data in VARIANT_GROUPS.items():
    for _item_id in _group_data["item_ids"]:
        ITEM_TO_VARIANT_GROUP[_item_id] = _group_key

bot: Bot | None = None
bot_polling_task: asyncio.Task[None] | None = None
dispatcher = Dispatcher()
router = Router()
dispatcher.include_router(router)
ADMIN_CHAT_ID: int | None = INITIAL_ADMIN_CHAT_ID


# ---------------------------------------------------------------------------
#  Telegram InitData verification (HMAC-SHA256)
# ---------------------------------------------------------------------------
def verify_telegram_init_data(init_data: str, bot_token: str) -> tuple[dict | None, str]:
    """Verify Telegram WebApp initData signature.

    Returns (user_dict, "") on success or (None, reason) on failure.
    """
    parsed: dict[str, str] = {}
    for part in init_data.split("&"):
        key, _, value = part.partition("=")
        if key:
            parsed[key] = value

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None, "no_hash"

    # Check auth_date freshness (max 24 hours)
    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            age = time_module.time() - int(auth_date)
            if age > 86400:
                return None, f"auth_date_expired(age={int(age)}s)"
        except (ValueError, TypeError):
            return None, "auth_date_invalid"

    # Data-check-string: sorted key=value pairs joined by \n
    data_check_string = "\n".join(
        f"{k}={unquote(v)}" for k, v in sorted(parsed.items())
    )

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        logging.warning(
            "InitData HMAC mismatch: keys=%s, hash_len=%d",
            sorted(parsed.keys()), len(received_hash),
        )
        return None, "hmac_mismatch"

    user_raw = parsed.get("user")
    if user_raw:
        try:
            return json_module.loads(unquote(user_raw)), ""
        except (json_module.JSONDecodeError, TypeError):
            return None, "user_json_invalid"
    return None, "no_user_field"


def get_verified_user_id(request: Request) -> int:
    """Extract verified Telegram user ID from request headers."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if init_data and BOT_TOKEN:
        user, reason = verify_telegram_init_data(init_data, BOT_TOKEN)
        if user and user.get("id"):
            return int(user["id"])
        logging.warning("Auth failed: reason=%s, initData_len=%d, path=%s",
                        reason, len(init_data), request.url.path)
        raise HTTPException(status_code=401, detail=f"Invalid Telegram authorization ({reason}).")

    if not init_data:
        logging.warning("Auth failed: no X-Telegram-Init-Data header, path=%s", request.url.path)
    if not BOT_TOKEN and DEV_MODE:
        # Dev fallback: only when BOT_TOKEN is empty AND DEV_MODE=true
        user_id = request.query_params.get("dev_user_id")
        if user_id:
            try:
                return int(user_id)
            except (ValueError, TypeError):
                pass

    raise HTTPException(status_code=401, detail="Authorization required.")


def get_verified_user_info(request: Request) -> tuple[int, str]:
    """Extract verified Telegram user ID and first_name from request headers."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if init_data and BOT_TOKEN:
        user, reason = verify_telegram_init_data(init_data, BOT_TOKEN)
        if user and user.get("id"):
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")
            name = f"{first_name} {last_name}".strip() or f"User {user['id']}"
            # Санитизация: удаляем HTML-теги и null bytes из имени
            import re as _re
            name = _re.sub(r"<[^>]+>", "", name).replace("\x00", "").strip()[:200]
            if not name:
                name = f"User {user['id']}"
            return int(user["id"]), name
        logging.warning("Auth failed: reason=%s, initData_len=%d, path=%s",
                        reason, len(init_data), request.url.path)
        raise HTTPException(status_code=401, detail=f"Invalid Telegram authorization ({reason}).")

    if not BOT_TOKEN and DEV_MODE:
        user_id = request.query_params.get("dev_user_id")
        if user_id:
            try:
                return int(user_id), f"Dev User {user_id}"
            except (ValueError, TypeError):
                pass

    raise HTTPException(status_code=401, detail="Authorization required.")


# ---------------------------------------------------------------------------
#  Simple in-memory rate limiter
# ---------------------------------------------------------------------------
class SimpleRateLimiter:
    """In-memory rate limiter with automatic cleanup to prevent memory leaks."""

    MAX_KEYS = 10_000  # Максимум уникальных ключей (защита от DoS)

    def __init__(self, max_requests: int, window: int):
        self.max = max_requests
        self.window = window
        self.hits: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time_module.time()
        self._cleanup_interval = max(window * 2, 120)  # Очистка каждые 2 окна или 2 мин

    def _cleanup(self, now: float) -> None:
        """Удаляем ключи с пустыми или просроченными записями."""
        to_delete = [
            key for key, timestamps in self.hits.items()
            if not timestamps or timestamps[-1] < now - self.window
        ]
        for key in to_delete:
            del self.hits[key]
        self._last_cleanup = now

    def check(self, key: str) -> bool:
        now = time_module.time()

        # Периодическая очистка
        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup(now)

        # Защита от переполнения: если слишком много ключей, принудительная очистка
        if len(self.hits) > self.MAX_KEYS:
            self._cleanup(now)
            # Если после очистки всё ещё много — отклоняем новые ключи
            if len(self.hits) > self.MAX_KEYS and key not in self.hits:
                return False

        self.hits[key] = [t for t in self.hits[key] if now - t < self.window]
        if len(self.hits[key]) >= self.max:
            return False
        self.hits[key].append(now)
        return True


order_limiter = SimpleRateLimiter(max_requests=10, window=60)    # 10 orders/min per user
review_limiter = SimpleRateLimiter(max_requests=5, window=60)    # 5 reviews/min per user
general_limiter = SimpleRateLimiter(max_requests=60, window=60)  # 60 req/min per IP
callback_limiter = SimpleRateLimiter(max_requests=30, window=60)  # 30 callbacks/min per IP
sbp_check_limiter = SimpleRateLimiter(max_requests=20, window=60)  # 20 status checks/min per user


def verify_kitchen_api_key(request: Request) -> None:
    """Verify X-Kitchen-Key header. Fail-closed: rejects if key not configured."""
    kitchen_key = os.getenv("KITCHEN_API_KEY", "").strip()
    if not kitchen_key:
        raise HTTPException(
            status_code=403,
            detail="Kitchen API key not configured. Set KITCHEN_API_KEY env var.",
        )
    provided_key = request.headers.get("X-Kitchen-Key", "")
    if not provided_key or not hmac.compare_digest(provided_key, kitchen_key):
        raise HTTPException(status_code=403, detail="Invalid kitchen API key")


class CartItem(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=50)


class CreateOrderRequest(BaseModel):
    items: list[CartItem] = Field(min_length=1, max_length=20)
    comment: str = Field(default="", max_length=500)


class SubmitReviewRequest(BaseModel):
    order_id: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class StopListRequest(BaseModel):
    item_id: Optional[int] = None
    category: Optional[str] = None
    action: str = Field(pattern=r"^(disable|enable)$")
    reason: Optional[str] = Field(default=None, max_length=200)
    available_in_minutes: Optional[int] = Field(default=None, ge=5, le=480)


# ---------------------------------------------------------------------------
#  Working hours (Irkutsk UTC+8)
# ---------------------------------------------------------------------------
IRKUTSK_TZ = timezone(timedelta(hours=8))
OPEN_HOUR = 9
CLOSE_HOUR = 22
LAST_ORDER_MINUTES_BEFORE_CLOSE = 15


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_cafe_schedule() -> dict[str, Any]:
    """Return current open/closed status in Irkutsk time."""
    irkutsk_now = now_utc().astimezone(IRKUTSK_TZ)
    current_minutes = irkutsk_now.hour * 60 + irkutsk_now.minute
    open_minutes = OPEN_HOUR * 60
    close_minutes = CLOSE_HOUR * 60
    last_order_minutes = close_minutes - LAST_ORDER_MINUTES_BEFORE_CLOSE

    is_open = open_minutes <= current_minutes < last_order_minutes
    minutes_left = max(0, last_order_minutes - current_minutes) if is_open else 0

    return {
        "is_open": is_open,
        "is_closing_soon": is_open and minutes_left <= 30,
        "minutes_until_last_order": minutes_left,
        "opens_at": f"{OPEN_HOUR:02d}:00",
        "closes_at": f"{CLOSE_HOUR:02d}:00",
        "last_order_at": (
            datetime(2000, 1, 1, CLOSE_HOUR) - timedelta(minutes=LAST_ORDER_MINUTES_BEFORE_CLOSE)
        ).strftime("%H:%M"),
        "current_time_irkutsk": irkutsk_now.strftime("%H:%M"),
    }


def rub(amount: int) -> str:
    return f"{amount} руб."


def db_session() -> Session:
    return SessionLocal()


def load_setting(session: Session, key: str) -> str | None:
    setting = session.get(AppSetting, key)
    return setting.value if setting else None


def save_setting(session: Session, key: str, value: str) -> None:
    setting = session.get(AppSetting, key)
    if setting is None:
        session.add(AppSetting(key=key, value=value))
    else:
        setting.value = value
    session.commit()


def seed_menu_items(session: Session) -> None:
    for item in MENU_SEED:
        existing = session.get(MenuItem, item["id"])
        if existing is None:
            session.add(
                MenuItem(
                    id=item["id"],
                    category=item["category"],
                    name=item["name"],
                    description=item["description"],
                    price=item["price"],
                    image_url=f"/api/placeholders/{item['id']}.svg",
                    is_available=True,
                    sort_order=item["sort_order"],
                )
            )
            continue

        existing.category = item["category"]
        existing.name = item["name"]
        existing.description = item["description"]
        # НЕ перезаписываем цену — она могла быть изменена владельцем
        # existing.price = item["price"]
        existing.image_url = f"/api/placeholders/{item['id']}.svg"
        # НЕ сбрасываем is_available — стоп-лист сохраняется между рестартами
        existing.sort_order = item["sort_order"]
    session.commit()


def _migrate_sqlite_columns() -> None:
    """Add new nullable columns to existing SQLite tables (safe to call multiple times)."""
    if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return
    import sqlite3
    db_path = SQLALCHEMY_DATABASE_URL.replace("sqlite:///", "")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(menu_items)")}
        if "unavailable_reason" not in existing_cols:
            cursor.execute("ALTER TABLE menu_items ADD COLUMN unavailable_reason VARCHAR(200)")
            logging.info("Migration: added menu_items.unavailable_reason column")
        if "available_at" not in existing_cols:
            cursor.execute("ALTER TABLE menu_items ADD COLUMN available_at DATETIME")
            logging.info("Migration: added menu_items.available_at column")

        # Migrate orders table
        order_cols = {row[1] for row in cursor.execute("PRAGMA table_info(orders)")}
        if "customer_name" not in order_cols:
            cursor.execute("ALTER TABLE orders ADD COLUMN customer_name VARCHAR(200)")
            logging.info("Migration: added orders.customer_name column")
        if "customer_comment" not in order_cols:
            cursor.execute("ALTER TABLE orders ADD COLUMN customer_comment VARCHAR(500)")
            logging.info("Migration: added orders.customer_comment column")

        conn.commit()
        conn.close()
    except Exception:
        logging.exception("SQLite migration failed (non-critical)")


def initialize_database() -> None:
    global ADMIN_CHAT_ID

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()
    with db_session() as session:
        seed_menu_items(session)
        saved_admin_chat_id = load_setting(session, "admin_chat_id")
        if saved_admin_chat_id and not ADMIN_CHAT_ID:
            ADMIN_CHAT_ID = int(saved_admin_chat_id)


def _resolve_image_url(item: MenuItem) -> str:
    """Return photo URL if a real photo exists, else SVG placeholder."""
    if item.image_url and not item.image_url.startswith("/api/placeholders/"):
        return item.image_url
    # Check for local photo file (jpg/webp/png)
    for ext in ("jpg", "jpeg", "webp", "png"):
        photo_path = BASE_DIR / "photos" / f"{item.id}.{ext}"
        if photo_path.exists():
            return f"/api/photos/{item.id}.{ext}"
    return f"/api/placeholders/{item.id}.svg"


def _format_available_at(dt: Optional[datetime]) -> Optional[str]:
    """Формат времени доступности для клиента, напр. '~14:30'."""
    if dt is None:
        return None
    try:
        local_dt = dt.astimezone(IRKUTSK_TZ)
        return f"~{local_dt.strftime('%H:%M')}"
    except Exception:
        return None


def serialize_menu_item(item: MenuItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "category": item.category,
        "category_title": CATEGORY_BY_SLUG[item.category]["title"],
        "name": item.name,
        "description": item.description,
        "price": item.price,
        "image_url": _resolve_image_url(item),
        "is_available": item.is_available,
        "unavailable_reason": item.unavailable_reason if not item.is_available else None,
        "available_at_display": _format_available_at(item.available_at) if not item.is_available else None,
        "sort_order": item.sort_order,
    }


def serialize_order(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "public_order_number": order.public_order_number,
        "user_id": order.telegram_user_id,
        "customer_name": order.customer_name,
        "customer_comment": order.customer_comment,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_mode": order.payment_mode,
        "total": order.total_amount,
        "gateway_order_id": order.gateway_order_id,
        "accounting_synced": order.accounting_synced,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
        "items": [
            {
                "id": item.id,
                "menu_item_id": item.menu_item_id,
                "name": item.name_snapshot,
                "price": item.price_snapshot,
                "quantity": item.quantity,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ],
    }


def format_order_for_cashier(order: Order) -> str:
    item_lines = "\n".join(
        f"• {item.name_snapshot} x{item.quantity} = {rub(item.subtotal)}"
        for item in order.items
    )
    status_labels = {
        "created": "🆕 Создан",
        "paid": "💳 Оплачен",
        "preparing": "🟡 Готовится",
        "ready": "🟢 Готов",
    }
    customer_line = f"Клиент: <b>{escape(order.customer_name)}</b>" if order.customer_name else f"Клиент ID: <code>{order.telegram_user_id}</code>"
    comment_line = f"\n💬 <i>{escape(order.customer_comment)}</i>" if order.customer_comment else ""
    return (
        f"<b>Заказ №{order.public_order_number}</b>\n"
        f"Статус: <b>{status_labels.get(order.status, order.status)}</b>\n"
        f"Оплата: <b>СБП</b>\n"
        f"{customer_line}\n\n"
        f"{item_lines}{comment_line}\n\n"
        f"<b>Сумма:</b> {rub(order.total_amount)}"
    )


def build_cashier_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup | None:
    if status == "paid":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🟡 Готовится",
                        callback_data=f"order:preparing:{order_id}",
                    ),
                    InlineKeyboardButton(
                        text="🟢 Готов",
                        callback_data=f"order:ready:{order_id}",
                    ),
                ]
            ]
        )
    if status == "preparing":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🟢 Готов к выдаче",
                        callback_data=f"order:ready:{order_id}",
                    )
                ]
            ]
        )
    return None


def next_public_order_number(session: Session) -> int:
    current = session.scalar(select(func.max(Order.public_order_number)))
    if current is None:
        return 4648
    next_num = current + 1
    # Wrap после 99999 → начинаем с 10000 (5-значные номера)
    if next_num > 99999:
        next_num = 10000
    return next_num


def fetch_order(session: Session, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    _ = order.items
    return order


async def notify_customer(order: Order, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(chat_id=order.telegram_user_id, text=text)
    except Exception:
        logging.exception("Failed to notify customer %s", order.telegram_user_id)


async def notify_cashier_about_paid_order(order_id: int) -> None:
    if bot is None:
        logging.warning("Bot is not configured, cashier notification skipped.")
        return
    if ADMIN_CHAT_ID is None:
        logging.warning("ADMIN_CHAT_ID is not set, cashier notification skipped.")
        return

    with db_session() as session:
        order = fetch_order(session, order_id)
        try:
            sent_message = await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=format_order_for_cashier(order),
                reply_markup=build_cashier_keyboard(order.id, order.status),
            )
        except Exception:
            logging.exception("Failed to notify cashier about order %s", order.public_order_number)
            return
        order.cashier_message_id = sent_message.message_id
        order.updated_at = now_utc()
        session.commit()


async def configure_bot_entrypoints() -> None:
    if bot is None:
        return
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть меню"),
                BotCommand(command="admin", description="Назначить этот чат кассой"),
                BotCommand(command="stop", description="Отключить блюдо (стоп-лист)"),
                BotCommand(command="stoplist", description="Текущий стоп-лист"),
                BotCommand(command="stats", description="Статистика за сегодня"),
            ]
        )
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Меню",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
    except Exception:
        logging.exception("Failed to configure Telegram bot entrypoints.")


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть меню",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )
    text = (
        "Онлайн-заказ кафе открыт.\n"
        "Выберите блюда, оформите заказ и ждите уведомления о статусе в Telegram."
    )
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("admin"))
async def handle_admin(message: Message) -> None:
    global ADMIN_CHAT_ID
    chat_id = message.chat.id

    # Security: only whitelisted users can become admin
    if ALLOWED_ADMIN_IDS and chat_id not in ALLOWED_ADMIN_IDS:
        logging.warning("Unauthorized /admin attempt from chat_id=%d", chat_id)
        await message.answer("Доступ запрещён. Обратитесь к владельцу бота.")
        return

    ADMIN_CHAT_ID = chat_id
    with db_session() as session:
        save_setting(session, "admin_chat_id", str(chat_id))
    await message.answer(
        "Этот чат назначен кассой. Сюда будут приходить оплаченные заказы."
    )


VALID_STATUS_TRANSITIONS: dict[str, list[str]] = {
    "paid": ["preparing", "ready"],
    "preparing": ["ready"],
}


@router.callback_query(F.data.startswith("order:"))
async def handle_order_status_change(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer("Некорректные данные.")
        return

    try:
        _, action, raw_order_id = callback.data.split(":")
        order_id = int(raw_order_id)
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные.")
        return

    with db_session() as session:
        order = fetch_order(session, order_id)

        allowed = VALID_STATUS_TRANSITIONS.get(order.status, [])
        if action not in allowed:
            await callback.answer("Невозможно изменить статус заказа.")
            return

        if action == "preparing":
            order.status = "preparing"
            order.updated_at = now_utc()
            session.commit()
            session.refresh(order)
            _ = order.items
            await notify_customer(
                order,
                f"Заказ №{order.public_order_number} передан на кухню. Сейчас его готовят.",
            )
            await callback.message.edit_text(
                format_order_for_cashier(order),
                reply_markup=build_cashier_keyboard(order.id, order.status),
            )
            await callback.answer("🟡 Готовится")
            return

        if action == "ready":
            order.status = "ready"
            order.updated_at = now_utc()
            session.commit()
            session.refresh(order)
            _ = order.items
            await notify_customer(
                order,
                f"✅ Заказ №{order.public_order_number} готов и ожидает вас в ресторане!",
            )
            await callback.message.edit_text(format_order_for_cashier(order), reply_markup=None)
            await callback.answer("🟢 Готов!")
            return

    await callback.answer("Неизвестное действие.")


# ---------------------------------------------------------------------------
#  Стоп-лист: Telegram-команды
# ---------------------------------------------------------------------------

@router.message(Command("stop"))
async def handle_stop(message: Message) -> None:
    """Быстрое отключение блюда: /stop Плов"""
    chat_id = message.chat.id
    if ALLOWED_ADMIN_IDS and chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    # Извлекаем название блюда из команды
    text = (message.text or "").strip()
    # Убираем /stop и возможный @botname
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        # Показать список категорий для отключения
        buttons = []
        for entry in CATEGORY_META:
            buttons.append([InlineKeyboardButton(
                text=f"🚫 {entry['title']}",
                callback_data=f"sl:cat_off:{entry['slug']}",
            )])
        await message.answer(
            "Укажите название блюда: <code>/stop Плов</code>\n\n"
            "Или отключите целую категорию:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    search_query = parts[1].strip().lower()

    with db_session() as session:
        all_items = session.scalars(select(MenuItem)).all()
        matches = [item for item in all_items if search_query in item.name.lower()]

    if not matches:
        await message.answer(f"Блюдо «{escape(parts[1].strip())}» не найдено.")
        return

    if len(matches) == 1:
        item = matches[0]
        if not item.is_available:
            await message.answer(f"«{escape(item.name)}» уже в стоп-листе.")
            return
        # Отключаем и предлагаем установить время
        with db_session() as session:
            db_item = session.get(MenuItem, item.id)
            if db_item:
                db_item.is_available = False
                db_item.unavailable_reason = "Временно недоступно"
                session.commit()

        buttons = [
            [
                InlineKeyboardButton(text="30 мин", callback_data=f"sl:time:{item.id}:30"),
                InlineKeyboardButton(text="1 час", callback_data=f"sl:time:{item.id}:60"),
            ],
            [
                InlineKeyboardButton(text="2 часа", callback_data=f"sl:time:{item.id}:120"),
                InlineKeyboardButton(text="Не знаю", callback_data=f"sl:time:{item.id}:0"),
            ],
        ]
        await message.answer(
            f"🚫 <b>{escape(item.name)}</b> отключено.\n\nКогда будет готово?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:
        # Несколько совпадений — предлагаем выбрать
        buttons = []
        for item in matches[:10]:
            status = "✅" if item.is_available else "🚫"
            buttons.append([InlineKeyboardButton(
                text=f"{status} {item.name} ({rub(item.price)})",
                callback_data=f"sl:off:{item.id}" if item.is_available else f"sl:on:{item.id}",
            )])
        await message.answer(
            f"Найдено {len(matches)} совпадений. Выберите:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.message(Command("stoplist"))
async def handle_stoplist(message: Message) -> None:
    """Показать текущий стоп-лист с кнопками управления."""
    chat_id = message.chat.id
    if ALLOWED_ADMIN_IDS and chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    with db_session() as session:
        unavailable = session.scalars(
            select(MenuItem).where(MenuItem.is_available.is_(False)).order_by(MenuItem.category, MenuItem.name)
        ).all()

    if not unavailable:
        await message.answer("✅ Стоп-лист пуст — все блюда доступны.")
        return

    lines = ["🚫 <b>Стоп-лист:</b>\n"]
    buttons = []
    for item in unavailable:
        cat_title = CATEGORY_BY_SLUG.get(item.category, {}).get("title", "")
        reason = escape(item.unavailable_reason or "")
        time_str = _format_available_at(item.available_at) or ""
        extra = f" ({reason})" if reason else ""
        extra += f" → вернётся {time_str}" if time_str else ""
        lines.append(f"• <b>{escape(item.name)}</b> [{escape(cat_title)}]{extra}")
        buttons.append([InlineKeyboardButton(
            text=f"✅ Вернуть: {item.name}",
            callback_data=f"sl:on:{item.id}",
        )])

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


@router.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    """Статистика продаж: /stats [today|week|month]"""
    chat_id = message.chat.id
    if ALLOWED_ADMIN_IDS and chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    # Парсим период
    text_msg = (message.text or "").strip()
    parts = text_msg.split(maxsplit=1)
    period = parts[1].strip().lower() if len(parts) > 1 else "today"

    now_irkutsk = datetime.now(IRKUTSK_TZ)
    today_start = now_irkutsk.replace(hour=0, minute=0, second=0, microsecond=0)

    if period in ("week", "неделя"):
        period_start = today_start - timedelta(days=today_start.weekday())  # Monday
        period_label = "за неделю"
    elif period in ("month", "месяц"):
        period_start = today_start.replace(day=1)
        period_label = "за месяц"
    else:
        period_start = today_start
        period_label = "за сегодня"

    period_start_utc = period_start.astimezone(timezone.utc)

    with db_session() as session:
        period_orders = session.scalar(
            select(func.count(Order.id)).where(
                Order.payment_status == "paid",
                Order.created_at >= period_start_utc,
            )
        ) or 0

        period_revenue = session.scalar(
            select(func.sum(Order.total_amount)).where(
                Order.payment_status == "paid",
                Order.created_at >= period_start_utc,
            )
        ) or 0

        # Топ-5 позиций
        top_items = session.execute(
            select(
                OrderItem.name_snapshot,
                func.sum(OrderItem.quantity).label("total_qty"),
            ).join(Order).where(
                Order.payment_status == "paid",
                Order.created_at >= period_start_utc,
            ).group_by(OrderItem.name_snapshot)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5)
        ).all()

        # Стоп-лист
        stopped_count = session.scalar(
            select(func.count(MenuItem.id)).where(MenuItem.is_available.is_(False))
        ) or 0

        # Незавершённые заказы
        pending_orders = session.scalar(
            select(func.count(Order.id)).where(
                Order.status.in_(["created", "paid", "preparing"]),
            )
        ) or 0

        # Неотправленные чеки
        pending_fiscal = session.scalar(
            select(func.count(FiscalQueue.id)).where(FiscalQueue.status == "pending")
        ) or 0

        # Статистика по дням (для week/month)
        daily_stats_text = ""
        if period != "today":
            daily_stats = session.execute(
                select(
                    func.date(Order.created_at).label("day"),
                    func.count(Order.id).label("cnt"),
                    func.sum(Order.total_amount).label("rev"),
                ).where(
                    Order.payment_status == "paid",
                    Order.created_at >= period_start_utc,
                ).group_by(func.date(Order.created_at))
                .order_by(func.date(Order.created_at).desc())
                .limit(14)
            ).all()
            if daily_stats:
                daily_lines = "\n".join(
                    f"  {day}: {cnt} зак. / {rub(rev or 0)}"
                    for day, cnt, rev in daily_stats
                )
                daily_stats_text = f"\n\n<b>По дням:</b>\n{daily_lines}"

    avg = period_revenue // period_orders if period_orders else 0
    top_lines = "\n".join(
        f"  {i+1}. {name} — {qty} шт." for i, (name, qty) in enumerate(top_items)
    )

    text = (
        f"📊 <b>Статистика {period_label}</b>\n\n"
        f"Заказов: <b>{period_orders}</b>\n"
        f"Выручка: <b>{rub(period_revenue)}</b>\n"
        f"Средний чек: <b>{rub(avg)}</b>\n\n"
        f"<b>Топ-5 позиций:</b>\n{top_lines or '  Нет данных'}"
        f"{daily_stats_text}\n\n"
        f"🚫 В стоп-листе: {stopped_count} блюд\n"
        f"⏳ Активных заказов: {pending_orders}\n"
        f"🧾 Неотправленных чеков: {pending_fiscal}"
    )
    await message.answer(text)


@router.callback_query(F.data.startswith("sl:"))
async def handle_stoplist_callback(callback: CallbackQuery) -> None:
    """Обработка inline-кнопок стоп-листа."""
    if callback.data is None or callback.message is None:
        await callback.answer("Ошибка.")
        return

    chat_id = callback.message.chat.id
    if ALLOWED_ADMIN_IDS and chat_id not in ALLOWED_ADMIN_IDS:
        await callback.answer("Доступ запрещён.")
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Неверный формат.")
        return

    action = parts[1]  # on, off, cat_off, cat_on, time

    if action == "on":
        item_id = int(parts[2])
        with db_session() as session:
            item = session.get(MenuItem, item_id)
            if item:
                item.is_available = True
                item.unavailable_reason = None
                item.available_at = None
                session.commit()
                await callback.answer(f"✅ {item.name} включено")
                # Обновляем сообщение
                await callback.message.edit_text(
                    f"✅ <b>{escape(item.name)}</b> снова доступно!",
                )
            else:
                await callback.answer("Блюдо не найдено.")

    elif action == "off":
        item_id = int(parts[2])
        with db_session() as session:
            item = session.get(MenuItem, item_id)
            if item:
                item.is_available = False
                item.unavailable_reason = "Временно недоступно"
                session.commit()
                buttons = [
                    [
                        InlineKeyboardButton(text="30 мин", callback_data=f"sl:time:{item_id}:30"),
                        InlineKeyboardButton(text="1 час", callback_data=f"sl:time:{item_id}:60"),
                    ],
                    [
                        InlineKeyboardButton(text="2 часа", callback_data=f"sl:time:{item_id}:120"),
                        InlineKeyboardButton(text="Не знаю", callback_data=f"sl:time:{item_id}:0"),
                    ],
                ]
                await callback.message.edit_text(
                    f"🚫 <b>{escape(item.name)}</b> отключено.\n\nКогда будет готово?",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                )
                await callback.answer(f"🚫 {item.name} отключено")
            else:
                await callback.answer("Блюдо не найдено.")

    elif action == "cat_off":
        slug = parts[2]
        cat_info = CATEGORY_BY_SLUG.get(slug)
        if not cat_info:
            await callback.answer("Категория не найдена.")
            return
        with db_session() as session:
            items = session.scalars(
                select(MenuItem).where(MenuItem.category == slug, MenuItem.is_available.is_(True))
            ).all()
            count = len(items)
            for item in items:
                item.is_available = False
                item.unavailable_reason = "Категория временно недоступна"
            session.commit()
        await callback.message.edit_text(
            f"🚫 Категория <b>{escape(cat_info['title'])}</b> отключена ({count} блюд).\n"
            f"Для включения: /stoplist",
        )
        await callback.answer(f"🚫 {cat_info['title']} отключена")

    elif action == "cat_on":
        slug = parts[2]
        cat_info = CATEGORY_BY_SLUG.get(slug)
        if not cat_info:
            await callback.answer("Категория не найдена.")
            return
        with db_session() as session:
            items = session.scalars(
                select(MenuItem).where(MenuItem.category == slug, MenuItem.is_available.is_(False))
            ).all()
            count = len(items)
            for item in items:
                item.is_available = True
                item.unavailable_reason = None
                item.available_at = None
            session.commit()
        await callback.message.edit_text(
            f"✅ Категория <b>{escape(cat_info['title'])}</b> включена ({count} блюд).",
        )
        await callback.answer(f"✅ {cat_info['title']} включена")

    elif action == "time":
        if len(parts) < 4:
            await callback.answer("Ошибка.")
            return
        item_id = int(parts[2])
        minutes = int(parts[3])
        with db_session() as session:
            item = session.get(MenuItem, item_id)
            if item:
                if minutes > 0:
                    item.available_at = now_utc() + timedelta(minutes=minutes)
                    item.unavailable_reason = f"Будет готово {_format_available_at(item.available_at) or 'позже'}"
                    session.commit()
                    await callback.message.edit_text(
                        f"🚫 <b>{escape(item.name)}</b> отключено.\n"
                        f"⏰ Вернётся автоматически {_format_available_at(item.available_at)}",
                    )
                    await callback.answer(f"Таймер установлен: {minutes} мин")
                else:
                    # "Не знаю" — оставляем без таймера
                    await callback.message.edit_text(
                        f"🚫 <b>{escape(item.name)}</b> отключено.\n"
                        f"Включите вручную: /stoplist",
                    )
                    await callback.answer("Блюдо отключено без таймера")
            else:
                await callback.answer("Блюдо не найдено.")


async def _keep_alive_ping():
    """Self-ping to prevent Render free tier from sleeping (every 14 min)."""
    import aiohttp

    await asyncio.sleep(60)  # wait for full startup
    url = f"{APP_BASE_URL}/healthz"
    logging.info("Keep-alive started: pinging %s every 14 min", url)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logging.info("Keep-alive ping: %s", r.status)
        except Exception as exc:
            logging.warning("Keep-alive ping failed: %s", exc)
        await asyncio.sleep(14 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global bot, bot_polling_task

    initialize_database()

    # ── Security startup warnings ─────────────────────────────────────────
    if not os.getenv("KITCHEN_API_KEY", "").strip():
        logging.warning(
            "⚠️  KITCHEN_API_KEY not set. Kitchen and admin API endpoints will reject all requests."
        )
    if not ALLOWED_ADMIN_IDS:
        logging.warning(
            "⚠️  ALLOWED_ADMIN_IDS not set. ANY Telegram user can become admin via /admin."
        )
    if not BOT_TOKEN and DEV_MODE:
        logging.critical(
            "⚠️  BOT_TOKEN is empty and DEV_MODE=true. dev_user_id auth bypass is ACTIVE."
        )
    if not os.getenv("SBP_CALLBACK_SECRET", "").strip():
        logging.warning(
            "⚠️  SBP_CALLBACK_SECRET not set. SBP payment callbacks will be rejected."
        )

    # Фоновые задачи
    stoplist_task = asyncio.create_task(_stoplist_auto_enable_worker())
    timeout_task = asyncio.create_task(_order_timeout_worker())
    fiscal_retry_task = asyncio.create_task(_fiscal_retry_worker())
    logging.info("Background workers started: stoplist auto-enable, order timeout, fiscal retry")

    # Keep-alive self-ping (only when deployed with a real URL)
    keep_alive_task = None
    if APP_BASE_URL and not APP_BASE_URL.startswith("http://127.0.0.1"):
        keep_alive_task = asyncio.create_task(_keep_alive_ping())

    if BOT_TOKEN:
        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        await configure_bot_entrypoints()
        bot_polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        )
        logging.info("Bot polling started.")
    else:
        logging.warning("BOT_TOKEN is empty. FastAPI will run without Telegram bot.")

    try:
        yield
    finally:
        # Останавливаем фоновые задачи
        for task in (stoplist_task, timeout_task, fiscal_retry_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if keep_alive_task is not None:
            keep_alive_task.cancel()
            with suppress(asyncio.CancelledError):
                await keep_alive_task
        if bot_polling_task is not None:
            bot_polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await bot_polling_task
        if bot is not None:
            await bot.session.close()


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
            _cors_origins.append(_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


# ── Security headers middleware ───────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org 'unsafe-inline'; "
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'"
        )
        if request.url.scheme == "https" or APP_BASE_URL.startswith("https"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)


@app.get("/", include_in_schema=False)
async def serve_index() -> Response:
    import mimetypes
    content = (BASE_DIR / "index.html").read_bytes()
    return Response(
        content=content,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/healthz")
async def healthcheck() -> Response:
    """Расширенная проверка здоровья: БД, бот, АТОЛ, 1С."""
    checks: dict[str, Any] = {}
    overall = True
    backend = "postgres" if "postgresql+psycopg" in SQLALCHEMY_DATABASE_URL else "sqlite"

    # 1. Database
    try:
        with db_session() as session:
            session.execute(select(func.count(MenuItem.id)))
        checks["database"] = {"status": "ok", "backend": backend}
    except Exception as e:
        checks["database"] = {"status": "error", "backend": backend}
        overall = False

    # 2. Telegram bot
    checks["telegram_bot"] = {"status": "ok" if bot else "not_configured"}

    # 3. ATOL (fiscalization)
    atol_configured = bool(
        os.getenv("ATOL_LOGIN", "").strip()
        and os.getenv("ATOL_PASSWORD", "").strip()
        and os.getenv("ATOL_GROUP_CODE", "").strip()
    )
    checks["atol"] = {"status": "configured" if atol_configured else "not_configured"}

    # 4. 1C:Fresh
    odata_configured = bool(os.getenv("ODATA_BASE_URL", "").strip())
    checks["accounting_1c"] = {"status": "configured" if odata_configured else "not_configured"}

    # 5. SBP payments
    sbp_configured = bool(
        os.getenv("SBP_MEMBER_ID", "").strip()
        and os.getenv("SBP_TERMINAL_ID", "").strip()
    )
    checks["sbp_payments"] = {"status": "configured" if sbp_configured else "not_configured"}

    status_code = 200 if overall else 503
    return JSONResponse(
        {"status": "ok" if overall else "degraded", "checks": checks},
        status_code=status_code,
    )


@app.get("/api/menu")
async def get_menu(request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    if not general_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    with db_session() as session:
        items = session.scalars(
            select(MenuItem).order_by(MenuItem.category, MenuItem.sort_order)
        ).all()

    items_by_id = {item.id: item for item in items}
    grouped: dict[str, list[dict[str, Any]]] = {entry["slug"]: [] for entry in CATEGORY_META}
    seen_variant_groups: set[str] = set()

    for item in items:
        group_key = ITEM_TO_VARIANT_GROUP.get(item.id)
        if group_key:
            if group_key in seen_variant_groups:
                continue
            seen_variant_groups.add(group_key)
            group_data = VARIANT_GROUPS[group_key]
            variant_items = [items_by_id[iid] for iid in group_data["item_ids"] if iid in items_by_id]
            if not variant_items:
                continue
            primary = variant_items[0]
            # Группа доступна если хотя бы один вариант доступен
            group_available = any(vi.is_available for vi in variant_items)
            group_reason = None
            group_available_at = None
            if not group_available:
                # Берём причину и время из первого недоступного
                group_reason = primary.unavailable_reason
                group_available_at = _format_available_at(primary.available_at)
            grouped[item.category].append({
                "id": primary.id,
                "category": item.category,
                "category_title": CATEGORY_BY_SLUG[item.category]["title"],
                "name": group_data["name"],
                "description": group_data["description"],
                "price": primary.price,
                "image_url": _resolve_image_url(primary),
                "is_available": group_available,
                "unavailable_reason": group_reason,
                "available_at_display": group_available_at,
                "sort_order": primary.sort_order,
                "variants": [
                    {
                        "id": vi.id,
                        "label": group_data["labels"][vi.id],
                        "price": vi.price,
                        "is_available": vi.is_available,
                    }
                    for vi in variant_items
                ],
            })
        else:
            grouped[item.category].append(serialize_menu_item(item))

    categories = []
    for entry in CATEGORY_META:
        category_items = grouped.get(entry["slug"], [])
        if not category_items:
            continue
        cat_data: dict[str, Any] = {
            "slug": entry["slug"],
            "title": entry["title"],
            "subtitle": entry["subtitle"],
            "items": category_items,
        }
        if entry.get("note"):
            cat_data["note"] = entry["note"]
        categories.append(cat_data)

    return {
        "categories": categories,
        "items_count": sum(len(category["items"]) for category in categories),
        "global_note": "Чай чёрный/зелёный 200 мл — бесплатно к каждому заказу",
        "schedule": get_cafe_schedule(),
    }


@app.get("/api/schedule")
async def get_schedule() -> dict[str, Any]:
    return get_cafe_schedule()


@app.get("/api/orders/{order_id}")
async def get_order(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    verified_user_id = get_verified_user_id(request)
    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")
        return serialize_order(order)


@app.post("/api/create_order")
async def create_order(payload: CreateOrderRequest, request: Request) -> dict[str, Any]:
    verified_user_id, customer_name = get_verified_user_info(request)

    if not order_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Слишком много заказов. Подождите минуту.")

    if not payload.items:
        raise HTTPException(status_code=400, detail="Cart is empty.")

    schedule = get_cafe_schedule()
    if not schedule["is_open"]:
        raise HTTPException(
            status_code=400,
            detail=f"Кафе сейчас закрыто. Часы работы: {schedule['opens_at']}–{schedule['closes_at']} (Иркутск). Последний заказ в {schedule['last_order_at']}.",
        )

    requested_quantities: dict[int, int] = {}
    for item in payload.items:
        requested_quantities[item.item_id] = requested_quantities.get(item.item_id, 0) + item.quantity

    # Security: limit total items per order
    total_quantity = sum(requested_quantities.values())
    if total_quantity > MAX_ITEMS_PER_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"Слишком много позиций в заказе (максимум {MAX_ITEMS_PER_ORDER}).",
        )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with db_session() as session:
                db_items = session.scalars(
                    select(MenuItem).where(MenuItem.id.in_(requested_quantities.keys()), MenuItem.is_available.is_(True))
                ).all()
                menu_items = {item.id: item for item in db_items}

                if len(menu_items) != len(requested_quantities):
                    raise HTTPException(status_code=400, detail="Some menu items are unavailable.")

                # Sanitize comment: удаляем HTML-теги и null bytes
                import re as _re
                clean_comment = _re.sub(r"<[^>]+>", "", payload.comment).replace("\x00", "").strip() if payload.comment else ""

                total = 0
                order = Order(
                    public_order_number=next_public_order_number(session),
                    telegram_user_id=verified_user_id,
                    customer_name=customer_name,
                    customer_comment=clean_comment[:500] if clean_comment else None,
                    total_amount=0,
                    status="created",
                    payment_status="pending",
                    payment_mode="sbp",
                    kitchen_printed=False,
                    created_at=now_utc(),
                    updated_at=now_utc(),
                )
                session.add(order)
                session.flush()

                for item_id, quantity in requested_quantities.items():
                    menu_item = menu_items[item_id]
                    subtotal = menu_item.price * quantity
                    total += subtotal
                    session.add(
                        OrderItem(
                            order_id=order.id,
                            menu_item_id=menu_item.id,
                            name_snapshot=menu_item.name,
                            price_snapshot=menu_item.price,
                            quantity=quantity,
                            subtotal=subtotal,
                        )
                    )

                # Security: limit order total
                if total > MAX_ORDER_TOTAL_RUB:
                    session.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail=f"Сумма заказа превышает лимит ({MAX_ORDER_TOTAL_RUB} руб.).",
                    )

                order.total_amount = total
                order.updated_at = now_utc()
                session.commit()
                session.refresh(order)
                _ = order.items
                return serialize_order(order)
        except IntegrityError:
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Не удалось создать заказ. Попробуйте ещё раз.")
            continue


# ---------------------------------------------------------------------------
#  СБП + Фискализация: реальные платёжные эндпоинты
# ---------------------------------------------------------------------------

@app.post("/api/sbp/create-payment/{order_id}")
async def sbp_create_payment(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """
    Создаёт платёж через СБП Сбербанк.
    Возвращает deeplink для оплаты в мобильном приложении банка.
    """
    from payments.sbp import create_sbp_payment

    verified_user_id = get_verified_user_id(request)

    with db_session() as session:
        # Lock row to prevent race condition on concurrent payment creation
        order = session.scalars(
            select(Order).where(Order.id == order_id).with_for_update()
        ).first()
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        # Нельзя создать платёж для отменённого заказа
        if order.status == "cancelled":
            raise HTTPException(status_code=400, detail="Заказ отменён. Создайте новый заказ.")

        if order.payment_status == "paid":
            _ = order.items
            return {"status": "already_paid", **serialize_order(order)}

        # Если gateway_order_id уже есть — платёж уже создан, возвращаем данные
        if order.gateway_order_id:
            _ = order.items
            return {
                "status": "payment_exists",
                "gateway_order_id": order.gateway_order_id,
                "order": serialize_order(order),
            }

        # Создаём платёж в Сбербанке ВНУТРИ транзакции (блокировка строки удерживается)
        result = await create_sbp_payment(
            order_id=order.id,
            order_number=order.public_order_number,
            total_amount=order.total_amount,
        )

        if not result.success:
            raise HTTPException(status_code=502, detail=f"СБП ошибка: {result.error_message}")

        # Сохраняем gateway_order_id в той же транзакции
        order.gateway_order_id = result.order_id
        order.payment_mode = "sbp"
        order.updated_at = now_utc()
        session.commit()

    return {
        "status": "created",
        "deeplink": result.deeplink,
        "payment_url": result.payment_url,
        "gateway_order_id": result.order_id,
    }


@app.get("/api/sbp/check-status/{order_id}")
async def sbp_check_status(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """
    Проверяет статус платежа через Sberbank API.
    Фронтенд вызывает каждые 3-5 сек после перехода в приложение банка.
    """
    from payments.sbp import check_sbp_payment

    verified_user_id = get_verified_user_id(request)

    if not sbp_check_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Too many status checks. Please wait.")

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.payment_status == "paid":
            return {"status": "paid", **serialize_order(order)}

        if not order.gateway_order_id:
            return {"status": "no_payment"}

        gateway_order_id = order.gateway_order_id

    # Проверяем статус в Сбербанке
    result = await check_sbp_payment(gateway_order_id)

    if not result.success:
        return {"status": "check_error", "error": result.error_message}

    if result.is_paid:
        # Проверяем сумму: amount из Сбербанка (копейки) должна совпасть с order.total_amount * 100
        with db_session() as session:
            order_check = fetch_order(session, order_id)
            expected_kopecks = order_check.total_amount * 100
            if result.amount and result.amount != expected_kopecks:
                logging.critical(
                    "AMOUNT MISMATCH: order %d expected %d kopecks, got %d from SBP",
                    order_id, expected_kopecks, result.amount,
                )
                return {"status": "amount_mismatch", "error": "Сумма оплаты не совпадает с суммой заказа."}

        # Оплата подтверждена — запускаем полный автоматический цикл
        await _process_paid_order(order_id)

        with db_session() as session:
            order = fetch_order(session, order_id)
            return {"status": "paid", **serialize_order(order)}

    return {
        "status": result.status_label,
        "order_status": result.order_status,
    }


@app.post("/api/sbp/callback")
async def sbp_callback(request: Request) -> dict[str, str]:
    """
    Callback от Сбербанка при изменении статуса платежа.
    POST с параметрами: mdOrder, orderNumber, operation, status, checksum
    """
    from payments.sbp import verify_callback

    # Rate limiting per IP
    client_ip = request.client.host if request.client else "unknown"
    if not callback_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many callback requests.")

    # Сбербанк может отправить параметры как в query string, так и в POST body
    params = dict(request.query_params)
    try:
        form_data = await request.form()
        for key, value in form_data.items():
            if key not in params:
                params[key] = value
    except Exception:
        pass  # Если body не form-encoded, используем только query params

    md_order = params.get("mdOrder", "")
    order_number = params.get("orderNumber", "")
    operation = params.get("operation", "")
    status = params.get("status", "")
    checksum = params.get("checksum", "")

    logging.info(
        "СБП callback: mdOrder=%s, orderNumber=%s, operation=%s, status=%s",
        md_order, order_number, operation, status,
    )

    # Проверяем подпись
    if not verify_callback(md_order, order_number, operation, status, checksum):
        logging.warning("СБП callback: неверная подпись для %s", md_order)
        raise HTTPException(status_code=403, detail="Invalid checksum")

    # Обработка успешной оплаты
    if operation == "deposited" and status == "1":
        # Найти заказ по gateway_order_id
        with db_session() as session:
            order = session.scalars(
                select(Order).where(Order.gateway_order_id == md_order)
            ).first()

            if order and order.payment_status != "paid":
                # Верификация суммы через Sberbank API
                from payments.sbp import check_sbp_payment
                verify_result = await check_sbp_payment(md_order)
                if verify_result.success and verify_result.amount:
                    expected_kopecks = order.total_amount * 100
                    if verify_result.amount != expected_kopecks:
                        logging.critical(
                            "CALLBACK AMOUNT MISMATCH: order %d expected %d, got %d",
                            order.id, expected_kopecks, verify_result.amount,
                        )
                        return {"status": "ok"}  # Не обрабатываем — сумма не совпала

                await _process_paid_order(order.id)
                logging.info("СБП callback: заказ %d оплачен через callback", order.id)
            elif order and order.payment_status == "paid":
                logging.info(
                    "СБП callback: дубликат для уже оплаченного заказа %d (replay ignored)", order.id
                )

    return {"status": "ok"}


@app.post("/api/orders/{order_id}/confirm-payment")
async def confirm_payment(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Mock оплата для тестирования. Только при DEV_MODE=true."""
    if not DEV_MODE:
        raise HTTPException(
            status_code=403,
            detail="Mock payments disabled in production. Use /api/sbp/create-payment.",
        )
    verified_user_id = get_verified_user_id(request)

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.payment_status == "paid":
            return serialize_order(order)

    await _process_paid_order(order_id)

    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)


async def _process_paid_order(order_id: int) -> None:
    """
    Полный автоматический цикл после подтверждения оплаты:
    1. Обновить статус заказа → paid
    2. Фискализация через АТОЛ Онлайн (чек продажи)
    3. Синхронизация с 1С:Бухгалтерия (документ "Реализация")
    4. Статус → preparing (заказ передан на кухню)
    5. Уведомление клиенту в Telegram
    6. Уведомление администратору (без кнопок управления)
    """
    from payments.fiscal import fiscalize_order
    from integrations.accounting import sync_order_to_1c

    # Атомарно помечаем заказ как оплаченный.
    # Если UPDATE затронул 0 строк — значит другой запрос уже обработал этот заказ.
    with db_session() as session:
        rows_updated = session.execute(
            select(Order).where(
                Order.id == order_id,
                Order.payment_status != "paid",
            ).with_for_update()
        )
        order = rows_updated.scalar_one_or_none()

        if order is None:
            return  # уже обработан другим запросом (race condition защита)

        # 1. Обновляем статус оплаты
        order.payment_status = "paid"
        order.status = "paid"
        order.updated_at = now_utc()
        session.commit()
        session.refresh(order)
        _ = order.items

        # Подготовим данные для фискализации
        fiscal_items = [
            {
                "name_snapshot": item.name_snapshot,
                "price_snapshot": item.price_snapshot,
                "quantity": item.quantity,
            }
            for item in order.items
        ]
        total_amount = order.total_amount
        order_number = order.public_order_number
        user_id = order.telegram_user_id

    # 2. Фискализация (АТОЛ Онлайн) — не блокируем основной поток при ошибке
    def _enqueue_fiscal_retry() -> None:
        """Сохраняем в очередь повторной фискализации (54-ФЗ)."""
        try:
            with db_session() as fq_session:
                # Защита от дублей: проверяем нет ли уже записи для этого заказа
                existing_fq = fq_session.scalars(
                    select(FiscalQueue).where(
                        FiscalQueue.order_id == order_id,
                        FiscalQueue.operation == "sell",
                        FiscalQueue.status.in_(["pending", "processing"]),
                    )
                ).first()
                if existing_fq:
                    logging.info("Фискализация: заказ %d уже в retry-очереди (id=%d)", order_id, existing_fq.id)
                    return
                fq_session.add(FiscalQueue(
                    order_id=order_id,
                    order_number=order_number,
                    operation="sell",
                    payload_json=json_module.dumps({"items": fiscal_items, "total_amount": total_amount}),
                    status="pending",
                    attempts=1,
                    created_at=now_utc(),
                    next_retry_at=now_utc() + timedelta(minutes=5),
                ))
                fq_session.commit()
            logging.info("Фискализация: заказ %d добавлен в retry-очередь", order_id)
        except Exception:
            logging.exception("Не удалось сохранить заказ %d в fiscal retry queue", order_id)

    try:
        fiscal_result = await fiscalize_order(
            order_id=order_id,
            order_number=order_number,
            items=fiscal_items,
            total_amount=total_amount,
        )
        if fiscal_result.success and fiscal_result.uuid:
            with db_session() as session:
                order = session.get(Order, order_id)
                if order:
                    order.fiscal_uuid = fiscal_result.uuid
                    session.commit()
            logging.info("Фискализация: чек создан для заказа %d, uuid=%s",
                         order_id, fiscal_result.uuid)
        else:
            logging.error("Фискализация: ошибка для заказа %d: %s",
                          order_id, fiscal_result.error)
            _enqueue_fiscal_retry()
    except Exception:
        logging.exception("Фискализация: критическая ошибка для заказа %d", order_id)
        _enqueue_fiscal_retry()

    # 3. Синхронизация с 1С:Бухгалтерия (документ "Реализация товаров и услуг")
    try:
        accounting_items = [
            {
                "name": item["name_snapshot"],
                "quantity": item["quantity"],
                "price": item["price_snapshot"],
                "total": item["price_snapshot"] * item["quantity"],
            }
            for item in fiscal_items
        ]
        sync_result = await sync_order_to_1c(
            order_id=order_id,
            order_number=str(order_number),
            items=accounting_items,
            total_amount=total_amount,
        )
        if sync_result.success:
            with db_session() as session:
                order = session.get(Order, order_id)
                if order:
                    order.accounting_synced = True
                    order.accounting_doc_id = sync_result.document_id
                    session.commit()
            logging.info("1С: документ создан для заказа %d, doc_id=%s",
                         order_id, sync_result.document_id)
        else:
            logging.warning("1С: не удалось синхронизировать заказ %d: %s",
                            order_id, sync_result.error)
    except Exception:
        logging.exception("1С: критическая ошибка синхронизации для заказа %d", order_id)

    # 4. Автоматически переводим в «Готовится» (кухня видит через принтер)
    with db_session() as session:
        order = fetch_order(session, order_id)
        order.status = "preparing"
        order.updated_at = now_utc()
        session.commit()

    # 5. Уведомление клиенту
    if bot:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ Заказ №{order_number} оплачен!\n\n"
                    f"Сумма: {rub(total_amount)}\n"
                    f"Статус: <b>Готовится</b>\n\n"
                    "Мы сообщим, когда заказ будет готов к выдаче."
                ),
            )
        except Exception:
            logging.exception("Не удалось уведомить клиента %s", user_id)

    # 6. Уведомление администратору (информационное, без кнопок)
    await notify_admin_about_order(order_id)


async def notify_admin_about_order(order_id: int) -> None:
    """Отправка информационного уведомления администратору (без кнопок управления)."""
    if bot is None or ADMIN_CHAT_ID is None:
        return

    with db_session() as session:
        order = fetch_order(session, order_id)
        item_lines = "\n".join(
            f"• {item.name_snapshot} x{item.quantity} = {rub(item.subtotal)}"
            for item in order.items
        )
        text = (
            f"🆕 <b>Новый заказ №{order.public_order_number}</b>\n"
            f"Статус: <b>Готовится</b> (автоматически)\n"
            f"Оплата: <b>СБП</b> ✅\n\n"
            f"{item_lines}\n\n"
            f"<b>Сумма:</b> {rub(order.total_amount)}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception:
            logging.exception("Не удалось уведомить админа о заказе %d", order.public_order_number)


# ---------------------------------------------------------------------------
#  Кухонный принтер: API для агента печати
# ---------------------------------------------------------------------------

@app.get("/api/kitchen/pending")
async def kitchen_pending(request: Request) -> dict[str, Any]:
    """
    Возвращает заказы, ожидающие печати на кухне.
    Агент печати (Windows) опрашивает каждые 5 сек.
    Защита: API-ключ в заголовке X-Kitchen-Key (fail-closed).
    """
    verify_kitchen_api_key(request)

    with db_session() as session:
        orders = session.scalars(
            select(Order).where(
                Order.status.in_(["paid", "preparing"]),
                Order.kitchen_printed.is_(False),
            ).order_by(Order.created_at)
        ).all()

        result = []
        for order in orders:
            _ = order.items  # load items
            result.append({
                "order_id": order.id,
                "order_number": order.public_order_number,
                "customer_name": order.customer_name,
                "customer_comment": order.customer_comment,
                "total": order.total_amount,
                "created_at": order.created_at.isoformat(),
                "items": [
                    {
                        "name": item.name_snapshot,
                        "quantity": item.quantity,
                        "price": item.price_snapshot,
                    }
                    for item in order.items
                ],
            })

    return {"orders": result, "count": len(result)}


@app.post("/api/kitchen/printed/{order_id}")
async def kitchen_mark_printed(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, str]:
    """
    Агент печати подтверждает, что заказ напечатан на кухне.
    """
    verify_kitchen_api_key(request)

    with db_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")

        order.kitchen_printed = True
        order.updated_at = now_utc()
        session.commit()

    logging.info("Кухня: заказ %d напечатан", order_id)
    return {"status": "ok"}


@app.post("/api/orders/{order_id}/mark-ready")
async def mark_order_ready(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """
    Пометить заказ как готовый к выдаче.
    Только для администратора/кухни (X-Kitchen-Key).
    Клиент получает уведомление.
    """
    verify_kitchen_api_key(request)

    with db_session() as session:
        order = fetch_order(session, order_id)

        if order.status == "ready":
            return serialize_order(order)

        order.status = "ready"
        order.updated_at = now_utc()
        session.commit()
        session.refresh(order)
        _ = order.items

        user_id = order.telegram_user_id
        order_number = order.public_order_number

    # Уведомляем клиента
    if bot:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Заказ №{order_number} готов и ожидает вас!",
            )
        except Exception:
            logging.exception("Не удалось уведомить клиента о готовности заказа %d", order_number)

    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)


# ---------------------------------------------------------------------------
#  Интеграция с 1С: статус синхронизации
# ---------------------------------------------------------------------------

@app.get("/api/admin/accounting-status")
async def accounting_status(request: Request) -> dict[str, Any]:
    """
    Статус синхронизации заказов с 1С:Бухгалтерия.
    Возвращает общую статистику и последние несинхронизированные заказы.
    Защита: API-ключ в заголовке X-Kitchen-Key (fail-closed).
    """
    verify_kitchen_api_key(request)

    with db_session() as session:
        # Общая статистика
        total_paid = session.scalar(
            select(func.count(Order.id)).where(Order.payment_status == "paid")
        ) or 0
        total_synced = session.scalar(
            select(func.count(Order.id)).where(
                Order.payment_status == "paid",
                Order.accounting_synced.is_(True),
            )
        ) or 0
        total_failed = total_paid - total_synced

        # Последние несинхронизированные
        unsynced_orders = session.scalars(
            select(Order).where(
                Order.payment_status == "paid",
                Order.accounting_synced.is_(False),
            ).order_by(Order.created_at.desc()).limit(20)
        ).all()

        unsynced_list = [
            {
                "order_id": o.id,
                "order_number": o.public_order_number,
                "total": o.total_amount,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in unsynced_orders
        ]

    # Проверка здоровья подключения к 1С
    from integrations.accounting import fresh_client
    health = await fresh_client.health_check()

    return {
        "1c_connection": health,
        "statistics": {
            "total_paid_orders": total_paid,
            "synced_to_1c": total_synced,
            "not_synced": total_failed,
        },
        "unsynced_orders": unsynced_list,
    }


@app.post("/api/admin/accounting-retry/{order_id}")
async def accounting_retry(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """
    Повторная синхронизация заказа с 1С.
    Используется если автоматическая синхронизация не сработала.
    """
    verify_kitchen_api_key(request)

    from integrations.accounting import sync_order_to_1c

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.payment_status != "paid":
            raise HTTPException(status_code=400, detail="Order is not paid")

        accounting_items = [
            {
                "name": item.name_snapshot,
                "quantity": item.quantity,
                "price": item.price_snapshot,
                "total": item.price_snapshot * item.quantity,
            }
            for item in order.items
        ]
        total_amount = order.total_amount
        order_number = str(order.public_order_number)

    sync_result = await sync_order_to_1c(
        order_id=order_id,
        order_number=order_number,
        items=accounting_items,
        total_amount=total_amount,
    )

    if sync_result.success:
        with db_session() as session:
            order = session.get(Order, order_id)
            if order:
                order.accounting_synced = True
                order.accounting_doc_id = sync_result.document_id
                session.commit()

    return sync_result.to_dict()


# ---------------------------------------------------------------------------
#  Стоп-лист: управление доступностью блюд
# ---------------------------------------------------------------------------

@app.get("/api/admin/stoplist")
async def get_stoplist(request: Request) -> dict[str, Any]:
    """Список всех отключённых блюд (стоп-лист)."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        unavailable = session.scalars(
            select(MenuItem).where(MenuItem.is_available.is_(False)).order_by(MenuItem.category, MenuItem.name)
        ).all()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in unavailable:
            cat_title = CATEGORY_BY_SLUG.get(item.category, {}).get("title", item.category)
            if cat_title not in grouped:
                grouped[cat_title] = []
            grouped[cat_title].append({
                "id": item.id,
                "name": item.name,
                "reason": item.unavailable_reason,
                "available_at": item.available_at.isoformat() if item.available_at else None,
                "available_at_display": _format_available_at(item.available_at),
            })

    return {"stoplist": grouped, "total_stopped": sum(len(v) for v in grouped.values())}


@app.post("/api/admin/stoplist")
async def manage_stoplist(payload: StopListRequest, request: Request) -> dict[str, Any]:
    """Управление стоп-листом: отключить/включить блюдо или категорию."""
    verify_kitchen_api_key(request)

    if not payload.item_id and not payload.category:
        raise HTTPException(status_code=400, detail="Укажите item_id или category.")

    # Санитизация reason: удаляем HTML-теги для защиты от XSS в Telegram и фронтенде
    clean_reason = payload.reason
    if clean_reason:
        import re as _re
        clean_reason = _re.sub(r"<[^>]+>", "", clean_reason).strip()
        if not clean_reason:
            clean_reason = None

    available_at_dt = None
    if payload.action == "disable" and payload.available_in_minutes:
        available_at_dt = now_utc() + timedelta(minutes=payload.available_in_minutes)

    affected: list[dict[str, Any]] = []

    with db_session() as session:
        if payload.item_id:
            item = session.get(MenuItem, payload.item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Блюдо не найдено.")
            items_to_update = [item]
        elif payload.category:
            if payload.category not in CATEGORY_BY_SLUG:
                raise HTTPException(status_code=400, detail=f"Неизвестная категория: {payload.category}")
            items_to_update = list(session.scalars(
                select(MenuItem).where(MenuItem.category == payload.category)
            ).all())
        else:
            items_to_update = []

        for item in items_to_update:
            if payload.action == "disable":
                item.is_available = False
                item.unavailable_reason = clean_reason or "Временно недоступно"
                item.available_at = available_at_dt
            else:  # enable
                item.is_available = True
                item.unavailable_reason = None
                item.available_at = None

            affected.append({"id": item.id, "name": item.name, "is_available": item.is_available})

        session.commit()

    action_text = "отключено" if payload.action == "disable" else "включено"
    logging.info("Стоп-лист: %s %d позиций", action_text, len(affected))
    return {"action": payload.action, "affected": affected, "count": len(affected)}


# ---------------------------------------------------------------------------
#  Фоновые задачи
# ---------------------------------------------------------------------------

async def _stoplist_auto_enable_worker() -> None:
    """Фоновая задача: автоматическое включение блюд по расписанию."""
    await asyncio.sleep(30)  # начальная задержка
    while True:
        try:
            with db_session() as session:
                now = now_utc()
                expired = session.scalars(
                    select(MenuItem).where(
                        MenuItem.is_available.is_(False),
                        MenuItem.available_at.isnot(None),
                        MenuItem.available_at <= now,
                    )
                ).all()

                for item in expired:
                    item.is_available = True
                    item.unavailable_reason = None
                    item.available_at = None
                    logging.info("Стоп-лист: авто-включение '%s' (id=%d)", item.name, item.id)

                if expired:
                    session.commit()
        except Exception:
            logging.exception("Ошибка в stoplist auto-enable worker")

        await asyncio.sleep(60)


async def _order_timeout_worker() -> None:
    """Фоновая задача: отмена неоплаченных заказов по таймауту."""
    await asyncio.sleep(60)  # начальная задержка
    while True:
        try:
            cutoff = now_utc() - timedelta(minutes=ORDER_PAYMENT_TIMEOUT_MINUTES)
            with db_session() as session:
                expired_orders = session.scalars(
                    select(Order).where(
                        Order.status == "created",
                        Order.payment_status == "pending",
                        Order.created_at < cutoff,
                    )
                ).all()

                for order in expired_orders:
                    order.status = "cancelled"
                    order.payment_status = "expired"
                    order.updated_at = now_utc()
                    logging.info(
                        "Таймаут оплаты: заказ №%d отменён (создан %s)",
                        order.public_order_number,
                        order.created_at.isoformat(),
                    )

                if expired_orders:
                    session.commit()
        except Exception:
            logging.exception("Ошибка в order timeout worker")

        await asyncio.sleep(60)


async def _fiscal_retry_worker() -> None:
    """Фоновая задача: повторная фискализация неудачных чеков (54-ФЗ)."""
    from payments.fiscal import fiscalize_order

    await asyncio.sleep(120)  # начальная задержка

    # Восстановление: записи застрявшие в "processing" после крэша — вернуть в pending
    try:
        with db_session() as session:
            stuck = session.scalars(
                select(FiscalQueue).where(FiscalQueue.status == "processing")
            ).all()
            for fq in stuck:
                fq.status = "pending"
                fq.next_retry_at = now_utc()
                logging.warning("Fiscal retry: восстановление stuck записи order_id=%d", fq.order_id)
            if stuck:
                session.commit()
    except Exception:
        logging.exception("Ошибка восстановления fiscal_queue processing records")

    while True:
        try:
            with db_session() as session:
                pending = session.scalars(
                    select(FiscalQueue).where(
                        FiscalQueue.status == "pending",
                        FiscalQueue.next_retry_at <= now_utc(),
                        FiscalQueue.attempts < FiscalQueue.max_attempts,
                    ).order_by(FiscalQueue.next_retry_at).limit(5)
                ).all()

                for fq in pending:
                    # Пропускаем записи для отменённых заказов
                    parent_order = session.get(Order, fq.order_id)
                    if parent_order and parent_order.status == "cancelled":
                        fq.status = "failed"
                        fq.last_error = "Order cancelled — fiscal retry skipped"
                        session.commit()
                        logging.info("Fiscal retry: пропуск отменённого заказа %d", fq.order_id)
                        continue

                    fq.status = "processing"
                    fq.attempts += 1
                    session.commit()

                    try:
                        payload = json_module.loads(fq.payload_json)
                        result = await fiscalize_order(
                            order_id=fq.order_id,
                            order_number=fq.order_number,
                            items=payload["items"],
                            total_amount=payload["total_amount"],
                        )

                        if result.success and result.uuid:
                            fq.status = "done"
                            fq.fiscal_uuid = result.uuid
                            fq.completed_at = now_utc()
                            # Обновляем заказ
                            order = session.get(Order, fq.order_id)
                            if order:
                                order.fiscal_uuid = result.uuid
                            logging.info(
                                "Fiscal retry: чек создан для заказа %d (попытка %d)",
                                fq.order_id, fq.attempts,
                            )
                        else:
                            fq.status = "pending"
                            fq.last_error = str(result.error)[:500] if result.error else "Unknown error"
                            # Экспоненциальный backoff: attempts * 5 мин, макс 2 часа
                            backoff_minutes = min(fq.attempts * 5, 120)
                            fq.next_retry_at = now_utc() + timedelta(minutes=backoff_minutes)
                            logging.warning(
                                "Fiscal retry: ошибка для заказа %d (попытка %d): %s",
                                fq.order_id, fq.attempts, fq.last_error,
                            )
                    except Exception as exc:
                        fq.status = "pending"
                        fq.last_error = str(exc)[:500]
                        backoff_minutes = min(fq.attempts * 5, 120)
                        fq.next_retry_at = now_utc() + timedelta(minutes=backoff_minutes)
                        logging.exception("Fiscal retry: exception for order %d", fq.order_id)

                    # Если исчерпаны попытки — помечаем как failed
                    if fq.attempts >= fq.max_attempts and fq.status == "pending":
                        fq.status = "failed"
                        logging.critical(
                            "Fiscal retry: ИСЧЕРПАНЫ ПОПЫТКИ для заказа %d (54-ФЗ нарушение!)",
                            fq.order_id,
                        )

                    session.commit()
        except Exception:
            logging.exception("Ошибка в fiscal retry worker")

        await asyncio.sleep(120)


def split_label(text: str, max_line_length: int = 18) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_line_length:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines[:3]


@app.get("/api/photos/{filename}", include_in_schema=False)
async def serve_photo(filename: str) -> FileResponse:
    """Serve menu item photos from the photos/ directory."""
    safe_name = Path(filename).name  # prevent path traversal
    photo_path = BASE_DIR / "photos" / safe_name
    if not photo_path.exists() or not photo_path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(
        photo_path,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/placeholders/{item_id}.svg", include_in_schema=False)
async def menu_placeholder(item_id: int) -> Response:
    with db_session() as session:
        item = session.get(MenuItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Menu item not found.")

    category = CATEGORY_BY_SLUG[item.category]
    primary, secondary = category["colors"]

    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="800" height="520" viewBox="0 0 800 520">
      <defs>
        <linearGradient id="bg" x1="0%" x2="100%" y1="0%" y2="100%">
          <stop offset="0%" stop-color="{primary}" />
          <stop offset="100%" stop-color="{secondary}" />
        </linearGradient>
      </defs>
      <rect width="800" height="520" fill="url(#bg)" rx="0" />
      <circle cx="680" cy="100" r="180" fill="rgba(255,255,255,0.06)" />
      <circle cx="150" cy="420" r="200" fill="rgba(255,248,239,0.05)" />
      <circle cx="400" cy="260" r="80" fill="rgba(255,255,255,0.04)" />
    </svg>
    """.strip()
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/my-orders")
async def my_orders(request: Request, limit: int = Query(default=20, ge=1, le=50)) -> dict[str, Any]:
    """История заказов текущего пользователя."""
    verified_user_id = get_verified_user_id(request)

    with db_session() as session:
        orders = session.scalars(
            select(Order).where(
                Order.telegram_user_id == verified_user_id,
                Order.payment_status == "paid",
            ).order_by(Order.created_at.desc()).limit(limit)
        ).all()

        result = []
        for order in orders:
            _ = order.items
            result.append(serialize_order(order))

    return {"orders": result, "count": len(result)}


@app.get("/api/app-config")
async def app_config() -> dict[str, Any]:
    return {
        "webapp_url": WEBAPP_URL,
        "app_base_url": APP_BASE_URL,
        "bot_configured": bool(BOT_TOKEN),
        "checkout_mode": "sbp",
        "payment_timeout_seconds": ORDER_PAYMENT_TIMEOUT_MINUTES * 60,
    }


@app.post("/api/reviews")
async def submit_review(payload: SubmitReviewRequest, request: Request) -> dict[str, str]:
    verified_user_id = get_verified_user_id(request)

    if not review_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Слишком много отзывов. Подождите минуту.")

    with db_session() as session:
        order = session.get(Order, payload.order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Not your order.")
        # Отзыв только для оплаченных заказов
        if order.payment_status != "paid":
            raise HTTPException(status_code=400, detail="Отзыв возможен только для оплаченных заказов.")

        existing = session.scalars(
            select(Review).where(
                Review.order_id == payload.order_id,
                Review.telegram_user_id == verified_user_id,
            )
        ).first()
        if existing:
            return {"status": "already_submitted"}

        # Санитизация комментария: убираем HTML-теги и null bytes
        import re as _re
        clean_comment = _re.sub(r"<[^>]+>", "", payload.comment).replace("\x00", "").strip() if payload.comment else ""

        review = Review(
            order_id=payload.order_id,
            telegram_user_id=verified_user_id,
            rating=payload.rating,
            comment=clean_comment,
            created_at=now_utc(),
        )
        session.add(review)
        session.commit()

    if bot and ADMIN_CHAT_ID and payload.rating:
        stars = "\u2B50" * payload.rating
        text = f"Новый отзыв к заказу №{order.public_order_number}\n{stars}"
        if payload.comment and payload.comment.strip():
            text += f"\n\n{escape(payload.comment[:200])}"
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception:
            logging.exception("Failed to send review notification")

    return {"status": "ok"}
