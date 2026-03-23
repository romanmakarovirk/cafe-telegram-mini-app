"""
Кухонный агент печати — скрипт для Windows.

Опрашивает бэкенд каждые 5 секунд, при получении нового заказа
автоматически печатает заказ-наряд на кухонный принтер (ESC/POS).

Установка на Windows:
1. Установить Python 3.10+
2. pip install requests python-escpos
3. Указать переменные окружения (или отредактировать конфиг ниже)
4. Добавить в автозапуск через Планировщик задач

Запуск:
  python kitchen_agent.py

Переменные окружения:
  KITCHEN_API_URL     — URL бэкенда (по умолчанию http://127.0.0.1:8000)
  KITCHEN_API_KEY     — ключ авторизации (совпадает с KITCHEN_API_KEY на сервере)
  KITCHEN_PRINTER     — имя принтера (Windows share name) или USB VID:PID
  KITCHEN_POLL_SEC    — интервал опроса в секундах (по умолчанию 5)
"""

import logging
import os
import sys
import time
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
#  Конфигурация
# ---------------------------------------------------------------------------
API_URL = os.getenv("KITCHEN_API_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.getenv("KITCHEN_API_KEY", "")
PRINTER_NAME = os.getenv("KITCHEN_PRINTER", "")  # Имя USB/Share принтера
POLL_INTERVAL = int(os.getenv("KITCHEN_POLL_SEC", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("kitchen_agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("kitchen_agent")


# ---------------------------------------------------------------------------
#  Форматирование заказа для печати
# ---------------------------------------------------------------------------
def format_order_text(order: dict) -> str:
    """Форматирует заказ в текст для печати (обычный текст, без ESC/POS)."""
    lines = []
    lines.append("=" * 32)
    lines.append(f"  ЗАКАЗ #{order['order_number']}")
    lines.append("=" * 32)
    lines.append("")

    # Время заказа
    try:
        dt = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        lines.append(f"Время: {dt.strftime('%H:%M  %d.%m.%Y')}")
    except Exception:
        lines.append(f"Время: {order.get('created_at', '???')}")

    lines.append(f"Сумма: {order['total']} руб.")
    lines.append("-" * 32)

    # Позиции заказа
    for item in order.get("items", []):
        name = item["name"]
        qty = item["quantity"]
        price = item.get("price", 0)
        if qty > 1:
            lines.append(f"{name}")
            lines.append(f"  x{qty}  ({price} руб./шт)")
        else:
            lines.append(f"{name}  ({price} руб.)")

    lines.append("-" * 32)
    lines.append(f"ИТОГО: {order['total']} руб.")
    lines.append("")
    lines.append("=" * 32)
    lines.append("")
    lines.append("")  # Отступ перед обрезкой

    return "\n".join(lines)


def format_order_escpos(order: dict) -> bytes:
    """
    Форматирует заказ в ESC/POS байты для термопринтера.
    Поддерживает кириллицу (кодовая страница CP866).
    """
    ESC = b"\x1b"
    GS = b"\x1d"

    cmds = bytearray()

    # Инициализация принтера
    cmds += ESC + b"@"                     # Reset
    cmds += ESC + b"t\x11"                 # Кодовая страница CP866 (кириллица)

    # --- Заголовок ---
    cmds += ESC + b"a\x01"                 # Выравнивание по центру
    cmds += ESC + b"!\x30"                 # Крупный шрифт (double width+height)
    cmds += f"ЗАКАЗ #{order['order_number']}\n".encode("cp866", errors="replace")
    cmds += ESC + b"!\x00"                 # Нормальный шрифт

    # Время
    try:
        dt = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M  %d.%m.%Y")
    except Exception:
        time_str = order.get("created_at", "???")
    cmds += f"{time_str}\n".encode("cp866", errors="replace")

    # Разделитель
    cmds += ESC + b"a\x00"                 # Выравнивание влево
    cmds += ("=" * 32 + "\n").encode("cp866")

    # --- Позиции ---
    cmds += ESC + b"!\x01"                 # Bold
    for item in order.get("items", []):
        name = item["name"]
        qty = item["quantity"]
        price = item.get("price", 0)
        if qty > 1:
            line = f"{name}\n  x{qty} ({price} р/шт)\n"
        else:
            line = f"{name}  ({price} р)\n"
        cmds += line.encode("cp866", errors="replace")
    cmds += ESC + b"!\x00"                 # Normal

    # --- Итого ---
    cmds += ("=" * 32 + "\n").encode("cp866")
    cmds += ESC + b"a\x01"                 # Центр
    cmds += ESC + b"!\x10"                 # Double height
    cmds += f"ИТОГО: {order['total']} руб.\n".encode("cp866", errors="replace")
    cmds += ESC + b"!\x00"                 # Normal
    cmds += ESC + b"a\x00"                 # Влево

    # Отступ + обрезка
    cmds += b"\n\n\n"
    cmds += GS + b"V\x42\x03"             # Частичная обрезка (3 строки отступ)

    return bytes(cmds)


# ---------------------------------------------------------------------------
#  Печать
# ---------------------------------------------------------------------------
def print_to_escpos_usb(data: bytes) -> bool:
    """Печать через USB (python-escpos)."""
    try:
        from escpos.printer import Usb
        # Типичные VID:PID для термопринтеров АТОЛ/чековых
        # Нужно указать реальные значения для конкретного принтера
        vid_pid = PRINTER_NAME.split(":")
        if len(vid_pid) == 2:
            vid = int(vid_pid[0], 16)
            pid = int(vid_pid[1], 16)
            p = Usb(vid, pid)
            p._raw(data)
            p.close()
            return True
    except ImportError:
        logger.warning("python-escpos не установлен, используем текстовую печать")
    except Exception as e:
        logger.error("Ошибка USB-печати: %s", e)
    return False


def print_to_windows_printer(text: str) -> bool:
    """Печать через Windows API (win32print)."""
    try:
        import win32print
        import win32ui

        if PRINTER_NAME:
            printer_name = PRINTER_NAME
        else:
            printer_name = win32print.GetDefaultPrinter()

        hprinter = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(hprinter, 1, ("KitchenOrder", None, "RAW"))
            win32print.StartPagePrinter(hprinter)
            win32print.WritePrinter(hprinter, text.encode("cp866", errors="replace"))
            win32print.EndPagePrinter(hprinter)
            win32print.EndDocPrinter(hprinter)
            return True
        finally:
            win32print.ClosePrinter(hprinter)
    except ImportError:
        logger.warning("win32print не доступен (не Windows или не установлен pywin32)")
    except Exception as e:
        logger.error("Ошибка Windows-печати: %s", e)
    return False


def print_order(order: dict) -> bool:
    """
    Печатает заказ на кухонный принтер.
    Пробует ESC/POS USB → Windows RAW → fallback в консоль.
    """
    order_num = order.get("order_number", "???")

    # Попытка 1: ESC/POS USB
    if PRINTER_NAME and ":" in PRINTER_NAME:
        escpos_data = format_order_escpos(order)
        if print_to_escpos_usb(escpos_data):
            logger.info("Заказ #%s напечатан (ESC/POS USB)", order_num)
            return True

    # Попытка 2: Windows принтер (RAW)
    text = format_order_text(order)
    if print_to_windows_printer(text):
        logger.info("Заказ #%s напечатан (Windows RAW)", order_num)
        return True

    # Fallback: вывод в консоль (НЕ считаем напечатанным — принтер не работает)
    logger.error("Принтер недоступен! Заказ #%s НЕ напечатан. Вывод в консоль:", order_num)
    print(text)
    return False


# ---------------------------------------------------------------------------
#  API: получение заказов и подтверждение печати
# ---------------------------------------------------------------------------
def get_pending_orders() -> list[dict]:
    """Получает заказы, ожидающие печати."""
    headers = {}
    if API_KEY:
        headers["X-Kitchen-Key"] = API_KEY

    try:
        resp = requests.get(
            f"{API_URL}/api/kitchen/pending",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("orders", [])
    except requests.RequestException as e:
        logger.error("Ошибка получения заказов: %s", e)
        return []


def mark_as_printed(order_id: int) -> bool:
    """Подтверждает серверу, что заказ напечатан."""
    headers = {}
    if API_KEY:
        headers["X-Kitchen-Key"] = API_KEY

    try:
        resp = requests.post(
            f"{API_URL}/api/kitchen/printed/{order_id}",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Ошибка подтверждения печати заказа %d: %s", order_id, e)
        return False


# ---------------------------------------------------------------------------
#  Основной цикл
# ---------------------------------------------------------------------------
def main():
    logger.info("=" * 50)
    logger.info("Кухонный агент печати запущен")
    logger.info("API: %s", API_URL)
    logger.info("Принтер: %s", PRINTER_NAME or "(по умолчанию / консоль)")
    logger.info("Интервал опроса: %d сек", POLL_INTERVAL)
    logger.info("=" * 50)

    while True:
        try:
            orders = get_pending_orders()

            if orders:
                logger.info("Получено заказов для печати: %d", len(orders))

            for order in orders:
                order_id = order.get("order_id")
                order_num = order.get("order_number", "???")

                logger.info("Печатаю заказ #%s (id=%s)...", order_num, order_id)

                if print_order(order):
                    if mark_as_printed(order_id):
                        logger.info("Заказ #%s — печать подтверждена", order_num)
                    else:
                        logger.warning("Заказ #%s — напечатан, но не подтверждён", order_num)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Агент остановлен пользователем.")
            break
        except Exception:
            logger.exception("Непредвиденная ошибка в основном цикле")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
