from __future__ import annotations

import json as json_module
import logging
import os
import re as _re
import sys
import uuid
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, HTTPException, Path as FastPath, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware

import database
from config import (
    APP_BASE_URL,
    BASE_DIR,
    BOT_TOKEN,
    DEFAULT_PREP_TIME_MINUTES,
    DEV_MODE,
    MAX_ITEMS_PER_ORDER,
    MAX_ORDER_TOTAL_RUB,
    ORDER_PAYMENT_TIMEOUT_MINUTES,
    SQLALCHEMY_DATABASE_URL,
    WEBAPP_URL,
)
from database import db_session, fetch_order, now_utc, next_public_order_number, rub
from menu_data import CATEGORY_BY_SLUG, CATEGORY_META, ITEM_TO_VARIANT_GROUP, VARIANT_GROUPS
from models import FiscalQueue, MenuItem, Order, OrderItem, Review
from security import (
    CreateOrderRequest,
    StopListRequest,
    SubmitReviewRequest,
    callback_limiter,
    general_limiter,
    get_client_ip,
    get_verified_user_id,
    get_verified_user_info,
    order_limiter,
    review_limiter,
    sbp_check_limiter,
    verify_kitchen_api_key,
)
from serializers import _format_available_at, _resolve_image_url, serialize_menu_item, serialize_order
import bot_setup

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None

router = APIRouter()

# Structured audit logger for payment/fiscal events (54-FZ compliance)
_audit = logging.getLogger("audit.payment")

# In-memory menu cache (TTL 5 min) — avoids DB query on every /api/menu request
_menu_cache: dict[str, Any] = TTLCache(maxsize=4, ttl=300) if TTLCache else {}


def invalidate_menu_cache() -> None:
    """Сброс кэша меню при изменении стоп-листа или позиций."""
    _menu_cache.clear()


def audit_log(event: str, **kwargs: Any) -> None:
    """Log a structured payment/fiscal event for audit trail."""
    _audit.info("%s | %s", event, " | ".join(f"{k}={v}" for k, v in kwargs.items()))


def _get_cafe_schedule() -> dict[str, Any]:
    """Look up get_cafe_schedule through main module for test patchability."""
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "get_cafe_schedule"):
        return main_mod.get_cafe_schedule()
    return database.get_cafe_schedule()


# ── Security headers middleware ───────────────────────────────────────────

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


# ── Request ID middleware ─────────────────────────────────────────────────

class RequestIdMiddleware(BaseHTTPMiddleware):
    """Adds a unique X-Request-Id header to every request/response for tracing."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


# ── Exception middleware ─────────────────────────────────────────────────

class ExceptionMiddleware(BaseHTTPMiddleware):
    """Catches unhandled exceptions, logs them with request context, returns structured JSON."""

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            request_id = getattr(request.state, "request_id", "unknown")
            logging.exception(
                "Unhandled exception: method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
                headers={"X-Request-Id": request_id},
            )


# ── Static / health / readiness ──────────────────────────────────────────

@router.get("/", include_in_schema=False)
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


@router.get("/cashier-guide")
async def cashier_guide() -> FileResponse:
    """Инструкция для кассира."""
    return FileResponse(BASE_DIR / "cashier-guide.html", media_type="text/html")


@router.get("/healthz")
async def healthz_liveness() -> dict[str, str]:
    """Liveness probe — лёгкий, без обращений к БД. Для Render / k8s."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz_readiness() -> Response:
    """Readiness probe — полная диагностика: БД, бот, АТОЛ, 1С, SBP."""
    checks: dict[str, Any] = {}
    overall = True
    backend = "postgres" if "postgresql+psycopg" in SQLALCHEMY_DATABASE_URL else "sqlite"

    # 1. Database
    try:
        with db_session() as session:
            session.execute(select(func.count(MenuItem.id)))
        checks["database"] = {"status": "ok", "backend": backend}
    except Exception:
        checks["database"] = {"status": "error", "backend": backend}
        overall = False

    # 2. Telegram bot
    checks["telegram_bot"] = {"status": "ok" if bot_setup.bot else "not_configured"}

    # 3. ATOL (fiscalization)
    atol_configured = bool(os.getenv("ATOL_LOGIN") and os.getenv("ATOL_GROUP_CODE"))
    checks["atol"] = {"status": "configured" if atol_configured else "not_configured"}

    # 4. 1C:Fresh
    fresh_configured = bool(os.getenv("FRESH_BASE_URL") and os.getenv("FRESH_ENABLED", "").lower() in ("true", "1"))
    checks["accounting_1c"] = {"status": "configured" if fresh_configured else "not_configured"}

    # 5. SBP payments
    sbp_configured = bool(os.getenv("SBP_USERNAME") or os.getenv("SBP_TOKEN"))
    checks["sbp_payments"] = {"status": "configured" if sbp_configured else "not_configured"}

    # 6. Secrets health
    checks["secrets"] = {
        "bot_token": "set" if BOT_TOKEN else "missing",
        "kitchen_key": "set" if os.getenv("KITCHEN_API_KEY", "").strip() else "missing",
        "admin_ids": "set" if os.getenv("ALLOWED_ADMIN_IDS", "").strip() else "missing",
    }

    status_code = 200 if overall else 503
    return JSONResponse(
        {"status": "ok" if overall else "degraded", "checks": checks, "dev_mode": DEV_MODE},
        status_code=status_code,
    )


# ── Menu ─────────────────────────────────────────────────────────────────

@router.get("/api/menu")
async def get_menu(request: Request) -> dict[str, Any]:
    client_ip = get_client_ip(request)
    if not general_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")

    # Проверяем кэш (TTL 5 мин)
    cached = _menu_cache.get("menu_categories")
    if cached is not None:
        return {
            **cached,
            "schedule": _get_cafe_schedule(),  # schedule всегда свежее
        }

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
            group_available = any(vi.is_available for vi in variant_items)
            group_reason = None
            group_available_at = None
            if not group_available:
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

    result = {
        "categories": categories,
        "items_count": sum(len(category["items"]) for category in categories),
        "global_note": "Чай чёрный/зелёный 200 мл — бесплатно к каждому заказу",
    }
    _menu_cache["menu_categories"] = result

    return {
        **result,
        "schedule": _get_cafe_schedule(),
    }


@router.get("/api/schedule")
async def get_schedule() -> dict[str, Any]:
    return _get_cafe_schedule()


# ── Orders ───────────────────────────────────────────────────────────────

@router.get("/api/orders/{order_id}")
async def get_order(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    verified_user_id = get_verified_user_id(request)
    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")
        return serialize_order(order)


@router.post("/api/create_order")
async def create_order(payload: CreateOrderRequest, request: Request) -> dict[str, Any]:
    verified_user_id, customer_name = get_verified_user_info(request)

    if not order_limiter.check(str(verified_user_id)):
        raise HTTPException(status_code=429, detail="Слишком много заказов. Подождите минуту.")

    if not payload.items:
        raise HTTPException(status_code=400, detail="Cart is empty.")

    schedule = _get_cafe_schedule()
    if not schedule["is_open"]:
        raise HTTPException(
            status_code=400,
            detail=f"Кафе сейчас закрыто. Часы работы: {schedule['opens_at']}–{schedule['closes_at']} (Иркутск). Последний заказ в {schedule['last_order_at']}.",
        )

    # Check if ordering is paused by admin
    with db_session() as _pause_session:
        pause_reason = database.is_ordering_paused(_pause_session)
        if pause_reason:
            raise HTTPException(status_code=400, detail=pause_reason)

    requested_quantities: dict[int, int] = {}
    for item in payload.items:
        requested_quantities[item.item_id] = requested_quantities.get(item.item_id, 0) + item.quantity

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

                if total <= 0:
                    session.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail="Сумма заказа должна быть больше 0.",
                    )
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


# ── SBP Payments ─────────────────────────────────────────────────────────

@router.post("/api/sbp/create-payment/{order_id}")
async def sbp_create_payment(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Создаёт платёж через СБП Сбербанк."""
    from payments.sbp import create_sbp_payment

    verified_user_id = get_verified_user_id(request)

    # 1. Проверяем и блокируем заказ, собираем данные для СБП
    with db_session() as session:
        order = session.scalars(
            select(Order).where(Order.id == order_id).with_for_update()
        ).first()
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        if order.telegram_user_id != verified_user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

        if order.status == "cancelled":
            raise HTTPException(status_code=400, detail="Заказ отменён. Создайте новый заказ.")

        if order.payment_status == "paid":
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
        order.gateway_order_id = "creating"
        session.commit()

    # 2. HTTP-вызов к СБП — вне FOR UPDATE lock
    result = await create_sbp_payment(
        order_id=create_order_id,
        order_number=create_order_number,
        total_amount=create_total_amount,
    )

    # 3. Сохраняем результат (или сбрасываем маркер при ошибке)
    with db_session() as session:
        order = session.get(Order, create_order_id)
        if order:
            if result.success:
                order.gateway_order_id = result.order_id
                order.payment_mode = "sbp"
                order.updated_at = now_utc()
                session.commit()
                audit_log("PAYMENT_CREATED", order_id=order.id, gateway_id=result.order_id,
                          amount=order.total_amount)
            else:
                order.gateway_order_id = None
                session.commit()

    if not result.success:
        logging.error("SBP create payment failed for order %d: %s", order_id, result.error_message)
        raise HTTPException(status_code=502, detail="Ошибка создания платежа. Попробуйте ещё раз.")

    return {
        "status": "created",
        "deeplink": result.deeplink,
        "payment_url": result.payment_url,
        "gateway_order_id": result.order_id,
    }


@router.get("/api/sbp/check-status/{order_id}")
async def sbp_check_status(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Проверяет статус платежа через Sberbank API."""
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

        if not order.gateway_order_id or order.gateway_order_id == "creating":
            return {"status": "creating" if order.gateway_order_id == "creating" else "no_payment"}

        gateway_order_id = order.gateway_order_id

    result = await check_sbp_payment(gateway_order_id)

    if not result.success:
        logging.warning("SBP check status failed for order %d: %s", order_id, result.error_message)
        return {"status": "check_error", "error": "Не удалось проверить статус платежа."}

    if result.is_paid:
        with db_session() as session:
            order_check = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).first()
            if order_check is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            if order_check.payment_status == "paid":
                _ = order_check.items
                session.commit()
                return {"status": "paid", **serialize_order(order_check)}
            expected_kopecks = order_check.total_amount * 100
            if result.amount is None:
                logging.error(
                    "SBP check_status: amount=None for paid order %d, blocking",
                    order_id,
                )
                audit_log("AMOUNT_NULL_CHECK_STATUS", order_id=order_id)
                from bot_handlers import alert_admin
                await alert_admin(
                    f"⚠️ SBP check_status: заказ #{order_check.public_order_number} оплачен, "
                    f"но SBP API не вернул сумму. Требуется ручная проверка."
                )
                return {"status": "check_error", "error": "Не удалось проверить сумму платежа. Обратитесь к кассиру."}
            if result.amount != expected_kopecks:
                order_check.payment_status = "amount_mismatch"
                order_check.updated_at = now_utc()
                session.commit()
                logging.critical(
                    "AMOUNT MISMATCH: order %d expected %d kopecks, got %d from SBP",
                    order_id, expected_kopecks, result.amount,
                )
                audit_log("AMOUNT_MISMATCH_CHECK_STATUS", order_id=order_id,
                          expected=expected_kopecks, got=result.amount)
                from bot_handlers import alert_admin
                await alert_admin(
                    f"AMOUNT MISMATCH (check_status): заказ #{order_check.public_order_number}, "
                    f"ожидали {expected_kopecks} коп, получили {result.amount} коп"
                )
                return {"status": "amount_mismatch", "error": "Сумма оплаты не совпадает с суммой заказа."}

        await _process_paid_order(order_id)

        with db_session() as session:
            order = fetch_order(session, order_id)
            return {"status": "paid", **serialize_order(order)}

    return {
        "status": result.status_label,
        "order_status": result.order_status,
    }


@router.post("/api/sbp/callback")
async def sbp_callback(request: Request) -> dict[str, str]:
    """Callback от Сбербанка при изменении статуса платежа."""
    from payments.sbp import verify_callback

    client_ip = get_client_ip(request)
    if not callback_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many callback requests.")

    params = dict(request.query_params)
    try:
        form_data = await request.form()
        for key, value in form_data.items():
            if key not in params:
                params[key] = value
    except Exception as e:
        logging.warning("СБП callback: ошибка парсинга form data: %s", e)

    md_order = params.get("mdOrder", "")
    order_number = params.get("orderNumber", "")
    operation = params.get("operation", "")
    status = params.get("status", "")
    checksum = params.get("checksum", "")

    expected_keys = {"mdOrder", "orderNumber", "operation", "status", "checksum"}
    extra_keys = set(params.keys()) - expected_keys
    if extra_keys:
        logging.warning("СБП callback: неожиданные параметры: %s (mdOrder=%s)", sorted(extra_keys), md_order)

    logging.info(
        "СБП callback: mdOrder=%s, orderNumber=%s, operation=%s, status=%s",
        md_order, order_number, operation, status,
    )

    if not verify_callback(md_order, order_number, operation, status, checksum):
        logging.warning("СБП callback: неверная подпись для %s", md_order)
        raise HTTPException(status_code=403, detail="Invalid checksum")

    if operation == "deposited" and status == "1":
        # 1. Короткая блокировка: читаем данные заказа и освобождаем lock
        order_id_to_process = None
        expected_kopecks = None
        with db_session() as session:
            order = session.scalars(
                select(Order).where(Order.gateway_order_id == md_order).with_for_update()
            ).first()

            if order and order.payment_status == "paid":
                logging.info(
                    "СБП callback: дубликат для уже оплаченного заказа %d (replay ignored)", order.id
                )
                session.commit()
                return {"status": "ok"}

            if order and order.payment_status not in ("paid", "amount_mismatch"):
                order_id_to_process = order.id
                expected_kopecks = order.total_amount * 100
            session.commit()  # release FOR UPDATE lock

        # 2. HTTP-вызов к SBP API ВНЕ блокировки (не держим lock на время сети)
        if order_id_to_process is not None:
            from payments.sbp import check_sbp_payment
            verify_result = await check_sbp_payment(md_order)
            if not verify_result.success or verify_result.amount is None:
                logging.warning(
                    "СБП callback: верификация суммы не удалась (SBP API unavailable), "
                    "заказ %d НЕ обработан — ждём polling worker", order_id_to_process,
                )
                from bot_handlers import alert_admin
                await alert_admin(
                    f"⚠️ СБП callback: не удалось верифицировать сумму для заказа {order_id_to_process} "
                    f"(SBP API недоступен). Polling worker подхватит."
                )
                return {"status": "ok"}
            if verify_result.amount != expected_kopecks:
                # 3. Amount mismatch — снова берём lock для обновления статуса
                with db_session() as session:
                    order = session.scalars(
                        select(Order).where(Order.id == order_id_to_process).with_for_update()
                    ).first()
                    if order and order.payment_status not in ("paid", "amount_mismatch"):
                        logging.critical(
                            "CALLBACK AMOUNT MISMATCH: order %d expected %d, got %d",
                            order.id, expected_kopecks, verify_result.amount,
                        )
                        order.payment_status = "amount_mismatch"
                        order.updated_at = now_utc()
                        session.commit()
                        audit_log("AMOUNT_MISMATCH", order_id=order.id,
                                  expected=expected_kopecks, got=verify_result.amount)
                        from bot_handlers import alert_admin
                        await alert_admin(
                            f"AMOUNT MISMATCH в callback! Заказ #{order.public_order_number}: "
                            f"ожидали {expected_kopecks} коп, получили {verify_result.amount} коп. "
                            f"Заказ помечен для ручного разбора."
                        )
                    else:
                        session.commit()
                return {"status": "ok"}

            await _process_paid_order(order_id_to_process)
            audit_log("CALLBACK_PAID", order_id=order_id_to_process, md_order=md_order)
            logging.info("СБП callback: заказ %d оплачен через callback", order_id_to_process)

    return {"status": "ok"}


@router.post("/api/orders/{order_id}/confirm-payment")
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


# ── Payment processing pipeline ──────────────────────────────────────────

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

    with db_session() as session:
        rows_updated = session.execute(
            select(Order).where(
                Order.id == order_id,
                Order.payment_status != "paid",
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

            order.status = "cancelled"
            order.payment_status = "refund_pending"
            order.updated_at = now_utc()
            session.commit()

        # --- Вне FOR UPDATE lock: сетевой вызов рефанда ---
        if unavailable_items:
            if gateway_oid:
                from payments.sbp import refund_sbp_payment
                refund_result = await refund_sbp_payment(gateway_oid, refund_amount)
                with db_session() as refund_session:
                    o = refund_session.get(Order, stoplist_order_id)
                    if o:
                        if refund_result.success:
                            o.payment_status = "refunded"
                            audit_log("STOPLIST_REFUND", order_id=stoplist_order_id,
                                      order_number=stoplist_order_number, items=names)

                            # 54-ФЗ: чек предоплаты + чек возврата предоплаты
                            fiscal_items = [
                                {
                                    "name_snapshot": item.name_snapshot,
                                    "price_snapshot": item.price_snapshot,
                                    "quantity": item.quantity,
                                }
                                for item in refund_session.query(OrderItem).filter(
                                    OrderItem.order_id == stoplist_order_id
                                ).all()
                            ]
                            fiscal_payload_sell = json_module.dumps({
                                "items": fiscal_items,
                                "total_amount": refund_amount,
                                "payment_method": "prepayment",
                            })
                            fiscal_payload_refund = json_module.dumps({
                                "items": fiscal_items,
                                "total_amount": refund_amount,
                                "payment_method": "prepayment",
                            })
                            refund_session.add(FiscalQueue(
                                order_id=stoplist_order_id,
                                order_number=stoplist_order_number,
                                operation="sell",
                                payload_json=fiscal_payload_sell,
                                status="pending", attempts=0, max_attempts=10,
                                created_at=now_utc(),
                                next_retry_at=now_utc(),
                            ))
                            refund_session.add(FiscalQueue(
                                order_id=stoplist_order_id,
                                order_number=stoplist_order_number,
                                operation="sell_refund",
                                payload_json=fiscal_payload_refund,
                                status="pending", attempts=0, max_attempts=10,
                                created_at=now_utc(),
                                next_retry_at=now_utc() + timedelta(minutes=2),
                            ))
                        else:
                            o.payment_status = "refund_failed"
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
                        f"Ошибка СБП: {refund_result.error_message}. Требуется ручной возврат!"
                    )
            else:
                with db_session() as refund_session:
                    o = refund_session.get(Order, stoplist_order_id)
                    if o:
                        o.payment_status = "cancelled"
                        refund_session.commit()

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

        order.payment_status = "paid"
        order.status = "paid"
        order.updated_at = now_utc()
        audit_log("ORDER_PAID", order_id=order_id, order_number=order.public_order_number,
                  amount=order.total_amount)

        _ = order.items  # eager load before building fiscal payload
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

        # Создаём FiscalQueue В ТОЙ ЖЕ транзакции — гарантия 54-ФЗ при краше
        fiscal_safety_record = FiscalQueue(
            order_id=order_id,
            order_number=order_number,
            operation="sell",
            payload_json=json_module.dumps({"items": fiscal_items, "total_amount": total_amount}),
            status="pending",
            attempts=0,
            max_attempts=10,
            created_at=now_utc(),
            next_retry_at=now_utc() + timedelta(minutes=5),
        )
        session.add(fiscal_safety_record)
        session.commit()
        fiscal_safety_id = fiscal_safety_record.id

    # 2. Фискализация (АТОЛ Онлайн) — FiscalQueue уже создана выше (гарантия 54-ФЗ)
    def _mark_fiscal_done(uuid: str) -> None:
        """Помечаем FiscalQueue как выполненную (онлайн-фискализация успешна)."""
        try:
            with db_session() as fq_session:
                fq = fq_session.get(FiscalQueue, fiscal_safety_id)
                if fq:
                    fq.status = "done"
                    fq_session.commit()
        except Exception:
            logging.exception("Не удалось пометить FiscalQueue %d как done", fiscal_safety_id)

    try:
        fiscal_result = await fiscalize_order(
            order_id=order_id,
            order_number=order_number,
            items=fiscal_items,
            total_amount=total_amount,
            payment_method="prepayment",  # Phase 1: prepayment receipt (54-FZ)
        )
        if fiscal_result.success and fiscal_result.uuid:
            with db_session() as session:
                order = session.get(Order, order_id)
                if order:
                    order.fiscal_prepayment_uuid = fiscal_result.uuid
                    session.commit()
            _mark_fiscal_done(fiscal_result.uuid)
            logging.info("Фискализация: чек создан для заказа %d, uuid=%s",
                         order_id, fiscal_result.uuid)
            audit_log("FISCAL_PREPAYMENT", order_id=order_id, uuid=fiscal_result.uuid)
        else:
            logging.error("Фискализация: ошибка для заказа %d: %s (retry worker подхватит)",
                          order_id, fiscal_result.error)
    except Exception:
        logging.exception("Фискализация: критическая ошибка для заказа %d (retry worker подхватит)", order_id)

    # 3. Синхронизация с 1С:Бухгалтерия
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

    # 4. Автоматически переводим в «Готовится» (только если кассир ещё не сменил статус)
    with db_session() as session:
        order = fetch_order(session, order_id)
        if order.status == "paid":
            order.status = "preparing"
            order.updated_at = now_utc()
            session.commit()

    # 5. Уведомление клиенту
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

    # 6. Уведомление администратору
    await notify_admin_about_order(order_id)


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
            f"Оплата: <b>СБП</b> ✅\n\n"
            f"{item_lines}\n\n"
            f"<b>Сумма:</b> {rub(order.total_amount)}"
        )
        try:
            await bot_setup.bot.send_message(chat_id=bot_setup.ADMIN_CHAT_ID, text=text)
        except Exception:
            logging.exception("Не удалось уведомить админа о заказе %d", order.public_order_number)


# ── Kitchen printer API ──────────────────────────────────────────────────

@router.get("/api/kitchen/pending")
async def kitchen_pending(request: Request) -> dict[str, Any]:
    """Заказы, ожидающие печати на кухне."""
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


@router.post("/api/kitchen/printed/{order_id}")
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


@router.post("/api/orders/{order_id}/mark-ready")
async def mark_order_ready(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Пометить заказ как готовый к выдаче."""
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

    if bot_setup.bot:
        try:
            await bot_setup.bot.send_message(
                chat_id=user_id,
                text=f"✅ Заказ №{order_number} готов и ожидает вас!",
            )
        except Exception:
            logging.exception("Не удалось уведомить клиента о готовности заказа %d", order_number)

    with db_session() as session:
        order = fetch_order(session, order_id)
        return serialize_order(order)


# ── 1C Accounting admin ──────────────────────────────────────────────────

@router.get("/api/admin/accounting-status")
async def accounting_status(request: Request) -> dict[str, Any]:
    """Статус синхронизации заказов с 1С:Бухгалтерия."""
    verify_kitchen_api_key(request)

    with db_session() as session:
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


@router.post("/api/admin/accounting-retry/{order_id}")
async def accounting_retry(order_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)], request: Request) -> dict[str, Any]:
    """Повторная синхронизация заказа с 1С."""
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


# ── Fiscal Queue Admin ───────────────────────────────────────────────────

@router.get("/api/admin/fiscal-queue")
async def get_fiscal_queue(
    request: Request,
    status_filter: str = Query(default="all", pattern="^(all|pending|failed|done|processing)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Просмотр очереди фискализации."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        query = select(FiscalQueue).order_by(FiscalQueue.id.desc())
        if status_filter != "all":
            query = query.where(FiscalQueue.status == status_filter)
        entries = session.scalars(query.limit(limit)).all()

        return {
            "count": len(entries),
            "entries": [
                {
                    "id": fq.id,
                    "order_id": fq.order_id,
                    "order_number": fq.order_number,
                    "operation": fq.operation,
                    "status": fq.status,
                    "attempts": fq.attempts,
                    "max_attempts": fq.max_attempts,
                    "last_error": fq.last_error,
                    "fiscal_uuid": fq.fiscal_uuid,
                    "created_at": fq.created_at.isoformat() if fq.created_at else None,
                    "next_retry_at": fq.next_retry_at.isoformat() if fq.next_retry_at else None,
                    "completed_at": fq.completed_at.isoformat() if fq.completed_at else None,
                }
                for fq in entries
            ],
        }


@router.post("/api/admin/fiscal-queue/{entry_id}/retry")
async def retry_fiscal_entry(
    entry_id: Annotated[int, FastPath(gt=0, le=2_147_483_647)],
    request: Request,
) -> dict[str, str]:
    """Сбросить failed запись в pending для повторной попытки фискализации."""
    verify_kitchen_api_key(request)

    with db_session() as session:
        fq = session.get(FiscalQueue, entry_id)
        if fq is None:
            raise HTTPException(status_code=404, detail="Fiscal queue entry not found.")
        if fq.status not in ("failed", "pending"):
            raise HTTPException(status_code=400, detail=f"Cannot retry entry with status '{fq.status}'.")

        fq.status = "pending"
        fq.attempts = 0
        fq.next_retry_at = now_utc()
        fq.last_error = None
        session.commit()
        logging.info("Fiscal queue entry %d reset to pending by admin", entry_id)

    return {"status": "ok", "message": f"Entry {entry_id} reset to pending."}


# ── Stoplist admin ───────────────────────────────────────────────────────

@router.get("/api/admin/stoplist")
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


@router.post("/api/admin/stoplist")
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

    invalidate_menu_cache()
    action_text = "отключено" if payload.action == "disable" else "включено"
    logging.info("Стоп-лист: %s %d позиций", action_text, len(affected))
    return {"action": payload.action, "affected": affected, "count": len(affected)}


# ── Photos & placeholders ────────────────────────────────────────────────

@router.get("/api/photos/{filename}", include_in_schema=False)
async def serve_photo(filename: str) -> FileResponse:
    """Serve menu item photos from the photos/ directory."""
    safe_name = Path(filename).name
    photo_path = BASE_DIR / "photos" / safe_name
    if not photo_path.exists() or not photo_path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    if not photo_path.resolve().is_relative_to((BASE_DIR / "photos").resolve()):
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(
        photo_path,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/placeholders/{item_id}.svg", include_in_schema=False)
async def menu_placeholder(item_id: int) -> Response:
    with db_session() as session:
        item = session.get(MenuItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Menu item not found.")

    category = CATEGORY_BY_SLUG.get(item.category)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found.")
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


# ── User orders & config ─────────────────────────────────────────────────

@router.get("/api/my-orders")
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


@router.get("/api/app-config")
async def app_config() -> dict[str, Any]:
    return {
        "webapp_url": WEBAPP_URL,
        "app_base_url": APP_BASE_URL,
        "bot_configured": bool(BOT_TOKEN),
        "checkout_mode": "sbp",
        "payment_timeout_seconds": ORDER_PAYMENT_TIMEOUT_MINUTES * 60,
        "company_info": {
            "name": os.getenv("COMPANY_NAME", ""),
            "inn": os.getenv("COMPANY_INN", ""),
            "ogrn": os.getenv("COMPANY_OGRN", ""),
            "address": os.getenv("COMPANY_ADDRESS", ""),
            "offer_url": os.getenv("OFFER_URL", ""),
            "privacy_url": os.getenv("PRIVACY_URL", ""),
        },
    }


# ── Reviews ──────────────────────────────────────────────────────────────

@router.post("/api/reviews")
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

        clean_comment = _re.sub(r"<[^>]+>", "", payload.comment).replace("\x00", "").strip() if payload.comment else ""

        review = Review(
            order_id=payload.order_id,
            telegram_user_id=verified_user_id,
            rating=payload.rating,
            comment=clean_comment[:1000] if clean_comment else "",
            created_at=now_utc(),
        )
        session.add(review)
        try:
            session.commit()
        except IntegrityError:
            return {"status": "already_submitted"}

    if bot_setup.bot and bot_setup.ADMIN_CHAT_ID and payload.rating:
        stars = "\u2B50" * payload.rating
        text = f"Новый отзыв к заказу №{order.public_order_number}\n{stars}"
        if payload.comment and payload.comment.strip():
            text += f"\n\n{escape(payload.comment[:200])}"
        try:
            await bot_setup.bot.send_message(chat_id=bot_setup.ADMIN_CHAT_ID, text=text)
        except Exception:
            logging.exception("Failed to send review notification")

    return {"status": "ok"}


# ── Client error reporting ───────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402


class ClientErrorPayload(_BaseModel):
    message: str = _Field(max_length=2000)
    source: str = _Field(default="", max_length=500)
    lineno: int = _Field(default=0, ge=0)
    colno: int = _Field(default=0, ge=0)
    stack: str = _Field(default="", max_length=5000)
    url: str = _Field(default="", max_length=1000)
    user_agent: str = _Field(default="", max_length=500)


@router.post("/api/client-error")
async def report_client_error(payload: ClientErrorPayload, request: Request) -> dict[str, str]:
    """Frontend reports JS errors here for server-side logging."""
    client_ip = get_client_ip(request)
    if not general_limiter.check(f"client_error:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many error reports.")

    request_id = getattr(request.state, "request_id", "unknown")
    logging.error(
        "Client error: message=%s source=%s line=%d col=%d url=%s request_id=%s",
        payload.message[:200],
        payload.source[:100],
        payload.lineno,
        payload.colno,
        payload.url[:200],
        request_id,
    )
    return {"status": "ok"}
