from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re as _re
import time as time_module
from collections import defaultdict
from typing import Optional
from urllib.parse import unquote

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from config import (
    AUTH_DATE_MAX_AGE_SECONDS,
    BOT_TOKEN,
    DEV_MODE,
    RATE_LIMIT_CALLBACK,
    RATE_LIMIT_GENERAL,
    RATE_LIMIT_ORDERS,
    RATE_LIMIT_REVIEWS,
    RATE_LIMIT_SBP_CHECK,
)

import json as json_module


def get_client_ip(request: Request) -> str:
    """Extract real client IP behind Render proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def verify_telegram_init_data(init_data: str, bot_token: str) -> tuple[dict | None, str]:
    """Verify Telegram WebApp initData signature."""
    parsed: dict[str, str] = {}
    for part in init_data.split("&"):
        key, _, value = part.partition("=")
        if key:
            parsed[key] = value

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None, "no_hash"

    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            age = time_module.time() - int(auth_date)
            if age > AUTH_DATE_MAX_AGE_SECONDS:
                return None, f"auth_date_expired(age={int(age)}s)"
        except (ValueError, TypeError):
            return None, "auth_date_invalid"

    data_check_string = "\n".join(
        f"{k}={unquote(v)}" for k, v in sorted(parsed.items())
    )

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        logging.warning(
            "InitData HMAC mismatch: keys=%s, hash_len=%d",
            sorted(parsed.keys()), len(received_hash),
        )
        return None, "hmac_mismatch"

    user_raw = parsed.get("user")
    if user_raw:
        try:
            return json_module.loads(unquote(user_raw)), ""
        except (json_module.JSONDecodeError, TypeError):
            return None, "user_json_invalid"
    return None, "no_user_field"


def get_verified_user_id(request: Request) -> int:
    """Extract verified Telegram user ID from request headers."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if init_data and BOT_TOKEN:
        user, reason = verify_telegram_init_data(init_data, BOT_TOKEN)
        if user and user.get("id"):
            return int(user["id"])
        logging.warning("Auth failed: reason=%s, initData_len=%d, path=%s",
                        reason, len(init_data), request.url.path)
        raise HTTPException(status_code=401, detail=f"Invalid Telegram authorization ({reason}).")

    if not init_data:
        logging.warning("Auth failed: no X-Telegram-Init-Data header, path=%s", request.url.path)
    if not BOT_TOKEN and DEV_MODE:
        user_id = request.query_params.get("dev_user_id")
        if user_id:
            try:
                return int(user_id)
            except (ValueError, TypeError):
                pass

    raise HTTPException(status_code=401, detail="Authorization required.")


def get_verified_user_info(request: Request) -> tuple[int, str]:
    """Extract verified Telegram user ID and first_name from request headers."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if init_data and BOT_TOKEN:
        user, reason = verify_telegram_init_data(init_data, BOT_TOKEN)
        if user and user.get("id"):
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")
            name = f"{first_name} {last_name}".strip() or f"User {user['id']}"
            name = _re.sub(r"<[^>]+>", "", name).replace("\x00", "").strip()[:200]
            if not name:
                name = f"User {user['id']}"
            return int(user["id"]), name
        logging.warning("Auth failed: reason=%s, initData_len=%d, path=%s",
                        reason, len(init_data), request.url.path)
        raise HTTPException(status_code=401, detail=f"Invalid Telegram authorization ({reason}).")

    if not BOT_TOKEN and DEV_MODE:
        user_id = request.query_params.get("dev_user_id")
        if user_id:
            try:
                return int(user_id), f"Dev User {user_id}"
            except (ValueError, TypeError):
                pass

    raise HTTPException(status_code=401, detail="Authorization required.")


class SimpleRateLimiter:
    """In-memory rate limiter with automatic cleanup to prevent memory leaks."""

    MAX_KEYS = 10_000

    def __init__(self, max_requests: int, window: int):
        self.max = max_requests
        self.window = window
        self.hits: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time_module.time()
        self._cleanup_interval = max(window * 2, 120)

    def _cleanup(self, now: float) -> None:
        to_delete = [
            key for key, timestamps in self.hits.items()
            if not timestamps or timestamps[-1] < now - self.window
        ]
        for key in to_delete:
            del self.hits[key]
        self._last_cleanup = now

    def check(self, key: str) -> bool:
        now = time_module.time()

        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup(now)

        if len(self.hits) > self.MAX_KEYS:
            self._cleanup(now)
            if len(self.hits) > self.MAX_KEYS and key not in self.hits:
                oldest_key = min(self.hits, key=lambda k: self.hits[k][-1] if self.hits[k] else 0)
                del self.hits[oldest_key]

        self.hits[key] = [t for t in self.hits[key] if now - t < self.window]
        if len(self.hits[key]) >= self.max:
            return False
        self.hits[key].append(now)
        return True


order_limiter = SimpleRateLimiter(max_requests=RATE_LIMIT_ORDERS, window=60)
review_limiter = SimpleRateLimiter(max_requests=RATE_LIMIT_REVIEWS, window=60)
general_limiter = SimpleRateLimiter(max_requests=RATE_LIMIT_GENERAL, window=60)
callback_limiter = SimpleRateLimiter(max_requests=RATE_LIMIT_CALLBACK, window=60)
sbp_check_limiter = SimpleRateLimiter(max_requests=RATE_LIMIT_SBP_CHECK, window=60)


def verify_kitchen_api_key(request: Request) -> None:
    """Verify X-Kitchen-Key header. Fail-closed."""
    kitchen_key = os.getenv("KITCHEN_API_KEY", "").strip()
    if not kitchen_key:
        raise HTTPException(
            status_code=403,
            detail="Kitchen API key not configured. Set KITCHEN_API_KEY env var.",
        )
    provided_key = request.headers.get("X-Kitchen-Key", "")
    if not provided_key or not hmac.compare_digest(provided_key, kitchen_key):
        raise HTTPException(status_code=403, detail="Invalid kitchen API key")


class CartItem(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=50)


class CreateOrderRequest(BaseModel):
    items: list[CartItem] = Field(min_length=1, max_length=20)
    comment: str = Field(default="", max_length=500)


class SubmitReviewRequest(BaseModel):
    order_id: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class StopListRequest(BaseModel):
    item_id: Optional[int] = None
    category: Optional[str] = None
    action: str = Field(pattern=r"^(disable|enable)$")
    reason: Optional[str] = Field(default=None, max_length=200)
    available_in_minutes: Optional[int] = Field(default=None, ge=5, le=480)
