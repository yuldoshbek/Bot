"""Кнопка, открывающая Mini App.

Живёт в синей кнопке «Меню» рядом с полем ввода, а не в нижней клавиатуре.
Причина простая: клавиатура собрана по ролям и в ней уже семь кнопок,
а восьмая перестаёт читаться. Кнопка «Меню» — штатное место Telegram
для приложения, и она ничего не занимает.

**Выключение выключает.** Адрес не задан — кнопка не просто не ставится,
а возвращается к обычной. Иначе снятый адрес оставил бы в чатах кнопку,
которая открывает то, чего больше нет: у каждого, кто её однажды получил.

**Надпись на языке человека.** Там, где язык известен — при `/start`
и при смене языка, — кнопка ставится на этот чат. Общая, для всех остальных,
остаётся на основном языке системы.
"""
import logging

from aiogram import Bot
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

from app.core.config import settings
from app.core.i18n import t

log = logging.getLogger("seta.bot")


def configured() -> bool:
    """Есть ли приложение вообще."""
    return bool(settings.miniapp_url.strip())


def button(locale: str | None = None) -> MenuButtonWebApp | MenuButtonCommands:
    """Что должно стоять в кнопке «Меню» у этого человека."""
    if not configured():
        return MenuButtonCommands()
    return MenuButtonWebApp(
        text=t("miniapp.button", locale),
        web_app=WebAppInfo(url=settings.miniapp_url.strip()),
    )


async def set_default(bot: Bot) -> None:
    """Общая кнопка для всех чатов. Ставится один раз, при запуске.

    Обращение к Telegram при старте не должно ронять запуск: не поставившаяся
    кнопка — это неудобство, а упавший при старте бот — отсутствие системы.
    """
    try:
        await bot.set_chat_menu_button(menu_button=button(settings.default_locale))
    except Exception as error:  # pragma: no cover — сеть до Telegram
        log.warning("кнопка приложения не поставлена: %s", error)


async def set_for(bot: Bot, chat_id: int, locale: str | None) -> None:
    """Кнопка этому человеку, на его языке.

    Отказ здесь тем более не должен ничего ломать: человек пришёл
    зарегистрироваться или сменить язык, а не за кнопкой.
    """
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=button(locale))
    except Exception as error:  # pragma: no cover — сеть до Telegram
        log.warning("кнопка приложения не поставлена в чате %s: %s", chat_id, error)
