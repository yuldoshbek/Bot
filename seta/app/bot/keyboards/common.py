"""Клавиатуры. Максимум 5-7 кнопок на экран, понятные названия, без техники."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.models.enums import RoleCode

# ── Названия пунктов главного меню ──────────────────────────────────────────
BTN_MY_DAY = "📅 Мой день"
BTN_MY_MEETINGS = "📅 Мои встречи"
BTN_MY_TASKS = "📋 Мои поручения"
BTN_REQUEST_MEETING = "➕ Запросить встречу"
BTN_NEW_TASK = "➕ Поручение"
BTN_QUICK_MEETING = "⚡ Совещание"
BTN_DECISIONS = "📌 Решения"
BTN_SEARCH = "🔎 Поиск"
BTN_CONTROL = "📊 Контроль"
BTN_AVAILABILITY = "🟢 Моя доступность"
BTN_WHO_IS_OPEN = "👤 Кто на связи"
BTN_ADMIN = "🛠 Администрирование"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "❓ Помощь"


# Какие кнопки исчезают вместе с выключенным разделом. Кнопка — только половина
# выключения: обработчик за ней обязан отказать сам, иначе старая кнопка
# в истории чата продолжит открывать закрытый раздел.
FEATURE_BUTTONS: dict[str, set[str]] = {
    "meetings": {BTN_MY_DAY, BTN_MY_MEETINGS, BTN_REQUEST_MEETING, BTN_QUICK_MEETING},
}


def main_menu(
    roles: set[RoleCode], features: dict[str, bool] | None = None
) -> ReplyKeyboardMarkup:
    """Меню собирается по ролям и по включённым разделам."""
    rows: list[list[KeyboardButton]] = []

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        rows.append([KeyboardButton(text=BTN_MY_DAY), KeyboardButton(text=BTN_CONTROL)])
        rows.append([KeyboardButton(text=BTN_NEW_TASK), KeyboardButton(text=BTN_AVAILABILITY)])
        rows.append([KeyboardButton(text=BTN_QUICK_MEETING), KeyboardButton(text=BTN_DECISIONS)])
        rows.append([KeyboardButton(text=BTN_SEARCH)])
    elif RoleCode.DEPT_HEAD in roles:
        rows.append([KeyboardButton(text=BTN_MY_MEETINGS), KeyboardButton(text=BTN_MY_TASKS)])
        rows.append([KeyboardButton(text=BTN_NEW_TASK), KeyboardButton(text=BTN_CONTROL)])
        rows.append([KeyboardButton(text=BTN_REQUEST_MEETING), KeyboardButton(text=BTN_QUICK_MEETING)])
        rows.append([KeyboardButton(text=BTN_DECISIONS), KeyboardButton(text=BTN_SEARCH)])
    else:
        rows.append([KeyboardButton(text=BTN_MY_MEETINGS), KeyboardButton(text=BTN_MY_TASKS)])
        rows.append([KeyboardButton(text=BTN_REQUEST_MEETING), KeyboardButton(text=BTN_WHO_IS_OPEN)])
        rows.append([KeyboardButton(text=BTN_DECISIONS), KeyboardButton(text=BTN_SEARCH)])

    if features:
        hidden = {
            text
            for code, buttons in FEATURE_BUTTONS.items()
            if not features.get(code, True)
            for text in buttons
        }
        if hidden:
            rows = [
                [button for button in row if button.text not in hidden] for row in rows
            ]
            rows = [row for row in rows if row]

    if RoleCode.ADMIN in roles:
        rows.append([KeyboardButton(text=BTN_ADMIN)])

    rows.append([KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def request_contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Подтвердить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def role_choice_kb() -> InlineKeyboardMarkup:
    """Роли, которые можно запросить при самостоятельной регистрации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сотрудник", callback_data="reg:role:EMPLOYEE")],
            [InlineKeyboardButton(text="Начальник отдела", callback_data="reg:role:DEPT_HEAD")],
            [InlineKeyboardButton(text="Ассистент", callback_data="reg:role:ASSISTANT")],
            [InlineKeyboardButton(text="Руководитель", callback_data="reg:role:EXECUTIVE")],
        ]
    )


def department_choice_kb(departments: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"reg:dept:{dept_id}")]
        for dept_id, name in departments
    ]
    rows.append([InlineKeyboardButton(text="Пропустить", callback_data="reg:dept:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"adm:approve:{user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:reject:{user_id}"),
            ],
            [InlineKeyboardButton(text="✏️ Изменить роль", callback_data=f"adm:role:{user_id}")],
        ]
    )


def approval_role_kb(user_id: int) -> InlineKeyboardMarkup:
    codes = [
        (RoleCode.EMPLOYEE, "Сотрудник"),
        (RoleCode.DEPT_HEAD, "Начальник отдела"),
        (RoleCode.ASSISTANT, "Ассистент"),
        (RoleCode.EXECUTIVE, "Руководитель"),
        (RoleCode.ADMIN, "Администратор"),
    ]
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"adm:setrole:{user_id}:{code}")]
        for code, title in codes
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"adm:card:{user_id}")])
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
