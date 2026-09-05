"""Клавиатуры. Максимум 5-7 кнопок на экран, понятные названия, без техники.

**Кнопка нижнего меню — это текст сообщения.** Telegram присылает не код
кнопки, а её надпись, поэтому обработчик ловит её сравнением строк. Как только
надписи стали переводиться, сравнение с одной строкой перестало работать:
человек с узбекским интерфейсом жмёт «Profil», а обработчик ждёт «Профиль».

Отсюда `MenuButton`: фильтр сверяет надпись со **всеми** переводами ключа.
Это не только чинит перевод, но и решает вторую задачу, которую иначе пришлось
бы решать отдельно: Telegram не обновляет нижнюю клавиатуру, уже лежащую
в чате. После смены языка человек какое-то время жмёт старые кнопки — и они
обязаны работать.
"""
from aiogram.filters import Filter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.core.i18n import LOCALES, t
from app.models.enums import RoleCode

# ── Пункты главного меню ────────────────────────────────────────────────────
# Ключи, а не надписи: надпись зависит от языка, ключ — нет.
MENU_MY_DAY = "menu.my_day"
MENU_MY_MEETINGS = "menu.my_meetings"
MENU_MY_TASKS = "menu.my_tasks"
MENU_REQUEST_MEETING = "menu.request_meeting"
MENU_NEW_TASK = "menu.new_task"
MENU_QUICK_MEETING = "menu.quick_meeting"
MENU_DECISIONS = "menu.decisions"
MENU_SEARCH = "menu.search"
MENU_CONTROL = "menu.control"
MENU_AVAILABILITY = "menu.availability"
MENU_WHO_IS_OPEN = "menu.who_is_open"
MENU_ADMIN = "menu.admin"
MENU_PROFILE = "menu.profile"
MENU_HELP = "menu.help"


def texts_for(key: str) -> set[str]:
    """Все надписи одной кнопки — на всех языках сразу.

    Нужно для разбора входящего сообщения: язык человека мог смениться,
    а кнопка в чате осталась прежней.
    """
    return {t(key, locale) for locale in LOCALES}


class MenuButton(Filter):
    """Нажата кнопка меню — на любом из языков."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def __call__(self, message: Message) -> bool:
        # Надписи собираются на каждый вызов, а не в __init__: фильтры
        # создаются при импорте обработчиков, и словари к этому моменту
        # могут быть ещё не загружены.
        return message.text in texts_for(self.key)


# Какие кнопки исчезают вместе с выключенным разделом. Кнопка — только половина
# выключения: обработчик за ней обязан отказать сам, иначе старая кнопка
# в истории чата продолжит открывать закрытый раздел.
FEATURE_BUTTONS: dict[str, set[str]] = {
    "meetings": {MENU_MY_DAY, MENU_MY_MEETINGS, MENU_REQUEST_MEETING, MENU_QUICK_MEETING},
}


def main_menu(
    roles: set[RoleCode],
    features: dict[str, bool] | None = None,
    locale: str | None = None,
) -> ReplyKeyboardMarkup:
    """Меню собирается по ролям, по включённым разделам и на языке человека."""
    def key(name: str) -> KeyboardButton:
        return KeyboardButton(text=t(name, locale))

    rows: list[list[KeyboardButton]] = []
    layout: list[list[str]]

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        layout = [
            [MENU_MY_DAY, MENU_CONTROL],
            [MENU_NEW_TASK, MENU_AVAILABILITY],
            [MENU_QUICK_MEETING, MENU_DECISIONS],
            [MENU_SEARCH],
        ]
    elif RoleCode.DEPT_HEAD in roles:
        layout = [
            [MENU_MY_MEETINGS, MENU_MY_TASKS],
            [MENU_NEW_TASK, MENU_CONTROL],
            [MENU_REQUEST_MEETING, MENU_QUICK_MEETING],
            [MENU_DECISIONS, MENU_SEARCH],
        ]
    else:
        layout = [
            [MENU_MY_MEETINGS, MENU_MY_TASKS],
            [MENU_REQUEST_MEETING, MENU_WHO_IS_OPEN],
            [MENU_DECISIONS, MENU_SEARCH],
        ]

    if features:
        hidden = {
            name
            for code, buttons in FEATURE_BUTTONS.items()
            if not features.get(code, True)
            for name in buttons
        }
        if hidden:
            layout = [[name for name in row if name not in hidden] for row in layout]
            layout = [row for row in layout if row]

    rows = [[key(name) for name in row] for row in layout]

    if RoleCode.ADMIN in roles:
        rows.append([key(MENU_ADMIN)])

    rows.append([key(MENU_PROFILE), key(MENU_HELP)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def request_contact_kb(locale: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("start.contact_button", locale), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def role_choice_kb(locale: str | None = None) -> InlineKeyboardMarkup:
    """Роли, которые можно запросить при самостоятельной регистрации."""
    codes = ["EMPLOYEE", "DEPT_HEAD", "ASSISTANT", "EXECUTIVE"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(f"role.{code.lower()}", locale),
                                  callback_data=f"reg:role:{code}")]
            for code in codes
        ]
    )


def department_choice_kb(
    departments: list[tuple[int, str]], locale: str | None = None
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"reg:dept:{dept_id}")]
        for dept_id, name in departments
    ]
    rows.append([InlineKeyboardButton(text=t("start.skip", locale), callback_data="reg:dept:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_button_kb(locale: str | None = None) -> InlineKeyboardMarkup:
    """Одна кнопка под карточкой профиля: раскрыть выбор языка."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=t("profile.change_language", locale), callback_data="lang:menu"
            )
        ]]
    )


def language_kb(current: str, locale: str | None = None) -> InlineKeyboardMarkup:
    """Выбор языка. Текущий отмечен галочкой — иначе непонятно, что уже стоит.

    Названия языков не переводятся: язык называется на самом себе, чтобы его
    узнал тот, кто нынешнего интерфейса не понимает. Это и есть тот человек,
    ради которого экран существует.
    """
    from app.core.i18n import available, normalize

    current = normalize(current)
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if code == current else "") + title,
            callback_data=f"lang:{code}",
        )]
        for code, title in available()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval_kb(user_id: int, locale: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"adm:approve:{user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:reject:{user_id}"),
            ],
            [InlineKeyboardButton(text="✏️ Изменить роль", callback_data=f"adm:role:{user_id}")],
        ]
    )


def approval_role_kb(user_id: int, locale: str | None = None) -> InlineKeyboardMarkup:
    codes = [
        RoleCode.EMPLOYEE, RoleCode.DEPT_HEAD, RoleCode.ASSISTANT,
        RoleCode.EXECUTIVE, RoleCode.ADMIN,
    ]
    rows = [
        [InlineKeyboardButton(
            text=t(f"role.{code.value.lower()}", locale),
            callback_data=f"adm:setrole:{user_id}:{code}",
        )]
        for code in codes
    ]
    rows.append([InlineKeyboardButton(text=t("common.back", locale),
                                      callback_data=f"adm:card:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def availability_kb() -> InlineKeyboardMarkup:
    """Переключение индикатора: одно касание, срок задаётся сразу."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Принимаю 30 мин", callback_data="av:OPEN:30"),
                InlineKeyboardButton(text="🟢 1 час", callback_data="av:OPEN:60"),
            ],
            [
                InlineKeyboardButton(text="🟢 До конца дня", callback_data="av:OPEN:day"),
                InlineKeyboardButton(text="🌙 Поздний приём", callback_data="av:OPENLATE:120"),
            ],
            [
                InlineKeyboardButton(text="🟡 Занят 1 час", callback_data="av:BUSY:60"),
                InlineKeyboardButton(text="🔴 Не беспокоить", callback_data="av:DND:120"),
            ],
            [InlineKeyboardButton(text="⚪ Снять индикатор", callback_data="av:OFF:0")],
        ]
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Заявки на регистрацию", callback_data="adm:pending")],
            [InlineKeyboardButton(text="👥 Сотрудники", callback_data="adm:users")],
            [InlineKeyboardButton(text="🏢 Отделы", callback_data="adm:depts")],
            [InlineKeyboardButton(text="🔗 Ссылки-приглашения", callback_data="adm:invites")],
            [InlineKeyboardButton(text="🕐 Рабочие часы", callback_data="adm:hours")],
            [InlineKeyboardButton(text="⏳ Лимиты времени", callback_data="adm:quotas")],
            [InlineKeyboardButton(text="📆 Праздники", callback_data="adm:holidays")],
            [InlineKeyboardButton(text="🏖 Отпуска", callback_data="adm:absences")],
            [InlineKeyboardButton(text="🎛 Разделы системы", callback_data="adm:features")],
            [InlineKeyboardButton(text="📜 Журнал действий", callback_data="adm:audit")],
        ]
    )
