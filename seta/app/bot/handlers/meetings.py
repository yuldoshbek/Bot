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
    MENU_MY_DAY,
    MENU_MY_MEETINGS,
    MENU_QUICK_MEETING,
    MENU_REQUEST_MEETING,
    MenuButton,
)
from app.bot.utils import callback_int
from app.core.config import settings
from app.core.i18n import t
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, to_local, utcnow
from app.models.enums import MeetingStatus, RequestStatus, RoleCode, UserStatus
from app.models.decision import AgendaItem
from app.models.meeting import Meeting, MeetingParticipant, MeetingRequest
from app.models.org import Organization
from app.models.user import User
from app.models.rbac import Role, UserRole
from app.core.dates import humanize_due, parse_due
from app.services import attendance, dashboard, meetings as service, quotas
from app.services import features as feature_service
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
    meeting: Meeting, grants: dict[str, Grant], now: datetime, locale: str,
    *, is_participant: bool,
) -> InlineKeyboardMarkup:
    """Кнопки карточки — только те, что этому человеку сейчас доступны."""
    rows: list[list[InlineKeyboardButton]] = []
    live = meeting.status != MeetingStatus.CANCELLED

    # Отмечается участник. Ассистенту, который видит встречу по области права,
    # эта кнопка не нужна и не работает: за других явку правят отдельно.
    if is_participant and live and now < meeting.end_at:
        if now >= meeting.start_at - timedelta(minutes=attendance.CHECKIN_OPENS_MINUTES):
            rows.append([InlineKeyboardButton(
                text=t("meeting.action.here", locale), callback_data=f"mt:here:{meeting.id}"
            )])

    if live and has_permission(grants, "meeting.reschedule"):
        rows.append([
            InlineKeyboardButton(text=t("meeting.action.move", locale),
                                 callback_data=f"mt:move:{meeting.id}"),
            InlineKeyboardButton(text=t("meeting.action.cancel", locale),
                                 callback_data=f"mt:kill:{meeting.id}"),
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
            text=t("meeting.action.decision", locale), callback_data=f"mt:dec:{meeting.id}"
        ))
    if has_permission(grants, "task.create"):
        outcome_row.append(InlineKeyboardButton(
            text=t("meeting.action.task", locale), callback_data=f"mt:task:{meeting.id}"
        ))
    if outcome_row:
        rows.append(outcome_row)

    # Протокол — только по прошедшей встрече и только при включённом ИИ:
    # черновик собирается из повестки и того, что уже записано, а до конца
    # встречи ни того, ни другого ещё нет.
    if (
        settings.ai_enabled
        and live
        and now >= meeting.end_at
        and has_permission(grants, "decision.create")
    ):
        rows.append([InlineKeyboardButton(
            text=t("meeting.action.protocol", locale),
            callback_data=f"mt:proto:{meeting.id}",
        )])

    tail = [InlineKeyboardButton(text=t("meeting.action.files", locale),
                                 callback_data=f"mt:files:{meeting.id}")]
    if has_permission(grants, "meeting.finish"):
        tail.append(InlineKeyboardButton(
            text=t("meeting.action.agenda", locale), callback_data=f"mt:agenda:{meeting.id}"
        ))
    rows.append(tail)

    if (
        live
        and meeting.status != MeetingStatus.FINISHED
        and now >= meeting.start_at
        and has_permission(grants, "meeting.finish")
    ):
        rows.append([InlineKeyboardButton(
            text=t("meeting.action.finish", locale), callback_data=f"mt:done:{meeting.id}"
        )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _card_text(
    session: AsyncSession, meeting: Meeting, viewer: User, people: list[User], locale: str
) -> str:
    owner = await session.get(User, meeting.owner_id)
    lines = [
        f"<b>{esc(meeting.title)}</b>",
        "",
        f"🕐 {fmt_dt(meeting.start_at, viewer.timezone)}"
        f"–{to_local(meeting.end_at, viewer.timezone).strftime('%H:%M')}",
        f"👤 {t('meeting.card.host', locale)}: "
        f"{esc(owner.full_name) if owner else t('meeting.card.unknown', locale)}",
    ]
    if len(people) > 1:
        # Сначала обрезаем, потом экранируем: обратный порядок режет строку
        # посреди `&lt;`, и Telegram не доставляет сообщение целиком.
        names = ", ".join(p.full_name for p in people if p.id != meeting.owner_id)
        lines.append(f"👥 {t('meeting.card.participants', locale)}: {esc(cut(names, 200))}")
    if meeting.status == MeetingStatus.CANCELLED:
        reason = esc(meeting.cancel_reason or "") or t("meeting.card.no_reason", locale)
        lines.append("\n" + t("meeting.card.cancelled", locale, reason=reason))
    elif meeting.reschedule_count:
        lines.append("\n" + t("meeting.card.moved", locale, count=meeting.reschedule_count))

    if meeting.status == MeetingStatus.FINISHED:
        made_decisions, made_tasks = await registry.meeting_outcome(session, meeting)
        if made_decisions or made_tasks:
            lines.append("\n" + t("meeting.card.outcome", locale,
                                  decisions=made_decisions, tasks=made_tasks))
        else:
            # Встреча без результата — не обвинение, а факт, который стоит видеть.
            lines.append("\n" + t("meeting.card.finished", locale))
    return "\n".join(lines)


# ── Мой день ────────────────────────────────────────────────────────────────
def _day_kb(board: dashboard.Board, locale: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📅 " + cut(m.title, 30), callback_data=f"mt:card:{m.id}")]
        for m in (board.running + board.ahead)[:4]
    ]
    if board.requests_waiting:
        rows.append([InlineKeyboardButton(
            text=t("meeting.card.requests", locale, count=board.requests_waiting),
            callback_data="rq:list",
        )])
    if board.to_review:
        rows.append([InlineKeyboardButton(
            text=t("meeting.card.in_review", locale, count=board.to_review),
            callback_data="tl:review",
        )])
    if board.overdue_total:
        rows.append([InlineKeyboardButton(
            text=t("meeting.card.overdue", locale, count=board.overdue_total),
            callback_data="tl:overdue",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(MenuButton(MENU_MY_DAY))
async def my_day(
    message: Message, session: AsyncSession, user: User,
    grants: dict[str, Grant], features: dict[str, bool], locale: str,
) -> None:
    # Выключенный раздел закрывается здесь, а не только в меню: кнопка,
    # отправленная час назад, всё ещё лежит в истории чата и нажимается.
    if not feature_service.is_on(features, "meetings"):
        await message.answer(t("feature.off", locale))
        return
    board = await dashboard.build(
        session, viewer=user, grants=grants, features=features
    )
    # Текст собирает служба: этот же экран уходит утренней сводкой, и два
    # описания одного экрана разошлись бы молча.
    await message.answer(dashboard.render(board, locale=locale),
                         reply_markup=_day_kb(board, locale))


@router.message(MenuButton(MENU_MY_MEETINGS))
async def my_meetings(
    message: Message, session: AsyncSession, user: User,
    features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "meetings"):
        await message.answer(t("feature.off", locale))
        return
    now = utcnow()
    upcoming = await _my_meetings(session, user, since=now, until=now + timedelta(days=14))
    if not upcoming:
        await message.answer(t("meeting.mine.empty", locale))
        return

    lines = [f"<b>{t('meeting.mine.title', locale)}</b>", ""]
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
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    people = await service.participants_of(session, meeting)
    await call.message.answer(
        await _card_text(session, meeting, user, people, locale),
        reply_markup=_card_kb(
            meeting, grants, utcnow(), locale,
            is_participant=any(p.id == user.id for p in people),
        ),
    )
    await call.answer()


# ── Запрос встречи ──────────────────────────────────────────────────────────
@router.message(MenuButton(MENU_REQUEST_MEETING))
async def request_start(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant],
    features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "meetings"):
        await message.answer(t("feature.off", locale))
        return
    if not has_permission(grants, "calendar.read_free"):
        await message.answer(t("meeting.request.no_rights", locale))
        return

    people = [p for p in await _executives(session, organization.id) if p.id != user.id]
    if not people:
        await message.answer(t("meeting.request.no_host", locale))
        return

    await state.clear()
    if len(people) == 1:
        await state.update_data(owner_id=people[0].id)
        await _ask_duration(message, state, people[0], locale)
        return

    await message.answer(
        f"<b>{t('meeting.request.title', locale)}</b>\n\n"
        f"{t('meeting.request.to_whom', locale)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"nm:who:{p.id}")]
            for p in people[:7]
        ]),
    )
    await state.set_state(NewRequest.owner)


async def _ask_duration(
    message: Message, state: FSMContext, owner: User, locale: str
) -> None:
    await message.answer(
        f"{t('meeting.request.to', locale)}: <b>{esc(owner.full_name)}</b>\n\n"
        f"{t('meeting.request.how_long', locale)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"{d}{t('metric.unit.minutes', locale)}",
                callback_data=f"nm:len:{d}",
            )
            for d in DURATIONS
        ]]),
    )
    await state.set_state(NewRequest.duration)


@router.callback_query(NewRequest.owner, F.data.startswith("nm:who:"))
async def request_owner(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    owner_id = callback_int(call.data)
    owner = await session.get(User, owner_id) if owner_id else None
    if owner is None or owner.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(owner_id=owner.id)
    await _ask_duration(call.message, state, owner, locale)
    await call.answer()


@router.callback_query(NewRequest.duration, F.data.startswith("nm:len:"))
async def request_duration(
    call: CallbackQuery, state: FSMContext, locale: str
) -> None:
    minutes = callback_int(call.data)
    if minutes not in DURATIONS:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(duration=minutes)
    await call.message.answer(t("meeting.request.ask_topic", locale))
    await state.set_state(NewRequest.title)
    await call.answer()


@router.message(NewRequest.title, F.text)
async def request_title(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer(t("meeting.request.topic_short", locale))
        return

    data = await state.get_data()
    owner = await session.get(User, data.get("owner_id", 0))
    if owner is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
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
            t("meeting.request.no_slots", locale, name=esc(owner.full_name))
        )
        return

    minutes = f"{data['duration']}{t('metric.unit.minutes', locale)}"
    head = f"<b>{esc(title)}</b> · {minutes}\n"
    if not limit.unlimited:
        head += f"{limit.render(locale)}\n"
    await message.answer(
        head + "\n" + t("meeting.request.choose_time", locale),
        reply_markup=_slots_kb(found, user.timezone, "nm:slot"),
    )
    await state.set_state(NewRequest.slot)


@router.callback_query(NewRequest.slot, F.data.startswith("nm:slot:"))
async def request_slot(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    code = callback_int(call.data)
    data = await state.get_data()
    owner = await session.get(User, data.get("owner_id", 0))
    if code is None or owner is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    outcome = await service.create_request(
        session, initiator=user, owner=owner,
        start_at=_slot_time(code), duration_minutes=data["duration"], title=data["title"],
    )
    if outcome.ok:
        await state.clear()
        when = fmt_dt(outcome.request.start_at, user.timezone)
        note = (
            "\n\n" + t("meeting.request.over_quota", locale)
            if outcome.request.over_quota else ""
        )
        await call.message.answer(
            f"{t('meeting.request.sent_title', locale)}\n\n"
            f"<b>{esc(data['title'])}</b>\n"
            f"{t('meeting.request.to', locale)}: {esc(owner.full_name)}\n"
            f"{t('meeting.request.when', locale)}: {when}\n\n"
            f"{t('meeting.request.held', locale)}{note}"
        )
        await call.answer()
        return

    # Отказ обязан предлагать выход.
    if outcome.alternatives:
        await call.message.answer(
            t("meeting.request.free_other", locale, reason=outcome.reason),
            reply_markup=_slots_kb(outcome.alternatives, user.timezone, "nm:slot"),
        )
    else:
        await state.clear()
        await call.message.answer(
            f"{outcome.reason}\n{t('meeting.request.retry', locale)}"
        )
    await call.answer()


# ── Решения по заявкам ──────────────────────────────────────────────────────
@router.callback_query(F.data == "rq:list")
async def request_list(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not has_permission(grants, "meeting.approve"):
        await call.answer(t("meeting.request.inbox_rights", locale), show_alert=True)
        return
    waiting = await service.pending_for(session, user)
    if not waiting:
        await call.answer(t("meeting.request.inbox_empty", locale), show_alert=True)
        return
    for request in waiting[:5]:
        await call.message.answer(
            await _request_text(session, request, user, locale),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t("meeting.request.accept", locale),
                                     callback_data=f"rq:ok:{request.id}"),
                InlineKeyboardButton(text=t("meeting.request.decline", locale),
                                     callback_data=f"rq:no:{request.id}"),
            ]]),
        )
    await call.answer()


async def _request_text(
    session: AsyncSession, request: MeetingRequest, viewer: User, locale: str
) -> str:
    who = await session.get(User, request.initiator_id)
    mark = (
        "\n" + t("meeting.request.over_quota_short", locale)
        if request.over_quota else ""
    )
    name = esc(who.full_name) if who else t("meeting.card.unknown", locale)
    length = t("meeting.request.duration", locale, minutes=request.duration_minutes)
    return (
        f"📅 <b>{esc(request.title)}</b>\n\n"
        f"{t('meeting.request.who', locale)}: {name}\n"
        f"{t('meeting.request.when', locale)}: "
        f"{fmt_dt(request.start_at, viewer.timezone)} · {length}{mark}"
    )


@router.callback_query(F.data.startswith("rq:ok:"))
async def request_approve(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    request_id = callback_int(call.data)
    request = await session.get(MeetingRequest, request_id) if request_id else None
    if request is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    result = await service.approve(session, request=request, actor=user)
    if not result.ok:
        await call.answer(result.reason or t("meeting.failed", locale), show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(t(
        "meeting.request.in_calendar", locale,
        when=fmt_dt(result.meeting.start_at, user.timezone),
    ))
    await call.answer()


@router.callback_query(F.data.startswith("rq:no:"))
async def request_decline(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    request_id = callback_int(call.data)
    request = await session.get(MeetingRequest, request_id) if request_id else None
    if request is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    # Причина уходит в запись и в журнал, поэтому берётся на языке заявителя,
    # а не того, кто нажал кнопку: читать её будет он.
    initiator = await session.get(User, request.initiator_id)
    reason = t("meeting.request.not_now", initiator.locale if initiator else locale)
    done = await service.decline(session, request=request, actor=user, reason=reason)
    if not done:
        await call.answer(t("meeting.request.already_decided", locale), show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(t("meeting.request.declined_done", locale))
    await call.answer()


# ── Явка и оценка ───────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mt:here:"))
async def check_in(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    ok, why = await attendance.check_in(session, meeting=meeting, user=user)
    await call.answer(
        t("meeting.checkin.ok", locale) if ok else (why or t("meeting.failed", locale)),
        show_alert=not ok,
    )


@router.callback_query(F.data.startswith("mt:rate:"))
async def rate(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    parts = (call.data or "").split(":")
    meeting_id = callback_int(call.data, 2)
    score = callback_int(call.data, 3)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or score is None or len(parts) != 4:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    ok, why = await attendance.rate(session, meeting=meeting, actor=user, score=score)
    said = t(attendance.SCORE_KEYS[score], locale)
    await call.answer(
        t("meeting.rated", locale, score=said) if ok
        else (why or t("meeting.failed", locale)),
        show_alert=not ok,
    )


# ── Перенос и отмена ────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mt:move:"))
async def move_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    if not has_permission(grants, "meeting.reschedule"):
        await call.answer(t("meeting.move.no_rights", locale), show_alert=True)
        return

    owner = await session.get(User, meeting.owner_id)
    duration = int((meeting.end_at - meeting.start_at).total_seconds() // 60)
    found = await slot_service.free_slots(
        session, owner=owner, duration_minutes=duration,
        days_ahead=7, limit=MAX_SLOT_BUTTONS,
    )
    if not found:
        await call.answer(t("meeting.move.no_slots", locale), show_alert=True)
        return

    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(
        f"<b>{esc(meeting.title)}</b>\n\n{t('meeting.move.ask_time', locale)}",
        reply_markup=_slots_kb(found, user.timezone, "mt:mvto"),
    )
    await state.set_state(MoveMeeting.when)
    await call.answer()


@router.callback_query(MoveMeeting.when, F.data.startswith("mt:mvto:"))
async def move_pick(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    code = callback_int(call.data)
    if code is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(new_start=code)
    await call.message.answer(t("meeting.move.ask_reason", locale))
    await state.set_state(MoveMeeting.reason)
    await call.answer()


@router.message(MoveMeeting.reason, F.text)
async def move_finish(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None or "new_start" not in data:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return

    result = await service.reschedule(
        session, meeting=meeting, actor=user,
        new_start=_slot_time(data["new_start"]), reason=message.text or "",
    )
    await state.clear()
    if not result.ok:
        await message.answer(f"{result.reason}\n{t('meeting.move.reopen', locale)}")
        return
    await message.answer(t(
        "meeting.move.done", locale, when=fmt_dt(meeting.start_at, user.timezone)
    ))


@router.callback_query(F.data.startswith("mt:kill:"))
async def kill_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    if not has_permission(grants, "meeting.cancel"):
        await call.answer(t("meeting.cancel.no_rights", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(t("meeting.cancel.ask_reason", locale))
    await state.set_state(KillMeeting.reason)
    await call.answer()


@router.message(KillMeeting.reason, F.text)
async def kill_finish(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return
    result = await service.cancel(
        session, meeting=meeting, actor=user, reason=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or t("meeting.cancel.failed", locale))
        return
    await message.answer(t("meeting.cancel.done", locale))


# ── Быстрое совещание ───────────────────────────────────────────────────────
@router.message(MenuButton(MENU_QUICK_MEETING))
async def quick_start(
    message: Message, state: FSMContext, grants: dict[str, Grant],
    features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "meetings"):
        await message.answer(t("feature.off", locale))
        return
    if not has_permission(grants, "meeting.create"):
        await message.answer(t("meeting.quick.no_rights", locale))
        return
    await state.clear()
    await message.answer(
        f"<b>{t('meeting.quick.title', locale)}</b>\n\n"
        f"{t('meeting.quick.ask_topic', locale)}"
    )
    await state.set_state(Quick.title)


@router.message(Quick.title, F.text)
async def quick_title(message: Message, state: FSMContext, locale: str) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer(t("meeting.request.topic_short", locale))
        return
    await state.update_data(title=title, people=[])
    await message.answer(
        t("meeting.quick.when", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("meeting.quick.in_15", locale),
                                 callback_data="qm:in:15"),
            InlineKeyboardButton(text=t("meeting.quick.in_30", locale),
                                 callback_data="qm:in:30"),
            InlineKeyboardButton(text=t("meeting.quick.in_60", locale),
                                 callback_data="qm:in:60"),
        ]]),
    )
    await state.set_state(Quick.when)


@router.callback_query(Quick.when, F.data.startswith("qm:in:"))
async def quick_when(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, locale: str,
) -> None:
    delay = callback_int(call.data)
    if delay is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
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
        await call.message.answer(t("meeting.quick.nobody", locale))
        await call.answer()
        return

    await state.update_data(candidates=[p.id for p in people])
    await call.message.answer(t("meeting.quick.choose", locale),
                              reply_markup=_people_kb([], people, locale))
    await state.set_state(Quick.people)
    await call.answer()


def _people_kb(chosen: list[int], people: list[User], locale: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if p.id in chosen else "") + cut(p.full_name, 28),
            callback_data=f"qm:who:{p.id}",
        )]
        for p in people[:7]
    ]
    if chosen:
        rows.append([InlineKeyboardButton(
            text=t("meeting.quick.gather", locale, count=len(chosen)),
            callback_data="qm:go",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(Quick.people, F.data.startswith("qm:who:"))
async def quick_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, locale: str
) -> None:
    person_id = callback_int(call.data)
    data = await state.get_data()
    if person_id is None or person_id not in data.get("candidates", []):
        await call.answer(t("error.stale_button", locale), show_alert=True)
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
    await call.message.edit_reply_markup(reply_markup=_people_kb(chosen, people, locale))
    await call.answer()


@router.callback_query(Quick.people, F.data == "qm:go")
async def quick_go(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    chosen = data.get("people", [])
    if not chosen:
        await call.answer(t("meeting.quick.nobody_chosen", locale), show_alert=True)
        return

    start_at = utcnow() + timedelta(minutes=data["delay"])
    result = await service.quick(
        session, organizer=user, participant_ids=chosen,
        title=data["title"], start_at=start_at,
    )
    await state.clear()
    if not result.ok:
        await call.message.answer(result.reason or t("meeting.quick.failed", locale))
        await call.answer()
        return
    await call.message.answer(t(
        "meeting.quick.done", locale,
        when=fmt_dt(result.meeting.start_at, user.timezone), count=len(chosen),
    ))
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
    """Встреча из нажатой кнопки — если она вообще открыта этому человеку.

    Совпадение организации само по себе доступа не даёт: у рядового сотрудника
    область права `meeting.read` — «только свои», и номер чужой встречи в
    callback не должен открывать ни тему, ни состав участников.
    """
    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None:
        return None
    if not await service.may_read(session, meeting=meeting, viewer=user):
        return None
    return meeting


@router.callback_query(F.data.startswith("mt:agenda:"))
async def agenda_show(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    items = await registry.agenda_of(session, meeting)
    lines = [
        f"📋 <b>{t('meeting.agenda.title', locale)}</b>\n{esc(cut(meeting.title, 60))}",
        "",
    ]
    if items:
        lines += [
            f"{'✅' if item.covered else '▫️'} {item.position}. {esc(item.title)}"
            for item in items
        ]
    else:
        lines.append(t("meeting.agenda.empty", locale))

    rows = []
    if meeting.status not in (MeetingStatus.FINISHED, MeetingStatus.CANCELLED):
        rows.append([InlineKeyboardButton(
            text=t("meeting.agenda.add", locale), callback_data=f"mt:agadd:{meeting.id}"
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
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(t("meeting.agenda.ask", locale))
    await state.set_state(AgendaInput.title)
    await call.answer()


@router.message(AgendaInput.title, F.text)
async def agenda_save(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    if meeting is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return
    result = await registry.add_agenda_item(
        session, meeting=meeting, actor=user, title=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or t("meeting.agenda.failed", locale))
        return
    await message.answer(
        t("meeting.agenda.added", locale, number=result.item.position)
        + f": {esc(result.item.title)}"
    )


@router.callback_query(F.data.startswith("mt:agok:"))
async def agenda_cover(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    item = await session.get(AgendaItem, callback_int(call.data) or 0)
    meeting = await session.get(Meeting, item.meeting_id) if item else None
    if item is None or meeting is None or meeting.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    result = await registry.mark_covered(session, item=item, meeting=meeting, actor=user)
    if not result.ok:
        await call.answer(result.reason or t("meeting.failed", locale), show_alert=True)
        return
    await call.answer(t("meeting.marked", locale))


@router.callback_query(F.data.startswith("mt:done:"))
async def finish_meeting(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    result = await service.finish(session, meeting=meeting, actor=user)
    if not result.ok:
        await call.answer(result.reason or t("meeting.failed", locale), show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        t("meeting.finish.done", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("meeting.action.decision", locale),
                                 callback_data=f"mt:dec:{meeting.id}"),
            InlineKeyboardButton(text=t("meeting.action.task", locale),
                                 callback_data=f"mt:task:{meeting.id}"),
        ]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("mt:dec:"))
async def decision_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(t("meeting.decision.ask", locale))
    await state.set_state(DecisionInput.title)
    await call.answer()


@router.message(DecisionInput.title, F.text)
async def decision_save(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    result = await registry.create(
        session, actor=user, title=message.text or "", meeting=meeting
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or t("meeting.decision.failed", locale))
        return
    await message.answer(
        t("meeting.decision.saved", locale, title=esc(result.item.title)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=t("meeting.decision.more", locale),
                callback_data=f"mt:dec:{meeting.id}",
            ),
            InlineKeyboardButton(
                text=t("meeting.action.task", locale),
                callback_data=f"mt:task:{meeting.id}",
            ),
        ]]) if meeting else None,
    )


@router.callback_query(F.data.startswith("mt:task:"))
async def task_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    if not has_permission(grants, "task.create"):
        await call.answer(t("meeting.task.no_rights", locale), show_alert=True)
        return

    people = [p for p in await service.participants_of(session, meeting) if p.id != user.id]
    if not people:
        await call.answer(t("meeting.task.alone", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(meeting_id=meeting.id)
    await call.message.answer(
        t("task.new.ask_assignee", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"mt:tsto:{p.id}")]
            for p in people[:7]
        ]),
    )
    await state.set_state(TaskFromMeeting.assignee)
    await call.answer()


@router.callback_query(TaskFromMeeting.assignee, F.data.startswith("mt:tsto:"))
async def task_assignee(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    person = await session.get(User, callback_int(call.data) or 0)
    if person is None or person.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(assignee_id=person.id)
    await call.message.answer(
        t("meeting.task.for", locale, name=esc(person.full_name))
        + ".\n\n" + t("meeting.task.ask", locale)
    )
    await state.set_state(TaskFromMeeting.title)
    await call.answer()


@router.message(TaskFromMeeting.title, F.text)
async def task_save(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    meeting = await session.get(Meeting, data.get("meeting_id", 0))
    assignee = await session.get(User, data.get("assignee_id", 0))
    if meeting is None or assignee is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
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
    when = (
        f"\n{t('task.field.due', locale)}: "
        f"{humanize_due(due_at, user.timezone, locale)}" if due_at else ""
    )
    await message.answer(
        t("meeting.task.button_for", locale, name=esc(assignee.full_name))
        + f":\n<b>{esc(task.title)}</b>{when}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=t("meeting.task.more", locale),
                callback_data=f"mt:task:{meeting.id}",
            ),
        ]]),
    )


@router.callback_query(F.data.startswith("mt:files:"))
async def meeting_files(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    meeting = await _meeting_or_none(session, call, user)
    if meeting is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    files = await document_service.for_meeting(session, meeting=meeting, viewer=user)
    if not files:
        await call.answer(t("meeting.files.empty", locale), show_alert=True)
        return
    await call.message.answer(
        f"{t('meeting.files.title', locale)}\n{esc(cut(meeting.title, 60))}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=cut(f.title or f.file_name, 40), callback_data=f"dc:card:{f.id}"
            )]
            for f in files[:7]
        ]),
    )
    await call.answer()
