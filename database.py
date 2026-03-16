from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from config import (
    BASE_DIR,
    SQLALCHEMY_DATABASE_URL,
    ENGINE_KWARGS,
)
from models import AppSetting, Base, MenuItem, Order
from menu_data import MENU_SEED

# ── Database engine ───────────────────────────────────────────────────────
engine = create_engine(SQLALCHEMY_DATABASE_URL, **ENGINE_KWARGS)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ── Working hours (Irkutsk UTC+8) ────────────────────────────────────────
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
    # Use module-level lookup so test patches to database.SessionLocal take effect
    return sys.modules[__name__].SessionLocal()


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
        existing.image_url = f"/api/placeholders/{item['id']}.svg"
        existing.sort_order = item["sort_order"]
    session.commit()


def _migrate_sqlite_columns() -> None:
    """Add new nullable columns to existing SQLite tables."""
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
    import bot_setup
    Base.metadata.create_all(bind=sys.modules[__name__].engine)
    _migrate_sqlite_columns()
    with db_session() as session:
        seed_menu_items(session)
        saved_admin_chat_id = load_setting(session, "admin_chat_id")
        if saved_admin_chat_id and not bot_setup.ADMIN_CHAT_ID:
            bot_setup.ADMIN_CHAT_ID = int(saved_admin_chat_id)


def next_public_order_number(session: Session) -> int:
    current = session.scalar(select(func.max(Order.public_order_number)))
    if current is None:
        return 4648
    next_num = current + 1
    if next_num > 99999:
        next_num = 10000
    return next_num


def fetch_order(session: Session, order_id: int) -> Order:
    from fastapi import HTTPException
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    _ = order.items
    return order
