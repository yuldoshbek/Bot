"""Документы в боте: приём файла, карточка, выдача доступа, получение.

Загрузка — это просто отправка файла боту. Ничего выбирать заранее не нужно:
человек присылает документ, бот спрашивает, к чему его отнести и кому открыть.
Спрашивать до отправки значило бы заставить держать файл наготове.

По умолчанию документ личный. Открыть его — отдельное осознанное движение,
а не галочка, которую проще не заметить.
"""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.utils import STALE_BUTTON, callback_int
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, utcnow
from app.models import Document, DocumentScope, IndexStatus, Meeting, User, UserStatus
from app.models.org import Organization
from app.services import documents as service
from app.services.rbac import Grant, has_permission

router = Router(name="documents")

SCOPE_LABELS = {
    DocumentScope.PRIVATE: "🔒 Только мне",
    DocumentScope.PARTICIPANTS: "👥 Участникам встречи",
    DocumentScope.DEPARTMENT: "🏢 Моему отделу",
    DocumentScope.ORGANIZATION: "🌐 Всей организации",
}

INDEX_LABELS = {
    IndexStatus.PENDING: "текст извлекается",
    IndexStatus.DONE: "поиск по тексту работает",
    IndexStatus.EMPTY: "текста внутри нет — найдётся по имени",
    IndexStatus.FAILED: "текст извлечь не удалось — найдётся по имени",
    IndexStatus.TOO_LARGE: "слишком большой для разбора — найдётся по имени",
    IndexStatus.UNSUPPORTED: "формат без текста — найдётся по имени",
}


class Sharing(StatesGroup):
    person = State()


def _scope_kb(document_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=SCOPE_LABELS[DocumentScope.PRIVATE], callback_data=f"dc:sc:{document_id}:PRIVATE"
        )],
        [InlineKeyboardButton(
            text=SCOPE_LABELS[DocumentScope.PARTICIPANTS],
            callback_data=f"dc:sc:{document_id}:PARTICIPANTS",
        )],
        [InlineKeyboardButton(
            text=SCOPE_LABELS[DocumentScope.DEPARTMENT],
            callback_data=f"dc:sc:{document_id}:DEPARTMENT",
        )],
        [InlineKeyboardButton(
            text=SCOPE_LABELS[DocumentScope.ORGANIZATION],
            callback_data=f"dc:sc:{document_id}:ORGANIZATION",
        )],
    ])


def _card_kb(document: Document, viewer: User, grants: dict[str, Grant]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text="📥 Получить файл", callback_data=f"dc:get:{document.id}"
    )]]
    if viewer.id == document.uploaded_by and has_permission(grants, "file.share"):
        rows.append([
            InlineKeyboardButton(text="🔓 Кому открыт", callback_data=f"dc:sc:ask:{document.id}"),
            InlineKeyboardButton(text="👤 Открыть человеку", callback_data=f"dc:to:{document.id}"),
        ])
        rows.append([InlineKeyboardButton(
            text="👁 Кто открывал", callback_data=f"dc:log:{document.id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _card_text(session: AsyncSession, document: Document, viewer: User) -> str:
    owner = await session.get(User, document.uploaded_by)
    size = document.size_bytes / 1024 / 1024
    lines = [
        f"📎 <b>{esc(document.title or document.file_name)}</b>",
        "",
        f"Загрузил: {esc(owner.full_name) if owner else 'неизвестно'}",
        f"Когда: {fmt_dt(document.created_at, viewer.timezone)}",
        f"Размер: {size:.1f} МБ" if size >= 0.1 else "Размер: меньше 0.1 МБ",
        f"Доступ: {SCOPE_LABELS.get(document.scope, document.scope)}",
        f"Поиск: {INDEX_LABELS.get(document.index_status, document.index_status)}",
    ]
    if document.meeting_id:
        meeting = await session.get(Meeting, document.meeting_id)
        if meeting is not None:
            lines.append(f"Встреча: {esc(cut(meeting.title, 60))}")
    return "\n".join(lines)


# ── Приём ───────────────────────────────────────────────────────────────────
@router.message(F.document)
async def receive(
    message: Message, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    incoming = message.document
    result = await service.store(
        session,
        uploader=user,
        file_id=incoming.file_id,
        file_unique_id=incoming.file_unique_id,
        file_name=incoming.file_name or "документ",
        size_bytes=incoming.file_size or 0,
        mime_type=incoming.mime_type,
        title=(message.caption or "").strip() or None,
    )
    if not result.ok:
        await message.answer(f"Не принял файл. {result.reason}")
        return

    await message.answer(
        f"📎 Принял: <b>{esc(result.document.file_name)}</b>\n\n"
        f"Пока документ виден только вам. Кому открыть?",
        reply_markup=_scope_kb(result.document.id),
    )


@router.callback_query(F.data.startswith("dc:sc:ask:"))
async def ask_scope(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await call.message.answer("Кому открыть документ?", reply_markup=_scope_kb(document.id))
    await call.answer()


@router.callback_query(F.data.startswith("dc:sc:"))
async def set_scope(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 4:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    document_id, scope = callback_int(call.data, 2), parts[3]
    document = await session.get(Document, document_id) if document_id else None
    if document is None or scope not in SCOPE_LABELS:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    if document.uploaded_by != user.id:
        await call.answer("Менять доступ может тот, кто загрузил документ.", show_alert=True)
        return

    document.scope = scope
    await session.flush()
    await call.message.edit_text(
        f"📎 <b>{esc(document.file_name)}</b>\n\nДоступ: {SCOPE_LABELS[scope]}"
    )
    await call.answer("Готово.")


# ── Карточка и выдача ───────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("dc:card:"))
async def card(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or not await service.may_read(
        session, document=document, viewer=user
    ):
        await call.answer("Этот документ вам не открыт.", show_alert=True)
        return
    await call.message.answer(
        await _card_text(session, document, user),
        reply_markup=_card_kb(document, user, grants),
    )
    await call.answer()


@router.callback_query(F.data.startswith("dc:get:"))
async def send_file(
    call: CallbackQuery, session: AsyncSession, user: User, bot: Bot
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    # Проверка прав и запись в журнал делаются одной функцией: получить файл
    # мимо журнала не должно быть способа.
    problem = await service.open_for(session, document=document, viewer=user)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await bot.send_document(
        call.from_user.id, document.file_id,
        caption=esc(document.title or document.file_name),
    )
    await call.answer()


@router.callback_query(F.data.startswith("dc:log:"))
async def views(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    log = await service.views_of(session, document)
    if not log:
        await call.answer("Документ ещё никто не открывал.", show_alert=True)
        return
    names = await session.execute(
        select(User.id, User.full_name).where(User.id.in_([v.user_id for v in log]))
    )
    who = {row[0]: row[1] for row in names.all()}
    lines = ["👁 <b>Кто открывал</b>", ""]
    lines += [
        f"{esc(who.get(v.user_id, 'неизвестно'))} — {fmt_dt(v.viewed_at, user.timezone)}"
        for v in log
    ]
    await call.message.answer("\n".join(lines))
    await call.answer()


# ── Выдача доступа конкретному человеку ─────────────────────────────────────
@router.callback_query(F.data.startswith("dc:to:"))
async def share_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(document_id=document.id)
    await call.message.answer("Кому открыть? Напишите фамилию.")
    await state.set_state(Sharing.person)
    await call.answer()


@router.message(Sharing.person, F.text)
async def share_find(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User,
) -> None:
    query = (message.text or "").strip().lower()
    if len(query) < 2:
        await message.answer("Напишите хотя бы два знака фамилии.")
        return
    people = (
        await session.execute(
            select(User).where(
                User.organization_id == organization.id,
                User.status == UserStatus.ACTIVE,
                User.id != user.id,
                func.lower(User.full_name).contains(query),
            ).limit(7)
        )
    ).scalars().all()
    if not people:
        await message.answer("Никого не нашёл. Попробуйте другую часть фамилии.")
        return
    await message.answer(
        "Кому открыть доступ?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"dc:give:{p.id}")]
            for p in people
        ]),
    )


@router.callback_query(Sharing.person, F.data.startswith("dc:give:"))
async def share_finish(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    document = await session.get(Document, data.get("document_id", 0))
    person = await session.get(User, callback_int(call.data) or 0)
    if document is None or person is None:
        await state.clear()
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    problem = await service.grant(session, document=document, actor=user, to_user=person)
    await state.clear()
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.message.answer(
        f"🔓 Доступ открыт: {esc(person.full_name)} → "
        f"{esc(document.title or document.file_name)}"
    )
    await call.answer()
