from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

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
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
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


class CartItem(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=50)


class CreateOrderRequest(BaseModel):
    user_id: int = Field(gt=0)
    items: list[CartItem]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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
        existing.price = item["price"]
        existing.image_url = f"/api/placeholders/{item['id']}.svg"
        existing.is_available = True
        existing.sort_order = item["sort_order"]
    session.commit()


def initialize_database() -> None:
    global ADMIN_CHAT_ID

    Base.metadata.create_all(bind=engine)
    with db_session() as session:
        seed_menu_items(session)
        saved_admin_chat_id = load_setting(session, "admin_chat_id")
        if saved_admin_chat_id and not ADMIN_CHAT_ID:
            ADMIN_CHAT_ID = int(saved_admin_chat_id)


def serialize_menu_item(item: MenuItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "category": item.category,
        "category_title": CATEGORY_BY_SLUG[item.category]["title"],
        "name": item.name,
        "description": item.description,
        "price": item.price,
        "image_url": item.image_url or f"/api/placeholders/{item.id}.svg",
        "is_available": item.is_available,
        "sort_order": item.sort_order,
    }


def serialize_order(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "public_order_number": order.public_order_number,
        "user_id": order.telegram_user_id,
        "status": order.status,
        "payment_status": order.payment_status,
        "payment_mode": order.payment_mode,
        "total": order.total_amount,
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
        "created": "Создан",
        "paid": "Оплачен",
        "preparing": "Готовится",
        "ready": "Готов",
    }
    return (
        f"<b>Заказ №{order.public_order_number}</b>\n"
        f"Статус: <b>{status_labels.get(order.status, order.status)}</b>\n"
        f"Оплата: <b>СБП</b>\n"
        f"Клиент Telegram ID: <code>{order.telegram_user_id}</code>\n\n"
        f"{item_lines}\n\n"
        f"<b>Сумма:</b> {rub(order.total_amount)}"
    )


def build_cashier_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup | None:
    if status == "paid":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Готовится",
                        callback_data=f"order:preparing:{order_id}",
                    ),
                    InlineKeyboardButton(
                        text="Готов",
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
                        text="Готов",
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
    return current + 1


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
    ADMIN_CHAT_ID = message.chat.id
    with db_session() as session:
        save_setting(session, "admin_chat_id", str(message.chat.id))
    await message.answer(
        "Этот чат назначен кассой. Сюда будут приходить оплаченные заказы."
    )


@router.callback_query(F.data.startswith("order:"))
async def handle_order_status_change(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer("Некорректные данные.")
        return

    _, action, raw_order_id = callback.data.split(":")
    order_id = int(raw_order_id)

    with db_session() as session:
        order = fetch_order(session, order_id)

        if action == "preparing":
            if order.status == "ready":
                await callback.answer("Заказ уже отмечен как готовый.")
                return
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
            await callback.answer("Статус обновлен: Готовится.")
            return

        if action == "ready":
            order.status = "ready"
            order.updated_at = now_utc()
            session.commit()
            session.refresh(order)
            _ = order.items
            await notify_customer(
                order,
                f"Заказ №{order.public_order_number} готов и ожидает вас в ресторане.",
            )
            await callback.message.edit_text(format_order_for_cashier(order), reply_markup=None)
            await callback.answer("Статус обновлен: Готов.")
            return

    await callback.answer("Неизвестное действие.")


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    backend = "postgres" if "postgresql+psycopg" in SQLALCHEMY_DATABASE_URL else "sqlite"
    return {"status": "ok", "database": backend}


@app.get("/api/menu")
async def get_menu() -> dict[str, Any]:
    with db_session() as session:
        items = session.scalars(
            select(MenuItem).where(MenuItem.is_available.is_(True)).order_by(MenuItem.category, MenuItem.sort_order)
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
            grouped[item.category].append({
                "id": primary.id,
                "category": item.category,
                "category_title": CATEGORY_BY_SLUG[item.category]["title"],
                "name": group_data["name"],
                "description": group_data["description"],
                "price": primary.price,
                "image_url": f"/api/placeholders/{primary.id}.svg",
                "is_available": True,
                "sort_order": primary.sort_order,
                "variants": [
                    {"id": vi.id, "label": group_data["labels"][vi.id], "price": vi.price}
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
    }


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int) -> dict[str, Any]:
    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)


@app.post("/api/create_order")
async def create_order(payload: CreateOrderRequest) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=400, detail="Cart is empty.")

    requested_quantities: dict[int, int] = {}
    for item in payload.items:
        requested_quantities[item.item_id] = requested_quantities.get(item.item_id, 0) + item.quantity

    with db_session() as session:
        db_items = session.scalars(
            select(MenuItem).where(MenuItem.id.in_(requested_quantities.keys()), MenuItem.is_available.is_(True))
        ).all()
        menu_items = {item.id: item for item in db_items}

        if len(menu_items) != len(requested_quantities):
            raise HTTPException(status_code=400, detail="Some menu items are unavailable.")

        total = 0
        order = Order(
            public_order_number=next_public_order_number(session),
            telegram_user_id=payload.user_id,
            total_amount=0,
            status="created",
            payment_status="pending",
            payment_mode="mock",
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

        order.total_amount = total
        order.updated_at = now_utc()
        session.commit()
        session.refresh(order)
        _ = order.items
        return serialize_order(order)


@app.post("/api/orders/{order_id}/confirm-payment")
async def confirm_payment(order_id: int) -> dict[str, Any]:
    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.payment_status == "paid":
            return serialize_order(order)

        order.payment_status = "paid"
        order.payment_mode = "mock"
        order.status = "paid"
        order.updated_at = now_utc()
        session.commit()
        session.refresh(order)
        _ = order.items
        payload = serialize_order(order)

    await notify_cashier_about_paid_order(order_id)
    return payload


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


@app.get("/api/app-config")
async def app_config() -> dict[str, Any]:
    return {
        "webapp_url": WEBAPP_URL,
        "app_base_url": APP_BASE_URL,
        "bot_configured": bool(BOT_TOKEN),
        "checkout_mode": "mock",
    }
