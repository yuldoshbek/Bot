"""Встречи в боте: мой день, мои встречи, запрос встречи, быстрое совещание.

Правило экрана: не больше пяти-семи кнопок, действие в одно касание там, где
это возможно, и ни одного отказа без выхода. «Это время занято» — плохой ответ;
«Это время занято, свободны 10:30 и 11:00» — рабочий.
"""
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import (
    BTN_MY_DAY,
    BTN_MY_MEETINGS,
    BTN_QUICK_MEETING,
    BTN_REQUEST_MEETING,
)
from app.bot.utils import STALE_BUTTON, callback_int
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, to_local, utcnow
from app.models.enums import MeetingStatus, RequestStatus, RoleCode, UserStatus
from app.models.decision import AgendaItem
from app.models.meeting import Meeting, MeetingParticipant, MeetingRequest
from app.models.org import Organization
from app.models.user import User
from app.models.rbac import Role, UserRole
from app.core.dates import humanize_due, parse_due
from app.services import attendance, meetings as service, quotas
from app.services import decisions as registry
from app.services import documents as document_service
from app.services import tasks as task_service
from app.services.tasks import TaskError
from app.services import slots as slot_service
from app.services.rbac import Grant, has_permission

router = Router(name="meetings")

DURATIONS = (15, 30, 60)
MAX_SLOT_BUTTONS = 6


class NewRequest(StatesGroup):
    owner = State()
    duration = State()
    title = State()
    slot = State()


class MoveMeeting(StatesGroup):
    when = State()
    reason = State()


class KillMeeting(StatesGroup):
    reason = State()


class Quick(StatesGroup):
    title = State()
    when = State()
    people = State()


# ── Общие мелочи ────────────────────────────────────────────────────────────
def _slot_code(slot: slot_service.Slot) -> str:
    """Окно в callback: минуты от эпохи. Короче ISO и разбирается одним int()."""
    return str(int(slot.start.timestamp()) // 60)


def _slot_time(code: int) -> datetime:
    return datetime.fromtimestamp(code * 60, tz=timezone.utc)


def _slots_kb(slots: list[slot_service.Slot], tz: str, prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for slot in slots[:MAX_SLOT_BUTTONS]:
        local = to_local(slot.start, tz)
        mark = "🌙 " if slot.is_late else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{local.strftime('%d.%m %H:%M')}",
            callback_data=f"{prefix}:{_slot_code(slot)}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _executives(session: AsyncSession, organization_id: int) -> list[User]:
    """Те, к кому вообще ходят на приём."""
    return list(
        (
            await session.execute(
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.organization_id == organization_id,
                    User.status == UserStatus.ACTIVE,
                    Role.code.in_([RoleCode.EXECUTIVE, RoleCode.DEPT_HEAD]),
                )
                .order_by(User.full_name)
                .distinct()
            )
        ).scalars().all()
    )


async def _my_meetings(
    session: AsyncSession, user: User, *, since: datetime, until: datetime
) -> list[Meeting]:
    return list(
        (
            await session.execute(
                select(Meeting)
                .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
                .where(
                    MeetingParticipant.user_id == user.id,
                    Meeting.status != MeetingStatus.CANCELLED,
                    Meeting.start_at >= since,
                    Meeting.start_at < until,
                )
                .order_by(Meeting.start_at)
                .distinct()
            )
        ).scalars().all()
    )


def _card_kb(
    meeting: Meeting, user: User, grants: dict[str, Grant], now: datetime
) -> InlineKeyboardMarkup:
    """Кнопки карточки — только те, что этому человеку сейчас доступны."""
    rows: list[list[InlineKeyboardButton]] = []
    live = meeting.status != MeetingStatus.CANCELLED

    if live and now < meeting.end_at:
        if now >= meeting.start_at - timedelta(minutes=attendance.CHECKIN_OPENS_MINUTES):
            rows.append([InlineKeyboardButton(
                text="🙋 Я на месте", callback_data=f"mt:here:{meeting.id}"
            )])

    if live and has_permission(grants, "meeting.reschedule"):
        rows.append([
            InlineKeyboardButton(text="🔄 Перенести", callback_data=f"mt:move:{meeting.id}"),
            InlineKeyboardButton(text="🚫 Отменить", callback_data=f"mt:kill:{meeting.id}"),
        ])

    if live and now >= meeting.end_at and has_permission(grants, "meeting.rate"):
        rows.append([
            InlineKeyboardButton(text="👍", callback_data=f"mt:rate:{meeting.id}:1"),
            InlineKeyboardButton(text="😐", callback_data=f"mt:rate:{meeting.id}:0"),
            InlineKeyboardButton(text="👎", callback_data=f"mt:rate:{meeting.id}:-1"),
        ])

    # Итоги встречи. Решение и поручение доступны и после завершения: их
    # обычно и фиксируют после, а не во время.
    outcome_row = []
    if has_permission(grants, "decision.create"):
        outcome_row.append(InlineKeyboardButton(
            text="📌 Решение", callback_data=f"mt:dec:{meeting.id}"
        ))
    if has_permission(grants, "task.create"):
        outcome_row.append(InlineKeyboardButton(
            text="➕ Поручение", callback_data=f"mt:task:{meeting.id}"
        ))
    if outcome_row:
        rows.append(outcome_row)

    tail = [InlineKeyboardButton(text="📎 Документы", callback_data=f"mt:files:{meeting.id}")]
    if has_permission(grants, "meeting.finish"):
        tail.append(InlineKeyboardButton(
            text="📋 Повестка", callback_data=f"mt:agenda:{meeting.id}"
        ))
    rows.append(tail)

    if (
        live
        and meeting.status != MeetingStatus.FINISHED
        and now >= meeting.start_at
        and has_permission(grants, "meeting.finish")
    ):
        rows.append([InlineKeyboardButton(
            text="🏁 Завершить встречу", callback_data=f"mt:done:{meeting.id}"
        )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _card_text(session: AsyncSession, meeting: Meeting, viewer: User) -> str:
    owner = await session.get(User, meeting.owner_id)
    people = await service.participants_of(session, meeting)
    lines = [
        f"<b>{esc(meeting.title)}</b>",
        "",
        f"🕐 {fmt_dt(meeting.start_at, viewer.timezone)}"
        f"–{to_local(meeting.end_at, viewer.timezone).strftime('%H:%M')}",
        f"👤 Ведёт: {esc(owner.full_name) if owner else 'неизвестно'}",
    ]
    if len(people) > 1:
        names = ", ".join(esc(p.full_name) for p in people if p.id != meeting.owner_id)
        lines.append(f"👥 Участники: {cut(names, 200)}")
    if meeting.status == MeetingStatus.CANCELLED:
        lines.append(f"\n🚫 Отменена. Причина: {esc(meeting.cancel_reason or 'не указана')}")
    elif meeting.reschedule_count:
        lines.append(f"\n🔄 Переносилась {meeting.reschedule_count} раз(а)")

    if meeting.status == MeetingStatus.FINISHED:
        made_decisions, made_tasks = await registry.meeting_outcome(session, meeting)
        if made_decisions or made_tasks:
            lines.append(f"\n🏁 Итоги: решений {made_decisions}, поручений {made_tasks}")
        else:
            # Встреча без результата — не обвинение, а факт, который стоит видеть.
            lines.append("\n🏁 Завершена. Решений и поручений не зафиксировано.")
    return "\n".join(lines)


# ── Мой день ────────────────────────────────────────────────────────────────
@router.message(F.text == BTN_MY_DAY)
async def my_day(
    message: Message, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    now = utcnow()
    local = to_local(now, user.timezone)
    day_start = (local.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(now.tzinfo)
    day_end = day_start + timedelta(days=1)

    today = await _my_meetings(session, user, since=day_start, until=day_end)
    current = [m for m in today if m.start_at <= now < m.end_at]
    ahead = [m for m in today if m.start_at > now]

    lines = [f"<b>Мой день · {local.strftime('%d.%m')}</b>", ""]
    if current:
        lines.append("<b>Сейчас</b>")
        for m in current:
            lines.append(f"🔴 {esc(m.title)} — до {to_local(m.end_at, user.timezone):%H:%M}")
        lines.append("")
    if ahead:
        lines.append("<b>Дальше</b>")
        for m in ahead[:5]:
            lines.append(f"🕐 {to_local(m.start_at, user.timezone):%H:%M} {esc(m.title)}")
    elif not current:
        lines.append("Встреч на сегодня нет.")

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📅 " + esc(cut(m.title, 30)), callback_data=f"mt:card:{m.id}")]
        for m in (current + ahead)[:4]
    ]

    if has_permission(grants, "meeting.approve"):
        waiting = await service.pending_for(session, user)
        over = await quotas.over_quota_requests(session, owner=user)
        if waiting:
            lines += ["", f"<b>Требуют решения: {len(waiting)}</b>"]
            if over:
                lines.append(f"из них сверх лимита: {len(over)}")
            rows.append([InlineKeyboardButton(
                text=f"📥 Заявки на встречу ({len(waiting)})", callback_data="rq:list"
            )])

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.message(F.text == BTN_MY_MEETINGS)
async def my_meetings(message: Message, session: AsyncSession, user: User) -> None:
    now = utcnow()
    upcoming = await _my_meetings(session, user, since=now, until=now + timedelta(days=14))
    if not upcoming:
        await message.answer(
            "Встреч на ближайшие две недели нет.\n"
            "Чтобы договориться о встрече, нажмите «Запросить встречу»."
        )
        return

    lines = ["<b>Мои встречи</b>", ""]
    rows = []
    for m in upcoming[:10]:
        lines.append(f"🕐 {fmt_dt(m.start_at, user.timezone)} — {esc(m.title)}")
        rows.append([InlineKeyboardButton(
            text=f"{to_local(m.start_at, user.timezone):%d.%m %H:%M} · {cut(m.title, 25)}",
            callback_data=f"mt:card:{m.id}",
        )])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mt:card:"))
async def meeting_card(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await call.message.answer(
        await _card_text(session, meeting, user),
        reply_markup=_card_kb(meeting, user, grants, utcnow()),
    )
    await call.answer()


# ── Запрос встречи ──────────────────────────────────────────────────────────
@router.message(F.text == BTN_REQUEST_MEETING)
async def request_start(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant],
) -> None:
    if not has_permission(grants, "calendar.read_free"):
        await message.answer("Запрашивать встречи может сотрудник организации.")
        return

    people = [p for p in await _executives(session, organization.id) if p.id != user.id]
    if not people:
        await message.answer(
            "Не к кому записаться: в организации ещё нет руководителя.\n"
            "Он появится, когда администратор подтвердит его роль."
        )
        return

    await state.clear()
    if len(people) == 1:
        await state.update_data(owner_id=people[0].id)
        await _ask_duration(message, state, people[0])
        return

    await message.answer(
        "<b>Запрос встречи</b>\n\nК кому?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"nm:who:{p.id}")]
            for p in people[:7]
        ]),
    )
    await state.set_state(NewRequest.owner)


async def _ask_duration(message: Message, state: FSMContext, owner: User) -> None:
    await message.answer(
        f"К кому: <b>{esc(owner.full_name)}</b>\n\nСколько времени нужно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{d} мин", callback_data=f"nm:len:{d}")
            for d in DURATIONS
        ]]),
    )
    await state.set_state(NewRequest.duration)


@router.callback_query(NewRequest.owner, F.data.startswith("nm:who:"))
async def request_owner(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    owner_id = callback_int(call.data)
    owner = await session.get(User, owner_id) if owner_id else None
    if owner is None or owner.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(owner_id=owner.id)
    await _ask_duration(call.message, state, owner)
    await call.answer()


@router.callback_query(NewRequest.duration, F.data.startswith("nm:len:"))
async def request_duration(call: CallbackQuery, state: FSMContext) -> None:
    minutes = callback_int(call.data)
    if minutes not in DURATIONS:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(duration=minutes)
    await call.message.answer("О чём встреча? Напишите тему одной строкой.")
    await state.set_state(NewRequest.title)
    await call.answer()


@router.message(NewRequest.title, F.text)
async def request_title(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("Слишком коротко. Напишите тему хотя бы в три знака.")
        return

    data = await state.get_data()
    owner = await session.get(User, data.get("owner_id", 0))
    if owner is None:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return

    await state.update_data(title=title)
    now = utcnow()
    limit = await quotas.view(session, owner=owner, subject=user, now=now)
    found = await slot_service.free_slots(
        session, owner=owner, duration_minutes=data["duration"],
        days_ahead=7, participants=[user], limit=MAX_SLOT_BUTTONS, now=now,
    )
    if not found:
        await state.clear()
        await message.answer(
            f"У {esc(owner.full_name)} нет свободных окон на ближайшую неделю.\n"
            "Попробуйте позже или напишите ассистенту."
        )
        return

    head = f"<b>{esc(title)}</b> · {data['duration']} мин\n"
    if not limit.unlimited:
        head += f"{limit.render()}\n"
    await message.answer(
        head + "\nВыберите время:",
        reply_markup=_slots_kb(found, user.timezone, "nm:slot"),
    )
    await state.set_state(NewRequest.slot)


@router.callback_query(NewRequest.slot, F.data.startswith("nm:slot:"))
async def request_slot(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    code = callback_int(call.data)
    data = await state.get_data()
    owner = await session.get(User, data.get("owner_id", 0))
    if code is None or owner is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    outcome = await service.create_request(
        session, initiator=user, owner=owner,
        start_at=_slot_time(code), duration_minutes=data["duration"], title=data["title"],
    )
    if outcome.ok:
        await state.clear()
        when = fmt_dt(outcome.request.start_at, user.timezone)
        note = "\n\n⚠️ Заявка сверх лимита времени." if outcome.request.over_quota else ""
        await call.message.answer(
            f"✅ Заявка отправлена.\n\n<b>{esc(data['title'])}</b>\n"
            f"К кому: {esc(owner.full_name)}\nКогда: {when}\n\n"
            f"Окно за вами, пока руководитель не ответит.{note}"
        )
        await call.answer()
        return

    # Отказ обязан предлагать выход.
    if outcome.alternatives:
        await call.message.answer(
            f"{outcome.reason} Свободно другое время:",
            reply_markup=_slots_kb(outcome.alternatives, user.timezone, "nm:slot"),
        )
    else:
        await state.clear()
        await call.message.answer(f"{outcome.reason}\nПопробуйте выбрать время заново.")
    await call.answer()


# ── Решения по заявкам ──────────────────────────────────────────────────────
@router.callback_query(F.data == "rq:list")
async def request_list(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "meeting.approve"):
        await call.answer("Заявки видит руководитель или его ассистент.", show_alert=True)
        return
    waiting = await service.pending_for(session, user)
    if not waiting:
        await call.answer("Нерешённых заявок нет.", show_alert=True)
        return
    for request in waiting[:5]:
        await call.message.answer(
            await _request_text(session, request, user),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Принять", callback_data=f"rq:ok:{request.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rq:no:{request.id}"),
            ]]),
        )
    await call.answer()


async def _request_text(session: AsyncSession, request: MeetingRequest, viewer: User) -> str:
    who = await session.get(User, request.initiator_id)
    mark = "\n⚠️ Сверх лимита времени" if request.over_quota else ""
    return (
        f"📅 <b>{esc(request.title)}</b>\n\n"
        f"Кто: {esc(who.full_name) if who else 'неизвестно'}\n"
        f"Когда: {fmt_dt(request.start_at, viewer.timezone)} · "
        f"{request.duration_minutes} мин{mark}"
    )


@router.callback_query(F.data.startswith("rq:ok:"))
async def request_approve(
    call: CallbackQuery, session: AsyncSession, user: User
) -> None:
    request_id = callback_int(call.data)
    request = await session.get(MeetingRequest, request_id) if request_id else None
    if request is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    result = await service.approve(session, request=request, actor=user)
    if not result.ok:
        await call.answer(result.reason or "Не получилось.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        f"✅ Встреча в календаре: {fmt_dt(result.meeting.start_at, user.timezone)}"
    )
    await call.answer()


@router.callback_query(F.data.startswith("rq:no:"))
async def request_decline(
    call: CallbackQuery, session: AsyncSession, user: User
) -> None:
    request_id = callback_int(call.data)
    request = await session.get(MeetingRequest, request_id) if request_id else None
    if request is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    done = await service.decline(session, request=request, actor=user, reason="Не сейчас")
    if not done:
        await call.answer("Решение по этой заявке уже принято.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("❌ Заявка отклонена, окно освободилось.")
    await call.answer()


# ── Явка и оценка ───────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mt:here:"))
async def check_in(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    ok, why = await attendance.check_in(session, meeting=meeting, user=user)
    await call.answer("Отметил, спасибо." if ok else (why or "Не получилось."), show_alert=not ok)


@router.callback_query(F.data.startswith("mt:rate:"))
async def rate(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    parts = (call.data or "").split(":")
    meeting_id = callback_int(call.data, 2)
    score = callback_int(call.data, 3)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or score is None or len(parts) != 4:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    ok, why = await attendance.rate(session, meeting=meeting, actor=user, score=score)
    await call.answer(
        attendance.SCORE_LABELS[score] + ", записал." if ok else (why or "Не получилось."),
        show_alert=not ok,
    )


# ── Перенос и отмена ────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mt:move:"))
async def move_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    if not has_permission(grants, "meeting.reschedule"):
        await call.answer("Переносить встречи может руководитель или его ассистент.", show_alert=True)
        return

    owner = await session.get(User, meeting.owner_id)
    duration = int((meeting.end_at - meeting.start_at).total_seconds() // 60)
    found = await slot_service.free_slots(
        session, owner=owner, duration_minutes=duration,
        days_ahead=7, limit=MAX_SLOT_BUTTONS,
    )
    if not found:
        await call.answer("Свободных окон на неделю вперёд нет.", show_alert=True)
        return

    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(
        f"<b>{esc(meeting.title)}</b>\n\nНа какое время переносим?",
        reply_markup=_slots_kb(found, user.timezone, "mt:mvto"),
    )
    await state.set_state(MoveMeeting.when)
    await call.answer()


@router.callback_query(MoveMeeting.when, F.data.startswith("mt:mvto:"))
async def move_pick(call: CallbackQuery, state: FSMContext) -> None:
    code = callback_int(call.data)
    if code is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(new_start=code)
    await call.message.answer(
        "Почему переносим? Причину увидят все участники — без неё перенос выглядит как каприз."
    )
    await state.set_state(MoveMeeting.reason)
    await call.answer()


@router.message(MoveMeeting.reason, F.text)
async def move_finish(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None or "new_start" not in data:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return

    result = await service.reschedule(
        session, meeting=meeting, actor=user,
        new_start=_slot_time(data["new_start"]), reason=message.text or "",
    )
    await state.clear()
    if not result.ok:
        await message.answer(f"{result.reason}\nОткройте карточку встречи и попробуйте снова.")
        return
    await message.answer(
        f"🔄 Перенесено на {fmt_dt(meeting.start_at, user.timezone)}.\nУчастники уведомлены."
    )


@router.callback_query(F.data.startswith("mt:kill:"))
async def kill_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    if not has_permission(grants, "meeting.cancel"):
        await call.answer("Отменять встречи может руководитель или его ассистент.", show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer("Почему отменяем? Причину увидят все участники.")
    await state.set_state(KillMeeting.reason)
    await call.answer()


@router.message(KillMeeting.reason, F.text)
async def kill_finish(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return
    result = await service.cancel(
        session, meeting=meeting, actor=user, reason=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or "Не получилось отменить.")
        return
    await message.answer("🚫 Встреча отменена, участники уведомлены, время свободно.")


# ── Быстрое совещание ───────────────────────────────────────────────────────
@router.message(F.text == BTN_QUICK_MEETING)
async def quick_start(
    message: Message, state: FSMContext, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "meeting.create"):
        await message.answer("Собирать совещания может руководитель, ассистент или начальник отдела.")
        return
    await state.clear()
    await message.answer("<b>Быстрое совещание</b>\n\nТема?")
    await state.set_state(Quick.title)


@router.message(Quick.title, F.text)
async def quick_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("Слишком коротко. Напишите тему хотя бы в три знака.")
        return
    await state.update_data(title=title, people=[])
    await message.answer(
        "Когда собираемся?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Через 15 мин", callback_data="qm:in:15"),
            InlineKeyboardButton(text="30 мин", callback_data="qm:in:30"),
            InlineKeyboardButton(text="1 час", callback_data="qm:in:60"),
        ]]),
    )
    await state.set_state(Quick.when)


@router.callback_query(Quick.when, F.data.startswith("qm:in:"))
async def quick_when(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User,
) -> None:
    delay = callback_int(call.data)
    if delay is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(delay=delay)

    people = list(
        (
            await session.execute(
                select(User).where(
                    User.organization_id == organization.id,
                    User.status == UserStatus.ACTIVE,
                    User.id != user.id,
                ).order_by(User.full_name).limit(20)
            )
        ).scalars().all()
    )
    if not people:
        await state.clear()
        await call.message.answer("Некого собирать: в организации нет других активных сотрудников.")
        await call.answer()
        return

    await state.update_data(candidates=[p.id for p in people])
    await call.message.answer("Кого зовём? Отмечайте по одному, потом «Собрать».",
                              reply_markup=_people_kb([], people))
    await state.set_state(Quick.people)
    await call.answer()


def _people_kb(chosen: list[int], people: list[User]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if p.id in chosen else "") + cut(p.full_name, 28),
            callback_data=f"qm:who:{p.id}",
        )]
        for p in people[:7]
    ]
    if chosen:
        rows.append([InlineKeyboardButton(
            text=f"📣 Собрать ({len(chosen)})", callback_data="qm:go"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(Quick.people, F.data.startswith("qm:who:"))
async def quick_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    person_id = callback_int(call.data)
    data = await state.get_data()
    if person_id is None or person_id not in data.get("candidates", []):
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    chosen = list(data.get("people", []))
    if person_id in chosen:
        chosen.remove(person_id)
    else:
        chosen.append(person_id)
    await state.update_data(people=chosen)

    people = list(
        (
            await session.execute(
                select(User).where(User.id.in_(data["candidates"])).order_by(User.full_name)
            )
        ).scalars().all()
    )
    await call.message.edit_reply_markup(reply_markup=_people_kb(chosen, people))
    await call.answer()


@router.callback_query(Quick.people, F.data == "qm:go")
async def quick_go(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    chosen = data.get("people", [])
    if not chosen:
        await call.answer("Никого не выбрали.", show_alert=True)
        return

    start_at = utcnow() + timedelta(minutes=data["delay"])
    result = await service.quick(
        session, organizer=user, participant_ids=chosen,
        title=data["title"], start_at=start_at,
    )
    await state.clear()
    if not result.ok:
        await call.message.answer(result.reason or "Не получилось собрать совещание.")
        await call.answer()
        return
    await call.message.answer(
        f"📣 Собрано на {fmt_dt(result.meeting.start_at, user.timezone)}.\n"
        f"Приглашения ушли: {len(chosen)}."
    )
    await call.answer()

# ── Итоги встречи: повестка, завершение, решение, поручение, документы ──────
class AgendaInput(StatesGroup):
    title = State()


class DecisionInput(StatesGroup):
    title = State()


class TaskFromMeeting(StatesGroup):
    assignee = State()
    title = State()


async def _meeting_or_none(session: AsyncSession, call: CallbackQuery, user: User):
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        return None
    return meeting


@router.callback_query(F.data.startswith("mt:agenda:"))
async def agenda_show(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    items = await registry.agenda_of(session, meeting)
    lines = [f"📋 <b>Повестка</b>\n{esc(cut(meeting.title, 60))}", ""]
    if items:
        lines += [
            f"{'✅' if item.covered else '▫️'} {item.position}. {esc(item.title)}"
            for item in items
        ]
    else:
        lines.append("Пунктов пока нет.")

    rows = []
    if meeting.status not in (MeetingStatus.FINISHED, MeetingStatus.CANCELLED):
        rows.append([InlineKeyboardButton(
            text="➕ Пункт повестки", callback_data=f"mt:agadd:{meeting.id}"
        )])
    for item in items:
        if not item.covered:
            rows.append([InlineKeyboardButton(
                text=f"✅ {cut(item.title, 30)}", callback_data=f"mt:agok:{item.id}"
            )])
    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:6]) if rows else None,
    )
    await call.answer()


@router.callback_query(F.data.startswith("mt:agadd:"))
async def agenda_add(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer("Что обсуждаем? Напишите пункт повестки.")
    await state.set_state(AgendaInput.title)
    await call.answer()


@router.message(AgendaInput.title, F.text)
async def agenda_save(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return
    result = await registry.add_agenda_item(
        session, meeting=meeting, actor=user, title=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or "Не получилось добавить пункт.")
        return
    await message.answer(f"📋 Пункт {result.item.position}: {esc(result.item.title)}")


@router.callback_query(F.data.startswith("mt:agok:"))
async def agenda_cover(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    item = await session.get(AgendaItem, callback_int(call.data) or 0)
    meeting = await session.get(Meeting, item.meeting_id) if item else None
    if item is None or meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    result = await registry.mark_covered(session, item=item, meeting=meeting, actor=user)
    if not result.ok:
        await call.answer(result.reason or "Не получилось.", show_alert=True)
        return
    await call.answer("Отмечено.")


@router.callback_query(F.data.startswith("mt:done:"))
async def finish_meeting(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    result = await service.finish(session, meeting=meeting, actor=user)
    if not result.ok:
        await call.answer(result.reason or "Не получилось.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        "🏁 Встреча завершена.\n\nЗафиксируйте итоги, пока помните:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📌 Решение", callback_data=f"mt:dec:{meeting.id}"),
            InlineKeyboardButton(text="➕ Поручение", callback_data=f"mt:task:{meeting.id}"),
        ]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("mt:dec:"))
async def decision_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer("Что решили? Сформулируйте одной строкой.")
    await state.set_state(DecisionInput.title)
    await call.answer()


@router.message(DecisionInput.title, F.text)
async def decision_save(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    result = await registry.create(
        session, actor=user, title=message.text or "", meeting=meeting
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or "Не получилось внести решение.")
        return
    await message.answer(
        f"📌 Записано: <b>{esc(result.item.title)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📌 Ещё решение", callback_data=f"mt:dec:{meeting.id}"
            ),
            InlineKeyboardButton(
                text="➕ Поручение", callback_data=f"mt:task:{meeting.id}"
            ),
        ]]) if meeting else None,
    )


@router.callback_query(F.data.startswith("mt:task:"))
async def task_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    if not has_permission(grants, "task.create"):
        await call.answer("Создавать поручения вам нельзя.", show_alert=True)
        return

    people = [p for p in await service.participants_of(session, meeting) if p.id != user.id]
    if not people:
        await call.answer("Кроме вас на встрече никого не было.", show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(
        "Кому поручаем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"mt:tsto:{p.id}")]
            for p in people[:7]
        ]),
    )
    await state.set_state(TaskFromMeeting.assignee)
    await call.answer()


@router.callback_query(TaskFromMeeting.assignee, F.data.startswith("mt:tsto:"))
async def task_assignee(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    person = await session.get(User, callback_int(call.data) or 0)
    if person is None or person.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(assignee_id=person.id)
    await call.message.answer(
        f"Поручение для {esc(person.full_name)}.\n\nЧто сделать? Можно со сроком: "
        "«подготовить смету до пятницы»."
    )
    await state.set_state(TaskFromMeeting.title)
    await call.answer()


@router.message(TaskFromMeeting.title, F.text)
async def task_save(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    assignee = await session.get(User, data.get("assignee_id", 0))
    if meeting is None or assignee is None:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return

    title, due_at = parse_due(message.text or "", assignee.timezone)
    try:
        task = await task_service.create_task(
            session, creator=user, assignee=assignee, title=title,
            due_at=due_at, meeting_id=meeting.id,
        )
    except TaskError as error:
        await state.clear()
        await message.answer(str(error))
        return

    await state.clear()
    when = f"\nСрок: {humanize_due(due_at, user.timezone)}" if due_at else ""
    await message.answer(
        f"➕ Поручение для {esc(assignee.full_name)}:\n<b>{esc(task.title)}</b>{when}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="➕ Ещё поручение", callback_data=f"mt:task:{meeting.id}"
            ),
        ]]),
    )


@router.callback_query(F.data.startswith("mt:files:"))
async def meeting_files(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    files = await document_service.for_meeting(session, meeting=meeting, viewer=user)
    if not files:
        await call.answer(
            "Документов, открытых вам, у этой встречи нет.\n"
            "Пришлите файл боту, чтобы приложить свой.",
            show_alert=True,
        )
        return
    await call.message.answer(
        f"📎 <b>Документы встречи</b>\n{esc(cut(meeting.title, 60))}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=cut(f.title or f.file_name, 40), callback_data=f"dc:card:{f.id}"
            )]
            for f in files[:7]
        ]),
    )
    await call.answer()
