from __future__ import annotations

import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


# ── Structured Logging ────────────────────────────────────────────────────
def _setup_logging() -> None:
    """JSON-логирование для прода, текстовое для локальной разработки."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    try:
        from pythonjsonlogger.json import JsonFormatter
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    except ImportError:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    root.handlers = [handler]

_setup_logging()

# ── Sentry Error Tracking ─────────────────────────────────────────────────
_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        )
        logging.info("Sentry initialized (traces_sample_rate=0.1)")
    except ImportError:
        logging.warning("sentry-sdk not installed, Sentry disabled")

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", APP_BASE_URL).rstrip("/")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}").strip()
INITIAL_ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0) or None

# ── Security settings ─────────────────────────────────────────────────────
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")


def _safe_int(env_name: str, default: int, min_val: int = 1) -> int:
    """Parse int from env var with fallback to default on error or out-of-range."""
    raw = os.getenv(env_name, str(default))
    try:
        val = int(raw)
        if val < min_val:
            logging.warning("%s=%d is below minimum %d, using default %d", env_name, val, min_val, default)
            return default
        return val
    except (ValueError, TypeError):
        logging.warning("%s=%r is not a valid integer, using default %d", env_name, raw, default)
        return default

ALLOWED_ADMIN_IDS: set[int] = set()
_raw_admin_ids = os.getenv("ALLOWED_ADMIN_IDS", "").strip()
if _raw_admin_ids:
    for _aid in _raw_admin_ids.split(","):
        _aid = _aid.strip()
        if _aid:
            with suppress(ValueError):
                ALLOWED_ADMIN_IDS.add(int(_aid))
if INITIAL_ADMIN_CHAT_ID:
    ALLOWED_ADMIN_IDS.add(INITIAL_ADMIN_CHAT_ID)

MAX_ITEMS_PER_ORDER = _safe_int("MAX_ITEMS_PER_ORDER", 100)
MAX_ORDER_TOTAL_RUB = _safe_int("MAX_ORDER_TOTAL_RUB", 50000)
ORDER_PAYMENT_TIMEOUT_MINUTES = _safe_int("ORDER_PAYMENT_TIMEOUT_MINUTES", 15)
KITCHEN_API_KEY = os.getenv("KITCHEN_API_KEY", "").strip()
DEFAULT_PREP_TIME_MINUTES = _safe_int("DEFAULT_PREP_TIME_MINUTES", 20)

# ── Named Constants (вместо magic numbers) ────────────────────────────────
AUTH_DATE_MAX_AGE_SECONDS = 86400          # Срок жизни Telegram initData (24ч)
RATE_LIMIT_ORDERS = 10                     # Заказов в минуту на пользователя
RATE_LIMIT_REVIEWS = 5                     # Отзывов в минуту
RATE_LIMIT_GENERAL = 60                    # Общих запросов в минуту
RATE_LIMIT_CALLBACK = 30                   # SBP callback в минуту на IP
RATE_LIMIT_SBP_CHECK = 20                  # Проверок статуса оплаты в минуту
FISCAL_RETRY_BATCH_SIZE = 5                # Записей за цикл fiscal retry worker
FISCAL_INITIAL_DELAY_SECONDS = 120         # Задержка старта fiscal worker
KEEPALIVE_INTERVAL_SECONDS = 12 * 60       # Пинг для Render (12 мин, запас 3 мин до spin-down)
KEEPALIVE_STARTUP_DELAY_SECONDS = 60       # Задержка старта keep-alive


def validate_production_config() -> None:
    """Проверка обязательных секретов при старте."""
    required_in_prod = {
        "BOT_TOKEN": BOT_TOKEN,
        "KITCHEN_API_KEY": KITCHEN_API_KEY,
    }
    sbp_configured = bool(os.getenv("SBP_USERNAME") or os.getenv("SBP_TOKEN"))
    if sbp_configured:
        required_in_prod["SBP_CALLBACK_SECRET"] = os.getenv("SBP_CALLBACK_SECRET", "")
        if os.getenv("SBP_USERNAME") and not os.getenv("SBP_TOKEN"):
            required_in_prod["SBP_PASSWORD"] = os.getenv("SBP_PASSWORD", "")

    atol_configured = bool(os.getenv("ATOL_LOGIN"))
    if atol_configured:
        required_in_prod["ATOL_INN"] = os.getenv("ATOL_INN", "")
        required_in_prod["ATOL_PASSWORD"] = os.getenv("ATOL_PASSWORD", "")
        required_in_prod["ATOL_GROUP_CODE"] = os.getenv("ATOL_GROUP_CODE", "")

    missing = [name for name, val in required_in_prod.items() if not val]

    if not ALLOWED_ADMIN_IDS:
        missing.append("ALLOWED_ADMIN_IDS")

    if missing:
        msg = f"Missing required config: {', '.join(missing)}"
        if not DEV_MODE:
            logging.critical("🚫 PRODUCTION STARTUP BLOCKED: %s", msg)
            logging.critical("Set these in Render Dashboard → Environment Variables")
            raise SystemExit(1)
        else:
            logging.warning("⚠️  DEV MODE: %s (OK for development)", msg)

    # Лог активных режимов — видно сразу при старте какой режим включён
    sbp_test = os.getenv("SBP_TEST_MODE", "true").lower() in ("true", "1", "yes")
    atol_test = os.getenv("ATOL_TEST_MODE", "true").lower() in ("true", "1", "yes")
    logging.info("Payment modes: SBP_TEST_MODE=%s, ATOL_TEST_MODE=%s", sbp_test, atol_test)
    if not DEV_MODE and (sbp_test or atol_test):
        logging.warning("⚠️  SANDBOX MODE ACTIVE in production — переключите TEST_MODE на Render")


def normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://") and "+psycopg" not in raw_url:
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


SQLALCHEMY_DATABASE_URL = normalize_database_url(DATABASE_URL)
ENGINE_KWARGS: dict[str, Any] = {"future": True, "pool_pre_ping": True}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    ENGINE_KWARGS["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL connection pool settings for production
    ENGINE_KWARGS["pool_size"] = _safe_int("DB_POOL_SIZE", 5)
    ENGINE_KWARGS["max_overflow"] = _safe_int("DB_MAX_OVERFLOW", 10, min_val=0)
    ENGINE_KWARGS["pool_timeout"] = _safe_int("DB_POOL_TIMEOUT", 30)
    ENGINE_KWARGS["pool_recycle"] = _safe_int("DB_POOL_RECYCLE", 1800)
