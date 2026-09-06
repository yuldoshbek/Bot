"""Маршруты чтения для Mini App.

**Ни один маршрут не решает, что человеку видно.** Это решают те же службы,
что отвечают боту: `tasks.my_tasks`, `meetings.by_participant`,
`decisions.registry`, `dashboard.build`. Приложение и бот на одних данных
обязаны показывать одно и то же, и единственный способ этого добиться —
звать один и тот же код, а не писать похожий.

Поэтому здесь нет ни одного `select` с условиями доступа. Появившийся здесь
запрос с `where` по правам — это второе описание прав, которое разойдётся
с первым.

**Ответ не богаче того, что человек видит в боте.** Поля выбираются поимённо,
а не отдаётся модель целиком: `model_dump()` вынес бы наружу и внутренние
отметки, и чужие идентификаторы, и то, что появится в таблице завтра.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import Caller, caller
from app.core.dates import humanize_due
from app.core.i18n import LOCALES, t
from app.core.timeutil import utcnow
from app.models.task import Task
from app.services import dashboard
from app.services import decisions as decision_service
from app.services import features as feature_service
from app.services import meetings as meeting_service
from app.services import tasks as task_service
from app.services.rbac import role_titles
from app.services.tasks import priority_title, status_title

router = APIRouter()

# Сколько записей отдаётся за раз. Совпадает с тем, что показывает бот:
# приложение — другой вид на те же данные, а не другой объём прав.
PAGE = 30
# Горизонт календаря по умолчанию — две недели, как в «Моих встречах».
CALENDAR_DAYS = 14


def _need(call: Caller, code: str) -> None:
    """Раздел выключен администратором — API молчит так же, как бот."""
    if not feature_service.is_on(call.features, code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "раздел выключен")


def _task_json(task: Task, call: Caller) -> dict:
    """Поручение для приложения. Ровно те поля, что видны в карточке бота."""
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "status_title": status_title(task.status, call.locale),
        "priority": task.priority,
        "priority_title": priority_title(task.priority, call.locale),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_human": (
            humanize_due(task.due_at, call.user.timezone, call.locale)
            if task.due_at else None
        ),
        "requires_review": task.requires_review,
        "personal_control": task.personal_control,
        "rework_count": task.rework_count,
    }


@router.get("/me")
async def me(call: Caller = Depends(caller)) -> dict:
    """Кто открыл приложение. Первый запрос клиента — и проверка входа заодно."""
    return {
        "id": call.user.id,
        "full_name": call.user.full_name,
        "roles": sorted(role.value for role in call.roles),
        "roles_title": role_titles(call.roles, call.locale),
        "timezone": call.user.timezone,
        "locale": call.locale,
        "locales": [{"code": code, "title": title} for code, title in LOCALES.items()],
        "organization": call.organization.name,
        # Приложение рисует только включённые разделы — тот же список,
        # по которому бот собирает нижнее меню.
        "features": call.features,
    }


@router.get("/day")
async def day(call: Caller = Depends(caller)) -> dict:
    """Экран «Мой день» — тот же `Board`, что уходит в бот и в утреннюю сводку."""
    board = await dashboard.build(
        call.session, viewer=call.user, grants=call.grants, features=call.features
    )
    return {
        "date": board.day.date().isoformat(),
        "quiet": board.quiet,
        "running": [
            {"id": m.id, "title": m.title, "end_at": m.end_at.isoformat()}
            for m in board.running
        ],
        "ahead": [
            {"id": m.id, "title": m.title, "start_at": m.start_at.isoformat()}
            for m in board.ahead
        ],
        "free_from": board.free_slot.start.isoformat() if board.free_slot else None,
        "requests_waiting": board.requests_waiting,
        "requests_over_quota": board.requests_over_quota,
        "to_review": board.to_review,
        "stale_decisions": board.stale_decisions,
        "overdue_total": board.overdue_total,
        "overdue_by_department": [
            # None вместо названия — поручение без отдела. Подпись ставит клиент,
            # как её ставит отрисовка в боте.
            {"department": name, "count": count}
            for name, count in board.overdue_by_department
        ],
        "overdue_other": board.overdue_other,
        "personal_overdue": [_task_json(task, call) for task in board.personal_overdue],
        "metrics": [
            {
                "key": metric.key,
                "title": metric.title(call.locale),
                "value": metric.value,
                "detail": metric.detail(call.locale),
                "no_data": metric.no_data,
                "text": metric.render(call.locale),
            }
            for metric in board.metrics
        ],
    }


@router.get("/tasks")
async def tasks(
    bucket: str = Query(default="active"),
    call: Caller = Depends(caller),
) -> dict:
    """Поручения в том же разрезе, что и кнопки списка в боте."""
    if bucket not in task_service.BUCKETS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "неизвестный разрез")
    items = await task_service.my_tasks(
        call.session, call.user, bucket=bucket, limit=PAGE
    )
    return {"bucket": bucket, "items": [_task_json(task, call) for task in items]}


@router.get("/tasks/{task_id}")
async def task_card(task_id: int, call: Caller = Depends(caller)) -> dict:
    """Карточка поручения. Доступ спрашивается у той же `access_for`, что в боте."""
    task = await call.session.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "не найдено")

    access = await task_service.access_for(call.session, task, call.user, call.grants)
    if not access.can_view:
        # Тот же ответ, что и на несуществующее: иначе по разнице ответов
        # перебором составляется список чужих поручений.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "не найдено")

    card = _task_json(task, call)
    card["description"] = task.description
    # Что человек может сделать — решает служба, а не клиент. Приложение рисует
    # кнопки по этому списку и не гадает по ролям.
    card["can"] = {
        "accept": access.can_accept,
        "start": access.can_start,
        "submit": access.can_submit,
        "review": access.can_review,
        "cancel": access.can_cancel,
        "comment": access.can_comment,
        "request_extension": access.can_request_extension,
        "decide_extension": access.can_decide_extension,
    }
    return card


@router.get("/meetings")
async def meetings(
    days: int = Query(default=CALENDAR_DAYS, ge=1, le=60),
    call: Caller = Depends(caller),
) -> dict:
    """Встречи человека за период — той же выборкой, что «Мои встречи»."""
    _need(call, "meetings")
    now = utcnow()
    items = await meeting_service.by_participant(
        call.session, user=call.user, since=now, until=now + timedelta(days=days)
    )
    return {
        "days": days,
        "items": [
            {
                "id": m.id,
                "title": m.title,
                "start_at": m.start_at.isoformat(),
                "end_at": m.end_at.isoformat(),
                "status": m.status,
            }
            for m in items
        ],
    }


@router.get("/decisions")
async def decisions(
    only_open: bool = Query(default=True),
    call: Caller = Depends(caller),
) -> dict:
    """Реестр решений — тем же `registry`, с теми же условиями видимости."""
    items = await decision_service.registry(
        call.session, user=call.user, grants=call.grants,
        only_open=only_open, limit=PAGE,
    )
    return {
        "items": [
            {
                "id": d.id,
                "title": d.title,
                "status": d.status,
                "status_title": t(decision_service.STATUS_KEYS[d.status], call.locale),
                "due_date": d.due_date.isoformat() if d.due_date else None,
                "responsible_id": d.responsible_id,
            }
            for d in items
        ]
    }
