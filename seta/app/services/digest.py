"""Утренняя сводка: один раз в день, в 07:30 по месту получателя.

Без неё не работает решение Р-10. Оно обещает, что руководителю не приходит
поштучно ничего, кроме личного контроля, — а взамен он получает всё одним
блоком утром. Пока этого блока нет, обещание не выполнено наполовину:
уведомления отфильтрованы, но взамен не приходит ничего.

Четыре правила, которые здесь важнее остального.

**Те же цифры, что на экране.** Сводка не считает ничего сама: она собирает
`dashboard.build` и отрисовывает `dashboard.render`. Иначе через месяц в чате
было бы одно число, а в сводке другое, и объяснить расхождение стало бы
невозможно.

**07:30 у получателя, а не на сервере.** Люди могут сидеть в разных поясах,
и «утро» у каждого своё. Проход идёт каждую минуту и смотрит на местное время
каждого, а не на время машины.

**Один день — одно сообщение.** Ключ события содержит местную дату получателя,
уникальность `event_key` в схеме делает остальное: сколько бы раз ни прошёл
планировщик, второго письма не будет.

**Пустой день не рассылается.** Сводка «сегодня ничего» — это письмо, которое
через неделю перестают открывать, и вместе с ним перестают открывать те, где
что-то есть. Если ни встреч, ни просрочек, ни ожидающих решения, письма нет.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import esc
from app.core.timeutil import parse_hhmm, to_local, utcnow
from app.models import (
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    MeetingStatus,
    NotificationPriority,
    RequestStatus,
    Role,
    RoleCode,
    User,
    UserRole,
    UserStatus,
)
from app.services import dashboard
from app.services.notifications import enqueue
from app.services.rbac import load_grants

log = logging.getLogger("seta.digest")

# Во сколько по местному времени уходит сводка. Совпадает с концом тихих часов:
# сводка — первое, что человек читает утром, и ждать ей уже нечего.
DIGEST_AT = time(7, 30)
# Сколько минут после этого времени проход ещё считает уместным её отправить.
# Сводка «на день», доставленная после обеда, врёт про день: полдня прошло.
# Если обработчик простоял всё утро, письма не будет — это честнее.
DIGEST_WINDOW = timedelta(minutes=150)
# Сколько получателей разбирается за один проход. Их единицы, предел — от сбоя.
MAX_RECIPIENTS = 50
# Кому вообще положена сводка: тем, кто управляет днём.
DIGEST_ROLES = (RoleCode.EXECUTIVE, RoleCode.ASSISTANT)


@dataclass(slots=True)
class ChiefDay:
    """Строка блока «у руководителя сегодня» — только для ассистента."""

    name: str
    meetings: int
    requests: int


def due_now(now: datetime, timezone_name: str) -> bool:
    """Наступило ли у этого человека время сводки.

    Окно, а не точная минута: проход раз в минуту может пропустить момент
    из-за долгой предыдущей итерации, и тогда сводка не ушла бы вовсе.
    """
    local = to_local(now, timezone_name)
    start = local.replace(
        hour=DIGEST_AT.hour, minute=DIGEST_AT.minute, second=0, microsecond=0
    )
    return start <= local < start + DIGEST_WINDOW


def local_date_key(now: datetime, timezone_name: str) -> str:
    """Местная дата получателя — она и делает сводку однодневной.

    Брать дату сервера нельзя: в Ташкенте 07:30 наступает, когда в UTC ещё
    вчерашний день, и ключ съезжал бы на сутки.
    """
    return to_local(now, timezone_name).strftime("%Y-%m-%d")


async def recipients(session: AsyncSession, limit: int = MAX_RECIPIENTS) -> list[User]:
    """Кому положена сводка: руководители и ассистенты, одним запросом."""
    return list(
        (
            await session.execute(
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.status == UserStatus.ACTIVE,
                    Role.code.in_(DIGEST_ROLES),
                )
                .order_by(User.id)
                .distinct()
                .limit(limit)
            )
        ).scalars().all()
    )


async def chiefs_day(
    session: AsyncSession, *, organization_id: int, now: datetime
) -> list[ChiefDay]:
    """Чем занят день руководителей — блок, который видит только ассистент.

    Ассистент — основной пользователь системы (решение Р-05), и его утро
    начинается не со своего календаря, а с чужого. Две выборки на всю
    организацию, а не по запросу на человека.
    """
    people = (
        await session.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.organization_id == organization_id,
                User.status == UserStatus.ACTIVE,
                Role.code == RoleCode.EXECUTIVE,
            )
            .order_by(User.full_name)
            .distinct()
        )
    ).scalars().all()
    if not people:
        return []

    ids = [person.id for person in people]
    # Границы дня у каждого свои, но руководители одной организации живут
    # в одном поясе; берём пояс первого и не плодим запрос на человека.
    local = to_local(now, people[0].timezone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
    end = start + timedelta(days=1)

    counted = dict(
        (
            await session.execute(
                select(MeetingParticipant.user_id, func.count(Meeting.id))
                .join(Meeting, Meeting.id == MeetingParticipant.meeting_id)
                .where(
                    MeetingParticipant.user_id.in_(ids),
                    Meeting.status != MeetingStatus.CANCELLED,
                    Meeting.start_at >= start,
                    Meeting.start_at < end,
                )
                .group_by(MeetingParticipant.user_id)
            )
        ).all()
    )
    waiting = dict(
        (
            await session.execute(
                select(MeetingRequest.owner_id, func.count(MeetingRequest.id))
                .where(
                    MeetingRequest.owner_id.in_(ids),
                    MeetingRequest.status == RequestStatus.NEW,
                )
                .group_by(MeetingRequest.owner_id)
            )
        ).all()
    )
    return [
        ChiefDay(
            name=person.full_name,
            meetings=int(counted.get(person.id, 0)),
            requests=int(waiting.get(person.id, 0)),
        )
        for person in people
    ]


async def build_for(
    session: AsyncSession, *, viewer: User, now: datetime
) -> tuple[str | None, dashboard.Board]:
    """Текст сводки для человека. None — отправлять нечего.

    Возвращает и доску: вызывающему бывает нужно знать, почему письма нет.
    """
    grants = await load_grants(session, viewer)
    board = await dashboard.build(session, viewer=viewer, grants=grants, now=now)

    is_assistant = await has_role(session, viewer, RoleCode.ASSISTANT)
    chiefs: list[ChiefDay] = []
    if is_assistant:
        chiefs = await chiefs_day(
            session, organization_id=viewer.organization_id, now=now
        )

    # Пустой день не рассылается. Ассистенту письмо всё же уходит, если день
    # непустой у руководителя: его работа — чужой день, а не свой.
    chiefs_busy = any(item.meetings or item.requests for item in chiefs)
    if board.quiet and not chiefs_busy:
        return None, board

    local = to_local(now, viewer.timezone)
    header = f"☀️ <b>Утро · {local.strftime('%d.%m')}</b>"
    text = dashboard.render(board, header=header)

    if chiefs:
        block = ["", "<b>У руководителя сегодня</b>"]
        for item in chiefs:
            parts = [f"встреч {item.meetings}"]
            if item.requests:
                parts.append(f"заявок ждут ответа {item.requests}")
            block.append(f"👤 {esc(item.name)}: {', '.join(parts)}")
        text += "\n" + "\n".join(block)

    return text, board


async def has_role(session: AsyncSession, user: User, code: RoleCode) -> bool:
    """Есть ли у человека эта роль прямо сейчас."""
    found = await session.scalar(
        select(UserRole.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user.id, Role.code == code)
    )
    return found is not None


async def send_digests(session: AsyncSession, now: datetime | None = None) -> int:
    """Один проход. Возвращает, сколько сводок поставлено в очередь.

    Запросы на человека здесь неизбежны: сводка по определению своя у каждого,
    как и досье к встрече. Ограничены они не правилом «ни одного запроса
    в цикле», а числом получателей — их единицы, а не тысячи.
    """
    now = now or utcnow()
    sent = 0
    for viewer in await recipients(session):
        if not due_now(now, viewer.timezone):
            continue

        text, _ = await build_for(session, viewer=viewer, now=now)
        if text is None:
            continue

        created = await enqueue(
            session,
            user_id=viewer.id,
            organization_id=viewer.organization_id,
            # Местная дата в ключе делает сводку однодневной независимо от того,
            # сколько раз за окно пройдёт планировщик.
            event_key=f"digest:{viewer.id}:{local_date_key(now, viewer.timezone)}",
            kind="digest.morning",
            priority=NotificationPriority.NORMAL,
            body=text,
            payload={"kind": "morning"},
            timezone_name=viewer.timezone,
        )
        sent += int(created)
    return sent
