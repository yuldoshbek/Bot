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

from app.bot.utils import callback_int
from app.core.i18n import t
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, utcnow
from app.models import Document, DocumentScope, IndexStatus, Meeting, User, UserStatus
from app.models.org import Organization
from app.services import documents as service
from app.services import features as feature_service
from app.services.rbac import Grant, has_permission

router = Router(name="documents")

# Ключи, а не надписи: подпись зависит от языка, набор — нет.
SCOPE_KEYS = {
    DocumentScope.PRIVATE: "document.scope.private_button",
    DocumentScope.PARTICIPANTS: "document.scope.participants_button",
    DocumentScope.DEPARTMENT: "document.scope.department_button",
    DocumentScope.ORGANIZATION: "document.scope.organization_button",
}

INDEX_KEYS = {
    IndexStatus.PENDING: "document.index.pending",
    IndexStatus.DONE: "document.index.ready",
    IndexStatus.EMPTY: "document.index.no_text",
    IndexStatus.FAILED: "document.index.failed",
    IndexStatus.TOO_LARGE: "document.index.too_big",
    IndexStatus.UNSUPPORTED: "document.index.no_format",
}


class Sharing(StatesGroup):
    person = State()


def _scope_kb(document_id: int, locale: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t(SCOPE_KEYS[scope], locale),
            callback_data=f"dc:sc:{document_id}:{scope.value}",
        )]
        for scope in (
            DocumentScope.PRIVATE, DocumentScope.PARTICIPANTS,
            DocumentScope.DEPARTMENT, DocumentScope.ORGANIZATION,
        )
    ])


def _card_kb(
    document: Document, viewer: User, grants: dict[str, Grant], locale: str
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=t("document.get_file", locale), callback_data=f"dc:get:{document.id}"
    )]]
    if viewer.id == document.uploaded_by and has_permission(grants, "file.share"):
        rows.append([
            InlineKeyboardButton(text=t("document.who_sees", locale),
                                 callback_data=f"dc:sc:ask:{document.id}"),
            InlineKeyboardButton(text=t("document.open_to_person", locale),
                                 callback_data=f"dc:to:{document.id}"),
        ])
        rows.append([InlineKeyboardButton(
            text=t("document.who_opened", locale), callback_data=f"dc:log:{document.id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _card_text(
    session: AsyncSession, document: Document, viewer: User, locale: str
) -> str:
    owner = await session.get(User, document.uploaded_by)
    size = document.size_bytes / 1024 / 1024
    scope_key = SCOPE_KEYS.get(document.scope)
    index_key = INDEX_KEYS.get(document.index_status)
    lines = [
        f"📎 <b>{esc(document.title or document.file_name)}</b>",
        "",
        f"{t('document.uploaded_by', locale)}: "
        f"{esc(owner.full_name) if owner else t('document.unknown', locale)}",
        f"{t('document.uploaded_at', locale)}: "
        f"{fmt_dt(document.created_at, viewer.timezone)}",
        (
            f"{t('document.size', locale)}: {size:.1f}{t('document.size_mb', locale)}"
            if size >= 0.1 else t("document.size_small", locale)
        ),
        f"{t('document.access', locale)}: "
        f"{t(scope_key, locale) if scope_key else document.scope}",
        f"{t('document.search_state', locale)}: "
        f"{t(index_key, locale) if index_key else document.index_status}",
    ]
    if document.meeting_id:
        meeting = await session.get(Meeting, document.meeting_id)
        if meeting is not None:
            lines.append(
                f"{t('document.meeting', locale)}: {esc(cut(meeting.title, 60))}"
            )
    return "\n".join(lines)


# ── Приём ───────────────────────────────────────────────────────────────────
@router.message(F.document)
async def receive(
    message: Message, session: AsyncSession, user: User,
    grants: dict[str, Grant], features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "documents"):
        await message.answer(t("feature.off", locale))
        return
    incoming = message.document
    result = await service.store(
        session,
        uploader=user,
        file_id=incoming.file_id,
        file_unique_id=incoming.file_unique_id,
        file_name=incoming.file_name or t("document.word", locale),
        size_bytes=incoming.file_size or 0,
        mime_type=incoming.mime_type,
        title=(message.caption or "").strip() or None,
    )
    if not result.ok:
        await message.answer(t("document.rejected", locale) + str(result.reason))
        return

    await message.answer(
        t("document.accepted", locale, title=esc(result.document.file_name))
        + "\n\n" + t("document.only_you_yet", locale),
        reply_markup=_scope_kb(result.document.id, locale),
    )


@router.callback_query(F.data.startswith("dc:sc:ask:"))
async def ask_scope(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await call.message.answer(t("document.ask_scope", locale),
                              reply_markup=_scope_kb(document.id, locale))
    await call.answer()


@router.callback_query(F.data.startswith("dc:sc:"))
async def set_scope(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 4:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    document_id, scope = callback_int(call.data, 2), parts[3]
    document = await session.get(Document, document_id) if document_id else None
    if document is None or scope not in SCOPE_KEYS:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    if document.uploaded_by != user.id:
        await call.answer(t("document.scope_rights", locale), show_alert=True)
        return

    document.scope = scope
    await session.flush()
    await call.message.edit_text(
        f"📎 <b>{esc(document.file_name)}</b>\n\n"
        f"{t('document.access', locale)}: {t(SCOPE_KEYS[scope], locale)}"
    )
    await call.answer(t("common.done", locale))


# ── Карточка и выдача ───────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("dc:card:"))
async def card(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or not await service.may_read(
        session, document=document, viewer=user
    ):
        await call.answer(t("document.not_open", locale), show_alert=True)
        return
    await call.message.answer(
        await _card_text(session, document, user, locale),
        reply_markup=_card_kb(document, user, grants, locale),
    )
    await call.answer()


@router.callback_query(F.data.startswith("dc:get:"))
async def send_file(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str, bot: Bot
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
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
async def views(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    log = await service.views_of(session, document)
    if not log:
        await call.answer(t("document.never_opened", locale), show_alert=True)
        return
    names = await session.execute(
        select(User.id, User.full_name).where(User.id.in_([v.user_id for v in log]))
    )
    who = {row[0]: row[1] for row in names.all()}
    unknown = t("document.unknown", locale)
    lines = [t("document.opened_title", locale), ""]
    lines += [
        f"{esc(who.get(v.user_id, unknown))} — {fmt_dt(v.viewed_at, user.timezone)}"
        for v in log
    ]
    await call.message.answer("\n".join(lines))
    await call.answer()


# ── Выдача доступа конкретному человеку ─────────────────────────────────────
@router.callback_query(F.data.startswith("dc:to:"))
async def share_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    document_id = callback_int(call.data)
    document = await session.get(Document, document_id) if document_id else None
    if document is None or document.uploaded_by != user.id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(document_id=document.id)
    await call.message.answer(t("document.ask_surname", locale))
    await state.set_state(Sharing.person)
    await call.answer()


@router.message(Sharing.person, F.text)
async def share_find(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, locale: str,
) -> None:
    query = (message.text or "").strip().lower()
    if len(query) < 2:
        await message.answer(t("document.surname_short", locale))
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
        await message.answer(t("document.nobody_found", locale))
        return
    await message.answer(
        t("document.ask_person", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p.full_name, callback_data=f"dc:give:{p.id}")]
            for p in people
        ]),
    )


@router.callback_query(Sharing.person, F.data.startswith("dc:give:"))
async def share_finish(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    document = await session.get(Document, data.get("document_id", 0))
    person = await session.get(User, callback_int(call.data) or 0)
    if document is None or person is None:
        await state.clear()
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    problem = await service.grant(session, document=document, actor=user, to_user=person)
    await state.clear()
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.message.answer(
        t("document.granted", locale, name=esc(person.full_name))
        + f" → {esc(document.title or document.file_name)}"
    )
    await call.answer()
