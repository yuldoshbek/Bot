"""Шаблоны поручений: типовая задача в одно нажатие.

Долг блока 2 — таблица `task_templates` создана с самого начала, интерфейса
не было. Функция 09 первой волны из раздела 15 архитектуры.

Три правила, из которых складывается всё остальное.

**Срок считается от дня применения.** Шаблон хранит не дату, а «через N дней»:
дата, записанная в шаблон однажды, через месяц превратила бы каждое поручение
в просроченное с рождения. Отсчёт идёт тем же разбором, что понимает «через
3 дня», набранное руками, — иначе два способа задать один срок дали бы разные
даты, и объяснить разницу было бы нечем.

**Применение проходит те же проверки, что обычное создание.** Шаблон — это
заготовка текста, а не обход прав. Право `task.create` есть и у рядового
сотрудника, с областью «только свои»; шаблон с чужим исполнителем по умолчанию
ему не поможет.

**Каталог общий, правка — нет.** Видеть шаблоны организации полезно всем: в этом
и смысл типовой задачи. Менять и удалять может тот, кто завёл, либо
администратор — иначе через месяц каталог перепишет кто угодно.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import parse_due
from app.core.timeutil import utcnow
from app.models import (
    Priority,
    RoleCode,
    Task,
    TaskStatus,
    TaskTemplate,
    User,
)
from app.services import tasks as task_service
from app.services.audit import write_audit
from app.services.rbac import Grant, has_permission, user_role_codes

# Сколько шаблонов держим в каталоге организации. Больше — это уже не «типовые
# задачи», а второй список поручений, в котором никто не разберётся.
MAX_TEMPLATES = 30
# Сколько кнопок показываем на экране создания поручения.
QUICK_BUTTONS = 5
# Предел «через N дней». Шаблон со сроком в полгода — не типовая задача.
MAX_DAYS = 180
# Окно, внутри которого повтор считается двойным нажатием, а не вторым
# намерением. Десять секунд мало (человек успевает передумать и нажать снова),
# сутки много (еженедельная задача склеилась бы с прошлой).
DOUBLE_TAP = timedelta(minutes=10)


@dataclass(slots=True)
class Outcome:
    """Исход действия: запись или причина отказа, читаемая человеком.

    `duplicate` означает, что запись уже была и второй не появилось. Это не
    отказ: человек получает ровно то, чего добивался, — просто узнаёт, что
    добился этого предыдущим нажатием.
    """

    item: TaskTemplate | Task | None = None
    reason: str | None = None
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.item is not None


def due_for(
    template: TaskTemplate, *, timezone_name: str, now: datetime | None = None
) -> datetime | None:
    """Срок поручения из шаблона — от дня применения, а не от дня создания.

    Считается тем же разбором, что и срок, набранный руками: «через 3 дня»
    из шаблона и «через 3 дня» из переписки обязаны дать одну и ту же дату.
    """
    if template.default_days <= 0:
        return None
    return parse_due(
        f"через {template.default_days} дней", timezone_name, now=now or utcnow()
    )


async def catalogue(
    session: AsyncSession, *, organization_id: int, limit: int = MAX_TEMPLATES
) -> list[TaskTemplate]:
    """Шаблоны организации. Каталог общий: типовая задача на то и типовая."""
    return list(
        (
            await session.execute(
                select(TaskTemplate)
                .where(TaskTemplate.organization_id == organization_id)
                .order_by(TaskTemplate.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def may_edit(
    session: AsyncSession, *, template: TaskTemplate, actor: User
) -> bool:
    """Менять и удалять может автор шаблона либо администратор."""
    if actor.organization_id != template.organization_id:
        return False
    if template.created_by == actor.id:
        return True
    return RoleCode.ADMIN in await user_role_codes(session, actor)


async def create(
    session: AsyncSession,
    *,
    actor: User,
    grants: dict[str, Grant],
    title: str,
    description: str | None = None,
    priority: str = Priority.NORMAL,
    days: int = 3,
    assignee: User | None = None,
    requires_review: bool | None = None,
) -> Outcome:
    """Заводит шаблон. Заводить может тот, кто вправе создавать поручения."""
    title = (title or "").strip()
    if len(title) < 3:
        return Outcome(reason="Нужно название шаблона — хотя бы три знака.")
    if not has_permission(grants, "task.create"):
        return Outcome(reason="Заводить шаблоны может тот, кто ставит поручения.")

    if assignee is not None and not await task_service.may_assign_to(
        session, actor=actor, grants=grants, assignee=assignee
    ):
        return Outcome(reason="Этому сотруднику вы поручать не можете.")

    # Десять нажатий «сохранить как шаблон» — это одно намерение, а не десять.
    # Каталог из десяти одинаковых «Еженедельных отчётов» бесполезен, поэтому
    # повтор возвращает уже заведённый шаблон, а не заводит второй.
    same = (
        await session.execute(
            select(TaskTemplate).where(
                TaskTemplate.organization_id == actor.organization_id,
                func.lower(TaskTemplate.title) == title.lower(),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if same is not None:
        return Outcome(item=same, duplicate=True)

    count = await session.scalar(
        select(func.count(TaskTemplate.id)).where(
            TaskTemplate.organization_id == actor.organization_id
        )
    )
    if int(count or 0) >= MAX_TEMPLATES:
        return Outcome(
            reason=f"Шаблонов уже {MAX_TEMPLATES}. Удалите ненужные, прежде чем заводить новый."
        )

    template = TaskTemplate(
        organization_id=actor.organization_id,
        title=title[:300],
        description=(description or "").strip() or None,
        default_priority=priority,
        default_assignee_id=assignee.id if assignee else None,
        default_days=max(0, min(int(days), MAX_DAYS)),
        requires_review=(
            task_service.default_requires_review(priority)
            if requires_review is None
            else requires_review
        ),
        created_by=actor.id,
    )
    session.add(template)
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="template.create",
        entity_type="task_template", entity_id=template.id,
        after={"title": template.title, "days": template.default_days},
    )
    return Outcome(item=template)


async def from_task(
    session: AsyncSession, *, task: Task, actor: User, grants: dict[str, Grant]
) -> Outcome:
    """Заводит шаблон по готовому поручению.

    Самый честный способ завести шаблон: у поручения уже есть и формулировка,
    и приоритет, и срок — переспрашивать всё это мастером значит заставить
    человека ввести дважды то, что система знает.
    """
    if task.organization_id != actor.organization_id:
        return Outcome(reason="Поручение другой организации.")

    days = 3
    if task.due_at is not None:
        # Сколько дней давали на эту задачу — столько же даст и шаблон.
        days = max(0, (task.due_at - task.created_at).days)

    assignee = await session.get(User, task.assignee_id)
    return await create(
        session,
        actor=actor,
        grants=grants,
        title=task.title,
        description=task.description,
        priority=task.priority,
        days=days,
        assignee=assignee,
        requires_review=task.requires_review,
    )


async def remove(
    session: AsyncSession, *, template: TaskTemplate, actor: User
) -> str | None:
    """Удаляет шаблон. Возвращает причину отказа или None.

    Шаблон удаляется по-настоящему, в отличие от решения: он не история,
    а заготовка, и хранить отменённые заготовки незачем.
    """
    if not await may_edit(session, template=template, actor=actor):
        return "Удалить шаблон может тот, кто его завёл, или администратор."
    await write_audit(
        session, actor_id=actor.id, action="template.delete",
        entity_type="task_template", entity_id=template.id,
        before={"title": template.title},
    )
    await session.delete(template)
    await session.flush()
    return None


async def apply(
    session: AsyncSession,
    *,
    template: TaskTemplate,
    actor: User,
    grants: dict[str, Grant],
    assignee: User | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Создаёт поручение по шаблону.

    Возвращает поручение, неотличимое от созданного руками: тот же
    `create_task`, та же история, тот же журнал. Отдельной ветки создания здесь
    нет намеренно — две ветки разошлись бы на первой же правке жизненного цикла.
    """
    now = now or utcnow()
    if template.organization_id != actor.organization_id:
        return Outcome(reason="Шаблон другой организации.")
    if not has_permission(grants, "task.create"):
        return Outcome(reason="Создавать поручения вам нельзя.")

    target = assignee
    if target is None and template.default_assignee_id:
        target = await session.get(User, template.default_assignee_id)
    if target is None:
        return Outcome(reason="Не указано, кому поручить.")

    # Шаблон не обходит область права: исполнитель по умолчанию проверяется
    # так же, как выбранный вручную. Иначе шаблон, заведённый руководителем,
    # позволил бы сотруднику поручить задачу кому угодно.
    if not await task_service.may_assign_to(
        session, actor=actor, grants=grants, assignee=target
    ):
        return Outcome(reason="Этому сотруднику вы поручать не можете.")

    # Проверка «нет ли уже такого» и создание — два действия, и между ними
    # успевает вклиниться второе нажатие: каждая транзакция не видит чужую
    # незакоммиченную запись, обе не находят близнеца, обе создают поручение.
    # Под нагрузкой из десяти нажатий так проходило два.
    #
    # Заблокировать нечего: гонка идёт за отсутствие записи, а не за неё.
    # Ровно для этого случая в PostgreSQL есть рекомендательная блокировка —
    # замок на значении, которого ещё нет. Она держится до конца транзакции
    # и снимается сама при фиксации или откате, забыть её нельзя.
    #
    # Ключ собран из двух чисел, а не из хеша строки: хеш строк в Python
    # разный от запуска к запуску, и замок в двух процессах оказался бы разным.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": (template.id << 32) | (target.id & 0xFFFFFFFF)},
    )

    # Признак двойного нажатия — не только статус, но и свежесть: еженедельный
    # отчёт, выданный в прошлый понедельник и так и не принятый, не делает
    # сегодняшний отчёт дубликатом. Поэтому окно короткое — ровно на длину
    # замешательства, а не на неделю.
    twin = (
        await session.execute(
            select(Task).where(
                Task.organization_id == actor.organization_id,
                Task.creator_id == actor.id,
                Task.assignee_id == target.id,
                Task.title == template.title,
                Task.status == TaskStatus.NEW,
                Task.created_at > now - DOUBLE_TAP,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if twin is not None:
        return Outcome(item=twin, duplicate=True)

    task = await task_service.create_task(
        session,
        creator=actor,
        assignee=target,
        title=template.title,
        description=template.description,
        due_at=due_for(template, timezone_name=actor.timezone, now=now),
        priority=template.default_priority,
        requires_review=template.requires_review,
    )
    await write_audit(
        session, actor_id=actor.id, action="template.apply",
        entity_type="task", entity_id=task.id,
        after={"template_id": template.id, "assignee_id": target.id},
    )
    return Outcome(item=task)
