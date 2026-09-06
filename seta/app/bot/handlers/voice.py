"""Голосовое поручение в боте.

Экран один: карточка черновика. Она честно говорит первой строкой, что
поручения ещё нет, показывает разобранное и ждёт нажатия. Пока человек
не нажал «Подтвердить», в базе не появляется ничего.

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

from app.ai import voice as service
from app.bot.utils import callback_int
from app.core.config import settings
from app.core.i18n import t
from app.core.text import cut, esc
from app.models.enums import RoleCode
from app.models.user import User
from app.services.rbac import Grant, has_permission
from app.services.tasks import TaskError, allowed_assignees

router = Router(name="voice")


class VoiceTask(StatesGroup):
    confirm = State()


@router.message(F.voice)
async def receive(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    """Голосовое → расшифровка → черновик. Поручений не создаётся."""
    if not settings.ai_enabled:
        # Выключенный ИИ не молчит и не ломается: он отправляет к обычному пути.
        await message.answer(t("voice.off", locale))
        return
    if not has_permission(grants, "task.create"):
        await message.answer(t("voice.no_rights", locale))
        return

    incoming = message.voice
    if incoming.duration and incoming.duration > service.MAX_SECONDS:
        await message.answer(
            t("voice.too_long", locale, limit=service.MAX_SECONDS // 60)
        )
        return

    waiting = await message.answer(t("voice.listening", locale))

    info = await message.bot.get_file(incoming.file_id)
    buffer = await message.bot.download_file(info.file_path)
    heard = await service.listen(session, buffer.read(), creator=user)
    if not heard.worked:
        await waiting.edit_text(t("voice.not_heard", locale))
        return

    draft = await service.draft(
        session, heard.text, creator=user, grants=grants
    )
    await state.set_state(VoiceTask.confirm)
    await state.update_data(draft=draft.to_state())
    text, keyboard = await _screen(
        session, draft, viewer=user, grants=grants, locale=locale
    )
    await waiting.edit_text(text, reply_markup=keyboard)


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
