"""Реестр решений, глобальный поиск и выгрузка.

Три экрана, связанные одной мыслью: система должна отвечать на вопрос «что у нас
было по этому поводу» без раскопок в переписке. Поиск ищет по всему сразу,
реестр держит решения, выгрузка отдаёт то же самое файлом.

Ни один из экранов не решает, что человеку показывать: условия видимости
приходят из служб, отвечающих за доступ к самим записям.
"""
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
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

from app.bot.keyboards.common import MenuButton, MENU_DECISIONS, MENU_SEARCH
from app.bot.utils import callback_int
from app.core.i18n import t
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt, to_local, utcnow
from app.models import Decision, DecisionStatus, Meeting, User
from app.services import decisions as service
from app.services import export as export_service
from app.services import questions as question_service
from app.services import search as search_service
from app.services.rbac import Grant, has_permission
from app.ai import question as question_ai
from app.core.config import settings

router = Router(name="registry")

KIND_ICONS = {
    "meeting": "📅", "task": "📋", "decision": "📌",
    "document": "📎", "person": "👤",
}
KIND_CALLBACK = {
    "meeting": "mt:card", "task": "t:open", "decision": "dn:card", "document": "dc:card",
}
EXPORT_KIND_KEYS = {
    "tasks": "search.tasks", "decisions": "search.decisions", "meetings": "search.meetings",
}
EXPORT_DAYS = 90


class Finding(StatesGroup):
    query = State()


class Asking(StatesGroup):
    question = State()


class NewDecision(StatesGroup):
    title = State()


def _line(hit: search_service.Hit, timezone_name: str) -> str:
    """Как выглядит найденная запись. Одно описание на поиск и на вопрос:
    две строчки, набранные порознь, разъезжаются на первой же правке."""
    when = f" · {to_local(hit.when, timezone_name):%d.%m}" if hit.when else ""
    return f"{KIND_ICONS[hit.kind]} {esc(cut(hit.title, 70))}{when}"


def _button(hit: search_service.Hit) -> list[InlineKeyboardButton] | None:
    """Кнопка к найденной записи. У сотрудника карточки нет — и кнопки тоже."""
    prefix = KIND_CALLBACK.get(hit.kind)
    if not prefix:
        return None
    return [InlineKeyboardButton(
        text=f"{KIND_ICONS[hit.kind]} {cut(hit.title, 30)}",
        callback_data=f"{prefix}:{hit.id}",
    )]


def _ask_kb(locale: str) -> InlineKeyboardMarkup | None:
    """Кнопка «спросить словами» — только когда есть кому разбирать вопрос.

    При выключенном ИИ кнопки нет, а команда остаётся и уходит в обычный
    поиск: пропавшая кнопка честнее кнопки, которая делает не то, что обещает.
    """
    if not settings.ai_enabled:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("ask.button", locale), callback_data="ask:start")
    ]])


# ── Поиск ───────────────────────────────────────────────────────────────────
@router.message(MenuButton(MENU_SEARCH))
async def search_start(message: Message, state: FSMContext, locale: str) -> None:
    await state.clear()
    await message.answer(
        f"{t('search.title', locale)}\n\n{t('search.hint', locale)}",
        reply_markup=_ask_kb(locale),
    )
    await state.set_state(Finding.query)


@router.message(Finding.query, F.text)
async def search_run(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    found = await search_service.search(
        session, user=user, grants=grants, query=message.text or ""
    )
    await state.clear()
    if found.empty:
        await message.answer(t("search.empty", locale))
        return

    lines = [t("search.found", locale, count=found.total), ""]
    rows: list[list[InlineKeyboardButton]] = []
    groups = (
        ("search.meetings", found.meetings), ("search.tasks", found.tasks),
        ("search.decisions", found.decisions), ("search.documents", found.documents),
        ("search.people", found.people),
    )
    for title_key, hits in groups:
        if not hits:
            continue
        lines.append(f"<b>{t(title_key, locale)}</b>")
        for hit in hits:
            lines.append(_line(hit, user.timezone))
            button = _button(hit)
            if button:
                rows.append(button)
        lines.append("")

    await message.answer(
        "\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:8]) if rows else None,
    )


# ── Вопрос своими словами ───────────────────────────────────────────────────
@router.callback_query(F.data == "ask:start")
async def ask_start(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    await state.clear()
    await call.message.answer(f"{t('ask.title', locale)}\n\n{t('ask.hint', locale)}")
    await state.set_state(Asking.question)
    await call.answer()


@router.message(Command("ask"))
async def ask_command(
    message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    """Вопрос одной строкой. Без текста — спрашиваем, о чём именно.

    Команда работает и при выключенном ИИ: разобрать вопрос будет некому,
    и он уйдёт в обычный поиск по словам. Это и есть «выключенный ИИ ничего
    не ломает»: функция отвечает хуже, но отвечает.
    """
    asked = (command.args or "").strip()
    if not asked:
        await message.answer(f"{t('ask.title', locale)}\n\n{t('ask.hint', locale)}")
        await state.set_state(Asking.question)
        return
    await state.clear()
    await _answer(message, session, user=user, grants=grants, locale=locale, asked=asked)


@router.message(Asking.question, F.text)
async def ask_run(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    await state.clear()
    await _answer(
        message, session, user=user, grants=grants, locale=locale, asked=message.text or ""
    )


async def _answer(
    message: Message, session: AsyncSession, *, user: User,
    grants: dict[str, Grant], locale: str, asked: str,
) -> None:
    """Показывает ответ на вопрос — вместе с тем, как вопрос понят.

    Строка «понял так» стоит первой и всегда. Молча неправильно понятый
    вопрос выглядит как правдивый ответ, и это худшее, что сценарий может
    сделать: человек уйдёт спокойным, не увидев того, о чём спрашивал.
    """
    answer = await question_ai.answer(session, asked, viewer=user, grants=grants)

    lines: list[str] = [t("ask.title", locale), ""]
    if answer.searched:
        # Разобрать не вышло. Человек должен видеть, что это поиск по буквам,
        # а не понятый вопрос: иначе пустота читается как «ничего нет».
        lines.append(t("ask.by_words", locale))
    elif answer.filter is not None:
        lines.append(question_service.describe(answer.filter, locale))
        lines.extend(t(note, locale) for note in answer.filter.notes)
    lines.append("")

    if answer.empty:
        lines.append(t("ask.empty", locale))
        await message.answer("\n".join(lines).strip())
        return

    rows: list[list[InlineKeyboardButton]] = []
    for hit in answer.hits:
        lines.append(_line(hit, user.timezone))
        button = _button(hit)
        if button:
            rows.append(button)
    if answer.more:
        lines += ["", t("ask.more", locale, count=question_service.MAX_ROWS)]

    await message.answer(
        "\n".join(lines).strip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:8]) if rows else None,
    )


# ── Реестр решений ──────────────────────────────────────────────────────────
@router.message(MenuButton(MENU_DECISIONS))
async def decisions_list(
    message: Message, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not has_permission(grants, "decision.read"):
        await message.answer(t("decision.no_access", locale))
        return

    items = await service.registry(session, user=user, grants=grants, only_open=True, limit=10)
    if not items:
        await message.answer(
            f"📌 <b>{t('decision.registry', locale)}</b>\n\n"
            f"{t('decision.none', locale)}",
            reply_markup=_registry_kb(grants, locale, has_items=False),
        )
        return

    lines = [f"📌 <b>{t('decision.open', locale)}</b>", ""]
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
            inline_keyboard=rows[:6]
            + _registry_kb(grants, locale, has_items=True).inline_keyboard
        ),
    )


def _registry_kb(
    grants: dict[str, Grant], locale: str, *, has_items: bool
) -> InlineKeyboardMarkup:
    rows = []
    if has_permission(grants, "decision.create"):
        rows.append([InlineKeyboardButton(
            text=t("decision.new", locale), callback_data="dn:new"
        )])
    if has_permission(grants, "export.read") and has_items:
        rows.append([InlineKeyboardButton(
            text=t("decision.export", locale), callback_data="ex:decisions"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("dn:card:"))
async def decision_card(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    decision_id = callback_int(call.data)
    decision = await session.get(Decision, decision_id) if decision_id else None
    if decision is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    # Видимость одной записи спрашивается у парной функции, а не выборкой
    # видимого списка: выборка ограничена `LIMIT`, и решение старше
    # пятисотого по дате отвечало бы «вам не открыто», хотя оно ваше.
    if not await service.may_read(session, decision=decision, viewer=user):
        await call.answer(t("decision.not_open", locale), show_alert=True)
        return

    author = await session.get(User, decision.author_id)
    responsible = (
        await session.get(User, decision.responsible_id) if decision.responsible_id else None
    )
    lines = [
        f"📌 <b>{esc(decision.title)}</b>",
        "",
        f"{t('decision.state', locale)}: "
        f"{t(service.STATUS_KEYS[decision.status], locale)}",
        f"{t('decision.author', locale)}: "
        f"{esc(author.full_name) if author else t('decision.unknown', locale)}",
    ]
    if responsible:
        lines.append(
            f"{t('decision.responsible', locale)}: {esc(responsible.full_name)}"
        )
    if decision.due_date:
        lines.append(
            f"{t('decision.due', locale)}: {fmt_dt(decision.due_date, user.timezone)}"
        )
    if decision.details:
        lines += ["", esc(cut(decision.details, 500))]
    if decision.meeting_id:
        meeting = await session.get(Meeting, decision.meeting_id)
        if meeting:
            lines.append(
                f"\n{t('decision.at_meeting', locale)}: {esc(cut(meeting.title, 60))}"
            )
    if decision.cancel_reason:
        lines.append(
            f"\n{t('decision.cancelled_at', locale)}: {esc(decision.cancel_reason)}"
        )

    rows = []
    if decision.status == DecisionStatus.OPEN and has_permission(grants, "decision.close"):
        rows.append([
            InlineKeyboardButton(text=t("decision.done", locale),
                                 callback_data=f"dn:done:{decision.id}"),
            InlineKeyboardButton(text=t("decision.cancel", locale),
                                 callback_data=f"dn:kill:{decision.id}"),
        ])
    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )
    await call.answer()


@router.callback_query(F.data.startswith("dn:done:"))
async def decision_done(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    decision = await session.get(Decision, callback_int(call.data) or 0)
    if decision is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    result = await service.close(session, decision=decision, actor=user, done=True)
    if not result.ok:
        await call.answer(result.reason or t("decision.failed", locale), show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(t("decision.closed", locale))
    await call.answer()


class Cancelling(StatesGroup):
    reason = State()


@router.callback_query(F.data.startswith("dn:kill:"))
async def decision_cancel_start(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    decision = await session.get(Decision, callback_int(call.data) or 0)
    if decision is None or decision.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(decision_id=decision.id)
    await call.message.answer(t("decision.ask_reason", locale))
    await state.set_state(Cancelling.reason)
    await call.answer()


@router.message(Cancelling.reason, F.text)
async def decision_cancel_finish(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    data = await state.get_data()
    decision = await session.get(Decision, data.get("decision_id", 0))
    if decision is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return
    result = await service.close(
        session, decision=decision, actor=user, done=False, reason=message.text or ""
    )
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or t("decision.cancel_failed", locale))
        return
    await message.answer(t("decision.cancelled", locale))


@router.callback_query(F.data == "dn:new")
async def decision_new(
    call: CallbackQuery, state: FSMContext, grants: dict[str, Grant], locale: str
) -> None:
    if not has_permission(grants, "decision.create"):
        await call.answer(t("decision.no_rights", locale), show_alert=True)
        return
    await state.clear()
    await call.message.answer(t("decision.ask_title", locale))
    await state.set_state(NewDecision.title)
    await call.answer()


@router.message(NewDecision.title, F.text)
async def decision_save(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, locale: str,
) -> None:
    result = await service.create(session, actor=user, title=message.text or "")
    await state.clear()
    if not result.ok:
        await message.answer(result.reason or t("decision.create_failed", locale))
        return
    await message.answer(
        f"{t('decision.saved', locale)}: <b>{esc(result.item.title)}</b>"
    )


# ── Выгрузка ────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ex:"))
async def export_run(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str, bot: Bot,
) -> None:
    kind = (call.data or "").split(":")[-1]
    if kind not in EXPORT_KIND_KEYS:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    until = utcnow()
    since = until - timedelta(days=EXPORT_DAYS)
    data, name, problem = await export_service.build(
        session, user=user, grants=grants, kind=kind, since=since, until=until, fmt="xlsx"
    )
    if data is None:
        await call.answer(problem or t("decision.export_failed", locale), show_alert=True)
        return
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(data, filename=name),
        caption=t(EXPORT_KIND_KEYS[kind], locale)
        + t("decision.export_period", locale, days=EXPORT_DAYS),
    )
    await call.answer()
