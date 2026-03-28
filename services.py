"""Бизнес-логика обработки платежей и уведомлений.

ЮKassa обрабатывает фискализацию (54-ФЗ) автоматически — чеки формируются
при создании платежа. Fiscal queue и АТОЛ больше не используются.
"""
from __future__ import annotations

import json as json_module
import logging
from html import escape
from typing import Any

from sqlalchemy import select

import bot_setup
import database
from config import DEFAULT_PREP_TIME_MINUTES
from database import db_session, fetch_order, now_utc, rub
from models import MenuItem, Order, OrderItem
from metrics import ORDERS_PAID, ORDERS_CANCELLED, PAYMENT_DURATION, PAYMENT_ERRORS
from statuses import OrderStatus, PaymentStatus

# Structured audit logger for payment/fiscal events (54-FZ compliance)
_audit = logging.getLogger("audit.payment")


def audit_log(event: str, **kwargs: Any) -> None:
    """Log a structured payment/fiscal event for audit trail."""
    _audit.info("%s | %s", event, " | ".join(f"{k}={v}" for k, v in kwargs.items()))


async def _process_paid_order(order_id: int) -> None:
    """
    Полный автоматический цикл после подтверждения оплаты ЮKassa:
    1. Обновить статус заказа -> paid
    2. Синхронизация с 1С:Бухгалтерия
    3. Статус -> preparing (заказ передан на кухню)
    4. Уведомление клиенту в Telegram
    5. Уведомление кассиру

    Фискализация (54-ФЗ) выполняется автоматически ЮKassa
    при создании платежа — здесь ничего делать не нужно.
    """
    from integrations.accounting import sync_order_to_1c

    with db_session() as session:
        rows_updated = session.execute(
            select(Order).where(
                Order.id == order_id,
                Order.payment_status.in_((PaymentStatus.PENDING, PaymentStatus.EXPIRED)),
            ).with_for_update()
        )
        order = rows_updated.scalar_one_or_none()

        if order is None:
            return

        # Check stoplist — cancel if items became unavailable after order creation
        unavailable_items = (
            session.query(MenuItem)
            .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
            .filter(OrderItem.order_id == order.id, MenuItem.is_available.is_(False))
            .all()
        )
        if unavailable_items:
            names = ", ".join(item.name for item in unavailable_items)

            # Сначала отменяем заказ и ОСВОБОЖДАЕМ блокировку (commit),
            # потом делаем сетевой вызов рефанда (await) вне FOR UPDATE lock.
            gateway_oid = order.gateway_order_id
            refund_amount = order.total_amount
            stoplist_order_id = order.id
            stoplist_order_number = order.public_order_number

            order.status = OrderStatus.CANCELLED
            order.payment_status = PaymentStatus.REFUND_PENDING
            order.updated_at = now_utc()
            session.commit()

        # --- Вне FOR UPDATE lock: сетевой вызов рефанда ---
        if unavailable_items:
            if gateway_oid:
                from payments.yookassa_payment import refund_yookassa_payment
                refund_result = await refund_yookassa_payment(gateway_oid, refund_amount)
                with db_session() as refund_session:
                    o = refund_session.scalars(
                        select(Order).where(Order.id == stoplist_order_id).with_for_update()
                    ).first()
                    if o:
                        if refund_result.success:
                            o.payment_status = PaymentStatus.REFUNDED
                            audit_log("STOPLIST_REFUND", order_id=stoplist_order_id,
                                      order_number=stoplist_order_number, items=names)
                            # ЮKassa автоматически создаёт чек возврата (54-ФЗ)
                        else:
                            o.payment_status = PaymentStatus.REFUND_FAILED
                            logging.critical(
                                "STOPLIST REFUND FAILED: order %d, error: %s",
                                stoplist_order_id, refund_result.error_message,
                            )
                        refund_session.commit()

                if not refund_result.success:
                    from bot_handlers import alert_admin as alert_admin_refund
                    await alert_admin_refund(
                        f"ВОЗВРАТ НЕ УДАЛСЯ! Заказ #{stoplist_order_number}: "
                        f"деньги ({rub(refund_amount)}) НЕ возвращены клиенту. "
                        f"Ошибка ЮKassa: {refund_result.error_message}. Требуется ручной возврат!"
                    )
            else:
                with db_session() as refund_session:
                    o = refund_session.get(Order, stoplist_order_id)
                    if o:
                        o.payment_status = PaymentStatus.CANCELLED
                        refund_session.commit()

            ORDERS_CANCELLED.inc()
            logging.warning(
                "Order %s cancelled at payment: unavailable items: %s",
                stoplist_order_id, names,
            )
            from bot_handlers import alert_admin
            await alert_admin(
                f"Заказ #{stoplist_order_number} отменён при оплате — "
                f"позиции в стоп-листе: {names}."
            )
            return

        order.payment_status = PaymentStatus.PAID
        order.status = OrderStatus.PAID
        order.updated_at = now_utc()
        ORDERS_PAID.inc()
        audit_log("ORDER_PAID", order_id=order_id, order_number=order.public_order_number,
                  amount=order.total_amount)

        total_amount = order.total_amount
        order_number = order.public_order_number
        user_id = order.telegram_user_id

        _ = order.items  # eager load
        fiscal_items = [
            {
                "name_snapshot": item.name_snapshot,
                "price_snapshot": item.price_snapshot,
                "quantity": item.quantity,
            }
            for item in order.items
        ]
        session.commit()

    # 2. Синхронизация с 1С:Бухгалтерия
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

    # 3. Автоматически переводим в «Готовится» (только если кассир ещё не сменил статус)
    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.status == OrderStatus.PAID:
            order.status = OrderStatus.PREPARING
            order.updated_at = now_utc()
            session.commit()

    # 4. Уведомление клиенту
    if bot_setup.bot:
        try:
            await bot_setup.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ Заказ №{order_number} оплачен!\n\n"
                    f"Сумма: {rub(total_amount)}\n"
                    f"Статус: <b>Готовится</b>\n"
                    f"⏰ Примерное время: ~{DEFAULT_PREP_TIME_MINUTES} мин\n\n"
                    "Мы сообщим, когда заказ будет готов к выдаче."
                ),
            )
        except Exception:
            logging.exception("Не удалось уведомить клиента %s", user_id)

    # 5. Уведомление кассиру (с клавиатурой управления заказом)
    from bot_handlers import notify_cashier_about_paid_order
    await notify_cashier_about_paid_order(order_id)


async def notify_admin_about_order(order_id: int) -> None:
    """Отправка информационного уведомления администратору (без кнопок управления)."""
    if bot_setup.bot is None or bot_setup.ADMIN_CHAT_ID is None:
        return

    with db_session() as session:
        order = fetch_order(session, order_id)
        item_lines = "\n".join(
            f"• {escape(item.name_snapshot)} x{item.quantity} = {rub(item.subtotal)}"
            for item in order.items
        )
        text = (
            f"🆕 <b>Новый заказ №{order.public_order_number}</b>\n"
            f"Статус: <b>Готовится</b> (автоматически)\n"
            f"Оплата: <b>ЮKassa</b> ✅\n\n"
            f"{item_lines}\n\n"
            f"<b>Сумма:</b> {rub(order.total_amount)}"
        )
        try:
            await bot_setup.bot.send_message(chat_id=bot_setup.ADMIN_CHAT_ID, text=text)
        except Exception:
            logging.exception("Не удалось уведомить админа о заказе %d", order.public_order_number)
