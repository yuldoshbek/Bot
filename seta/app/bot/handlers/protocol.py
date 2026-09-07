"""Протокол встречи в боте: пункт за пунктом.

Экран показывает по одному предложению и два действия — записать или
пропустить. Кнопки «принять всё» здесь нет намеренно: протокол, принятый
одним нажатием, — это протокол, который никто не прочитал.

Пока человек не нажал «Записать», в реестре решений и в поручениях
не появляется ничего.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import protocol as service
from app.bot.utils import callback_int
from app.core.config import settings
from app.core.i18n import t
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt
from app.models import Meeting, User
from app.services import meetings as meeting_service
from app.services.rbac import Grant, has_permission

router = Router(name="protocol")


class Protocol(StatesGroup):
    walk = State()


@router.callback_query(F.data.startswith("mt:proto:"))
async def start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    if not settings.ai_enabled:
        await call.answer(t("protocol.off", locale), show_alert=True)
        return
    if not has_permission(grants, "decision.create"):
        await call.answer(t("protocol.no_rights", locale), show_alert=True)
        return

    meeting_id = callback_int(call.data)
    meeting = await session.get(Meeting, meeting_id) if meeting_id else None
    if meeting is None or not await meeting_service.may_read(
        session, meeting=meeting, viewer=user
    ):
        await call.answer(t("error.not_found", locale), show_alert=True)
        return

    await call.answer(t("protocol.building", locale))
    draft = await service.build(
        session, meeting=meeting, actor=user, grants=grants
    )
    if not draft.items:
        await call.message.answer(
            t("protocol.no_agenda" if not await _has_agenda(session, meeting)
              else "protocol.nothing", locale)
        )
        return

    await state.set_state(Protocol.walk)
    await state.update_data(protocol=draft.to_state())
    await _show(call, session, draft, locale, user.timezone)


@router.callback_query(Protocol.walk, F.data == "pr:yes")
async def take(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    draft, meeting = await _restore(state, session)
    if draft is None or meeting is None:
        await _expired(call, state, locale)
        return

    index = draft.next_index()
    if index is None:
        await _finish(call, state, draft, locale)
        return

    ok, reason = await service.take(
        session, draft, index, meeting=meeting, actor=user, grants=grants
    )
    if not ok:
        # Причина бывает ключом словаря, а бывает готовым текстом службы
        # решений: `t` вернёт ключ как есть, если такого ключа нет.
        await call.answer(t(reason, locale), show_alert=True)
        if reason == "protocol.err.stale":
            await _expired(call, state, locale)
            return

    await state.update_data(protocol=draft.to_state())
    await _advance(call, state, session, draft, locale, user.timezone)


@router.callback_query(Protocol.walk, F.data == "pr:no")
async def skip(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    draft, _ = await _restore(state, session)
    if draft is None:
        await _expired(call, state, locale)
        return
    index = draft.next_index()
    if index is not None:
        await service.drop(session, draft, index)
    await state.update_data(protocol=draft.to_state())
    await call.answer()
    await _advance(call, state, session, draft, locale, user.timezone)


@router.callback_query(Protocol.walk, F.data == "pr:stop")
async def stop(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_text(t("protocol.stopped", locale))


async def _advance(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    draft: service.Draft, locale: str, tz: str,
) -> None:
    if draft.next_index() is None:
        await _finish(call, state, draft, locale)
        return
    await _show(call, session, draft, locale, tz, edit=True)


async def _finish(
    call: CallbackQuery, state: FSMContext, draft: service.Draft, locale: str
) -> None:
    await state.clear()
    await call.message.edit_text(
        t("protocol.done", locale, taken=draft.taken, dropped=draft.dropped)
    )


async def _expired(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    await state.clear()
    await call.answer(t("protocol.err.stale", locale), show_alert=True)


async def _restore(
    state: FSMContext, session: AsyncSession
) -> tuple["service.Draft | None", Meeting | None]:
    data = await state.get_data()
    draft = service.Draft.from_state(data.get("protocol"))
    if draft is None:
        return None, None
    return draft, await session.get(Meeting, draft.meeting_id)


async def _has_agenda(session: AsyncSession, meeting: Meeting) -> bool:
    from app.services.decisions import agenda_of

    return bool(await agenda_of(session, meeting))


async def _show(
    call: CallbackQuery, session: AsyncSession, draft: service.Draft,
    locale: str, tz: str, *, edit: bool = False,
) -> None:
    index = draft.next_index()
    if index is None:
        return
    item = draft.items[index]

    lines = [
        f"<b>{t('protocol.header', locale)}</b>",
        t("protocol.not_yet", locale),
        "",
        t("protocol.step", locale, number=index + 1, total=len(draft.items)),
        t("protocol.from_agenda", locale, number=item.agenda_number),
        "",
        f"<b>{t(f'protocol.kind.{item.kind}', locale)}</b>",
        f"📋 {esc(cut(item.title, 300))}",
    ]
    if item.responsible_id:
        person = await session.get(User, item.responsible_id)
        if person is not None:
            lines.append(
                f"👤 {t('protocol.field.responsible', locale)}: "
                f"{esc(person.full_name)}"
            )
    if item.due_at:
        lines.append(f"⏰ {fmt_dt(item.due_at, tz)}")
    if item.notes:
        lines.append("")
        lines += [
            "• " + t(note, locale, name=esc(cut(item.heard_name, 60)))
            for note in item.notes
        ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("protocol.btn.take", locale),
                                 callback_data="pr:yes"),
            InlineKeyboardButton(text=t("protocol.btn.skip", locale),
                                 callback_data="pr:no"),
        ],
        [InlineKeyboardButton(text=t("protocol.btn.stop", locale),
                              callback_data="pr:stop")],
    ])
    text = "\n".join(lines)
    if edit:
        await call.message.edit_text(text, reply_markup=keyboard)
    else:
        await call.message.answer(text, reply_markup=keyboard)
