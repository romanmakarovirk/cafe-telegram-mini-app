from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BASE_DIR
from database import IRKUTSK_TZ, rub
from menu_data import CATEGORY_BY_SLUG
from models import MenuItem, Order


def _resolve_image_url(item: MenuItem) -> str:
    """Return photo URL if a real photo exists, else SVG placeholder."""
    if item.image_url and not item.image_url.startswith("/api/placeholders/"):
        return item.image_url
    for ext in ("jpg", "jpeg", "webp", "png"):
        photo_path = BASE_DIR / "photos" / f"{item.id}.{ext}"
        if photo_path.exists():
            return f"/api/photos/{item.id}.{ext}"
    return f"/api/placeholders/{item.id}.svg"


def _format_available_at(dt: Optional[datetime]) -> Optional[str]:
    """Формат времени доступности для клиента."""
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


def _receipt_url(uuid: str | None) -> str | None:
    """Build OFD receipt URL from ATOL fiscal UUID."""
    if not uuid:
        return None
    return f"https://receipt.atol.ru/{uuid}"


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
        "receipt_url": _receipt_url(order.fiscal_prepayment_uuid),
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
        "ready": "🟢 Выдан",
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
                        text="🟢 Выдан",
                        callback_data=f"order:ready:{order_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(text="⏰ 15м", callback_data=f"preptime:{order_id}:15"),
                    InlineKeyboardButton(text="⏰ 20м", callback_data=f"preptime:{order_id}:20"),
                    InlineKeyboardButton(text="⏰ 30м", callback_data=f"preptime:{order_id}:30"),
                    InlineKeyboardButton(text="⏰ 40м", callback_data=f"preptime:{order_id}:40"),
                ],
            ]
        )
    if status == "preparing":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🟢 Выдать клиенту",
                        callback_data=f"order:ready:{order_id}",
                    )
                ]
            ]
        )
    return None


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
