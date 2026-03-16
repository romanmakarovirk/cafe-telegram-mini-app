"""
АТОЛ Онлайн API v4 — модуль фискализации.

Автоматически создаёт фискальные чеки (54-ФЗ) через облачную кассу АТОЛ Онлайн.
Чек отправляется клиенту на email/телефон, а данные уходят в ОФД (Платформа ОФД).

Документация: https://online.atol.ru/files/API_atol_online_v4.pdf
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Конфигурация (из переменных окружения)
# ---------------------------------------------------------------------------
ATOL_BASE_URL = os.getenv("ATOL_BASE_URL", "https://online.atol.ru/possystem/v4")
ATOL_LOGIN = os.getenv("ATOL_LOGIN", "")
ATOL_PASSWORD = os.getenv("ATOL_PASSWORD", "")
ATOL_GROUP_CODE = os.getenv("ATOL_GROUP_CODE", "")
ATOL_COMPANY_EMAIL = os.getenv("ATOL_COMPANY_EMAIL", "")
ATOL_INN = os.getenv("ATOL_INN", "")
ATOL_PAYMENT_ADDRESS = os.getenv("ATOL_PAYMENT_ADDRESS", "")  # URL Mini App или адрес точки
ATOL_CALLBACK_URL = os.getenv("ATOL_CALLBACK_URL", "")  # URL для callback от АТОЛ
ATOL_SNO = os.getenv("ATOL_SNO", "usn_income")  # УСН доходы

# Иркутск UTC+8
IRKUTSK_TZ = timezone(timedelta(hours=8))

# Тестовый режим (песочница АТОЛ)
ATOL_TEST_MODE = os.getenv("ATOL_TEST_MODE", "true").lower() in ("true", "1", "yes")
ATOL_TEST_URL = "https://testonline.atol.ru/possystem/v4"


@dataclass
class AtolToken:
    """Хранит токен авторизации АТОЛ Онлайн (действует 24 часа)."""
    value: str = ""
    expires_at: float = 0.0  # unix timestamp

    @property
    def is_valid(self) -> bool:
        # Обновляем за 1 час до истечения для надёжности
        return bool(self.value) and time.time() < (self.expires_at - 3600)


@dataclass
class FiscalResult:
    """Результат фискализации."""
    success: bool
    uuid: str = ""
    error: str = ""
    status: str = ""  # wait / done / fail
    fiscal_data: dict[str, Any] = field(default_factory=dict)


class AtolOnlineClient:
    """
    Клиент АТОЛ Онлайн API v4.

    Основные методы:
    - sell() — создание чека продажи
    - sell_refund() — чек возврата
    - get_report() — статус фискализации по uuid
    """

    def __init__(self):
        self._token = AtolToken()
        self._base_url = ATOL_TEST_URL if ATOL_TEST_MODE else ATOL_BASE_URL
        self._client: httpx.AsyncClient | None = None
        self._token_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли все необходимые параметры."""
        return all([ATOL_LOGIN, ATOL_PASSWORD, ATOL_GROUP_CODE])

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ----- Авторизация -----

    async def _get_token(self) -> str:
        """
        Получает токен авторизации.
        POST /possystem/v4/getToken
        Body: {"login": "...", "pass": "..."}
        Ответ: {"token": "...", "error": null}
        Токен действует 24 часа.
        """
        # Fast path without lock
        if self._token.is_valid:
            return self._token.value

        # Double-check locking: serialize concurrent token refreshes
        async with self._token_lock:
            if self._token.is_valid:
                return self._token.value

            return await self._fetch_new_token()

    async def _fetch_new_token(self) -> str:
        """Внутренний метод: запрашивает новый токен у АТОЛ API."""
        client = await self._get_client()
        url = f"{self._base_url}/getToken"
        payload = {"login": ATOL_LOGIN, "pass": ATOL_PASSWORD}

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if data.get("error"):
                error_info = data["error"]
                logger.error("АТОЛ getToken ошибка: code=%s, text=%s",
                             error_info.get("code"), error_info.get("text"))
                raise RuntimeError(f"АТОЛ auth error: {error_info.get('text', 'unknown')}")

            token = data.get("token", "")
            if not token:
                raise RuntimeError("АТОЛ вернул пустой токен")

            self._token.value = token
            self._token.expires_at = time.time() + 24 * 3600  # 24 часа
            logger.info("АТОЛ Онлайн: токен получен, действует 24ч")
            return token

        except httpx.HTTPStatusError as e:
            logger.error("АТОЛ getToken HTTP %s: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("АТОЛ getToken сетевая ошибка: %s", e)
            raise

    # ----- Операции с чеками -----

    async def sell(
        self,
        order_id: int,
        order_number: int,
        items: list[dict[str, Any]],
        total: float,
        client_email: str = "",
        client_phone: str = "",
    ) -> FiscalResult:
        """
        Создание чека продажи.

        POST /possystem/v4/{group_code}/sell
        Header: Token: <token>

        Args:
            order_id: ID заказа в нашей системе
            order_number: Публичный номер заказа
            items: Список позиций [{name, price, quantity, sum}]
            total: Итоговая сумма
            client_email: Email клиента для отправки чека
            client_phone: Телефон клиента для отправки чека (формат +7...)

        Returns:
            FiscalResult с uuid для отслеживания
        """
        if not self.is_configured:
            logger.warning("АТОЛ Онлайн не настроен, фискализация пропущена")
            return FiscalResult(success=False, error="АТОЛ Онлайн не настроен")

        token = await self._get_token()
        client = await self._get_client()

        # Формируем external_id (уникальный для каждого чека)
        external_id = f"order-{order_id}-{uuid.uuid4().hex[:8]}"

        # Текущее время по Иркутску (формат: dd.mm.yyyy HH:MM:SS)
        now_irkutsk = datetime.now(IRKUTSK_TZ)
        timestamp = now_irkutsk.strftime("%d.%m.%Y %H:%M:%S")

        # Клиент (обязательно email или телефон)
        receipt_client: dict[str, str] = {}
        if client_email:
            receipt_client["email"] = client_email
        elif client_phone:
            receipt_client["phone"] = client_phone
        else:
            # Если ни email ни телефон не указан, используем email компании
            receipt_client["email"] = ATOL_COMPANY_EMAIL or "noreply@cafe.ru"

        # Позиции чека
        receipt_items = []
        for item in items:
            price = round(float(item["price"]), 2)
            quantity = float(item["quantity"])
            item_sum = round(price * quantity, 2)

            receipt_items.append({
                "name": item["name"],
                "price": price,
                "quantity": quantity,
                "sum": item_sum,
                "measurement_unit": "шт",
                "payment_method": "full_payment",      # Полная оплата
                "payment_object": "commodity",           # Товар
                "vat": {"type": "none"},                 # Без НДС (УСН)
            })

        # Тело запроса
        payload = {
            "external_id": external_id,
            "receipt": {
                "client": receipt_client,
                "company": {
                    "email": ATOL_COMPANY_EMAIL,
                    "sno": ATOL_SNO,
                    "inn": ATOL_INN,
                    "payment_address": ATOL_PAYMENT_ADDRESS,
                },
                "items": receipt_items,
                "payments": [
                    {
                        "type": 1,  # Электронный платёж
                        "sum": round(total, 2),
                    }
                ],
                "vats": [
                    {
                        "type": "none",  # Без НДС
                        "sum": 0,
                    }
                ],
                "total": round(total, 2),
            },
            "timestamp": timestamp,
        }

        # Добавляем callback URL если настроен
        if ATOL_CALLBACK_URL:
            payload["service"] = {"callback_url": ATOL_CALLBACK_URL}

        url = f"{self._base_url}/{ATOL_GROUP_CODE}/sell"
        headers = {"Token": token}

        try:
            logger.info("АТОЛ sell: order=%d, total=%.2f, items=%d",
                        order_number, total, len(receipt_items))
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            if data.get("error"):
                error_info = data["error"]
                logger.error("АТОЛ sell ошибка: code=%s, text=%s, order=%d",
                             error_info.get("code"), error_info.get("text"), order_number)
                return FiscalResult(
                    success=False,
                    error=f"АТОЛ error {error_info.get('code')}: {error_info.get('text')}",
                )

            receipt_uuid = data.get("uuid", "")
            status = data.get("status", "")

            logger.info("АТОЛ sell OK: uuid=%s, status=%s, order=%d",
                        receipt_uuid, status, order_number)

            return FiscalResult(
                success=True,
                uuid=receipt_uuid,
                status=status,  # "wait" — чек в очереди на фискализацию
            )

        except httpx.HTTPStatusError as e:
            error_text = e.response.text[:500]
            logger.error("АТОЛ sell HTTP %s: %s", e.response.status_code, error_text)

            # Если 401 — сбрасываем токен и пробуем ещё раз (один retry)
            if e.response.status_code == 401:
                self._token = AtolToken()
                try:
                    new_token = await self._get_token()
                    headers_retry = {"Token": new_token}
                    resp2 = await client.post(url, json=payload, headers=headers_retry)
                    resp2.raise_for_status()
                    data2 = resp2.json()
                    if data2.get("error"):
                        err2 = data2["error"]
                        return FiscalResult(
                            success=False,
                            error=f"АТОЛ retry error: {err2.get('text')}",
                        )
                    logger.info("АТОЛ sell OK (after 401 retry): uuid=%s", data2.get("uuid", ""))
                    return FiscalResult(
                        success=True,
                        uuid=data2.get("uuid", ""),
                        status=data2.get("status", ""),
                    )
                except Exception as retry_exc:
                    logger.error("АТОЛ sell retry also failed: %s", retry_exc)
                    return FiscalResult(success=False, error=f"Retry after 401 failed: {retry_exc}")

            return FiscalResult(success=False, error=f"HTTP {e.response.status_code}: {error_text}")

        except httpx.RequestError as e:
            logger.error("АТОЛ sell сетевая ошибка: %s", e)
            return FiscalResult(success=False, error=f"Network error: {e}")

    async def sell_refund(
        self,
        order_id: int,
        order_number: int,
        items: list[dict[str, Any]],
        total: float,
        client_email: str = "",
        client_phone: str = "",
    ) -> FiscalResult:
        """
        Создание чека возврата.

        POST /possystem/v4/{group_code}/sell_refund
        Аналогичен sell, но операция — возврат.
        """
        if not self.is_configured:
            return FiscalResult(success=False, error="АТОЛ Онлайн не настроен")

        token = await self._get_token()
        client = await self._get_client()

        external_id = f"refund-{order_id}-{uuid.uuid4().hex[:8]}"
        now_irkutsk = datetime.now(IRKUTSK_TZ)
        timestamp = now_irkutsk.strftime("%d.%m.%Y %H:%M:%S")

        receipt_client: dict[str, str] = {}
        if client_email:
            receipt_client["email"] = client_email
        elif client_phone:
            receipt_client["phone"] = client_phone
        else:
            receipt_client["email"] = ATOL_COMPANY_EMAIL or "noreply@cafe.ru"

        receipt_items = []
        for item in items:
            price = round(float(item["price"]), 2)
            quantity = float(item["quantity"])
            item_sum = round(price * quantity, 2)
            receipt_items.append({
                "name": item["name"],
                "price": price,
                "quantity": quantity,
                "sum": item_sum,
                "measurement_unit": "шт",
                "payment_method": "full_payment",
                "payment_object": "commodity",
                "vat": {"type": "none"},
            })

        payload = {
            "external_id": external_id,
            "receipt": {
                "client": receipt_client,
                "company": {
                    "email": ATOL_COMPANY_EMAIL,
                    "sno": ATOL_SNO,
                    "inn": ATOL_INN,
                    "payment_address": ATOL_PAYMENT_ADDRESS,
                },
                "items": receipt_items,
                "payments": [{"type": 1, "sum": round(total, 2)}],
                "vats": [{"type": "none", "sum": 0}],
                "total": round(total, 2),
            },
            "timestamp": timestamp,
        }

        if ATOL_CALLBACK_URL:
            payload["service"] = {"callback_url": ATOL_CALLBACK_URL}

        url = f"{self._base_url}/{ATOL_GROUP_CODE}/sell_refund"
        headers = {"Token": token}

        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            if data.get("error"):
                error_info = data["error"]
                return FiscalResult(
                    success=False,
                    error=f"АТОЛ refund error: {error_info.get('text')}",
                )

            return FiscalResult(
                success=True,
                uuid=data.get("uuid", ""),
                status=data.get("status", ""),
            )

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("АТОЛ sell_refund ошибка: %s", e)
            return FiscalResult(success=False, error=str(e))

    # ----- Проверка статуса чека -----

    async def get_report(self, receipt_uuid: str) -> FiscalResult:
        """
        Получение отчёта о фискализации чека.

        GET /possystem/v4/{group_code}/report/{uuid}
        Header: Token: <token>

        Статусы:
        - wait: чек в очереди
        - done: чек успешно фискализирован
        - fail: ошибка фискализации
        """
        if not self.is_configured:
            return FiscalResult(success=False, error="АТОЛ Онлайн не настроен")

        token = await self._get_token()
        client = await self._get_client()

        url = f"{self._base_url}/{ATOL_GROUP_CODE}/report/{receipt_uuid}"
        headers = {"Token": token}

        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status", "")
            error = data.get("error")

            if error:
                return FiscalResult(
                    success=False,
                    uuid=receipt_uuid,
                    status=status,
                    error=f"code={error.get('code')}: {error.get('text')}",
                    fiscal_data=data.get("payload", {}),
                )

            return FiscalResult(
                success=status == "done",
                uuid=receipt_uuid,
                status=status,
                fiscal_data=data.get("payload", {}),
            )

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("АТОЛ report ошибка: %s", e)
            return FiscalResult(success=False, uuid=receipt_uuid, error=str(e))

    async def wait_for_result(
        self,
        receipt_uuid: str,
        max_attempts: int = 10,
        interval: float = 3.0,
    ) -> FiscalResult:
        """
        Ожидание завершения фискализации с polling.
        Обычно чек фискализируется за 5-30 секунд.
        """
        for attempt in range(1, max_attempts + 1):
            result = await self.get_report(receipt_uuid)

            if result.status == "done":
                logger.info("АТОЛ чек фискализирован: uuid=%s, attempt=%d", receipt_uuid, attempt)
                return result

            if result.status == "fail":
                logger.error("АТОЛ чек ОШИБКА: uuid=%s, error=%s", receipt_uuid, result.error)
                return result

            # status == "wait" — ещё обрабатывается
            logger.info("АТОЛ чек в очереди: uuid=%s, attempt=%d/%d",
                        receipt_uuid, attempt, max_attempts)
            await asyncio.sleep(interval)

        logger.warning("АТОЛ чек не дождались: uuid=%s после %d попыток",
                        receipt_uuid, max_attempts)
        return FiscalResult(
            success=False,
            uuid=receipt_uuid,
            status="wait",
            error="Timeout waiting for fiscalization",
        )


# Глобальный экземпляр клиента
atol_client = AtolOnlineClient()


# ---------------------------------------------------------------------------
#  Удобные функции для вызова из main.py
# ---------------------------------------------------------------------------

async def fiscalize_order(
    order_id: int,
    order_number: int,
    items: list[dict[str, Any]],
    total_amount: int,  # в рублях (целое число)
    client_email: str = "",
    client_phone: str = "",
    payment_method: str = "full_payment",
) -> FiscalResult:
    """
    Фискализировать заказ (создать чек продажи).

    Args:
        order_id: ID заказа в БД
        order_number: Публичный номер заказа
        items: Позиции заказа из БД [{name_snapshot, price_snapshot, quantity}]
        total_amount: Сумма в рублях (целое число, как в БД)
        client_email: Email для чека
        client_phone: Телефон для чека
        payment_method: "full_payment" (полный расчёт) or "prepayment" (предоплата 100%)

    Returns:
        FiscalResult
    """
    # Конвертируем позиции из формата БД в формат АТОЛ
    atol_items = [
        {
            "name": item["name_snapshot"],
            "price": float(item["price_snapshot"]),
            "quantity": item["quantity"],
            "payment_method": payment_method,
        }
        for item in items
    ]

    result = await atol_client.sell(
        order_id=order_id,
        order_number=order_number,
        items=atol_items,
        total=float(total_amount),
        client_email=client_email,
        client_phone=client_phone,
    )

    if result.success and result.uuid:
        # Запускаем фоновую проверку статуса (не блокируем основной поток)
        asyncio.create_task(_check_fiscal_status(result.uuid, order_id))

    return result


async def _check_fiscal_status(receipt_uuid: str, order_id: int) -> None:
    """Фоновая задача: проверка статуса фискализации через 10 секунд."""
    try:
        await asyncio.sleep(10)
        result = await atol_client.get_report(receipt_uuid)
        if result.status == "done":
            logger.info("Фискализация подтверждена: order=%d, uuid=%s", order_id, receipt_uuid)
        elif result.status == "fail":
            logger.error("Фискализация ПРОВАЛЕНА: order=%d, uuid=%s, error=%s",
                         order_id, receipt_uuid, result.error)
        else:
            logger.info("Фискализация ещё обрабатывается: order=%d, uuid=%s", order_id, receipt_uuid)
    except Exception:
        logger.exception("Ошибка проверки фискализации: order=%d", order_id)


async def refund_order(
    order_id: int,
    order_number: int,
    items: list[dict[str, Any]],
    total_amount: int,
    client_email: str = "",
    client_phone: str = "",
) -> FiscalResult:
    """Создать чек возврата."""
    atol_items = [
        {
            "name": item["name_snapshot"],
            "price": float(item["price_snapshot"]),
            "quantity": item["quantity"],
        }
        for item in items
    ]

    return await atol_client.sell_refund(
        order_id=order_id,
        order_number=order_number,
        items=atol_items,
        total=float(total_amount),
        client_email=client_email,
        client_phone=client_phone,
    )
