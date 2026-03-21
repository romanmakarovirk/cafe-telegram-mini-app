"""
СБП Сбербанк — модуль приёма онлайн-платежей через Систему Быстрых Платежей.

Интеграция через Sberbank Acquiring REST API (register.do + SBP C2B).
Клиент оплачивает в мобильном приложении банка по deeplink.
Деньги зачисляются на расчётный счёт ИП в Сбербанке.

Документация: https://securepayments.sberbank.ru/wiki/doku.php/integration:api:start
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Конфигурация (из переменных окружения)
# ---------------------------------------------------------------------------
SBP_BASE_URL = os.getenv("SBP_BASE_URL", "https://securepayments.sberbank.ru")
SBP_USERNAME = os.getenv("SBP_USERNAME", "")      # логин вида myshop-api
SBP_PASSWORD = os.getenv("SBP_PASSWORD", "")       # пароль
SBP_TOKEN = os.getenv("SBP_TOKEN", "")             # альтернатива: единый токен
SBP_RETURN_URL = os.getenv("SBP_RETURN_URL", "")   # URL после успешной оплаты
SBP_FAIL_URL = os.getenv("SBP_FAIL_URL", "")       # URL при ошибке оплаты
SBP_CALLBACK_SECRET = os.getenv("SBP_CALLBACK_SECRET", "")  # ключ для проверки callback

# Тестовый режим (песочница Сбербанка)
SBP_TEST_MODE = os.getenv("SBP_TEST_MODE", "true").lower() in ("true", "1", "yes")
SBP_TEST_URL = "https://ecomtest.sberbank.ru"


# ---------------------------------------------------------------------------
#  Модели данных
# ---------------------------------------------------------------------------
@dataclass
class SbpPaymentResult:
    """Результат создания платежа."""
    success: bool
    order_id: str = ""          # Sberbank gateway order UUID
    payment_url: str = ""       # URL платёжной формы (fallback)
    deeplink: str = ""          # SBP deeplink (для мобильного приложения банка)
    qrc_id: str = ""            # ID QR-кода НСПК
    error_code: str = ""
    error_message: str = ""


@dataclass
class SbpStatusResult:
    """Результат проверки статуса платежа."""
    success: bool
    order_status: int = -1      # 0=создан, 2=оплачен, 3=отменён, 4=возврат, 6=отклонён
    amount: Optional[int] = None  # сумма в копейках (None если SBP API не вернул)
    error_code: str = ""
    error_message: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        """Статус 2 = DEPOSITED = деньги получены."""
        return self.order_status == 2

    @property
    def is_declined(self) -> bool:
        return self.order_status == 6

    @property
    def status_label(self) -> str:
        labels = {
            0: "created",
            1: "approved",
            2: "deposited",
            3: "reversed",
            4: "refunded",
            5: "auth_started",
            6: "declined",
        }
        return labels.get(self.order_status, f"unknown({self.order_status})")


@dataclass
class SbpRefundResult:
    """Результат возврата."""
    success: bool
    error_code: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
#  Клиент СБП Сбербанк
# ---------------------------------------------------------------------------
class SbpSberbankClient:
    """
    Клиент Sberbank Acquiring API для приёма платежей через СБП.

    Основные методы:
    - create_payment()  — создание платежа (register.do с SBP параметрами)
    - get_status()      — проверка статуса (getOrderStatusExtended.do)
    - refund()          — возврат средств (refund.do)
    """

    def __init__(self):
        self._base_url = SBP_TEST_URL if SBP_TEST_MODE else SBP_BASE_URL
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли credentials."""
        has_token = bool(SBP_TOKEN)
        has_login = bool(SBP_USERNAME) and bool(SBP_PASSWORD)
        return has_token or has_login

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _auth_params(self) -> dict[str, str]:
        """Параметры авторизации (токен или логин/пароль)."""
        if SBP_TOKEN:
            return {"token": SBP_TOKEN}
        return {"userName": SBP_USERNAME, "password": SBP_PASSWORD}

    # ----- Создание платежа -----

    async def create_payment(
        self,
        order_number: str,
        amount_rub: int,
        description: str = "",
        return_url: str = "",
        fail_url: str = "",
    ) -> SbpPaymentResult:
        """
        Создание платежа через СБП.

        POST /payment/rest/register.do
        Content-Type: application/x-www-form-urlencoded

        Args:
            order_number: Уникальный номер заказа (для Сбербанка)
            amount_rub: Сумма в рублях (целое число)
            description: Описание платежа
            return_url: URL для возврата после успешной оплаты
            fail_url: URL для возврата при ошибке

        Returns:
            SbpPaymentResult с deeplink для оплаты
        """
        if not self.is_configured:
            logger.warning("СБП Сбербанк не настроен, платёж невозможен")
            return SbpPaymentResult(success=False, error_message="СБП не настроен")

        client = await self._get_client()
        url = f"{self._base_url}/payment/rest/register.do"

        # Сумма в копейках (Сбербанк API принимает копейки)
        amount_kopecks = amount_rub * 100

        # JSON-параметры для активации СБП C2B
        import json
        json_params = json.dumps({
            "qrType": "DYNAMIC_QR_SBP",
            "sbp.scenario": "C2B",
            "description": description or f"Заказ #{order_number}",
        }, ensure_ascii=False)

        params = {
            **self._auth_params(),
            "orderNumber": order_number,
            "amount": str(amount_kopecks),
            "currency": "643",  # RUB
            "returnUrl": return_url or SBP_RETURN_URL,
            "failUrl": fail_url or SBP_FAIL_URL,
            "description": description or f"Заказ #{order_number}",
            "jsonParams": json_params,
        }

        try:
            logger.info("СБП create_payment: order=%s, amount=%d руб.", order_number, amount_rub)
            resp = await client.post(
                url,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

            error_code = str(data.get("errorCode", "0"))
            if error_code != "0":
                error_msg = data.get("errorMessage", "Unknown error")
                logger.error("СБП create_payment ошибка: code=%s, msg=%s", error_code, error_msg)
                return SbpPaymentResult(
                    success=False,
                    error_code=error_code,
                    error_message=error_msg,
                )

            gateway_order_id = data.get("orderId", "")
            form_url = data.get("formUrl", "")

            # Извлекаем SBP deeplink из externalParams
            ext = data.get("externalParams", {})
            deeplink = ext.get("sbpPayload", "")
            qrc_id = ext.get("qrcId", "")

            logger.info(
                "СБП create_payment OK: gateway_order=%s, deeplink=%s",
                gateway_order_id,
                "yes" if deeplink else "no",
            )

            return SbpPaymentResult(
                success=True,
                order_id=gateway_order_id,
                payment_url=form_url,
                deeplink=deeplink,
                qrc_id=qrc_id,
            )

        except httpx.HTTPStatusError as e:
            error_text = e.response.text[:500]
            logger.error("СБП create_payment HTTP %s: %s", e.response.status_code, error_text)
            return SbpPaymentResult(
                success=False,
                error_message=f"HTTP {e.response.status_code}: {error_text}",
            )
        except httpx.RequestError as e:
            logger.error("СБП create_payment сетевая ошибка: %s", e)
            return SbpPaymentResult(success=False, error_message=f"Network error: {e}")

    # ----- Проверка статуса платежа -----

    async def get_status(self, gateway_order_id: str) -> SbpStatusResult:
        """
        Проверка статуса платежа.

        POST /payment/rest/getOrderStatusExtended.do

        Статусы:
        - 0: CREATED — заказ создан, не оплачен
        - 2: DEPOSITED — оплачен (деньги получены)
        - 3: REVERSED — отменён
        - 4: REFUNDED — возврат
        - 6: DECLINED — отклонён

        Args:
            gateway_order_id: UUID заказа в системе Сбербанка

        Returns:
            SbpStatusResult
        """
        if not self.is_configured:
            return SbpStatusResult(success=False, error_message="СБП не настроен")

        client = await self._get_client()
        url = f"{self._base_url}/payment/rest/getOrderStatusExtended.do"

        params = {
            **self._auth_params(),
            "orderId": gateway_order_id,
        }

        try:
            resp = await client.post(
                url,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

            error_code = str(data.get("errorCode", "0"))
            if error_code != "0":
                return SbpStatusResult(
                    success=False,
                    error_code=error_code,
                    error_message=data.get("errorMessage", ""),
                    raw_data=data,
                )

            order_status = data.get("orderStatus", -1)
            amount = data.get("amount")  # None if SBP API didn't return amount

            logger.info(
                "СБП get_status: gateway_order=%s, status=%d (%s)",
                gateway_order_id,
                order_status,
                SbpStatusResult(success=True, order_status=order_status).status_label,
            )

            return SbpStatusResult(
                success=True,
                order_status=order_status,
                amount=amount,
                raw_data=data,
            )

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("СБП get_status ошибка: %s", e)
            return SbpStatusResult(success=False, error_message=str(e))

    # ----- Возврат средств -----

    async def refund(
        self,
        gateway_order_id: str,
        amount_rub: int,
    ) -> SbpRefundResult:
        """
        Возврат средств (полный или частичный).

        POST /payment/rest/refund.do

        Args:
            gateway_order_id: UUID заказа в системе Сбербанка
            amount_rub: Сумма возврата в рублях

        Returns:
            SbpRefundResult
        """
        if not self.is_configured:
            return SbpRefundResult(success=False, error_message="СБП не настроен")

        client = await self._get_client()
        url = f"{self._base_url}/payment/rest/refund.do"

        params = {
            **self._auth_params(),
            "orderId": gateway_order_id,
            "amount": str(amount_rub * 100),  # В копейках
        }

        try:
            logger.info("СБП refund: gateway_order=%s, amount=%d руб.", gateway_order_id, amount_rub)
            resp = await client.post(
                url,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

            error_code = str(data.get("errorCode", "0"))
            if error_code != "0":
                error_msg = data.get("errorMessage", "Unknown error")
                logger.error("СБП refund ошибка: code=%s, msg=%s", error_code, error_msg)
                return SbpRefundResult(
                    success=False,
                    error_code=error_code,
                    error_message=error_msg,
                )

            logger.info("СБП refund OK: gateway_order=%s", gateway_order_id)
            return SbpRefundResult(success=True)

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("СБП refund ошибка: %s", e)
            return SbpRefundResult(success=False, error_message=str(e))


# Глобальный экземпляр клиента
sbp_client = SbpSberbankClient()


# ---------------------------------------------------------------------------
#  Удобные функции для вызова из main.py
# ---------------------------------------------------------------------------

async def create_sbp_payment(
    order_id: int,
    order_number: int,
    total_amount: int,
    description: str = "",
) -> SbpPaymentResult:
    """
    Создать платёж через СБП.

    Args:
        order_id: ID заказа в нашей БД
        order_number: Публичный номер заказа
        total_amount: Сумма в рублях (целое число, как в БД)
        description: Описание для клиента

    Returns:
        SbpPaymentResult с deeplink для оплаты
    """
    # Уникальный orderNumber для Сбербанка (order_id + номер)
    sber_order_number = f"SHASHLIK-{order_number}"

    result = await sbp_client.create_payment(
        order_number=sber_order_number,
        amount_rub=total_amount,
        description=description or f"Кафе Шашлык и Плов — заказ #{order_number}",
    )

    if result.success:
        logger.info(
            "СБП платёж создан: order=%d, gateway=%s, deeplink=%s",
            order_id, result.order_id, bool(result.deeplink),
        )
    else:
        logger.error(
            "СБП платёж НЕ создан: order=%d, error=%s",
            order_id, result.error_message,
        )

    return result


async def check_sbp_payment(gateway_order_id: str) -> SbpStatusResult:
    """Проверить статус платежа по gateway_order_id."""
    return await sbp_client.get_status(gateway_order_id)


async def refund_sbp_payment(
    gateway_order_id: str,
    amount: int,
) -> SbpRefundResult:
    """
    Вернуть средства клиенту.

    Args:
        gateway_order_id: UUID заказа в Сбербанке
        amount: Сумма возврата в рублях
    """
    return await sbp_client.refund(gateway_order_id, amount)


def verify_callback(
    order_id: str,
    order_number: str,
    operation: str,
    status: str,
    checksum: str = "",
) -> bool:
    """
    Проверка подлинности callback от Сбербанка.

    Callback приходит на наш URL:
    POST /api/sbp/callback?mdOrder={orderId}&orderNumber={orderNumber}
        &operation={operation}&status={status}&checksum={checksum}

    Args:
        order_id: Gateway order UUID (mdOrder)
        order_number: Номер заказа мерчанта
        operation: Тип операции (deposited, reversed, refunded)
        status: Статус (0 = успех, 1 = ошибка)
        checksum: HMAC-SHA256 подпись

    Returns:
        True если callback подлинный
    """
    if not SBP_CALLBACK_SECRET:
        # Fail-secure: без секрета callback отклоняется
        logger.error(
            "СБП callback secret не настроен (SBP_CALLBACK_SECRET). "
            "Callback отклонён. Настройте секрет для приёма платежей."
        )
        return False

    if not checksum:
        logger.warning("СБП callback без checksum")
        return False

    import hashlib
    import hmac

    # Формируем строку для подписи (порядок параметров важен)
    sign_string = f"{order_id};{order_number};{operation};{status}"
    expected = hmac.new(
        SBP_CALLBACK_SECRET.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected.lower(), checksum.lower())
    if not is_valid:
        logger.warning(
            "СБП callback: неверный checksum для order=%s (got=%s...)",
            order_number, checksum[:8],
        )

    return is_valid
