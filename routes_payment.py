"""Платёжные эндпоинты: создание платежа, проверка статуса, webhook, mock-оплата."""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path as FastPath, Request
from sqlalchemy import select

import bot_setup
import database
from config import DEV_MODE
from database import db_session, fetch_order, now_utc
from metrics import PAYMENT_WEBHOOKS, PAYMENT_ERRORS
from models import Order
from security import (
    callback_limiter,
    get_client_ip,
    get_verified_user_id,
    order_limiter,
    payment_check_limiter,
)
from serializers import serialize_order
from services import audit_log
from statuses import OrderStatus, PaymentStatus


def _get_process_paid_order():
    """Lazy lookup: позволяет тестам патчить _process_paid_order через routes module."""
    import sys
    routes_mod = sys.modules.get("routes")
    if routes_mod and hasattr(routes_mod, "_process_paid_order"):
        return routes_mod._process_paid_order
    from services import _process_paid_order
    return _process_paid_order

payment_router = APIRouter()


@payment_router.post("/api/payment/create/{order_id}")
async def create_payment(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Создаёт платёж через ЮKassa (с автоматической фискализацией 54-ФЗ)."""
    from payments.yookassa_payment import create_yookassa_payment

    verified_user_id = get_verified_user_id(request)
    if not order_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Too many payment requests.")

    # 1. Проверяем и блокируем заказ, собираем данные
    with db_session() as session:
        order = session.scalars(
            select(Order).where(Order.id == order_id).with_for_update()
        ).first()
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.status == OrderStatus.CANCELLED:
            raise HTTPException(status_code=400, detail="Заказ отменён. Создайте новый заказ.")

        if order.payment_status == PaymentStatus.PAID:
            _ = order.items
            return {"status": "already_paid", **serialize_order(order)}

        if order.gateway_order_id == "creating":
            return {"status": "creating", "message": "Платёж создаётся, подождите."}

        if order.gateway_order_id:
            _ = order.items
            return {
                "status": "payment_exists",
                "gateway_order_id": order.gateway_order_id,
                "order": serialize_order(order),
            }

        # Ставим маркер до HTTP-вызова — предотвращает двойное создание платежа
        create_order_id = order.id
        create_order_number = order.public_order_number
        create_total_amount = order.total_amount
        # Собираем позиции для чека (54-ФЗ) в одной транзакции
        order_items = [
            {
                "name_snapshot": item.name_snapshot,
                "price_snapshot": item.price_snapshot,
                "quantity": item.quantity,
            }
            for item in order.items
        ]
        order.gateway_order_id = "creating"
        order.updated_at = now_utc()
        session.commit()

    # 2. HTTP-вызов к ЮKassa — вне FOR UPDATE lock
    result = await create_yookassa_payment(
        order_id=create_order_id,
        order_number=create_order_number,
        total_amount=create_total_amount,
        items=order_items,
    )

    # 3. Сохраняем результат (compare-and-set: только если маркер ещё "creating")
    with db_session() as session:
        order = session.scalars(
            select(Order).where(
                Order.id == create_order_id,
                Order.gateway_order_id == "creating",
            ).with_for_update()
        ).first()
        if order:
            if result.success:
                order.gateway_order_id = result.payment_id
                order.payment_mode = "yookassa"
                order.updated_at = now_utc()
                session.commit()
                audit_log("PAYMENT_CREATED", order_id=order.id, gateway_id=result.payment_id,
                          amount=order.total_amount)
            else:
                order.gateway_order_id = None
                order.updated_at = now_utc()
                session.commit()
        else:
            session.commit()
            logging.warning(
                "Create-payment: order %d gateway_order_id no longer 'creating' — "
                "skipping result save (concurrent update detected)",
                create_order_id,
            )

    if not result.success:
        logging.error("YooKassa create payment failed for order %d: %s", order_id, result.error_message)
        from bot_handlers import alert_admin
        await alert_admin(f"YooKassa create-payment failed: order #{order_id}, error: {result.error_message}")
        raise HTTPException(status_code=502, detail="Платёжная система временно недоступна. Попробуйте через 1-2 минуты.")

    return {
        "status": "created",
        "confirmation_url": result.confirmation_url,
        "payment_id": result.payment_id,
    }


@payment_router.get("/api/payment/check-status/{order_id}")
async def check_payment_status(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Проверяет статус платежа через ЮKassa API."""
    from payments.yookassa_payment import check_yookassa_payment

    verified_user_id = get_verified_user_id(request)

    if not payment_check_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Too many status checks. Please wait.")

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.payment_status == PaymentStatus.PAID:
            return {"status": "paid", **serialize_order(order)}

        if not order.gateway_order_id or order.gateway_order_id == "creating":
            return {"status": "creating" if order.gateway_order_id == "creating" else "no_payment"}

        gateway_order_id = order.gateway_order_id

    result = await check_yookassa_payment(gateway_order_id)

    if not result.success:
        logging.warning("YooKassa check status failed for order %d: %s", order_id, result.error_message)
        return {"status": "check_error", "error": "Не удалось проверить статус платежа."}

    if result.is_paid:
        # Собираем данные в короткой транзакции, await — снаружи
        amount_mismatch = False
        mismatch_data = None

        with db_session() as session:
            order_check = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).first()
            if order_check is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            if order_check.payment_status == PaymentStatus.PAID:
                _ = order_check.items
                session.commit()
                return {"status": "paid", **serialize_order(order_check)}
            expected_kopecks = order_check.total_amount * 100
            if result.amount is not None and result.amount != expected_kopecks:
                order_check.payment_status = PaymentStatus.AMOUNT_MISMATCH
                order_check.updated_at = now_utc()
                session.commit()
                amount_mismatch = True
                mismatch_data = {
                    "order_number": order_check.public_order_number,
                    "expected": expected_kopecks,
                    "got": result.amount,
                }
            else:
                session.commit()

        if amount_mismatch:
            logging.critical(
                "AMOUNT MISMATCH: order %d expected %d kopecks, got %d from YooKassa",
                order_id, mismatch_data["expected"], mismatch_data["got"],
            )
            audit_log("AMOUNT_MISMATCH_CHECK_STATUS", order_id=order_id,
                      expected=mismatch_data["expected"], got=mismatch_data["got"])
            from bot_handlers import alert_admin
            await alert_admin(
                f"AMOUNT MISMATCH (check_status): заказ #{mismatch_data['order_number']}, "
                f"ожидали {mismatch_data['expected']} коп, получили {mismatch_data['got']} коп"
            )
            return {"status": "amount_mismatch", "error": "Сумма оплаты не совпадает с суммой заказа."}

        await _get_process_paid_order()(order_id)

        with db_session() as session:
            order = fetch_order(session, order_id)
            return {"status": "paid", **serialize_order(order)}

    if result.status == "canceled":
        return {"status": "canceled"}

    return {
        "status": result.status,
        "paid": result.paid,
    }


@payment_router.post("/api/payment/webhook")
async def yookassa_webhook(request: Request) -> dict[str, str]:
    """Webhook от ЮKassa при изменении статуса платежа."""
    from payments.yookassa_payment import is_trusted_ip, check_yookassa_payment

    client_ip = get_client_ip(request)
    if not callback_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many callback requests.")

    # Проверяем что запрос от ЮKassa
    if not is_trusted_ip(client_ip):
        logging.warning("ЮKassa webhook: недоверенный IP %s", client_ip)
        raise HTTPException(status_code=403, detail="Untrusted IP")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = body.get("event", "")
    payment_obj = body.get("object", {})
    if not isinstance(payment_obj, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook structure")
    payment_id = payment_obj.get("id", "")

    logging.info(
        "ЮKassa webhook: event=%s, payment_id=%s",
        event_type, payment_id,
    )

    if event_type == "payment.succeeded":
        # 1. Короткая блокировка: находим заказ, коммитим, закрываем сессию
        order_id_to_process = None
        expected_kopecks = None

        with db_session() as session:
            order = session.scalars(
                select(Order).where(Order.gateway_order_id == payment_id).with_for_update()
            ).first()

            if order and order.payment_status == PaymentStatus.PAID:
                logging.info(
                    "ЮKassa webhook: дубликат для уже оплаченного заказа %d (replay ignored)", order.id
                )
                session.commit()
                return {"status": "ok"}

            if order and order.payment_status not in (
                PaymentStatus.PAID, PaymentStatus.AMOUNT_MISMATCH, PaymentStatus.CANCELLED,
                PaymentStatus.REFUNDED, PaymentStatus.REFUND_PENDING, PaymentStatus.REFUND_FAILED,
            ):
                order_id_to_process = order.id
                expected_kopecks = order.total_amount * 100

            session.commit()  # release FOR UPDATE lock

        # 2. Верификация через API ЮKassa — НЕ доверяем webhook body
        #    (defense in depth: IP-заголовки могут быть подменены при обходе Cloudflare)
        if order_id_to_process is not None:
            api_result = await check_yookassa_payment(payment_id)

            if not api_result.success:
                logging.error(
                    "ЮKassa webhook: не удалось верифицировать платёж %s через API: %s",
                    payment_id, api_result.error_message,
                )
                # Fail-closed: без верификации не обрабатываем.
                # Polling worker подхватит через 15 сек.
                return {"status": "ok"}

            if not api_result.is_paid:
                logging.warning(
                    "ЮKassa webhook payment.succeeded, но API: status=%s paid=%s для %s",
                    api_result.status, api_result.paid, payment_id,
                )
                return {"status": "ok"}

            # Проверка суммы из API (не из webhook body!)
            if api_result.amount is not None and api_result.amount != expected_kopecks:
                logging.critical(
                    "AMOUNT MISMATCH (API verified): order %d expected %d, got %d",
                    order_id_to_process, expected_kopecks, api_result.amount,
                )
                with db_session() as session:
                    mismatch_order = session.scalars(
                        select(Order).where(Order.id == order_id_to_process).with_for_update()
                    ).first()
                    if mismatch_order and mismatch_order.payment_status not in (
                        PaymentStatus.PAID, PaymentStatus.AMOUNT_MISMATCH,
                    ):
                        mismatch_order.payment_status = PaymentStatus.AMOUNT_MISMATCH
                        mismatch_order.updated_at = now_utc()
                    session.commit()

                audit_log("AMOUNT_MISMATCH", order_id=order_id_to_process,
                          expected=expected_kopecks, got=api_result.amount)
                from bot_handlers import alert_admin
                await alert_admin(
                    f"⚠️ AMOUNT MISMATCH! Заказ ID {order_id_to_process}: "
                    f"ожидали {expected_kopecks} коп, получили {api_result.amount} коп."
                )
                PAYMENT_WEBHOOKS.labels(result="amount_mismatch").inc()
                return {"status": "ok"}

            # API подтвердил: платёж реальный, сумма совпадает
            await _get_process_paid_order()(order_id_to_process)
            audit_log("WEBHOOK_PAID", order_id=order_id_to_process, payment_id=payment_id)
            logging.info("ЮKassa webhook: заказ %d оплачен (API verified)", order_id_to_process)
            PAYMENT_WEBHOOKS.labels(result="paid").inc()

    elif event_type == "payment.canceled":
        logging.info("ЮKassa webhook: платёж %s отменён", payment_id)
        # Быстро обновляем статус — не ждём таймаута
        with db_session() as session:
            order = session.scalars(
                select(Order).where(Order.gateway_order_id == payment_id).with_for_update()
            ).first()
            if order and order.payment_status == PaymentStatus.PENDING:
                order.payment_status = PaymentStatus.CANCELLED
                order.status = OrderStatus.CANCELLED
                order.updated_at = now_utc()
            session.commit()

    return {"status": "ok"}


@payment_router.post("/api/orders/{order_id}/confirm-payment")
async def confirm_payment(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Mock оплата для тестирования. Только при DEV_MODE=true."""
    if not DEV_MODE:
        raise HTTPException(
            status_code=403,
            detail="Mock payments disabled in production. Use /api/payment/create.",
        )
    verified_user_id = get_verified_user_id(request)

    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.payment_status == PaymentStatus.PAID:
            return serialize_order(order)

    await _get_process_paid_order()(order_id)

    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)
