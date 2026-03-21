"""
1С:Fresh OData integration module.

Automatically syncs online orders to 1С:Бухгалтерия Предприятия (Fresh)
by creating "Реализация товаров и услуг" documents via OData REST API.

1С:Fresh OData docs: https://1cfresh.com/articles/data_odata

Environment variables:
    FRESH_BASE_URL      — base URL of the 1C:Fresh instance
                          e.g. https://1cfresh.com/a/i12345/odata/standard.odata
    FRESH_USERNAME      — service user login (role: УдаленныйДоступOData)
    FRESH_PASSWORD      — service user password
    FRESH_ENABLED       — "true" to enable sync (default: "false")
    FRESH_COUNTERPARTY  — counterparty name for retail sales
                          (default: "Розничный покупатель")
    FRESH_WAREHOUSE     — warehouse name (default: "Основной склад")
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx

logger = logging.getLogger("integrations.accounting")

# ── Configuration ──────────────────────────────────────────────────────────

FRESH_BASE_URL = os.getenv("FRESH_BASE_URL", "")
FRESH_USERNAME = os.getenv("FRESH_USERNAME", "")
FRESH_PASSWORD = os.getenv("FRESH_PASSWORD", "")
FRESH_ENABLED = os.getenv("FRESH_ENABLED", "false").lower() in ("true", "1", "yes")
FRESH_COUNTERPARTY = os.getenv("FRESH_COUNTERPARTY", "Розничный покупатель")
FRESH_WAREHOUSE = os.getenv("FRESH_WAREHOUSE", "Основной склад")

# Retry settings
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds, exponential backoff


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    """Result of syncing an order to 1C."""
    success: bool
    document_id: Optional[str] = None
    document_number: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "document_id": self.document_id,
            "document_number": self.document_number,
            "error": self.error,
            "attempts": self.attempts,
        }


@dataclass
class NomenclatureItem:
    """1C nomenclature item mapping."""
    ref_key: str          # GUID in 1C
    name: str             # Name in 1C
    code: str = ""        # Code in 1C
    unit_key: str = ""    # Unit of measurement GUID
    unit_name: str = ""   # e.g. "шт", "порц"


@dataclass
class NomenclatureCache:
    """Cache for 1C nomenclature to avoid repeated API calls."""
    items: dict[str, NomenclatureItem] = field(default_factory=dict)
    loaded_at: Optional[datetime] = None
    ttl_seconds: int = 3600  # refresh every hour

    @property
    def is_stale(self) -> bool:
        if self.loaded_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self.loaded_at).total_seconds()
        return elapsed > self.ttl_seconds


# ── OData Client ───────────────────────────────────────────────────────────

class FreshODataClient:
    """
    Client for 1С:Fresh OData REST API.

    Handles authentication (Basic), nomenclature lookup,
    and document creation for "Реализация товаров и услуг".
    """

    def __init__(
        self,
        base_url: str = "",
        username: str = "",
        password: str = "",
    ):
        self.base_url = (base_url or FRESH_BASE_URL).rstrip("/")
        self.username = username or FRESH_USERNAME
        self.password = password or FRESH_PASSWORD
        self.enabled = FRESH_ENABLED and bool(self.base_url)
        self._nomenclature = NomenclatureCache()
        self._http: Optional[httpx.AsyncClient] = None

    # ── HTTP layer ────────────────────────────────────────────────────

    def _auth_header(self) -> str:
        """Build Basic auth header value."""
        cred = f"{self.username}:{self.password}"
        b64 = base64.b64encode(cred.encode("utf-8")).decode("ascii")
        return f"Basic {b64}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json;odata=verbose",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers=self._headers(),
                follow_redirects=True,
            )
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """Execute HTTP request with retry + exponential backoff."""
        client = await self._get_client()
        url = f"{self.base_url}/{path.lstrip('/')}"

        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    json=json_data,
                    params=params,
                )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response

            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "1C OData request failed (attempt %d/%d): %s. "
                        "Retrying in %.1fs...",
                        attempt, MAX_RETRIES, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "1C OData request failed after %d attempts: %s",
                        MAX_RETRIES, exc,
                    )
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    # ── Nomenclature ──────────────────────────────────────────────────

    async def get_nomenclature(self, force_refresh: bool = False) -> dict[str, NomenclatureItem]:
        """
        Fetch nomenclature catalog from 1C.
        Returns dict: name (lower) → NomenclatureItem.
        Caches results for 1 hour.
        """
        if not force_refresh and not self._nomenclature.is_stale:
            return self._nomenclature.items

        logger.info("Fetching nomenclature catalog from 1C:Fresh...")

        try:
            response = await self._request(
                "GET",
                "Catalog_Номенклатура",
                params={
                    "$select": "Ref_Key,Description,Code",
                    "$filter": "DeletionMark eq false",
                    "$top": "1000",
                    "$format": "json",
                },
            )
            response.raise_for_status()
            data = response.json()

            items: dict[str, NomenclatureItem] = {}
            for entry in data.get("value", []):
                item = NomenclatureItem(
                    ref_key=entry.get("Ref_Key", ""),
                    name=entry.get("Description", ""),
                    code=entry.get("Code", ""),
                )
                # Index by lowercase name for fuzzy matching
                items[item.name.lower().strip()] = item

            self._nomenclature.items = items
            self._nomenclature.loaded_at = datetime.now(timezone.utc)
            logger.info("Loaded %d nomenclature items from 1C", len(items))
            return items

        except Exception as exc:
            logger.error("Failed to fetch nomenclature: %s", exc)
            # Return stale cache if available
            if self._nomenclature.items:
                logger.warning("Using stale nomenclature cache (%d items)",
                               len(self._nomenclature.items))
                return self._nomenclature.items
            raise

    # ── Nomenclature mapping ──────────────────────────────────────────

    async def find_nomenclature(self, menu_item_name: str) -> Optional[NomenclatureItem]:
        """
        Find 1C nomenclature item by menu item name.
        Uses fuzzy matching: exact → contains → starts with.
        """
        items = await self.get_nomenclature()
        name_lower = menu_item_name.lower().strip()

        # 1. Exact match
        if name_lower in items:
            return items[name_lower]

        # 2. Contains match
        for key, item in items.items():
            if name_lower in key or key in name_lower:
                return item

        # 3. First word match (e.g. "Шашлык свиной 300г" → "Шашлык")
        first_word = name_lower.split()[0] if name_lower else ""
        if first_word and len(first_word) > 2:
            for key, item in items.items():
                if key.startswith(first_word):
                    return item

        logger.warning(
            "No 1C nomenclature match for menu item: '%s'", menu_item_name
        )
        return None

    # ── Document creation ─────────────────────────────────────────────

    async def create_sale_document(
        self,
        order_id: int,
        order_number: str,
        items: list[dict[str, Any]],
        total_amount: float,
        payment_date: Optional[datetime] = None,
    ) -> SyncResult:
        """
        Create "Реализация товаров и услуг" document in 1С:БП.

        Args:
            order_id: Internal order ID
            order_number: Public order number (e.g. "#4648")
            items: List of dicts with keys: name, quantity, price, total
            total_amount: Total order amount
            payment_date: When the payment was made (default: now)

        Returns:
            SyncResult with document details or error
        """
        if not self.enabled:
            return SyncResult(
                success=False,
                error="1C integration is disabled (FRESH_ENABLED=false)",
                attempts=0,
            )

        payment_date = payment_date or datetime.now(timezone.utc)

        # Build line items (табличная часть "Товары")
        line_items = []
        for idx, item in enumerate(items):
            # Try to find nomenclature in 1C
            nom = await self.find_nomenclature(item.get("name", ""))

            line_entry: dict[str, Any] = {
                "LineNumber": str(idx + 1),
                "Количество": item.get("quantity", 1),
                "Цена": item.get("price", 0),
                "Сумма": item.get("total", item.get("price", 0) * item.get("quantity", 1)),
                "СтавкаНДС": "БезНДС",  # УСН — without VAT
            }

            if nom:
                line_entry["Номенклатура_Key"] = nom.ref_key
            else:
                # If no match — use description as comment
                line_entry["СодержаниеУслуги"] = (
                    f"{item.get('name', 'Блюдо')} (заказ #{order_number})"
                )

            line_items.append(line_entry)

        # Build document payload
        doc_payload: dict[str, Any] = {
            "Date": payment_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "Комментарий": f"Онлайн-заказ #{order_number} (Telegram Mini App)",
            "Товары": line_items,
        }

        # Optional: set counterparty if known
        # This requires knowing the Ref_Key of the counterparty in 1C
        # For now, we set it via comment — the accountant can adjust

        logger.info(
            "Creating sale document in 1C for order #%s (%d items, %.2f RUB)...",
            order_number, len(items), total_amount,
        )

        # Retry-логика уже встроена в self._request() (3 попытки с backoff для
        # сетевых ошибок и 5xx). Здесь обрабатываем только бизнес-коды ответа.
        try:
            response = await self._request(
                "POST",
                "Document_РеализацияТоваровУслуг",
                json_data=doc_payload,
                params={"$format": "json"},
            )

            if response.status_code in (200, 201):
                result_data = response.json()
                doc_id = result_data.get("Ref_Key", "")
                doc_number = result_data.get("Number", "")

                logger.info(
                    "✅ Sale document created in 1C: ID=%s, Number=%s "
                    "(order #%s)",
                    doc_id, doc_number, order_number,
                )

                return SyncResult(
                    success=True,
                    document_id=doc_id,
                    document_number=doc_number,
                    attempts=1,
                )

            elif response.status_code == 400:
                error_detail = response.text[:500]
                logger.error(
                    "1C rejected document (400): %s", error_detail
                )
                return SyncResult(
                    success=False,
                    error=f"1C validation error: {error_detail}",
                    attempts=1,
                )

            elif response.status_code == 401:
                logger.error("1C authentication failed (401)")
                return SyncResult(
                    success=False,
                    error="Authentication failed — check FRESH_USERNAME/FRESH_PASSWORD",
                    attempts=1,
                )

            elif response.status_code == 403:
                logger.error("1C access denied (403)")
                return SyncResult(
                    success=False,
                    error="Access denied — service user needs role УдаленныйДоступOData",
                    attempts=1,
                )

            else:
                error_text = f"Unexpected status {response.status_code}: {response.text[:200]}"
                logger.error("1C document creation failed: %s", error_text)
                return SyncResult(
                    success=False,
                    error=error_text,
                    attempts=1,
                )

        except Exception as exc:
            logger.error("1C sync failed: %s", exc)
            return SyncResult(
                success=False,
                error=f"Request failed: {exc}",
                attempts=1,
            )

    # ── Document status check ─────────────────────────────────────────

    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    async def get_document_status(self, document_id: str) -> dict[str, Any]:
        """Check if a document exists and is posted in 1C."""
        # Security: validate UUID format to prevent OData injection
        if not self._UUID_RE.match(document_id):
            logger.warning("Invalid document_id format (not UUID): %s", document_id[:50])
            return {"exists": None, "error": "Invalid document_id format"}

        try:
            response = await self._request(
                "GET",
                f"Document_РеализацияТоваровУслуг(guid'{document_id}')",
                params={
                    "$select": "Ref_Key,Number,Date,Posted,DeletionMark",
                    "$format": "json",
                },
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "exists": True,
                    "number": data.get("Number", ""),
                    "date": data.get("Date", ""),
                    "posted": data.get("Posted", False),
                    "deleted": data.get("DeletionMark", False),
                }
            elif response.status_code == 404:
                return {"exists": False}
            else:
                return {
                    "exists": None,
                    "error": f"Status {response.status_code}",
                }
        except Exception as exc:
            return {"exists": None, "error": str(exc)}

    # ── Health check ──────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Check connectivity to 1C:Fresh."""
        if not self.enabled:
            return {"status": "disabled", "enabled": False}

        try:
            response = await self._request(
                "GET",
                "$metadata",
                params={"$format": "json"},
            )
            return {
                "status": "ok" if response.status_code == 200 else "error",
                "enabled": True,
                "base_url": self.base_url,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {
                "status": "error",
                "enabled": True,
                "error": str(exc),
            }


# ── Global client instance ─────────────────────────────────────────────────

fresh_client = FreshODataClient()


# ── High-level helper ──────────────────────────────────────────────────────

async def sync_order_to_1c(
    order_id: int,
    order_number: str,
    items: list[dict[str, Any]],
    total_amount: float,
    payment_date: Optional[datetime] = None,
    client: Optional[FreshODataClient] = None,
) -> SyncResult:
    """
    Sync a paid order to 1С:Бухгалтерия.

    This is the main entry point called from _process_paid_order() in main.py.

    Args:
        order_id: Database order ID
        order_number: Public order number shown to customer
        items: List of order items with name, quantity, price, total
        total_amount: Total order amount in RUB
        payment_date: When payment was confirmed
        client: Optional client override (for testing)

    Returns:
        SyncResult

    Usage:
        result = await sync_order_to_1c(
            order_id=42,
            order_number="4648",
            items=[
                {"name": "Шашлык свиной", "quantity": 2, "price": 350, "total": 700},
                {"name": "Плов", "quantity": 1, "price": 280, "total": 280},
            ],
            total_amount=980.0,
        )
        if result.success:
            logger.info("Synced to 1C: doc %s", result.document_number)
    """
    _client = client or fresh_client

    if not _client.enabled:
        logger.debug(
            "1C sync skipped for order #%s (integration disabled)", order_number
        )
        return SyncResult(
            success=False,
            error="1C integration disabled",
            attempts=0,
        )

    logger.info("Syncing order #%s to 1C:Fresh...", order_number)

    try:
        result = await _client.create_sale_document(
            order_id=order_id,
            order_number=order_number,
            items=items,
            total_amount=total_amount,
            payment_date=payment_date,
        )

        if result.success:
            logger.info(
                "✅ Order #%s synced to 1C (doc: %s, attempts: %d)",
                order_number, result.document_number, result.attempts,
            )
        else:
            logger.error(
                "❌ Order #%s failed to sync to 1C: %s (attempts: %d)",
                order_number, result.error, result.attempts,
            )

        return result

    except Exception as exc:
        logger.error(
            "❌ Unexpected error syncing order #%s to 1C: %s",
            order_number, exc,
        )
        return SyncResult(
            success=False,
            error=f"Unexpected error: {exc}",
            attempts=0,
        )
