"""Реестр решений, глобальный поиск и выгрузка.

Три экрана, связанные одной мыслью: система должна отвечать на вопрос «что у нас
было по этому поводу» без раскопок в переписке. Поиск ищет по всему сразу,
реестр держит решения, выгрузка отдаёт то же самое файлом.

Ни один из экранов не решает, что человеку показывать: условия видимости
приходят из служб, отвечающих за доступ к самим записям.
"""
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import BTN_DECISIONS, BTN_SEARCH
from app.bot.utils import STALE_BUTTON, callback_int
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, to_local, utcnow
from app.models import Decision, DecisionStatus, Meeting, User
from app.services import decisions as service
from app.services import export as export_service
from app.services import search as search_service
from app.services.rbac import Grant, has_permission

router = Router(name="registry")

KIND_ICONS = {
    "meeting": "📅", "task": "📋", "decision": "📌",
    "document": "📎", "person": "👤",
}
KIND_CALLBACK = {
    "meeting": "mt:card", "task": "t:open", "decision": "dn:card", "document": "dc:card",
}
EXPORT_KINDS = {"tasks": "Поручения", "decisions": "Решения", "meetings": "Встречи"}
EXPORT_DAYS = 90


class Finding(StatesGroup):
    query = State()


class NewDecision(StatesGroup):
    title = State()


# ── Поиск ───────────────────────────────────────────────────────────────────
@router.message(F.text == BTN_SEARCH)
async def search_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🔎 <b>Поиск</b>\n\n"
        "Напишите, что ищете: тему встречи, фразу из документа, фамилию.\n"
        "Найдётся только то, что вам открыто."
    )
    await state.set_state(Finding.query)


@router.message(Finding.query, F.text)
async def search_run(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    found = await search_service.search(
        session, user=user, grants=grants, query=message.text or ""
    )
    await state.clear()
    if found.empty:
        await message.answer(
            "Ничего не нашёл.\n"
            "Попробуйте другое слово — поиск понимает формы: «совещания» найдёт «совещание»."
        )
        return

    lines = [f"🔎 <b>Нашлось: {found.total}</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    groups = (
        ("Встречи", found.meetings), ("Поручения", found.tasks),
        ("Решения", found.decisions), ("Документы", found.documents),
        ("Люди", found.people),
    )
    for title, hits in groups:
        if not hits:
            continue
        lines.append(f"<b>{title}</b>")
        for hit in hits:
            when = f" · {to_local(hit.when, user.timezone):%d.%m}" if hit.when else ""
            lines.append(f"{KIND_ICONS[hit.kind]} {esc(cut(hit.title, 70))}{when}")
            prefix = KIND_CALLBACK.get(hit.kind)
            if prefix:
                rows.append([InlineKeyboardButton(
                    text=f"{KIND_ICONS[hit.kind]} {cut(hit.title, 30)}",
                    callback_data=f"{prefix}:{hit.id}",
                )])
        lines.append("")

    await message.answer(
        "\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:8]) if rows else None,
    )


# ── Реестр решений ──────────────────────────────────────────────────────────
@router.message(F.text == BTN_DECISIONS)
async def decisions_list(
    message: Message, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "decision.read"):
        await message.answer("Реестр решений вам не открыт.")
        return

    items = await service.registry(session, user=user, grants=grants, only_open=True, limit=10)
    if not items:
        await message.answer(
            "📌 <b>Реестр решений</b>\n\nНезакрытых решений нет.",
            reply_markup=_registry_kb(grants, has_items=False),
        )
        return

    lines = ["📌 <b>Незакрытые решения</b>", ""]
    rows = []
    for decision in items:
        responsible = (
            await session.get(User, decision.responsible_id)
            if decision.responsible_id else None
        )
        who = f" — {esc(responsible.full_name)}" if responsible else ""
        lines.append(f"• {esc(cut(decision.title, 90))}{who}")
        rows.append([InlineKeyboardButton(
            text=cut(decision.title, 40), callback_data=f"dn:card:{decision.id}"
        )])
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows[:6] + _registry_kb(grants, has_items=True).inline_keyboard
        ),
    )


def _registry_kb(grants: dict[str, Grant], *, has_items: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_permission(grants, "decision.create"):
        rows.append([InlineKeyboardButton(text="➕ Решение", callback_data="dn:new")])
    if has_permission(grants, "export.read") and has_items:
        rows.append([InlineKeyboardButton(
            text="📤 Выгрузить решения", callback_data="ex:decisions"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("dn:card:"))
async def decision_card(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    decision_id = callback_int(call.data)
    decision = await session.get(Decision, decision_id) if decision_id else None
    if decision is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    # Видимость одной записи спрашивается у парной функции, а не выборкой
    # видимого списка: выборка ограничена `LIMIT`, и решение старше
    # пятисотого по дате отвечало бы «вам не открыто», хотя оно ваше.
    if not await service.may_read(session, decision=decision, viewer=user):
        await call.answer("Это решение вам не открыто.", show_alert=True)
        return

    author = await session.get(User, decision.author_id)
    responsible = (
        await session.get(User, decision.responsible_id) if decision.responsible_id else None
    )
    lines = [
        f"📌 <b>{esc(decision.title)}</b>",
        "",
        f"Состояние: {service.STATUS_LABELS[decision.status]}",
        f"Автор: {esc(author.full_name) if author else 'неизвестно'}",
    ]
    if responsible:
        lines.append(f"Ответственный: {esc(responsible.full_name)}")
    if decision.due_date:
        lines.append(f"Срок: {fmt_dt(decision.due_date, user.timezone)}")
    if decision.details:
        lines += ["", esc(cut(decision.details, 500))]
    if decision.meeting_id:
        meeting = await session.get(Meeting, decision.meeting_id)
        if meeting:
            lines.append(f"\nПринято на встрече: {esc(cut(meeting.title, 60))}")
    if decision.cancel_reason:
        lines.append(f"\nОтменено: {esc(decision.cancel_reason)}")

    rows = []
    if decision.status == DecisionStatus.OPEN and has_permission(grants, "decision.close"):
        rows.append([
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"dn:done:{decision.id}"),
            InlineKeyboardButton(text="🚫 Отменить", callback_data=f"dn:kill:{decision.id}"),
        ])
    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )
    await call.answer()


@router.callback_query(F.data.startswith("dn:done:"))
async def decision_done(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    decision = await session.get(Decision, callback_int(call.data) or 0)
    if decision is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    result = await service.close(session, decision=decision, actor=user, done=True)
    if not result.ok:
        await call.answer(result.reason or "Не получилось.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("✅ Решение отмечено выполненным.")
    await call.answer()


class Cancelling(StatesGroup):
    reason = State()


@router.callback_query(F.data.startswith("dn:kill:"))
async def decision_cancel_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    decision = await session.get(Decision, callback_int(call.data) or 0)
    if decision is None or decision.organization_id != user.organization_id:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(decision_id=decision.id)
    await call.message.answer("Почему отменяем? Причина останется в реестре навсегда.")
    await state.set_state(Cancelling.reason)
    await call.answer()


@router.message(Cancelling.reason, F.text)
async def decision_cancel_finish(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    decision = await session.get(Decision, data.get("decision_id", 0))
    if decision is None:
        await state.clear()
        await message.answer(STALE_BUTTON)
        return
    result = await service.close(
        session, decision=decision, actor=user, done=False, reason=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or "Не получилось отменить.")
        return
    await message.answer("🚫 Решение отменено, причина записана в реестр.")


@router.callback_query(F.data == "dn:new")
async def decision_new(
    call: CallbackQuery, state: FSMContext, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "decision.create"):
        await call.answer("Вносить решения может руководитель или ассистент.", show_alert=True)
        return
    await state.clear()
    await call.message.answer("Сформулируйте решение одной строкой.")
    await state.set_state(NewDecision.title)
    await call.answer()


@router.message(NewDecision.title, F.text)
async def decision_save(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    result = await service.create(session, actor=user, title=message.text or "")
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or "Не получилось внести решение.")
        return
    await message.answer(f"📌 Записано: <b>{esc(result.item.title)}</b>")


# ── Выгрузка ────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ex:"))
async def export_run(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], bot: Bot,
) -> None:
    kind = (call.data or "").split(":")[-1]
    if kind not in EXPORT_KINDS:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    until = utcnow()
    since = until - timedelta(days=EXPORT_DAYS)
    data, name, problem = await export_service.build(
        session, user=user, grants=grants, kind=kind, since=since, until=until, fmt="xlsx"
    )
    if data is None:
        await call.answer(problem or "Не получилось выгрузить.", show_alert=True)
        return
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(data, filename=name),
        caption=f"{EXPORT_KINDS[kind]} за {EXPORT_DAYS} дней",
    )
    await call.answer()
