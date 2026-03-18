# Проект: Кафе «Шашлык и Плов» — Telegram Mini App

## Build & Test
- **Тесты:** `cd "/Users/romanmakarov/Documents/Шашлык и плов/New project" && python3 -m pytest test_system.py -v --tb=short`
- **Запуск сервера:** `python main.py` (требует .env)
- **Генерация PDF:** `python generate_proposal.py`
- **Деплой:** push в GitHub → автодеплой на Render.com

## Обязательные правила
- После КАЖДОГО изменения кода — запускать pytest
- НИКОГДА не хардкодить API-ключи, токены, пароли в коде
- НЕ предлагать создать Telegram-бота — он уже создан и работает
- НЕ рефакторить на микросервисы — монолит осознанный выбор
- НЕ менять стабильный код без явной причины

## ⚠️ КРИТИЧЕСКИ ОПАСНЫЕ ДЕЙСТВИЯ — ТОЛЬКО ВРУЧНУЮ ЧЕЛОВЕКОМ

### ПЕРЕКЛЮЧЕНИЕ РЕЖИМОВ (TEST_MODE)
**НИКОГДА НЕ МЕНЯТЬ `SBP_TEST_MODE`, `ATOL_TEST_MODE`, `DEV_MODE`, `FRESH_ENABLED` в коде, .env, render.yaml, или где-либо ещё.**

Если задача требует переключения режима — НЕ ДЕЛАТЬ ЭТО САМОСТОЯТЕЛЬНО. Вместо этого:
1. НАПИСАТЬ ЧЕЛОВЕКУ ОТДЕЛЬНЫМ СООБЩЕНИЕМ КАПСОМ:
   ```
   ⚠️ ВНИМАНИЕ: ДЛЯ ЭТОЙ ЗАДАЧИ ТРЕБУЕТСЯ ПЕРЕКЛЮЧЕНИЕ [SBP_TEST_MODE/ATOL_TEST_MODE] НА RENDER.
   ЭТО РИСКОВАННОЕ ДЕЙСТВИЕ — ВКЛЮЧАЕТ РЕАЛЬНЫЕ ПЛАТЕЖИ/ФИСКАЛИЗАЦИЮ.
   ПЕРЕКЛЮЧЕНИЕ ДОЛЖНО БЫТЬ ВЫПОЛНЕНО ВРУЧНУЮ ЧЕЛОВЕКОМ В RENDER DASHBOARD.
   ```
2. Объяснить ЗАЧЕМ нужно переключение и ЧТО ПРОИЗОЙДЁТ после
3. Дождаться подтверждения от человека
4. После переключения — попросить человека сделать тестовый заказ и проверить чек в ОФД

### Безопасная работа с API — обязательные проверки
При ЛЮБОЙ работе с платёжным кодом (routes.py, payments/, bot_handlers.py, workers.py):
1. **Проверить TEST_MODE:** убедиться что код НЕ хардкодит `test_mode=True/False`, а берёт из `config.py`
2. **Проверить FOR UPDATE:** если есть `with_for_update()` — внутри блока НЕ ДОЛЖНО быть `await`, HTTP-вызовов, Telegram API. Паттерн: `читай+коммить → await снаружи → новая сессия`
3. **Проверить фискализацию:** любая операция с деньгами требует чек(и) по 54-ФЗ. См. memory/fiscal_rules.md
4. **Проверить payment_status:** допустимые переходы строго определены. См. memory/fiscal_rules.md
5. **Проверить идемпотентность:** retry/повтор НЕ должен создавать дубликаты (чеков, платежей, уведомлений)

## Структура проекта
```
New project/
├── main.py           # Монолит: модели, API, бот, workers (~3121 строк)
├── config.py         # Env-переменные, валидация конфига
├── database.py       # SQLAlchemy engine, sessions
├── routes.py         # HTTP endpoints, SBP callback, audit trail
├── bot_handlers.py   # Telegram bot commands, Phase 2 fiscal
├── workers.py        # Background: fiscal retry, keepalive, watchdog
├── payments/
│   ├── sbp.py        # СБП Сбербанк интеграция
│   └── fiscal.py     # АТОЛ Онлайн v4, двухфазная фискализация
├── index.html        # Frontend (Telegram Mini App)
├── test_system.py    # 229 тестов
├── generate_proposal.py  # PDF коммерческого предложения
└── photos/           # Фото блюд для меню
```

## Ключевые env-переменные
- `DEV_MODE` — true для разработки, false для прода
- `SBP_TEST_MODE` — true = песочница ecomtest.sberbank.ru
- `ATOL_TEST_MODE` — true = песочница testonline.atol.ru
- `FRESH_ENABLED` — true = включить синхронизацию с 1С

## Опасные паттерны — НЕ ПОВТОРЯТЬ
Эти ошибки находили многократно при аудите. Запомни их:

1. **async I/O внутри FOR UPDATE** — HTTP-вызовы, `await`, Telegram API внутри `with db_session()` + `with_for_update()` блокирует соединение БД и создаёт дедлоки. Правильный паттерн:
   ```python
   # ШАГ 1: читай данные + коммить внутри сессии
   with db_session() as session:
       order = session.scalars(select(Order).where(...).with_for_update()).first()
       data = order.total_amount  # сохрани нужные данные
       order.status = "processing"
       session.commit()
   # ШАГ 2: async вызов СНАРУЖИ
   result = await external_api_call(data)
   # ШАГ 3: сохрани результат в НОВОЙ сессии
   with db_session() as session:
       order = session.get(Order, order_id)
       order.external_id = result.id
       session.commit()
   ```

2. **Один чек вместо двух** — при возврате денег по 54-ФЗ нужно ДВА чека: чек продажи (prepayment) и чек возврата (sell_refund). При отмене оплаченного заказа — тоже два чека.

3. **Нестабильный external_id** — `external_id` в АТОЛ должен быть детерминированным (`order-{id}-prepay`, `order-{id}-refund`), НЕ содержать UUID. Иначе retry создаст дубликат чека.

4. **amount=0 и amount=None** — при проверке суммы всегда использовать `is not None`, не truthy проверку. `if amount:` пропустит `amount=0`.

## Текущий статус (18 марта 2026)
- 229 тестов green, задеплоено на Render
- 6 раундов аудита безопасности, 31 исправление
- Ждём встречу с клиентом для получения боевых доступов
