"""Профиль, помощь и разделы, которые появятся в следующих блоках.

Пока раздел не готов, бот честно говорит, в каком блоке он появится,
вместо того чтобы показывать пустой экран.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import (
    BTN_HELP,
    BTN_MY_DAY,
    BTN_MY_MEETINGS,
    BTN_PROFILE,
    BTN_REQUEST_MEETING,
    main_menu,
)
from app.core.timeutil import fmt_time
from app.models.enums import RoleCode
from app.models.org import Department
from app.models.schedule import WorkingHours
from app.models.user import User
from app.services.availability import get_view
from app.services.rbac import ROLE_TITLES

router = Router(name="menu")

COMING_SOON = {
    BTN_MY_MEETINGS: ("Встречи", 3),
    BTN_REQUEST_MEETING: ("Запрос встречи", 3),
    BTN_MY_DAY: ("Мой день", 3),
}


@router.message(F.text == BTN_PROFILE)
async def profile(message: Message, session: AsyncSession, user: User, roles: set[RoleCode]) -> None:
    department = "не указано"
    if user.department_id:
        dept = await session.get(Department, user.department_id)
        if dept:
            department = dept.name

    titles = ", ".join(ROLE_TITLES[r] for r in sorted(roles, key=lambda r: r.value)) or "без роли"

    hours = (
        await session.execute(
            select(WorkingHours).where(WorkingHours.user_id == user.id, WorkingHours.weekday == 0)
        )
    ).scalar_one_or_none()
    schedule = (
        f"{hours.start_time.strftime('%H:%M')}–{hours.end_time.strftime('%H:%M')}"
        if hours
        else "не задано"
    )

    lines = [
        "<b>Профиль</b>",
        "",
        f"👤 {user.full_name}",
        f"🏢 {department}",
        f"🔑 {titles}",
        f"📱 {user.phone or 'номер не подтверждён'}",
        f"🕐 Рабочий день: {schedule}",
        f"🌍 Часовой пояс: {user.timezone}",
    ]

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        view = await get_view(session, user.id)
        lines.append(f"📶 Доступность: {view.render(user.timezone)}")
        if view.until_at:
            lines.append(f"   действует до {fmt_time(view.until_at, user.timezone)}")

    await message.answer("\n".join(lines), reply_markup=main_menu(roles))


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def help_message(message: Message, roles: set[RoleCode]) -> None:
    lines = [
        "<b>Как пользоваться</b>",
        "",
        "Система ведёт встречи, поручения и контроль их исполнения.",
        "",
        "Сейчас работает: регистрация и права, поручения со сроками и контролем,",
        "напоминания и эскалация, индикатор доступности.",
        "Дальше по плану: календарь и встречи (блок 3).",
        "",
        "Команды: /start — главное меню, /help — эта справка, /id — ваш Telegram ID.",
    ]
    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        lines += [
            "",
            "<b>Индикатор доступности</b>",
            "Кнопка «Моя доступность» сообщает сотрудникам, что вы принимаете сейчас.",
            "У состояния всегда есть срок — по его истечении индикатор снимается сам.",
        ]
    await message.answer("\n".join(lines))


@router.message(F.text.in_(COMING_SOON.keys()))
async def coming_soon(message: Message) -> None:
    title, block = COMING_SOON[message.text]
    await message.answer(
        f"Раздел «{title}» появится в блоке {block}.\n"
        "Сейчас работают поручения: создание, сроки, напоминания, проверка."
    )
