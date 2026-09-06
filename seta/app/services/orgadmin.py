"""Управление организацией без программиста.

До этой фазы рабочие часы, квоты, праздники и отпуска правил разработчик — что
прямо противоречит разделу «Администрирование» архитектуры, где обещано
управление без программирования.

Три правила, общие для всех действий здесь.

**Каждое изменение — в журнал с «было/стало».** Настройки меняют то, как система
считает свободные окна и сроки. Через месяц вопрос «почему у него рабочий день
до восьми» должен иметь ответ с датой и именем, а не догадку.

**Настройка действует сразу.** Рабочие часы читаются расчётом окон при каждом
обращении, а не кэшируются: администратор изменил — сотрудник в следующую
секунду видит другой список окон. Кэш здесь означал бы, что человек правит
настройку, смотрит на результат и не понимает, почему ничего не изменилось.

**Администратор управляет системой, а не читает переписку.** Он видит и меняет
параметры, но содержание встреч и поручений ему по-прежнему закрыто — решение
Р-07. Ни одна функция здесь не отдаёт ни названия поручения, ни темы встречи.
"""
from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import t
from app.models import (
    Absence,
    AbsenceKind,
    Department,
    Holiday,
    QuotaPeriod,
    TimeQuota,
    User,
    UserStatus,
    WorkingHours,
)
from app.services.audit import write_audit
from app.services.rbac import Grant, has_permission

# Предел, за которым рабочий день перестаёт быть рабочим днём.
MIN_BUFFER, MAX_BUFFER = 0, 120
MIN_CONSECUTIVE, MAX_CONSECUTIVE = 1, 12
# Сколько минут в неделю можно отдать одному человеку или отделу.
MAX_QUOTA_MINUTES = 60 * 40
# Насколько вперёд разрешено заводить праздники и отпуска.
MAX_HORIZON_DAYS = 366 * 2

# Ключи, а не названия: день недели и вид отсутствия человек видит на своём языке.
WEEKDAY_KEYS = tuple(f"weekday.{i}" for i in range(7))
ABSENCE_KEYS = {
    AbsenceKind.VACATION: "absence.vacation",
    AbsenceKind.TRIP: "absence.trip",
    AbsenceKind.SICK: "absence.sick",
    AbsenceKind.OTHER: "absence.other",
}


@dataclass(slots=True)
class Outcome:
    """Исход настройки: что получилось или почему нет."""

    item: object | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.item is not None


def _allowed(grants: dict[str, Grant]) -> bool:
    return has_permission(grants, "admin.settings")


# ── Рабочие часы ────────────────────────────────────────────────────────────
async def hours_of(session: AsyncSession, user: User) -> list[WorkingHours]:
    """Неделя человека по дням. Порядок — от понедельника."""
    return list(
        (
            await session.execute(
                select(WorkingHours)
                .where(WorkingHours.user_id == user.id)
                .order_by(WorkingHours.weekday)
            )
        ).scalars().all()
    )


async def set_hours(
    session: AsyncSession,
    *,
    actor: User,
    grants: dict[str, Grant],
    subject: User,
    weekday: int | None = None,
    start: time | None = None,
    end: time | None = None,
    lunch_start: time | None = None,
    lunch_end: time | None = None,
    is_working: bool | None = None,
    buffer_minutes: int | None = None,
    max_consecutive: int | None = None,
) -> Outcome:
    """Меняет рабочие часы. `weekday=None` — сразу на все рабочие дни недели.

    Меняются только переданные поля: администратор правит буфер, не трогая
    обед, и наоборот. Отсутствие поля означает «оставить как было», а не
    «сбросить» — иначе каждая правка требовала бы ввести всё заново.
    """
    if not _allowed(grants):
        return Outcome(reason=t("orgadmin.err.rights", actor.locale))
    if subject.organization_id != actor.organization_id:
        return Outcome(reason=t("orgadmin.err.other_org_user", actor.locale))
    if start is not None and end is not None and end <= start:
        return Outcome(reason=t("orgadmin.err.end_before_start", actor.locale))
    if lunch_start is not None and lunch_end is not None and lunch_end <= lunch_start:
        return Outcome(reason=t("orgadmin.err.lunch_end", actor.locale))
    if buffer_minutes is not None and not MIN_BUFFER <= buffer_minutes <= MAX_BUFFER:
        return Outcome(reason=t("orgadmin.err.buffer_range", actor.locale,
                            min=MIN_BUFFER, max=MAX_BUFFER))
    if max_consecutive is not None and not MIN_CONSECUTIVE <= max_consecutive <= MAX_CONSECUTIVE:
        return Outcome(
            reason=t("orgadmin.err.row_range", actor.locale,
                     min=MIN_CONSECUTIVE, max=MAX_CONSECUTIVE)
        )

    rows = await hours_of(session, subject)
    if weekday is not None:
        rows = [row for row in rows if row.weekday == weekday]
    if not rows:
        return Outcome(reason=t("orgadmin.err.no_schedule", actor.locale))

    before = [_hours_snapshot(row) for row in rows]

    # Сначала считаем, что получится, и проверяем; меняем — только потом.
    # Откат посреди цикла сбросил бы всю транзакцию вместе с чужой работой,
    # а половина изменённых дней осталась бы в памяти сессии.
    planned = []
    for row in rows:
        new_start = start if start is not None else row.start_time
        new_end = end if end is not None else row.end_time
        new_lunch_start = lunch_start if lunch_start is not None else row.lunch_start
        new_lunch_end = lunch_end if lunch_end is not None else row.lunch_end
        if new_end <= new_start:
            return Outcome(reason=t(
                "orgadmin.err.day_end_before", actor.locale,
                day=t(WEEKDAY_KEYS[row.weekday], actor.locale).capitalize(),
            ))
        # Обед должен помещаться в рабочий день: иначе расчёт окон вычтет
        # промежуток за его пределами и день молча укоротится не там, где ждали.
        if new_lunch_start is not None and new_lunch_end is not None:
            if new_lunch_start < new_start or new_lunch_end > new_end:
                return Outcome(
                    reason=t(
                        "orgadmin.err.lunch_outside", actor.locale,
                        day=t(WEEKDAY_KEYS[row.weekday], actor.locale).capitalize(),
                    )
                )
        planned.append((row, new_start, new_end, new_lunch_start, new_lunch_end))

    for row, new_start, new_end, new_lunch_start, new_lunch_end in planned:
        row.start_time = new_start
        row.end_time = new_end
        row.lunch_start = new_lunch_start
        row.lunch_end = new_lunch_end
        if is_working is not None:
            row.is_working = is_working
        if buffer_minutes is not None:
            row.buffer_minutes = buffer_minutes
        if max_consecutive is not None:
            row.max_consecutive_meetings = max_consecutive
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="settings.hours",
        entity_type="user", entity_id=subject.id,
        before={"days": before},
        after={"days": [_hours_snapshot(row) for row in rows]},
    )
    return Outcome(item=rows)


def _hours_snapshot(row: WorkingHours) -> dict:
    """Слепок дня для журнала. Времена строками — журнал читают люди."""
    return {
        "weekday": row.weekday,
        "is_working": row.is_working,
        "start": row.start_time.strftime("%H:%M"),
        "end": row.end_time.strftime("%H:%M"),
        "lunch": (
            f"{row.lunch_start:%H:%M}–{row.lunch_end:%H:%M}"
            if row.lunch_start and row.lunch_end
            else None
        ),
        "buffer": row.buffer_minutes,
        "in_a_row": row.max_consecutive_meetings,
    }


# ── Лимиты времени ──────────────────────────────────────────────────────────
async def set_quota(
    session: AsyncSession,
    *,
    actor: User,
    grants: dict[str, Grant],
    owner: User,
    minutes: int,
    period: str = QuotaPeriod.WEEK,
    subject_user: User | None = None,
    subject_department: Department | None = None,
) -> Outcome:
    """Задаёт норму времени руководителя на человека или отдел.

    Норма не запрещает заявку, а помечает её (решение блока 3). Здесь только
    её величина: смысл нормы живёт в `quotas`, и дублировать его нельзя.
    """
    if not _allowed(grants):
        return Outcome(reason=t("orgadmin.err.quota_rights", actor.locale))
    if owner.organization_id != actor.organization_id:
        return Outcome(reason=t("orgadmin.err.other_org_owner", actor.locale))
    if subject_user is None and subject_department is None:
        return Outcome(reason=t("orgadmin.err.no_subject", actor.locale))
    if subject_user is not None and subject_department is not None:
        return Outcome(reason=t("orgadmin.err.person_or_department", actor.locale))
    if subject_user is not None and subject_user.organization_id != actor.organization_id:
        return Outcome(reason=t("orgadmin.err.other_org_user", actor.locale))
    if (
        subject_department is not None
        and subject_department.organization_id != actor.organization_id
    ):
        return Outcome(reason=t("orgadmin.err.other_org_department", actor.locale))
    if not 0 <= minutes <= MAX_QUOTA_MINUTES:
        return Outcome(reason=t("orgadmin.err.quota_range", actor.locale, max=MAX_QUOTA_MINUTES))
    if period not in (QuotaPeriod.WEEK, QuotaPeriod.MONTH):
        return Outcome(reason=t("orgadmin.err.bad_period", actor.locale))

    quota = (
        await session.execute(
            select(TimeQuota).where(
                TimeQuota.owner_id == owner.id,
                TimeQuota.subject_user_id == (subject_user.id if subject_user else None),
                TimeQuota.subject_department_id == (
                    subject_department.id if subject_department else None
                ),
            )
        )
    ).scalar_one_or_none()

    before = (
        {"minutes": quota.minutes, "period": quota.period} if quota is not None else None
    )
    if quota is None:
        quota = TimeQuota(
            organization_id=actor.organization_id,
            owner_id=owner.id,
            subject_user_id=subject_user.id if subject_user else None,
            subject_department_id=subject_department.id if subject_department else None,
        )
        session.add(quota)
    quota.minutes = minutes
    quota.period = period
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="settings.quota",
        entity_type="time_quota", entity_id=quota.id,
        before=before,
        after={
            "minutes": minutes,
            "period": period,
            "subject_user_id": subject_user.id if subject_user else None,
            "subject_department_id": subject_department.id if subject_department else None,
        },
    )
    return Outcome(item=quota)


async def quotas_of(session: AsyncSession, owner: User) -> list[TimeQuota]:
    return list(
        (
            await session.execute(
                select(TimeQuota).where(TimeQuota.owner_id == owner.id).order_by(TimeQuota.id)
            )
        ).scalars().all()
    )


# ── Праздники и перенесённые рабочие дни ────────────────────────────────────
async def set_holiday(
    session: AsyncSession,
    *,
    actor: User,
    grants: dict[str, Grant],
    day: date,
    title: str,
    is_working_day: bool = False,
    today: date | None = None,
) -> Outcome:
    """Заводит праздник или объявляет выходной рабочим.

    Одна дата — одна запись: повторное заведение правит существующую, а не
    создаёт вторую. Две записи на один день сделали бы расчёт окон зависящим
    от того, какая нашлась первой.
    """
    if not _allowed(grants):
        return Outcome(reason=t("orgadmin.err.calendar_rights", actor.locale))
    title = (title or "").strip()
    if len(title) < 2:
        return Outcome(reason=t("orgadmin.err.need_title", actor.locale))
    horizon = (today or date.today())
    if abs((day - horizon).days) > MAX_HORIZON_DAYS:
        return Outcome(reason=t("orgadmin.err.too_far", actor.locale))

    holiday = (
        await session.execute(
            select(Holiday).where(
                Holiday.organization_id == actor.organization_id, Holiday.day == day
            )
        )
    ).scalar_one_or_none()
    before = (
        {"title": holiday.title, "is_working_day": holiday.is_working_day}
        if holiday is not None
        else None
    )
    if holiday is None:
        holiday = Holiday(organization_id=actor.organization_id, day=day)
        session.add(holiday)
    holiday.title = title[:200]
    holiday.is_working_day = is_working_day
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="settings.holiday",
        entity_type="holiday", entity_id=holiday.id,
        before=before,
        after={"day": day.isoformat(), "title": holiday.title, "is_working_day": is_working_day},
    )
    return Outcome(item=holiday)


async def drop_holiday(
    session: AsyncSession, *, actor: User, grants: dict[str, Grant], holiday: Holiday
) -> str | None:
    if not _allowed(grants):
        return t("orgadmin.err.calendar_rights", actor.locale)
    if holiday.organization_id != actor.organization_id:
        return t("orgadmin.err.other_org_day", actor.locale)
    await write_audit(
        session, actor_id=actor.id, action="settings.holiday.delete",
        entity_type="holiday", entity_id=holiday.id,
        before={"day": holiday.day.isoformat(), "title": holiday.title},
    )
    await session.execute(delete(Holiday).where(Holiday.id == holiday.id))
    await session.flush()
    return None


async def holidays_of(
    session: AsyncSession, *, organization_id: int, since: date, limit: int = 20
) -> list[Holiday]:
    return list(
        (
            await session.execute(
                select(Holiday)
                .where(Holiday.organization_id == organization_id, Holiday.day >= since)
                .order_by(Holiday.day)
                .limit(limit)
            )
        ).scalars().all()
    )


# ── Отпуска и командировки ──────────────────────────────────────────────────
async def set_absence(
    session: AsyncSession,
    *,
    actor: User,
    grants: dict[str, Grant],
    subject: User,
    kind: str,
    start_date: date,
    end_date: date,
    substitute: User | None = None,
    comment: str | None = None,
) -> Outcome:
    """Заводит отсутствие. Влияет и на календарь, и на назначение сроков."""
    if not _allowed(grants):
        return Outcome(reason=t("orgadmin.err.absence_rights", actor.locale))
    if subject.organization_id != actor.organization_id:
        return Outcome(reason=t("orgadmin.err.other_org_user", actor.locale))
    if end_date < start_date:
        return Outcome(reason=t("orgadmin.err.absence_end", actor.locale))
    if kind not in ABSENCE_KEYS:
        return Outcome(reason=t("orgadmin.err.unknown_kind", actor.locale))
    if substitute is not None:
        if substitute.organization_id != actor.organization_id:
            return Outcome(reason=t("orgadmin.err.other_org_substitute", actor.locale))
        if substitute.id == subject.id:
            return Outcome(reason=t("orgadmin.err.self_substitute", actor.locale))
        if substitute.status != UserStatus.ACTIVE:
            return Outcome(reason=t("orgadmin.err.substitute_inactive", actor.locale))

    # Пересечения запрещены: два отпуска на один день — это не два отпуска,
    # а ошибка ввода, и расчёт окон всё равно учтёт только факт отсутствия.
    overlap = (
        await session.execute(
            select(Absence).where(
                Absence.user_id == subject.id,
                Absence.start_date <= end_date,
                Absence.end_date >= start_date,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if overlap is not None:
        kind_key = ABSENCE_KEYS.get(overlap.kind, "absence.other")
        return Outcome(reason=t(
            "orgadmin.err.overlaps", actor.locale,
            kind=(
                f"{t(kind_key, actor.locale)} "
                f"{overlap.start_date:%d.%m}–{overlap.end_date:%d.%m}"
            ),
        ))

    absence = Absence(
        user_id=subject.id,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        substitute_user_id=substitute.id if substitute else None,
        comment=(comment or "").strip() or None,
    )
    session.add(absence)
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="settings.absence",
        entity_type="absence", entity_id=absence.id,
        after={
            "user_id": subject.id,
            "kind": kind,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "substitute_id": substitute.id if substitute else None,
        },
    )
    return Outcome(item=absence)


async def drop_absence(
    session: AsyncSession, *, actor: User, grants: dict[str, Grant], absence: Absence
) -> str | None:
    if not _allowed(grants):
        return t("orgadmin.err.absence_rights", actor.locale)
    subject = await session.get(User, absence.user_id)
    if subject is None or subject.organization_id != actor.organization_id:
        return t("orgadmin.err.other_org_record", actor.locale)
    await write_audit(
        session, actor_id=actor.id, action="settings.absence.delete",
        entity_type="absence", entity_id=absence.id,
        before={
            "user_id": absence.user_id,
            "from": absence.start_date.isoformat(),
            "to": absence.end_date.isoformat(),
        },
    )
    await session.execute(delete(Absence).where(Absence.id == absence.id))
    await session.flush()
    return None


async def absences_of(
    session: AsyncSession, *, organization_id: int, since: date, limit: int = 20
) -> list[Absence]:
    """Ближайшие отсутствия по организации — одним запросом с людьми."""
    return list(
        (
            await session.execute(
                select(Absence)
                .join(User, User.id == Absence.user_id)
                .where(User.organization_id == organization_id, Absence.end_date >= since)
                .order_by(Absence.start_date)
                .limit(limit)
            )
        ).scalars().all()
    )
