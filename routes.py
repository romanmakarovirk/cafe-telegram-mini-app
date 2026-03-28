from __future__ import annotations

import json as json_module
import logging
import os
import re as _re
import sys
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
from metrics import ORDERS_CREATED, PAYMENT_WEBHOOKS, PAYMENT_ERRORS
from statuses import OrderStatus, PaymentStatus
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
from models import MenuItem, Order, OrderItem, Review
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
    payment_check_limiter,
    verify_kitchen_api_key,
)
from serializers import _format_available_at, _resolve_image_url, serialize_menu_item, serialize_order
import bot_setup

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None

router = APIRouter()

# Re-export from services for backward compatibility (workers.py, main.py import from routes)
from services import _process_paid_order, notify_admin_about_order, audit_log  # noqa: F401

# Structured audit logger for payment/fiscal events (54-FZ compliance)
_audit = logging.getLogger("audit.payment")

# In-memory menu cache (TTL 5 min) — avoids DB query on every /api/menu request
_menu_cache: dict[str, Any] = TTLCache(maxsize=4, ttl=300) if TTLCache else {}


def invalidate_menu_cache() -> None:
    """Сброс кэша меню при изменении стоп-листа или позиций."""
    _menu_cache.clear()




def _get_cafe_schedule() -> dict[str, Any]:
    """Look up get_cafe_schedule through main module for test patchability."""
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "get_cafe_schedule"):
        return main_mod.get_cafe_schedule()
    return database.get_cafe_schedule()


# Re-export middleware for backward compatibility (main.py imports from routes)
from routes_middleware import SecurityHeadersMiddleware, RequestIdMiddleware, ExceptionMiddleware  # noqa: F401

# Re-export from domain routers for backward compatibility (tests import from routes)
from routes_payment import (  # noqa: F401
    payment_router,
    create_payment,
    check_payment_status,
    yookassa_webhook,
    confirm_payment,
)
from routes_kitchen import (  # noqa: F401
    kitchen_router,
    kitchen_pending,
    kitchen_mark_printed,
    mark_order_ready,
    accounting_status,
    accounting_retry,
    get_stoplist,
    manage_stoplist,
)

# Include domain routers
router.include_router(payment_router)
router.include_router(kitchen_router)


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
    """Readiness probe — полная диагностика: БД, бот, ЮKassa, 1С."""
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

    # 3. ЮKassa payments + fiscalization
    yookassa_configured = bool(os.getenv("YOOKASSA_SHOP_ID") and os.getenv("YOOKASSA_SECRET_KEY"))
    checks["yookassa"] = {"status": "configured" if yookassa_configured else "not_configured"}

    # 4. 1C:Fresh
    fresh_configured = bool(os.getenv("FRESH_BASE_URL") and os.getenv("FRESH_ENABLED", "").lower() in ("true", "1"))
    checks["accounting_1c"] = {"status": "configured" if fresh_configured else "not_configured"}

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
                    status=OrderStatus.CREATED,
                    payment_status=PaymentStatus.PENDING,
                    payment_mode="yookassa",
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
                ORDERS_CREATED.inc()
                session.refresh(order)
                _ = order.items
                return serialize_order(order)
        except IntegrityError:
            if attempt == max_retries - 1:
                raise HTTPException(status_code=500, detail="Не удалось создать заказ. Попробуйте ещё раз.")
            continue


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
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── User orders & config ─────────────────────────────────────────────────

@router.get("/api/my-orders")
async def my_orders(request: Request, limit: int = Query(default=20, ge=1, le=50)) -> dict[str, Any]:
    """История заказов текущего пользователя."""
    verified_user_id = get_verified_user_id(request)

    with db_session() as session:
        orders = session.scalars(
            select(Order).where(
                Order.telegram_user_id == verified_user_id,
                Order.payment_status == PaymentStatus.PAID,
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
        "checkout_mode": "yookassa",
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
        if order.payment_status != PaymentStatus.PAID:
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


# ── Prometheus Metrics ────────────────────────────────────────────────────

@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus-compatible metrics endpoint."""
    verify_kitchen_api_key(request)
    from metrics import METRICS_AVAILABLE, generate_latest, CONTENT_TYPE_LATEST

    if not METRICS_AVAILABLE:
        return Response("prometheus_client not installed", status_code=501)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
