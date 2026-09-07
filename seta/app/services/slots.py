"""Расчёт свободных окон.

Ядро блока 3. Сотрудник видит не «дыры в расписании», а несколько подходящих
вариантов — а система при этом учитывает всё, что делает окно непригодным:

    рабочие часы дня недели · обед · буфер до и после встречи
    существующие встречи · личные блокировки · удержанные окна
    отпуска и командировки · праздники и перенесённые дни
    максимум встреч подряд · поздний приём · часовой пояс каждого участника

Два правила, от которых зависит скорость и правильность:

1. **Ни одного запроса к базе внутри цикла.** Всё нужное грузится шестью
   запросами вперёд, дальше — арифметика по интервалам в памяти. Иначе поиск
   окна для встречи на десятерых за неделю превратился бы в сотни запросов
   на одно нажатие кнопки.
2. **Дни перебираются в часовом поясе владельца календаря, а интервалы
   сравниваются в UTC.** Рабочий день — понятие местное, занятость — абсолютное.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import to_local, utcnow
from app.models.enums import MeetingStatus
from app.models.meeting import Meeting, MeetingParticipant, SlotHold
from app.models.schedule import Absence, CalendarBlock, Holiday, WorkingHours
from app.models.user import User
from app.services.availability import get_view

# Дальше этого срока окна не ищем: предлагать встречу через месяц бессмысленно.
MAX_HORIZON_DAYS = 21
# Шаг сетки: окна предлагаются на «круглое» время, а не на 14:37.
GRID_MINUTES = 15


@dataclass(slots=True, frozen=True)
class Slot:
    """Свободное окно, готовое к показу."""

    start: datetime
    end: datetime
    is_late: bool = False

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


Interval = tuple[datetime, datetime]


# ── Публичная функция ───────────────────────────────────────────────────────
async def free_slots(
    session: AsyncSession,
    *,
    owner: User,
    duration_minutes: int = 30,
    days_ahead: int = 7,
    participants: list[User] | None = None,
    limit: int = 5,
    per_day: int = 3,
    now: datetime | None = None,
) -> list[Slot]:
    """Подходящие окна в календаре владельца, общие со всеми участниками.

    Возвращает не все дыры подряд, а несколько пригодных, **распределённых
    по дням**: пять вариантов подряд в одно утро — плохой выбор, «сегодня два,
    завтра два, послезавтра один» — хороший.

    Распределение не режет выдачу: сначала берётся по `per_day` из каждого дня,
    потом добирается второй заход, третий и так далее. Спросили один день —
    получите из него всё, что просили; спросили неделю — получите её срез.
    """
    now = now or utcnow()
    # 0 — это «только сегодня», осмысленный запрос, а не ошибка: зажимать его
    # до суток нельзя, иначе половина выдачи уедет на завтра.
    days_ahead = max(0, min(days_ahead, MAX_HORIZON_DAYS))
    duration = timedelta(minutes=max(GRID_MINUTES, duration_minutes))

    people = [owner] + [p for p in (participants or []) if p.id != owner.id]
    people_ids = [p.id for p in people]

    horizon_end = now + timedelta(days=days_ahead + 1)
    data = await _load(session, people_ids, owner, now, horizon_end)
    late_until = await _late_allowed(session, owner)

    by_day: list[list[Slot]] = []
    owner_tz = ZoneInfo(owner.timezone)
    first_day = to_local(now, owner.timezone).date()

    for offset in range(days_ahead + 1):
        day = first_day + timedelta(days=offset)

        # Окно дня строится по владельцу, затем сужается каждым участником:
        # нужно пересечение доступности, а не объединение.
        windows = _day_windows(owner, day, data, late_until)
        if not windows:
            continue

        for person in people[1:]:
            # День владельца может приходиться на два местных дня участника:
            # полночь в Ташкенте — это ещё вчера в Москве. Берём оба соседних
            # дня и объединяем, иначе коллега западнее выпадал бы целиком.
            owner_day_start = _local_midnight(day, owner_tz)
            person_days = {
                to_local(owner_day_start, person.timezone).date(),
                to_local(owner_day_start + timedelta(days=1), person.timezone).date(),
            }
            available: list[Interval] = []
            for person_day in sorted(person_days):
                available.extend(
                    (start, end)
                    for start, end, _ in _day_windows(
                        person, person_day, data, late_until=None
                    )
                )
            available = _merge(available)
            windows = [
                (start, end, is_late)
                for (start, end), is_late in _intersect_tagged(windows, available)
            ]
            if not windows:
                break
        if not windows:
            continue

        busy = _busy_for(people_ids, data, buffer_minutes=_buffer_of(owner, day, data))
        today: list[Slot] = []
        for start, end, is_late in sorted(windows):
            for gap in _subtract((start, end), busy):
                for slot in _slice(gap, duration, now, is_late):
                    if _too_many_in_a_row(owner, slot, data):
                        continue
                    today.append(slot)
                    if len(today) >= limit:
                        break
                if len(today) >= limit:
                    break
            if len(today) >= limit:
                break
        if today:
            by_day.append(today)

    slots: list[Slot] = []
    chunk = max(1, per_day)
    for round_no in range(0, (limit // chunk) + 2):
        taken_this_round = False
        for day_slots in by_day:
            for slot in day_slots[round_no * chunk:(round_no + 1) * chunk]:
                slots.append(slot)
                taken_this_round = True
                if len(slots) >= limit:
                    return sorted(slots, key=lambda s: s.start)
        if not taken_this_round:
            break

    return sorted(slots, key=lambda s: s.start)


async def is_free(
    session: AsyncSession,
    *,
    owner: User,
    start_at: datetime,
    end_at: datetime,
    exclude_meeting_id: int | None = None,
) -> bool:
    """Свободно ли конкретное время. Проверяется перед подтверждением заявки:
    между показом окон и решением руководителя время могли занять."""
    data = await _load(
        session, [owner.id], owner, start_at - timedelta(days=1), end_at + timedelta(days=1)
    )
    # Буфер здесь не применяется: проверяется именно занятость, а не удобство.
    # Иначе подтверждение окна, выбранного самим руководителем, упиралось бы
    # в его же буфер.
    for start, end in _busy_for([owner.id], data, exclude_meeting_id=exclude_meeting_id):
        if start < end_at and start_at < end:
            return False
    return True


# ── Загрузка данных: шесть запросов, ни одного в цикле ─────────────────────
@dataclass(slots=True)
class _Data:
    hours: dict[tuple[int, int], WorkingHours]
    meetings: dict[int, list[tuple[int, datetime, datetime]]]
    blocks: dict[int, list[Interval]]
    absences: dict[int, list[tuple[date, date]]]
    holidays: dict[date, bool]
    holds: list[Interval]


async def _load(
    session: AsyncSession,
    people_ids: list[int],
    owner: User,
    since: datetime,
    until: datetime,
) -> _Data:
    hours_rows = await session.execute(
        select(WorkingHours).where(WorkingHours.user_id.in_(people_ids))
    )
    hours = {(h.user_id, h.weekday): h for h in hours_rows.scalars().all()}

    # Встречи, где человек владелец календаря либо участник — одним запросом.
    meeting_rows = await session.execute(
        select(Meeting, MeetingParticipant.user_id)
        .outerjoin(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
        .where(
            Meeting.status != MeetingStatus.CANCELLED,
            Meeting.end_at > since,
            Meeting.start_at < until,
            or_(Meeting.owner_id.in_(people_ids), MeetingParticipant.user_id.in_(people_ids)),
        )
    )
    # Соединение с участниками даёт по строке на участника: встреча на троих
    # приходит трижды. Для занятости это безвредно — интервалы всё равно
    # схлопываются, — но счётчик встреч подряд принял бы три копии одной
    # встречи за три разные и убрал бы из выдачи свободные окна. Поэтому пара
    # «человек + встреча» запоминается ровно один раз.
    wanted = set(people_ids)
    seen: set[tuple[int, int]] = set()
    meetings: dict[int, list[tuple[int, datetime, datetime]]] = {}
    for meeting, participant_id in meeting_rows.all():
        for person_id in {meeting.owner_id, participant_id} & wanted:
            if (person_id, meeting.id) in seen:
                continue
            seen.add((person_id, meeting.id))
            meetings.setdefault(person_id, []).append(
                (meeting.id, meeting.start_at, meeting.end_at)
            )

    block_rows = await session.execute(
        select(CalendarBlock).where(
            CalendarBlock.user_id.in_(people_ids),
            CalendarBlock.end_at > since,
            CalendarBlock.start_at < until,
        )
    )
    blocks: dict[int, list[Interval]] = {}
    for block in block_rows.scalars().all():
        blocks.setdefault(block.user_id, []).append((block.start_at, block.end_at))

    absence_rows = await session.execute(
        select(Absence).where(
            Absence.user_id.in_(people_ids),
            Absence.end_date >= since.date(),
            Absence.start_date <= until.date(),
        )
    )
    absences: dict[int, list[tuple[date, date]]] = {}
    for absence in absence_rows.scalars().all():
        absences.setdefault(absence.user_id, []).append((absence.start_date, absence.end_date))

    holiday_rows = await session.execute(
        select(Holiday).where(
            Holiday.organization_id == owner.organization_id,
            Holiday.day >= since.date(),
            Holiday.day <= until.date(),
        )
    )
    holidays = {h.day: h.is_working_day for h in holiday_rows.scalars().all()}

    hold_rows = await session.execute(
        select(SlotHold).where(
            SlotHold.owner_id == owner.id,
            SlotHold.released_at.is_(None),
            SlotHold.expires_at > utcnow(),
            SlotHold.end_at > since,
            SlotHold.start_at < until,
        )
    )
    holds = [(h.start_at, h.end_at) for h in hold_rows.scalars().all()]

    return _Data(hours, meetings, blocks, absences, holidays, holds)


async def _late_allowed(session: AsyncSession, owner: User) -> datetime | None:
    """До какого момента открыт поздний приём. None — не открыт вовсе.

    Расширять рабочие часы на весь вечер нельзя: посыпались бы предложения
    встреч в 21:00. Решение Р-12.

    Разрешение действует ровно столько, сколько сказал руководитель: «принимаю
    до 21:00» — это про сегодня, а не про каждый вечер до конца горизонта.
    Поэтому возвращается срок, и поздние окна за ним не строятся.
    """
    view = await get_view(session, owner.id)
    if not view.opens_late_slots:
        return None
    return view.until_at


# ── Построение окон дня ─────────────────────────────────────────────────────
def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def _at(day: date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


Window = tuple[datetime, datetime, bool]


def _day_windows(
    person: User, day: date, data: _Data, late_until: datetime | None
) -> list[Window]:
    """Рабочие окна человека в этот день, уже без обеда.

    Третий элемент — признак позднего окна: оно появляется, только когда
    руководитель открыл поздний приём, и в списке помечается отдельно.
    """
    hours = data.hours.get((person.id, day.weekday()))
    if hours is None:
        return []

    # Праздник закрывает день, даже если он рабочий по расписанию.
    # Перенесённый рабочий день, наоборот, открывает выходной.
    holiday = data.holidays.get(day)
    working = hours.is_working
    if holiday is True:
        working = True
    elif holiday is False:
        working = False
    if not working:
        return []

    for start, end in data.absences.get(person.id, []):
        if start <= day <= end:
            return []

    tz = ZoneInfo(person.timezone)
    plain: list[Interval] = [(_at(day, hours.start_time, tz), _at(day, hours.end_time, tz))]

    if hours.lunch_start and hours.lunch_end:
        lunch = (_at(day, hours.lunch_start, tz), _at(day, hours.lunch_end, tz))
        plain = _subtract_all(plain, [lunch])

    windows: list[Window] = [(s, e, False) for s, e in plain if e > s]

    if late_until and hours.late_end_time and hours.late_end_time > hours.end_time:
        late_start = _at(day, hours.end_time, tz)
        late_end = min(_at(day, hours.late_end_time, tz), late_until)
        if late_end > late_start:
            windows.append((late_start, late_end, True))

    return windows


def _busy_for(
    people_ids: list[int],
    data: _Data,
    *,
    buffer_minutes: int = 0,
    exclude_meeting_id: int | None = None,
) -> list[Interval]:
    """Занятое время всех участников.

    Вокруг встреч добавляется буфер с обеих сторон: иначе окно предлагалось бы
    впритык к предыдущей встрече, и человек шёл бы с одной на другую без паузы.
    Личные блокировки и удержанные окна буфером не расширяются — это не встречи.
    """
    meetings: list[Interval] = []
    other: list[Interval] = []
    for person_id in people_ids:
        for meeting_id, start, end in data.meetings.get(person_id, []):
            if exclude_meeting_id is not None and meeting_id == exclude_meeting_id:
                continue
            meetings.append((start, end))
        other.extend(data.blocks.get(person_id, []))
    other.extend(data.holds)

    pad = timedelta(minutes=buffer_minutes)
    padded = [(start - pad, end + pad) for start, end in meetings]
    return _merge(padded + other)


def _buffer_of(owner: User, day: date, data: _Data) -> int:
    hours = data.hours.get((owner.id, day.weekday()))
    return hours.buffer_minutes if hours else 15


def _intersect_tagged(
    windows: list[Window], available: list[Interval]
) -> list[tuple[Interval, bool]]:
    """Пересечение окон владельца с доступностью участника, признак сохраняется."""
    result: list[tuple[Interval, bool]] = []
    for start, end, is_late in windows:
        for piece in _intersect([(start, end)], available):
            result.append((piece, is_late))
    return result


# ── Арифметика интервалов ───────────────────────────────────────────────────
def _merge(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract(window: Interval, busy: list[Interval]) -> list[Interval]:
    """Что остаётся от окна после вычитания занятого времени."""
    free = [window]
    for taken_start, taken_end in busy:
        remaining: list[Interval] = []
        for start, end in free:
            if taken_end <= start or taken_start >= end:
                remaining.append((start, end))
                continue
            if taken_start > start:
                remaining.append((start, taken_start))
            if taken_end < end:
                remaining.append((taken_end, end))
        free = remaining
        if not free:
            break
    return free


def _subtract_all(windows: list[Interval], busy: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for window in windows:
        result.extend(_subtract(window, busy))
    return result


def _intersect(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Общее время двух наборов окон."""
    result: list[Interval] = []
    for a_start, a_end in left:
        for b_start, b_end in right:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end > start:
                result.append((start, end))
    return _merge(result)


def _round_up(moment: datetime) -> datetime:
    """Округление к сетке: 14:37 → 14:45. Люди не назначают встречи на 14:37."""
    minutes = (moment.minute // GRID_MINUTES) * GRID_MINUTES
    floored = moment.replace(minute=minutes, second=0, microsecond=0)
    return floored if floored == moment else floored + timedelta(minutes=GRID_MINUTES)


def _slice(
    gap: Interval, duration: timedelta, now: datetime, is_late: bool
) -> list[Slot]:
    """Режет свободный промежуток на окна нужной длительности."""
    gap_start, gap_end = gap
    cursor = _round_up(max(gap_start, now))
    result: list[Slot] = []
    while cursor + duration <= gap_end:
        result.append(Slot(cursor, cursor + duration, is_late=is_late))
        cursor += duration
    return result


def _too_many_in_a_row(owner: User, slot: Slot, data: _Data) -> bool:
    """Не создаст ли встреча слишком длинную цепочку подряд.

    «Подряд» — это встречи, между которыми меньше буфера: именно они
    складываются в тот самый день без единой паузы.
    """
    hours = data.hours.get((owner.id, to_local(slot.start, owner.timezone).weekday()))
    if hours is None:
        return False
    limit = hours.max_consecutive_meetings or 3
    gap = timedelta(minutes=hours.buffer_minutes or 15)

    same_day = to_local(slot.start, owner.timezone).date()
    day_meetings = sorted(
        (start, end)
        for _, start, end in data.meetings.get(owner.id, [])
        if to_local(start, owner.timezone).date() == same_day
    )
    chain = 1
    cursor_start, cursor_end = slot.start, slot.end
    # Считаем цепочку назад и вперёд от кандидата.
    for start, end in reversed([m for m in day_meetings if m[1] <= cursor_start]):
        if cursor_start - end <= gap:
            chain += 1
            cursor_start = start
        else:
            break
    for start, end in [m for m in day_meetings if m[0] >= cursor_end]:
        if start - cursor_end <= gap:
            chain += 1
            cursor_end = end
        else:
            break
    return chain > limit
