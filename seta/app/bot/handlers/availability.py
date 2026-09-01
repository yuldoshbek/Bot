"""Индикатор доступности руководителя.

Одно касание: руководитель сообщает, что он на месте и готов принимать.
Сотрудники видят это в разделе «Кто на связи» и обращаются без заявки.
"""
from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import BTN_AVAILABILITY, BTN_WHO_IS_OPEN, availability_kb
from app.bot.utils import STALE_BUTTON, callback_int
from app.core.config import settings
from app.core.text import esc
from app.core.timeutil import fmt_time, parse_hhmm, to_local, utcnow
from app.models.enums import Availability, RoleCode
from app.models.org import Organization
from app.models.user import User
from app.services.availability import get_view, open_executives, set_state
from app.services.rbac import Grant, has_permission

router = Router(name="availability")


def _minutes_until_end_of_day(user: User) -> int:
    """Сколько минут осталось до конца рабочего дня в часовом поясе сотрудника."""
    now_local = to_local(utcnow(), user.timezone)
    end = parse_hhmm(settings.work_end)
    end_local = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_local <= now_local:
        end_local = now_local + timedelta(minutes=60)
    return max(15, int((end_local - now_local).total_seconds() // 60))


@router.message(F.text == BTN_AVAILABILITY)
async def show_availability(
    message: Message, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "availability.set"):
        await message.answer("Управление индикатором доступно руководителю и ассистенту.")
        return

    view = await get_view(session, user.id)
    await message.answer(
        "<b>Ваша доступность</b>\n\n"
        f"Сейчас: {view.render(user.timezone)}\n\n"
        "🟢 — сотрудники видят, что вы принимаете, и могут обратиться без заявки\n"
        "🟡 — заявки принимаются, но обращаться сейчас не нужно\n"
        "🔴 — придерживаются даже срочные обращения\n"
        "🌙 — поздний приём: открываются окна после рабочего дня",
        reply_markup=availability_kb(),
    )


@router.callback_query(F.data.startswith("av:"))
async def switch_availability(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "availability.set"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    _, action, argument = parts

    if action not in ("OFF", "OPEN", "OPENLATE", "BUSY", "DND"):
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    if action == "OFF":
        view = await set_state(
            session, user=user, state=Availability.OFFLINE, minutes=None, note=None
        )
        await call.answer("Индикатор снят")
    else:
        opens_late = action == "OPENLATE"
        state = Availability.OPEN if action in ("OPEN", "OPENLATE") else Availability(action)
        if argument == "day":
            minutes = _minutes_until_end_of_day(user)
        else:
            value = callback_int(call.data)
            if value is None:
                await call.answer(STALE_BUTTON, show_alert=True)
                return
            # Ограничение по смыслу решения Р-12: вечного «доступен» не бывает,
            # а значение из кнопки приходит от клиента и может быть любым.
            minutes = min(max(value, 15), 720)
        view = await set_state(
            session,
            user=user,
            state=state,
            minutes=minutes,
            opens_late_slots=opens_late,
            changed_by=user.id,
        )
        await call.answer("Готово")

    tail = ""
    if view.is_open:
        tail = "\n\nСотрудники видят это в разделе «Кто на связи»."
        if view.opens_late_slots:
            tail += "\nОткрыты и поздние окна — после конца рабочего дня."

    await call.message.edit_text(
        f"<b>Ваша доступность</b>\n\nСейчас: {view.render(user.timezone)}{tail}",
        reply_markup=availability_kb(),
    )


@router.message(F.text == BTN_WHO_IS_OPEN)
async def who_is_open(
    message: Message, session: AsyncSession, organization: Organization, user: User
) -> None:
    """Экран сотрудника: кто из руководителей принимает прямо сейчас."""
    rows = await open_executives(session, organization.id)
    if not rows:
        await message.answer(
            "Сейчас никто не отмечен как доступный.\n"
            "Запросите встречу — система предложит свободные окна."
        )
        return

    lines = ["<b>Сейчас принимают</b>", ""]
    for person, view in rows:
        until = f" до {fmt_time(view.until_at, user.timezone)}" if view.until_at else ""
        note = f"\n   {esc(view.note)}" if view.note else ""
        lines.append(f"🟢 <b>{esc(person.full_name)}</b>{until}{note}")
    lines.append("")
    lines.append("Можно обратиться сейчас, не создавая заявку.")

    await message.answer("\n".join(lines))
