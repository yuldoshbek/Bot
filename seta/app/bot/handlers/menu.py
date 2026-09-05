"""Профиль, помощь и выбор языка.

Язык живёт здесь, а не в админке: его выбирает себе человек, а не кто-то
за него. Администратор задаёт язык по умолчанию для новых сотрудников —
и на этом его участие заканчивается.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import (
    MENU_HELP,
    MENU_PROFILE,
    MenuButton,
    language_button_kb,
    language_kb,
    main_menu,
)
from app.core.i18n import LOCALES, normalize, t
from app.core.text import esc
from app.core.timeutil import fmt_time
from app.models.enums import RoleCode
from app.models.org import Department
from app.models.schedule import WorkingHours
from app.models.user import User
from app.services.availability import get_view
from app.services.rbac import role_titles

router = Router(name="menu")

COMING_SOON: dict[str, tuple[str, int]] = {}


async def _profile_text(
    session: AsyncSession, user: User, roles: set[RoleCode], locale: str
) -> str:
    department = t("profile.department_none", locale)
    if user.department_id:
        dept = await session.get(Department, user.department_id)
        if dept:
            department = dept.name

    hours = (
        await session.execute(
            select(WorkingHours).where(WorkingHours.user_id == user.id, WorkingHours.weekday == 0)
        )
    ).scalar_one_or_none()
    schedule = (
        f"{hours.start_time.strftime('%H:%M')}–{hours.end_time.strftime('%H:%M')}"
        if hours
        else t("profile.workday_none", locale)
    )

    lines = [
        f"<b>{t('profile.title', locale)}</b>",
        "",
        f"👤 {esc(user.full_name)}",
        f"🏢 {esc(department)}",
        f"🔑 {role_titles(roles, locale)}",
        f"📱 {esc(user.phone) or t('profile.phone_none', locale)}",
        f"🕐 {t('profile.workday', locale)}: {schedule}",
        f"🌍 {t('profile.timezone', locale)}: {user.timezone}",
        f"🌐 {t('profile.language', locale)}: {LOCALES[normalize(locale)]}",
    ]

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        view = await get_view(session, user.id)
        lines.append(f"📶 {t('profile.availability', locale)}: {view.render(user.timezone)}")
        if view.until_at:
            lines.append(
                "   " + t("profile.availability_until", locale,
                          time=fmt_time(view.until_at, user.timezone))
            )

    return "\n".join(lines)


@router.message(MenuButton(MENU_PROFILE))
async def profile(
    message: Message, session: AsyncSession, user: User, roles: set[RoleCode], locale: str,
) -> None:
    await message.answer(
        await _profile_text(session, user, roles, locale),
        reply_markup=language_button_kb(locale),
    )


@router.callback_query(F.data == "lang:menu")
async def choose_language(call: CallbackQuery, user: User, locale: str) -> None:
    """Разворачивает выбор языка прямо в карточке профиля."""
    await call.message.edit_reply_markup(reply_markup=language_kb(locale))
    await call.answer()


@router.callback_query(F.data.startswith("lang:"))
async def switch_language(
    call: CallbackQuery, session: AsyncSession, user: User,
    roles: set[RoleCode], features: dict[str, bool], locale: str,
) -> None:
    """Смена языка. Ответ приходит уже на новом языке — иначе непонятно,
    сработало ли переключение.

    Нижнюю клавиатуру приходится присылать вторым сообщением: у сообщения
    может быть только одна разметка, а Telegram не обновляет уже лежащую
    в чате клавиатуру сам. Поэтому второе сообщение — короткое. Старые
    кнопки при этом продолжают работать: `MenuButton` сверяет надпись
    со всеми языками.
    """
    # Сверяется присланный код, а не приведённый: `normalize` никогда не вернёт
    # ничего за пределами списка языков, и проверка после неё не проверяла бы
    # ничего. «lang:menu», попав сюда, молча поставил бы человеку узбекский.
    code = call.data.split(":", 1)[1]
    if code not in LOCALES:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    chosen = normalize(code)

    user.locale = chosen
    await session.flush()

    await call.message.edit_text(
        await _profile_text(session, user, roles, chosen),
        reply_markup=language_kb(chosen),
    )
    await call.answer()
    await call.message.answer(
        t("profile.language_changed", chosen, language=LOCALES[chosen]),
        reply_markup=main_menu(roles, features, chosen),
    )


@router.message(MenuButton(MENU_HELP))
@router.message(Command("help"))
async def help_message(message: Message, roles: set[RoleCode], locale: str) -> None:
    lines = [
        f"<b>{t('help.title', locale)}</b>",
        "",
        t("help.intro", locale),
        "",
        t("help.works_now", locale),
        "",
        f"<b>{t('help.documents_title', locale)}</b>",
        t("help.documents", locale),
        "",
        f"<b>{t('help.search_title', locale)}</b>",
        t("help.search", locale),
        "",
        t("help.commands", locale),
    ]
    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        lines += [
            "",
            f"<b>{t('help.availability_title', locale)}</b>",
            t("help.availability", locale),
        ]
    await message.answer("\n".join(lines))


@router.message(F.text.in_(COMING_SOON.keys()))
async def coming_soon(message: Message) -> None:
    title, block = COMING_SOON[message.text]
    await message.answer(
        f"Раздел «{title}» появится в блоке {block}.\n"
        "Сейчас работают поручения: создание, сроки, напоминания, проверка."
    )
