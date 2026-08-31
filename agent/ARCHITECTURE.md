# Архитектура для агента

Краткая техническая карта. Полный документ для людей — `Архитектура SETA.html`.

## Слои

```
Telegram (бот + Mini App)
      ↓
Caddy: HTTPS, секретный токен вебхука, лимиты частоты
      ↓
Core API (FastAPI) — модули: auth, users, departments, calendar,
      meetings, tasks, notifications, files, analytics, ai, admin, audit
      ↓
PostgreSQL 16 (источник истины)   Redis (очередь, блокировки, кэш, удержание слотов)
      ↓
Фоновые обработчики: планировщик, рассыльщик, ИИ, индексатор
      ↓
Наружу: Telegram Bot API, OpenAI API, резервные копии
```

**Правило вебхука.** Обработчик делает три вещи: сохраняет событие, ставит в
очередь, отвечает «принято». Ничего долгого внутри запроса — Telegram ждёт секунды.

**Идемпотентность.** У каждого события свой идентификатор. Повторная доставка от
Telegram или повторный запуск обработчика не создают второе уведомление.

## Структура кода

```
seta/
  app/
    core/       config.py, db.py, redis.py, timeutil.py
    models/     base, enums, org, user, rbac, schedule, audit
    services/   rbac, registration, availability, bootstrap, audit
    bot/        loader, run, middlewares/auth, handlers/*, keyboards/*
    api/        main.py: вебхук + /health
  migrations/   версии схемы (Alembic)
  scripts/      smoke_block1.py и служебные
```

## Модель данных

Реализовано (блок 1) — 16 таблиц:

| Таблица | Назначение |
|---|---|
| `organizations` | Организация. `organization_id` заложен везде, хотя она пока одна |
| `departments` | Дерево подразделений, начальник отдела |
| `users` | Сотрудник: связь Telegram ID ↔ корпоративная личность, отдел, статус, часовой пояс |
| `invites` | Приглашения: персональные одноразовые и многоразовые ссылки отделов |
| `roles`, `permissions`, `role_permissions`, `user_roles` | Роли, права и области видимости |
| `delegations` | Передача прав ассистенту на срок |
| `working_hours` | Рабочие часы по дням недели, обед, буфер, разрешение поздних встреч |
| `availability_states` | Текущий индикатор доступности (одна строка на человека) |
| `availability_log` | История переключений — из неё считается реальная доступность |
| `calendar_blocks` | Личные блокировки «не занимать» |
| `absences` | Отпуска, командировки, больничные + замещающий |
| `holidays` | Праздники и перенесённые рабочие дни |
| `audit_log` | Журнал: только добавление, поля «было/стало», настоящий автор |

Запланировано (блоки 2–6): `tasks`, `task_events`, `task_comments`, `task_extensions`,
`task_dependencies`, `task_templates`, `recurring_tasks`, `meetings`,
`meeting_participants`, `meeting_attendance`, `meeting_ratings`, `meeting_requests`,
`slot_holds`, `rooms`, `room_bookings`, `decisions`, `files`, `file_texts`,
`time_quotas`, `notifications`, `notification_templates`, `notification_prefs`,
`approvals`, `polls`, `poll_votes`, `ai_jobs`, `ai_budget`, `feature_flags`,
`ui_config`, `search_index`, `feedback`.

**Правила модели.** Все временные метки — `timestamptz` в UTC, показ в часовом
поясе пользователя. `telegram_user_id` — 64-битное целое. Значения перечислений
хранятся строками (читаемо в базе и в журнале).

## Права

Четыре уровня, работают одновременно:

1. **Роль** — кто ты. Шесть: `EXECUTIVE`, `ASSISTANT`, `DEPT_HEAD`, `EMPLOYEE`, `ADMIN`, `AUDITOR`.
2. **Право** — что тебе разрешено. 28 штук, каталог в `app/services/rbac.py::PERMISSIONS`.
3. **Область** — над какими записями: `SELF` → `SUBORDINATES` → `DEPARTMENT` → `ORGANIZATION`.
4. **Проверка записи** — `can_access_object()`: я автор? исполнитель? проверяющий? мой отдел?

Матрица ролей — `ROLE_MATRIX` в том же файле. Права загружаются функцией
`load_grants()`: собственные роли **плюс** действующие делегирования.

Повышенные роли перечислены в `ELEVATED_ROLES` — самостоятельно не выдаются.

## Индикатор доступности

Состояния: `OPEN` (принимаю), `BUSY` (занят), `DND` (не беспокоить),
`OFFLINE` (не выставлен, работают обычные правила календаря).

- У состояния всегда есть `until_at`; истёкшее показывается как `OFFLINE`
  без фонового задания — проверка происходит при чтении (`get_view`).
- Флаг `opens_late_slots` открывает окна после конца рабочего дня («поздний приём»).
- `visible_to_all` управляет тем, видят ли индикатор все сотрудники.
- Каждое переключение пишется в `availability_log` и в журнал аудита.

Рабочий день по умолчанию: **09:00–19:00**, обед 13:00–14:00, буфер 15 минут,
предел позднего приёма 22:00, суббота и воскресенье нерабочие.

## Порты

Стандартные порты на машине владельца заняты другими проектами, поэтому:
Postgres — `55432`, Redis — `56379`, API — `8010` (только на `127.0.0.1`).

## Что ещё не построено

Блоки 2–6 по плану: поручения и контроль → календарь и встречи →
встреча/решения/документы → Mini App и дашборды → ИИ и вторая волна функций.
Состав каждого блока и критерии готовности — в `Архитектура SETA.html`, раздел 17.
