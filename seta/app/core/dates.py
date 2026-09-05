"""Разбор срока, написанного человеком.

Руководитель пишет «до пятницы» или «завтра», а не «2026-09-04T19:00:00+05:00».
Разбираем то, как люди действительно пишут сроки, и всегда показываем результат
на подтверждение - угаданная дата без подтверждения хуже, чем спросить.
"""
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.timeutil import parse_hhmm, to_local, to_utc

WEEKDAYS: dict[str, int] = {
    "понедельник": 0, "пн": 0, "понедельника": 0,
    "вторник": 1, "вт": 1, "вторника": 1,
    "среда": 2, "ср": 2, "среду": 2, "среды": 2,
    "четверг": 3, "чт": 3, "четверга": 3,
    "пятница": 4, "пт": 4, "пятницу": 4, "пятницы": 4,
    "суббота": 5, "сб": 5, "субботу": 5, "субботы": 5,
    "воскресенье": 6, "вс": 6, "воскресенья": 6,
}

MONTHS: dict[str, int] = {
    "января": 1, "январь": 1, "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3, "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5, "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7, "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9, "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11, "декабря": 12, "декабрь": 12,
}


def parse_due(
    text: str, tz_name: str | None = None, *, now: datetime | None = None
) -> datetime | None:
    """Превращает текст в срок (UTC). Возвращает None, если понять не удалось.

    Понимает: сегодня, завтра, послезавтра, день недели, «через N дней»,
    05.09, 05.09.2026, «5 сентября». Время по умолчанию - конец рабочего дня.

    `now` подставляется явно там, где точку отсчёта нельзя брать из часов
    машины: шаблон поручения считает срок от дня применения, и проверка обязана
    уметь задать этот день. Без параметра берётся текущий момент, как раньше.
    """
    if not text:
        return None

    raw = text.strip().lower().replace("ё", "е")
    raw = re.sub(r"^(до|к|на)\s+", "", raw)

    tz = ZoneInfo(tz_name or settings.default_timezone)
    now_local = to_local(now or datetime.now(tz=tz), tz_name)

    # Время указано отдельно: «завтра 15:00».
    # Только через двоеточие: точка означает дату, иначе «05.09» превращается в 05:09.
    explicit_time: time | None = None
    time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", raw)
    if time_match:
        hours, minutes = int(time_match.group(1)), int(time_match.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            explicit_time = time(hours, minutes)
            raw = raw.replace(time_match.group(0), " ").strip()

    default_time = explicit_time or parse_hhmm(settings.work_end)
    target: datetime | None = None

    if "послезавтра" in raw:
        target = now_local + timedelta(days=2)
    elif "завтра" in raw:
        target = now_local + timedelta(days=1)
    elif "сегодня" in raw:
        target = now_local

    if target is None:
        through = re.search(r"через\s+(\d{1,3})\s*(дн|день|дня|дней|недел)", raw)
        if through:
            amount = int(through.group(1))
            days = amount * 7 if through.group(2).startswith("недел") else amount
            target = now_local + timedelta(days=days)

    if target is None:
        for name, weekday in WEEKDAYS.items():
            if re.search(rf"\b{name}\b", raw):
                ahead = (weekday - now_local.weekday()) % 7
                # «в пятницу», сказанное в пятницу, означает следующую пятницу
                target = now_local + timedelta(days=ahead or 7)
                break

    if target is None:
        numeric = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", raw)
        if numeric:
            day, month = int(numeric.group(1)), int(numeric.group(2))
            year = int(numeric.group(3) or now_local.year)
            if year < 100:
                year += 2000
            try:
                target = now_local.replace(year=year, month=month, day=day)
            except ValueError:
                return None
            if numeric.group(3) is None and target.date() < now_local.date():
                target = target.replace(year=year + 1)

    if target is None:
        worded = re.search(r"\b(\d{1,2})\s+([а-я]+)\b", raw)
        if worded and worded.group(2) in MONTHS:
            day, month = int(worded.group(1)), MONTHS[worded.group(2)]
            try:
                target = now_local.replace(month=month, day=day)
            except ValueError:
                return None
            if target.date() < now_local.date():
                target = target.replace(year=target.year + 1)

    if target is None:
        return None

    target = target.replace(
        hour=default_time.hour, minute=default_time.minute, second=0, microsecond=0
    )
    return to_utc(target.replace(tzinfo=None), tz_name)


def humanize_due(due_at: datetime, tz_name: str | None = None) -> str:
    """«Завтра, 19:00» вместо «04.09.2026 19:00» - так понятнее с одного взгляда."""
    local = to_local(due_at, tz_name)
    today = to_local(datetime.now(tz=ZoneInfo(tz_name or settings.default_timezone)), tz_name).date()
    delta = (local.date() - today).days

    if delta == 0:
        prefix = "Сегодня"
    elif delta == 1:
        prefix = "Завтра"
    elif delta == 2:
        prefix = "Послезавтра"
    elif delta == -1:
        prefix = "Вчера"
    elif 0 < delta < 7:
        names = ["в понедельник", "во вторник", "в среду", "в четверг",
                 "в пятницу", "в субботу", "в воскресенье"]
        prefix = names[local.weekday()].capitalize()
    else:
        prefix = local.strftime("%d.%m.%Y")

    return f"{prefix}, {local.strftime('%H:%M')}"
