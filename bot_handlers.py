from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
    BotCommand,
)
from sqlalchemy import func, select

import bot_setup
import database
from config import ALLOWED_ADMIN_IDS, WEBAPP_URL
from database import IRKUTSK_TZ, rub
from menu_data import CATEGORY_BY_SLUG, CATEGORY_META, CATEGORY_ORDER
from models import FiscalQueue, MenuItem, Order, OrderItem
from serializers import (
    _format_available_at,
    build_cashier_keyboard,
    format_order_for_cashier,
)


# ── Notifications ─────────────────────────────────────────────────────────

async def notify_customer(order: Order, text: str) -> None:
    if bot_setup.bot is None:
        return
    try:
        await bot_setup.bot.send_message(chat_id=order.telegram_user_id, text=text)
    except Exception:
        logging.exception("Failed to notify customer %s", order.telegram_user_id)


async def alert_admin(message: str) -> None:
    """Send critical alert to admin via Telegram."""
    if not bot_setup.bot or not bot_setup.ADMIN_CHAT_ID:
        logging.error("Cannot send admin alert (bot not configured): %s", message)
        return
    try:
        await bot_setup.bot.send_message(
            chat_id=bot_setup.ADMIN_CHAT_ID,
            text=f"\u26a0\ufe0f <b>ALERT</b>\n\n{escape(message)}",
        )
    except Exception:
        logging.exception("Failed to send admin alert")


async def notify_cashier_about_paid_order(order_id: int) -> None:
    if bot_setup.bot is None:
        logging.warning("Bot is not configured, cashier notification skipped.")
        return
    if bot_setup.ADMIN_CHAT_ID is None:
        logging.warning("ADMIN_CHAT_ID is not set, cashier notification skipped.")
        return

    with database.db_session() as session:
        order = database.fetch_order(session, order_id)
        cashier_text = format_order_for_cashier(order)
        cashier_keyboard = build_cashier_keyboard(order.id, order.status)
        notify_order_id = order.id
        notify_order_number = order.public_order_number

    # Telegram API вызов ВНЕ db_session (не держим DB connection)
    try:
        sent_message = await bot_setup.bot.send_message(
            chat_id=bot_setup.ADMIN_CHAT_ID,
            text=cashier_text,
            reply_markup=cashier_keyboard,
        )
    except Exception:
        logging.exception("Failed to notify cashier about order %s", notify_order_number)
        return

    with database.db_session() as session:
        order = session.get(Order, notify_order_id)
        if order:
            order.cashier_message_id = sent_message.message_id
            order.updated_at = database.now_utc()
            session.commit()


# ── Bot configuration ─────────────────────────────────────────────────────

async def configure_bot_entrypoints() -> None:
    if bot_setup.bot is None:
        return
    try:
        await bot_setup.bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть меню"),
                BotCommand(command="admin", description="Назначить этот чат кассой"),
                BotCommand(command="stop", description="Отключить блюдо (стоп-лист)"),
                BotCommand(command="stoplist", description="Текущий стоп-лист"),
                BotCommand(command="pause", description="Пауза приёма заказов"),
                BotCommand(command="refund", description="Возврат по заказу"),
                BotCommand(command="stats", description="Статистика за сегодня"),
            ]
        )
        await bot_setup.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Меню",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
    except Exception:
        logging.exception("Failed to configure Telegram bot entrypoints.")


# ── Handlers ──────────────────────────────────────────────────────────────

@bot_setup.router.message(Command("start"))
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


@bot_setup.router.message(Command("admin"))
async def handle_admin(message: Message) -> None:
    chat_id = message.chat.id

    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        logging.warning("Unauthorized /admin attempt from chat_id=%d", chat_id)
        await message.answer("Доступ запрещён. Обратитесь к владельцу бота.")
        return

    bot_setup.ADMIN_CHAT_ID = chat_id
    with database.db_session() as session:
        database.save_setting(session, "admin_chat_id", str(chat_id))
    await message.answer(
        "Этот чат назначен кассой. Сюда будут приходить оплаченные заказы."
    )


VALID_STATUS_TRANSITIONS: dict[str, list[str]] = {
    "paid": ["preparing", "ready"],
    "preparing": ["ready"],
}


@bot_setup.router.callback_query(F.data.startswith("order:"))
async def handle_order_status_change(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer("Некорректные данные.")
        return

    chat_id = callback.message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await callback.answer("Нет прав.")
        return

    try:
        _, action, raw_order_id = callback.data.split(":")
        order_id = int(raw_order_id)
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные.")
        return

    with database.db_session() as session:
        order = session.scalars(
            select(Order).where(Order.id == order_id).with_for_update()
        ).first()
        if order is None:
            await callback.answer("Заказ не найден.")
            return
        _ = order.items  # eager load

        allowed = VALID_STATUS_TRANSITIONS.get(order.status, [])
        if action not in allowed:
            await callback.answer("Невозможно изменить статус заказа.")
            return

        if action == "preparing":
            order.status = "preparing"
            order.updated_at = database.now_utc()
            session.commit()
            session.refresh(order)
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
            order.updated_at = database.now_utc()

            # Phase 2 fiscal receipt: full settlement (54-FZ two-phase)
            import json as json_module
            fiscal_items = [
                {
                    "name_snapshot": item.name_snapshot,
                    "price_snapshot": item.price_snapshot,
                    "quantity": item.quantity,
                }
                for item in order.items
            ]
            order_id_ready = order.id
            order_number_ready = order.public_order_number
            total_amount_ready = order.total_amount
            has_phase1 = bool(order.fiscal_prepayment_uuid)

            # Создаём FiscalQueue В ТОЙ ЖЕ транзакции что status="ready"
            # Гарантия 54-ФЗ: если сервер упадёт после commit, retry worker подхватит
            fiscal_safety_id = None
            if has_phase1:
                fiscal_safety_record = FiscalQueue(
                    order_id=order_id_ready,
                    order_number=order_number_ready,
                    operation="sell_settlement",
                    payload_json=json_module.dumps({
                        "items": fiscal_items,
                        "total_amount": total_amount_ready,
                    }),
                    status="pending",
                    attempts=0,
                    max_attempts=10,
                    created_at=database.now_utc(),
                    next_retry_at=database.now_utc() + timedelta(minutes=5),
                )
                session.add(fiscal_safety_record)

            session.commit()
            session.refresh(order)
            if has_phase1 and fiscal_safety_record:
                fiscal_safety_id = fiscal_safety_record.id

            receipt_msg = f"✅ Заказ №{order.public_order_number} готов и ожидает вас в ресторане!"
            if order.fiscal_prepayment_uuid:
                receipt_msg += f'\n\n<a href="https://receipt.atol.ru/{order.fiscal_prepayment_uuid}">Кассовый чек</a>'
            await notify_customer(order, receipt_msg)
            await callback.message.edit_text(format_order_for_cashier(order), reply_markup=None)
            await callback.answer("🟢 Готов!")

            if not has_phase1:
                logging.warning(
                    "Phase 2 fiscal: Phase 1 (prepayment) not found for order %d. "
                    "Skipping full_payment receipt.",
                    order_id_ready,
                )
                await alert_admin(
                    f"Заказ #{order_number_ready}: Phase 1 чек (prepayment) отсутствует. "
                    f"Phase 2 чек (full_payment) не создан. Проверьте fiscal_queue!"
                )
            else:
                # Пробуем онлайн-фискализацию; FiscalQueue уже создана (гарантия 54-ФЗ)
                try:
                    from payments.fiscal import fiscalize_order
                    fiscal_result = await fiscalize_order(
                        order_id=order_id_ready,
                        order_number=order_number_ready,
                        items=fiscal_items,
                        total_amount=total_amount_ready,
                        payment_method="full_payment",
                    )
                    if fiscal_result.success and fiscal_result.uuid:
                        with database.db_session() as fs:
                            o = fs.get(Order, order_id_ready)
                            if o:
                                o.fiscal_uuid = fiscal_result.uuid
                            # Помечаем FiscalQueue как выполненную
                            if fiscal_safety_id:
                                fq = fs.get(FiscalQueue, fiscal_safety_id)
                                if fq:
                                    fq.status = "done"
                                    fq.fiscal_uuid = fiscal_result.uuid
                                    fq.completed_at = database.now_utc()
                            fs.commit()
                        logging.info(
                            "Phase 2 fiscal receipt for order %d, uuid=%s",
                            order_id_ready, fiscal_result.uuid,
                        )
                    else:
                        logging.error(
                            "Phase 2 fiscal failed for order %d: %s — retry worker подхватит",
                            order_id_ready, fiscal_result.error,
                        )
                except Exception:
                    logging.exception(
                        "Phase 2 fiscalization error for order %d — retry worker подхватит",
                        order_id_ready,
                    )
            return

    await callback.answer("Неизвестное действие.")


@bot_setup.router.message(Command("stop"))
async def handle_stop(message: Message) -> None:
    """Быстрое отключение блюда: /stop Плов"""
    chat_id = message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
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

    with database.db_session() as session:
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
        with database.db_session() as session:
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


@bot_setup.router.message(Command("stoplist"))
async def handle_stoplist(message: Message) -> None:
    """Показать текущий стоп-лист."""
    chat_id = message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    with database.db_session() as session:
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


@bot_setup.router.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    """Статистика продаж."""
    chat_id = message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    text_msg = (message.text or "").strip()
    parts = text_msg.split(maxsplit=1)
    period = parts[1].strip().lower() if len(parts) > 1 else "today"

    now_irkutsk = datetime.now(IRKUTSK_TZ)
    today_start = now_irkutsk.replace(hour=0, minute=0, second=0, microsecond=0)

    if period in ("week", "неделя"):
        period_start = today_start - timedelta(days=today_start.weekday())
        period_label = "за неделю"
    elif period in ("month", "месяц"):
        period_start = today_start.replace(day=1)
        period_label = "за месяц"
    else:
        period_start = today_start
        period_label = "за сегодня"

    period_start_utc = period_start.astimezone(timezone.utc)

    with database.db_session() as session:
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

        stopped_count = session.scalar(
            select(func.count(MenuItem.id)).where(MenuItem.is_available.is_(False))
        ) or 0

        pending_orders = session.scalar(
            select(func.count(Order.id)).where(
                Order.status.in_(["created", "paid", "preparing"]),
            )
        ) or 0

        pending_fiscal = session.scalar(
            select(func.count(FiscalQueue.id)).where(FiscalQueue.status == "pending")
        ) or 0

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


@bot_setup.router.callback_query(F.data.startswith("sl:"))
async def handle_stoplist_callback(callback: CallbackQuery) -> None:
    """Обработка inline-кнопок стоп-листа."""
    if callback.data is None or callback.message is None:
        await callback.answer("Ошибка.")
        return

    chat_id = callback.message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await callback.answer("Доступ запрещён.")
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Неверный формат.")
        return

    action = parts[1]

    try:
        _int_part = int(parts[2]) if action in ("on", "off", "time") else None
    except (ValueError, IndexError):
        await callback.answer("Некорректные данные.")
        return

    if action == "on":
        item_id = _int_part
        with database.db_session() as session:
            item = session.get(MenuItem, item_id)
            if item:
                item.is_available = True
                item.unavailable_reason = None
                item.available_at = None
                session.commit()
                await callback.answer(f"✅ {item.name} включено")
                await callback.message.edit_text(
                    f"✅ <b>{escape(item.name)}</b> снова доступно!",
                )
            else:
                await callback.answer("Блюдо не найдено.")

    elif action == "off":
        item_id = _int_part
        with database.db_session() as session:
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
        with database.db_session() as session:
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
        with database.db_session() as session:
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
        item_id = _int_part
        try:
            minutes = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer("Некорректные данные.")
            return
        with database.db_session() as session:
            item = session.get(MenuItem, item_id)
            if item:
                if minutes > 0:
                    item.available_at = database.now_utc() + timedelta(minutes=minutes)
                    item.unavailable_reason = f"Будет готово {_format_available_at(item.available_at) or 'позже'}"
                    session.commit()
                    await callback.message.edit_text(
                        f"🚫 <b>{escape(item.name)}</b> отключено.\n"
                        f"⏰ Вернётся автоматически {_format_available_at(item.available_at)}",
                    )
                    await callback.answer(f"Таймер установлен: {minutes} мин")
                else:
                    await callback.message.edit_text(
                        f"🚫 <b>{escape(item.name)}</b> отключено.\n"
                        f"Включите вручную: /stoplist",
                    )
                    await callback.answer("Блюдо отключено без таймера")
            else:
                await callback.answer("Блюдо не найдено.")


# ── Pause ordering ───────────────────────────────────────────────────────

@bot_setup.router.message(Command("pause"))
async def handle_pause(message: Message) -> None:
    """Пауза приёма заказов: /pause 30 — пауза на 30 мин, /pause — снять."""
    chat_id = message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    text_msg = (message.text or "").strip()
    parts = text_msg.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        with database.db_session() as session:
            database.save_setting(session, "ordering_paused_until", "")
        await message.answer("✅ Приём заказов возобновлён.")
        return

    try:
        minutes = int(parts[1].strip())
    except ValueError:
        await message.answer("Укажите число минут: <code>/pause 30</code>")
        return

    if minutes < 1 or minutes > 480:
        await message.answer("Укажите от 1 до 480 минут.")
        return

    pause_until = database.now_utc() + timedelta(minutes=minutes)
    with database.db_session() as session:
        database.save_setting(session, "ordering_paused_until", pause_until.isoformat())
    await message.answer(
        f"⏸ Приём заказов приостановлен на {minutes} мин.\n"
        f"Для возобновления: /pause"
    )


# ── Refund ────────────────────────────────────────────────────────────────

@bot_setup.router.message(Command("refund"))
async def handle_refund(message: Message) -> None:
    """Возврат: /refund <номер_заказа>"""
    chat_id = message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    text_msg = (message.text or "").strip()
    parts = text_msg.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажите номер заказа: <code>/refund 1001</code>")
        return

    try:
        order_number = int(parts[1].strip())
    except ValueError:
        await message.answer("Номер заказа должен быть числом.")
        return

    # 1. Захватить блокировку, проверить статус, подготовить данные для рефанда
    with database.db_session() as session:
        order = session.scalars(
            select(Order).where(
                Order.public_order_number == order_number
            ).with_for_update()
        ).first()

        if not order:
            await message.answer(f"Заказ №{order_number} не найден.")
            return

        if order.payment_status != "paid":
            await message.answer(
                f"Заказ №{order_number} нельзя вернуть "
                f"(статус оплаты: {order.payment_status})."
            )
            return

        # Сохраняем данные и помечаем refund_pending ДО сетевого вызова
        gateway_oid = order.gateway_order_id
        refund_amount = order.total_amount
        refund_order_id = order.id
        refund_order_number = order.public_order_number
        refund_user_id = order.telegram_user_id
        _ = order.items  # eager load
        import json as json_module
        fiscal_items = [
            {
                "name_snapshot": item.name_snapshot,
                "price_snapshot": item.price_snapshot,
                "quantity": item.quantity,
            }
            for item in order.items
        ]

        order.payment_status = "refund_pending"
        order.updated_at = database.now_utc()
        session.commit()  # Освобождаем FOR UPDATE lock

    # 2. Возврат денег через СБП (вне блокировки — async I/O)
    if gateway_oid:
        from payments.sbp import refund_sbp_payment
        refund_result = await refund_sbp_payment(gateway_oid, refund_amount)
        if not refund_result.success:
            # Откатываем статус обратно
            with database.db_session() as session:
                o = session.get(Order, refund_order_id)
                if o and o.payment_status == "refund_pending":
                    o.payment_status = "paid"
                    o.updated_at = database.now_utc()
                    session.commit()
            await message.answer(
                f"Ошибка возврата СБП: {refund_result.error_message}\n"
                f"Заказ №{order_number} НЕ возвращён. Обратитесь в банк."
            )
            return

    # 3. Обновить статусы и создать фискальный чек возврата
    with database.db_session() as session:
        order = session.get(Order, refund_order_id)
        if order:
            order.payment_status = "refunded"
            order.status = "cancelled"
            order.updated_at = database.now_utc()

            fiscal_payload = json_module.dumps({
                "items": fiscal_items,
                "total_amount": refund_amount,
            })

            fiscal_refund = FiscalQueue(
                order_id=refund_order_id,
                order_number=refund_order_number,
                operation="sell_refund",
                payload_json=fiscal_payload,
                status="pending",
                attempts=0,
                max_attempts=10,
                created_at=database.now_utc(),
                next_retry_at=database.now_utc(),
            )
            session.add(fiscal_refund)
            session.commit()

        if order:
            await notify_customer(
                order,
                f"Возврат средств по заказу No{refund_order_number}. "
                f"Сумма {database.rub(refund_amount)} будет возвращена.",
            )

    await message.answer(
        f"✅ Возврат по заказу №{order_number} оформлен.\n"
        f"Сумма: {database.rub(refund_amount)}\n"
        f"Фискальный чек возврата поставлен в очередь."
    )


# ── Prep time callback ───────────────────────────────────────────────────

@bot_setup.router.callback_query(F.data.startswith("preptime:"))
async def handle_prep_time(callback: CallbackQuery) -> None:
    """Кассир уточняет время готовности."""
    if callback.data is None or callback.message is None:
        await callback.answer("Ошибка.")
        return

    chat_id = callback.message.chat.id
    if not ALLOWED_ADMIN_IDS or chat_id not in ALLOWED_ADMIN_IDS:
        await callback.answer("Нет прав.")
        return

    try:
        _, raw_order_id, raw_minutes = callback.data.split(":")
        order_id = int(raw_order_id)
        minutes = int(raw_minutes)
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные.")
        return

    with database.db_session() as session:
        order = database.fetch_order(session, order_id)
        await notify_customer(
            order,
            f"⏰ Заказ №{order.public_order_number} — "
            f"примерное время готовности: ~{minutes} мин.",
        )

    await callback.answer(f"Клиент уведомлён: ~{minutes} мин")
