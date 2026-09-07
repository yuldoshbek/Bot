"""Голосовые в боте: сначала текст, потом — если нужно — поручение.

Любое голосовое расшифровывается и показывается текстом с абзацами. Это
основное поведение, и оно работает без ключа OpenAI: речь слушает своя
служба, а она денег не стоит. Запись при этом сохраняется — распознавание
ошибается, и спор «я такого не говорил» разрешается только звуком.

**Поручение — отдельное действие, а не побочный эффект.** Раньше голосовое
сразу открывало черновик поручения; но голосовые пересылают, пересказывают
и просто шлют вместо письма, и превращать каждое в поручение неверно. Теперь
человек видит текст, и кнопка «Сделать поручением» стоит рядом — для тех,
у кого есть право поручать.

Карточка черновика честно говорит первой строкой, что поручения ещё нет.
Пока человек не нажал «Подтвердить», в базе не появляется ничего.

Редактора здесь намеренно нет. Черновик — предложение, а не форма ввода:
поправить исполнителя можно (без него поручения не бывает), всё остальное
проще переговорить заново или набрать руками через ➕. Второй редактор
поручений означал бы второй путь создания, а два пути расходятся на первой
же правке жизненного цикла.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate, voice as service
from app.core import speech
from app.bot.utils import callback_int
from app.core.config import settings
from app.core.i18n import t
from app.core.text import cut, esc
from app.models.enums import RoleCode
from app.models.user import User
from app.models.voice import VoiceNote
from app.services.rbac import Grant, has_permission
from app.services.tasks import TaskError, allowed_assignees

router = Router(name="voice")


class VoiceTask(StatesGroup):
    confirm = State()


@router.message(F.voice)
async def receive(
    message: Message, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    """Голосовое → текст. Поручений здесь не создаётся."""
    if not gate.hearing():
        # Ни своей службы, ни ключа. Молчать нельзя: человек ждёт ответа.
        await message.answer(t("voice.off", locale))
        return

    incoming = message.voice
    limit = service.max_seconds()
    if incoming.duration and incoming.duration > limit:
        await message.answer(t("voice.too_long", locale, limit=limit // 60))
        return

    note = await service.remember(
        session,
        user=user,
        file_id=incoming.file_id,
        file_unique_id=incoming.file_unique_id,
        duration_seconds=incoming.duration or 0,
        size_bytes=incoming.file_size or 0,
    )
    waiting = await message.answer(t("voice.listening", locale))

    text = note.transcript
    if not text:
        info = await message.bot.get_file(incoming.file_id)
        buffer = await message.bot.download_file(info.file_path)
        text = await service.write_down(session, note, buffer.read(), user=user)
    if not text:
        await waiting.edit_text(t("voice.not_heard", locale))
        return

    await waiting.edit_text(
        f"<b>{t('voice.text.title', locale, length=speech.duration(note.duration_seconds))}</b>\n\n"
        f"{esc(text)}\n\n"
        f"<i>{t('voice.text.kept', locale)}</i>",
        reply_markup=_after_text(note.id, grants, locale),
    )


def _after_text(
    note_id: int, grants: dict[str, Grant], locale: str
) -> InlineKeyboardMarkup | None:
    """Что можно сделать с расшифровкой. Поручение — только тем, кто вправе."""
    if not settings.ai_enabled or not has_permission(grants, "task.create"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=t("voice.btn.task", locale), callback_data=f"vt:draft:{note_id}"
    )]])


@router.callback_query(F.data.startswith("vt:draft:"))
async def to_task(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    """Черновик поручения по уже расшифрованному голосовому."""
    if not settings.ai_enabled:
        await call.answer(t("voice.off", locale), show_alert=True)
        return
    if not has_permission(grants, "task.create"):
        await call.answer(t("voice.no_rights", locale), show_alert=True)
        return

    note_id = callback_int(call.data)
    note = await session.get(VoiceNote, note_id) if note_id else None
    if note is None or note.organization_id != user.organization_id or not note.transcript:
        await call.answer(t("voice.stale", locale), show_alert=True)
        return

    await call.answer()
    draft = await service.draft(
        session, note.transcript, creator=user, grants=grants
    )
    await state.set_state(VoiceTask.confirm)
    await state.update_data(draft=draft.to_state())
    text, keyboard = await _screen(
        session, draft, viewer=user, grants=grants, locale=locale
    )
    await call.message.answer(text, reply_markup=keyboard)


@router.callback_query(VoiceTask.confirm, F.data == "vt:ok")
async def accept(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, roles: set[RoleCode], grants: dict[str, Grant], locale: str,
) -> None:
    draft = await _draft_of(state)
    if draft is None:
        await call.answer(t("voice.stale", locale), show_alert=True)
        await state.clear()
        return

    try:
        task = await service.confirm(
            session, draft, creator=user, grants=grants,
            on_behalf_of_id=await _on_behalf(session, user, roles),
        )
    except TaskError as error:
        await call.answer(str(error), show_alert=True)
        return

    await state.clear()
    await call.answer(t("voice.created", locale))
    review = "\n" + t("task.new.needs_review", locale) if task.requires_review else ""
    await call.message.edit_text(
        f"{t('task.new.created_title', locale)}\n\n"
        f"📋 {esc(cut(task.title, 200))}{review}\n\n"
        f"{t('task.new.notified', locale)}"
    )


@router.callback_query(VoiceTask.confirm, F.data == "vt:no")
async def reject(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, locale: str
) -> None:
    """Отказ. Ничего не создаётся, но в журнале он виден."""
    draft = await _draft_of(state)
    if draft is not None:
        await service.decline(session, draft)
    await state.clear()
    await call.answer()
    await call.message.edit_text(t("voice.cancelled", locale))


@router.callback_query(VoiceTask.confirm, F.data.startswith("vt:who:"))
async def choose(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    draft = await _draft_of(state)
    person_id = callback_int(call.data, 2)
    if draft is None or person_id is None:
        await call.answer(t("voice.stale", locale), show_alert=True)
        await state.clear()
        return

    if not await service.pick(
        session, draft, person_id=person_id, creator=user, grants=grants
    ):
        await call.answer(t("task.new.cannot_assign", locale), show_alert=True)
        return

    await state.update_data(draft=draft.to_state())
    text, keyboard = await _screen(
        session, draft, viewer=user, grants=grants, locale=locale
    )
    await call.answer()
    await call.message.edit_text(text, reply_markup=keyboard)


async def _draft_of(state: FSMContext) -> service.Draft | None:
    data = await state.get_data()
    return service.Draft.from_state(data.get("draft"))


async def _screen(
    session: AsyncSession, draft: service.Draft, *, viewer: User,
    grants: dict[str, Grant], locale: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка и кнопки под ней. Собираются вместе: обеим нужен один список.

    Кнопка «Подтвердить» появляется, только когда подтверждать есть что.
    Кнопка, которая всегда отказывает, — это не кнопка, а ловушка.
    """
    name = ""
    if draft.assignee_id:
        person = await session.get(User, draft.assignee_id)
        name = person.full_name if person else ""

    text = service.render(
        draft, locale, assignee_name=name, timezone_name=viewer.timezone
    )
    rows: list[list[InlineKeyboardButton]] = []

    if draft.ready:
        rows.append([InlineKeyboardButton(
            text=t("voice.btn.confirm", locale), callback_data="vt:ok"
        )])
    else:
        # Список тех, кому этот человек вправе поручать, — тот же, что
        # в ручном вводе: одно правило, одна выборка.
        people = await allowed_assignees(session, actor=viewer, grants=grants)
        text += "\n\n" + t("voice.pick" if people else "voice.nobody", locale)
        rows += [
            [InlineKeyboardButton(
                text=cut(person.full_name, 40), callback_data=f"vt:who:{person.id}"
            )]
            for person in people[:10]
        ]

    rows.append([InlineKeyboardButton(
        text=t("voice.btn.cancel", locale), callback_data="vt:no"
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _on_behalf(
    session: AsyncSession, user: User, roles: set[RoleCode]
) -> int | None:
    """Ассистент наговаривает от имени руководителя — в карточке видно обоих."""
    if RoleCode.ASSISTANT not in roles or RoleCode.EXECUTIVE in roles:
        return None
    from app.bot.handlers.tasks import _executive_of

    executive = await _executive_of(session, user.organization_id)
    return executive.id if executive else None
