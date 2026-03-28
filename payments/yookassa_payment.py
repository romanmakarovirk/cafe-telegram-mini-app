"""
ЮKassa — модуль приёма онлайн-платежей.

Интеграция через YooKassa API v3 (HTTP + Basic Auth).
Поддержка: СБП, банковские карты, YooMoney.
Встроенная фискализация (54-ФЗ) — чеки формируются автоматически ЮKassa.

Документация: https://yookassa.ru/developers/api
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Конфигурация (из переменных окружения)
# ---------------------------------------------------------------------------
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

# Тестовый режим
YOOKASSA_TEST_MODE = os.getenv("YOOKASSA_TEST_MODE", "true").lower() in ("true", "1", "yes")

# URL API (тестовый и боевой одинаковый, режим определяется credentials)
YOOKASSA_API_URL = "https://api.yookassa.ru/v3"

# URL возврата после оплаты (куда вернётся пользователь из платёжной формы)
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "")

# Доверенные IP-адреса ЮKassa для webhook (IPv4)
# https://yookassa.ru/developers/using-api/webhooks#ip
YOOKASSA_TRUSTED_IPS = {
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11",
    "77.75.156.35",
    "77.75.154.128/25",
    "2a02:5180::/32",
}

# ИНН и email компании для чеков (COMPANY_* — основные, ATOL_* — fallback для переходного периода)
YOOKASSA_INN = os.getenv("COMPANY_INN") or os.getenv("ATOL_INN", "")
YOOKASSA_COMPANY_EMAIL = os.getenv("COMPANY_EMAIL") or os.getenv("ATOL_COMPANY_EMAIL", "")
YOOKASSA_TAX_SYSTEM = os.getenv("COMPANY_SNO") or os.getenv("ATOL_SNO", "usn_income")

# Маппинг SNO → tax_system_code для ЮKassa
_TAX_SYSTEM_MAP = {
    "osn": 1,
    "usn_income": 2,
    "usn_income_outcome": 3,
    "envd": 4,        # Deprecated
    "esn": 5,
    "patent": 6,
}

# Маппинг vat_code для ЮKassa (без НДС для УСН)
VAT_CODE_NONE = 1  # Без НДС


# ---------------------------------------------------------------------------
#  Модели данных
# ---------------------------------------------------------------------------
@dataclass
class YookassaPaymentResult:
    """Результат создания платежа."""
    success: bool
    payment_id: str = ""         # ID платежа в ЮKassa
    confirmation_url: str = ""   # URL для оплаты (redirect)
    status: str = ""             # pending, waiting_for_capture, succeeded, canceled
    error_code: str = ""
    error_message: str = ""


@dataclass
class YookassaStatusResult:
    """Результат проверки статуса платежа."""
    success: bool
    payment_id: str = ""
    status: str = ""             # pending, waiting_for_capture, succeeded, canceled
    paid: bool = False
    amount: Optional[int] = None  # сумма в копейках (None если не удалось получить)
    error_message: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.status == "succeeded" and self.paid


@dataclass
class YookassaRefundResult:
    """Результат возврата."""
    success: bool
    refund_id: str = ""
    status: str = ""             # succeeded, canceled
    error_code: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
#  Клиент ЮKassa
# ---------------------------------------------------------------------------
class YookassaClient:
    """
    Клиент YooKassa API v3.

    Основные методы:
    - create_payment()  — создание платежа с чеком (54-ФЗ автоматически)
    - get_payment()     — проверка статуса платежа
    - create_refund()   — возврат средств с чеком возврата
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли credentials."""
        return bool(YOOKASSA_SHOP_ID) and bool(YOOKASSA_SECRET_KEY)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ----- Создание платежа -----

    async def create_payment(
        self,
        order_id: int,
        order_number: int,
        amount_rub: int,
        items: list[dict[str, Any]],
        description: str = "",
        customer_email: str = "",
        customer_phone: str = "",
        return_url: str = "",
        payment_method: str = "",
    ) -> YookassaPaymentResult:
        """
        Создание платежа с автоматической фискализацией.

        ЮKassa сама формирует чек 54-ФЗ и отправляет в ОФД.

        Args:
            order_id: ID заказа в нашей БД
            order_number: Публичный номер заказа
            amount_rub: Сумма в рублях (целое число)
            items: Позиции заказа [{name_snapshot, price_snapshot, quantity}]
            description: Описание платежа
            customer_email: Email для чека
            customer_phone: Телефон для чека
            return_url: URL возврата после оплаты
            payment_method: "sbp" / "bank_card" / "" (любой)

        Returns:
            YookassaPaymentResult с URL для оплаты
        """
        if not self.is_configured:
            logger.warning("ЮKassa не настроена, платёж невозможен")
            return YookassaPaymentResult(success=False, error_message="ЮKassa не настроена")

        if amount_rub <= 0:
            return YookassaPaymentResult(success=False, error_message=f"Invalid amount: {amount_rub}")

        client = await self._get_client()
        url = f"{YOOKASSA_API_URL}/payments"

        # Позиции чека для 54-ФЗ
        receipt_items = []
        for item in items:
            price = int(item["price_snapshot"])
            quantity = int(item["quantity"])
            item_total = price * quantity

            receipt_items.append({
                "description": str(item["name_snapshot"])[:128],
                "quantity": str(quantity),
                "amount": {
                    "value": f"{item_total}.00",
                    "currency": "RUB",
                },
                "vat_code": VAT_CODE_NONE,
                "payment_subject": "commodity",
                "payment_mode": "full_payment",
            })

        # Данные покупателя для чека
        receipt_customer: dict[str, str] = {}
        if customer_email:
            receipt_customer["email"] = customer_email
        elif customer_phone:
            receipt_customer["phone"] = customer_phone
        else:
            receipt_customer["email"] = YOOKASSA_COMPANY_EMAIL or "noreply@cafe.ru"

        # Тело запроса
        payload: dict[str, Any] = {
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB",
            },
            "capture": True,  # Автоматическое подтверждение платежа
            "confirmation": {
                "type": "redirect",
                "return_url": return_url or YOOKASSA_RETURN_URL or "https://t.me",
            },
            "description": description or f"Заказ #{order_number} — Шашлык и Плов",
            "metadata": {
                "order_id": str(order_id),
                "order_number": str(order_number),
            },
            "receipt": {
                "customer": receipt_customer,
                "items": receipt_items,
                "tax_system_code": _TAX_SYSTEM_MAP.get(YOOKASSA_TAX_SYSTEM, 2),
            },
        }

        # Принудительно СБП если указано
        if payment_method == "sbp":
            payload["payment_method_data"] = {"type": "sbp"}

        # Idempotency key — предотвращает дубли при retry
        idempotency_key = f"order-{order_id}-{order_number}"

        try:
            logger.info("ЮKassa create_payment: order=%d, amount=%d руб.", order_number, amount_rub)
            resp = await client.post(
                url,
                json=payload,
                headers={"Idempotence-Key": idempotency_key},
            )

            if resp.status_code >= 400:
                error_text = resp.text[:500]
                logger.error("ЮKassa create_payment HTTP %s: %s", resp.status_code, error_text)
                try:
                    err_data = resp.json()
                    err_code = err_data.get("code", "")
                    err_desc = err_data.get("description", error_text)
                except Exception:
                    err_code = str(resp.status_code)
                    err_desc = error_text
                return YookassaPaymentResult(
                    success=False,
                    error_code=err_code,
                    error_message=err_desc,
                )

            data = resp.json()
            payment_id = data.get("id", "")
            status = data.get("status", "")

            # Извлекаем URL для оплаты
            confirmation = data.get("confirmation", {})
            confirmation_url = confirmation.get("confirmation_url", "")

            logger.info(
                "ЮKassa create_payment OK: payment_id=%s, status=%s, url=%s",
                payment_id, status, bool(confirmation_url),
            )

            return YookassaPaymentResult(
                success=True,
                payment_id=payment_id,
                confirmation_url=confirmation_url,
                status=status,
            )

        except httpx.RequestError as e:
            logger.error("ЮKassa create_payment сетевая ошибка: %s", e)
            return YookassaPaymentResult(success=False, error_message=f"Network error: {e}")

    # ----- Проверка статуса платежа -----

    async def get_payment(self, payment_id: str) -> YookassaStatusResult:
        """
        Проверка статуса платежа.

        GET /v3/payments/{payment_id}

        Статусы:
        - pending: ожидает оплаты
        - waiting_for_capture: оплачен, ожидает подтверждения (capture=False)
        - succeeded: оплата завершена
        - canceled: отменён

        Args:
            payment_id: ID платежа в ЮKassa

        Returns:
            YookassaStatusResult
        """
        if not self.is_configured:
            return YookassaStatusResult(success=False, error_message="ЮKassa не настроена")

        client = await self._get_client()
        url = f"{YOOKASSA_API_URL}/payments/{payment_id}"

        try:
            resp = await client.get(url)

            if resp.status_code >= 400:
                return YookassaStatusResult(
                    success=False,
                    error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )

            data = resp.json()
            status = data.get("status", "")
            paid = data.get("paid", False)

            # Сумма в копейках
            amount = None
            amount_obj = data.get("amount", {})
            try:
                from decimal import Decimal
                amount = int(Decimal(amount_obj.get("value", "0")) * 100)
            except (ValueError, TypeError):
                pass

            logger.info(
                "ЮKassa get_payment: id=%s, status=%s, paid=%s",
                payment_id, status, paid,
            )

            return YookassaStatusResult(
                success=True,
                payment_id=payment_id,
                status=status,
                paid=paid,
                amount=amount,
                raw_data=data,
            )

        except httpx.RequestError as e:
            logger.error("ЮKassa get_payment ошибка: %s", e)
            return YookassaStatusResult(success=False, error_message=str(e))

    # ----- Возврат средств -----

    async def create_refund(
        self,
        payment_id: str,
        amount_rub: int,
        reason: str = "Позиции недоступны",
    ) -> YookassaRefundResult:
        """
        Возврат средств (полный или частичный).

        POST /v3/refunds

        ЮKassa автоматически создаёт чек возврата (54-ФЗ).

        Args:
            payment_id: ID платежа в ЮKassa
            amount_rub: Сумма возврата в рублях

        Returns:
            YookassaRefundResult
        """
        if not self.is_configured:
            return YookassaRefundResult(success=False, error_message="ЮKassa не настроена")

        if amount_rub <= 0:
            return YookassaRefundResult(success=False, error_message=f"Invalid refund amount: {amount_rub}")

        client = await self._get_client()
        url = f"{YOOKASSA_API_URL}/refunds"

        payload = {
            "payment_id": payment_id,
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB",
            },
            "description": reason,
        }

        idempotency_key = f"refund-{payment_id}-{amount_rub}"

        try:
            logger.info("ЮKassa refund: payment=%s, amount=%d руб.", payment_id, amount_rub)
            resp = await client.post(
                url,
                json=payload,
                headers={"Idempotence-Key": idempotency_key},
            )

            if resp.status_code >= 400:
                error_text = resp.text[:500]
                logger.error("ЮKassa refund HTTP %s: %s", resp.status_code, error_text)
                try:
                    err_data = resp.json()
                    err_code = err_data.get("code", "")
                    err_desc = err_data.get("description", error_text)
                except Exception:
                    err_code = str(resp.status_code)
                    err_desc = error_text
                return YookassaRefundResult(
                    success=False,
                    error_code=err_code,
                    error_message=err_desc,
                )

            data = resp.json()
            refund_id = data.get("id", "")
            status = data.get("status", "")

            logger.info("ЮKassa refund OK: refund_id=%s, status=%s", refund_id, status)
            return YookassaRefundResult(
                success=True,
                refund_id=refund_id,
                status=status,
            )

        except httpx.RequestError as e:
            logger.error("ЮKassa refund ошибка: %s", e)
            return YookassaRefundResult(success=False, error_message=str(e))


# Глобальный экземпляр клиента
yookassa_client = YookassaClient()


# ---------------------------------------------------------------------------
#  Удобные функции (совместимые по интерфейсу с sbp.py)
# ---------------------------------------------------------------------------

async def create_yookassa_payment(
    order_id: int,
    order_number: int,
    total_amount: int,
    items: list[dict[str, Any]],
    customer_email: str = "",
    customer_phone: str = "",
    description: str = "",
) -> YookassaPaymentResult:
    """
    Создать платёж через ЮKassa.

    Args:
        order_id: ID заказа в нашей БД
        order_number: Публичный номер заказа
        total_amount: Сумма в рублях
        items: Позиции заказа [{name_snapshot, price_snapshot, quantity}]
        customer_email: Email покупателя для чека
        customer_phone: Телефон покупателя для чека

    Returns:
        YookassaPaymentResult с URL для оплаты
    """
    result = await yookassa_client.create_payment(
        order_id=order_id,
        order_number=order_number,
        amount_rub=total_amount,
        items=items,
        description=description or f"Заказ #{order_number} — Шашлык и Плов",
        customer_email=customer_email,
        customer_phone=customer_phone,
    )

    if result.success:
        logger.info(
            "ЮKassa платёж создан: order=%d, payment_id=%s",
            order_id, result.payment_id,
        )
    else:
        logger.error(
            "ЮKassa платёж НЕ создан: order=%d, error=%s",
            order_id, result.error_message,
        )

    return result


async def check_yookassa_payment(payment_id: str) -> YookassaStatusResult:
    """Проверить статус платежа по payment_id."""
    return await yookassa_client.get_payment(payment_id)


async def refund_yookassa_payment(
    payment_id: str,
    amount: int,
) -> YookassaRefundResult:
    """
    Вернуть средства клиенту.

    Args:
        payment_id: ID платежа в ЮKassa
        amount: Сумма возврата в рублях
    """
    return await yookassa_client.create_refund(payment_id, amount)


def is_trusted_ip(ip: str) -> bool:
    """
    Проверка что IP-адрес принадлежит ЮKassa (для webhook).

    В тестовом режиме принимает любые IP.
    В боевом — проверяет по списку доверенных подсетей.
    """
    if YOOKASSA_TEST_MODE:
        return True

    import ipaddress
    try:
        client_ip = ipaddress.ip_address(ip)
        for network_str in YOOKASSA_TRUSTED_IPS:
            try:
                network = ipaddress.ip_network(network_str)
                if client_ip in network:
                    return True
            except ValueError:
                # Одиночный IP а не подсеть
                if str(client_ip) == network_str:
                    return True
        return False
    except ValueError:
        logger.warning("ЮKassa webhook: невалидный IP %s", ip)
        return False
