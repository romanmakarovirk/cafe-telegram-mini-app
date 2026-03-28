"""Kitchen printer API + admin endpoints (accounting, stoplist)."""
from __future__ import annotations

import logging
import re as _re
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path as FastPath, Request, Response
from sqlalchemy import func, select

import bot_setup
import database
from database import db_session, fetch_order, now_utc
from menu_data import CATEGORY_BY_SLUG
from models import MenuItem, Order
from security import StopListRequest, verify_kitchen_api_key
from serializers import _format_available_at, serialize_order
from statuses import OrderStatus, PaymentStatus

kitchen_router = APIRouter()


# ── Kitchen printer API ──────────────────────────────────────────────────

@kitchen_router.get("/api/kitchen/pending")
async def kitchen_pending(request: Request) -> dict[str, Any]:
    """Заказы, ожидающие печати на кухне."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        orders = session.scalars(
            select(Order).where(
                Order.status.in_([OrderStatus.PAID, OrderStatus.PREPARING]),
                Order.kitchen_printed.is_(False),
            ).order_by(Order.created_at)
        ).all()

        result = []
        for order in orders:
            _ = order.items
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


@kitchen_router.post("/api/kitchen/printed/{order_id}")
async def kitchen_mark_printed(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, str]:
    """Агент печати подтверждает, что заказ напечатан на кухне."""
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


@kitchen_router.post("/api/orders/{order_id}/mark-ready")
async def mark_order_ready(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Пометить заказ как выданный клиенту."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        order = session.scalars(
            select(Order).where(Order.id == order_id).with_for_update()
        ).first()
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        _ = order.items

        if order.status == OrderStatus.READY:
            return serialize_order(order)

        if order.payment_status != PaymentStatus.PAID:
            raise HTTPException(
                status_code=400,
                detail=f"Заказ не оплачен (статус: {order.payment_status}). Нельзя пометить готовым.",
            )

        order.status = OrderStatus.READY
        order.updated_at = now_utc()

        session.commit()
        session.refresh(order)
        _ = order.items

        user_id = order.telegram_user_id
        order_number = order.public_order_number

    if bot_setup.bot:
        try:
            await bot_setup.bot.send_message(
                chat_id=user_id,
                text=f"✅ Заказ №{order_number} выдан. Приятного аппетита!",
            )
        except Exception:
            logging.exception("Не удалось уведомить клиента о готовности заказа %d", order_number)

    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)


# ── 1C Accounting admin ──────────────────────────────────────────────────

@kitchen_router.get("/api/admin/accounting-status")
async def accounting_status(request: Request) -> dict[str, Any]:
    """Статус синхронизации заказов с 1С:Бухгалтерия."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        total_paid = session.scalar(
            select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)
        ) or 0
        total_synced = session.scalar(
            select(func.count(Order.id)).where(
                Order.payment_status == PaymentStatus.PAID,
                Order.accounting_synced.is_(True),
            )
        ) or 0
        total_failed = total_paid - total_synced

        unsynced_orders = session.scalars(
            select(Order).where(
                Order.payment_status == PaymentStatus.PAID,
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


@kitchen_router.post("/api/admin/accounting-retry/{order_id}")
async def accounting_retry(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Повторная синхронизация заказа с 1С."""
    verify_kitchen_api_key(request)

    from integrations.accounting import sync_order_to_1c

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.payment_status != PaymentStatus.PAID:
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


# ── Stoplist admin ───────────────────────────────────────────────────────

@kitchen_router.get("/api/admin/stoplist")
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


@kitchen_router.post("/api/admin/stoplist")
async def manage_stoplist(payload: StopListRequest, request: Request) -> dict[str, Any]:
    """Управление стоп-листом: отключить/включить блюдо или категорию."""
    verify_kitchen_api_key(request)

    if not payload.item_id and not payload.category:
        raise HTTPException(status_code=400, detail="Укажите item_id или category.")

    clean_reason = payload.reason
    if clean_reason:
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
            else:
                item.is_available = True
                item.unavailable_reason = None
                item.available_at = None

            affected.append({"id": item.id, "name": item.name, "is_available": item.is_available})

        session.commit()

    from routes import invalidate_menu_cache
    invalidate_menu_cache()
    action_text = "отключено" if payload.action == "disable" else "включено"
    logging.info("Стоп-лист: %s %d позиций", action_text, len(affected))
    return {"action": payload.action, "affected": affected, "count": len(affected)}
