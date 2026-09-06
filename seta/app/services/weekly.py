"""Недельный отчёт: пятнадцать показателей и движение относительно прошлой недели.

**Считает система.** Отчёт — это таблица показателей за неделю и то, как они
изменились. Ни одного числа здесь не берётся ниоткуда, кроме `analytics`,
и отчёт уходит целиком при выключенном ИИ. Слова сверху — добавка (`app/ai/report.py`).

**Движение показывается, оценка — нет.** Стрелка говорит, что число выросло;
хорошо это или плохо, зависит от показателя: выросшая дисциплина сроков —
хорошо, выросшая загрузка календаря — обычно нет. Раскладывать это по таблице
значило бы зашить в код пятнадцать суждений, которые спорны и меняются.
Оценка — работа человека, а с недавних пор и модели, которая пишет вступление.

**Прогноз не сравнивается с прошлой неделей.** «Перегрузка на следующей неделе»
считается от сегодняшнего дня, а не за период; сравнивать её с собой недельной
давности бессмысленно, поэтому у неё просто нет прошлого значения.

**Раз в неделю, в понедельник утром.** Отчёт за прошедшую неделю читают в её
начале — иначе он про позавчера. Ключ события содержит номер недели получателя,
и второго письма не будет, сколько бы раз ни прошёл планировщик.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import t
from app.core.text import esc
from app.core.timeutil import to_local, utcnow
from app.models import NotificationPriority, User
from app.services import analytics
from app.services import features as feature_service
from app.services.analytics import Metric, Period
from app.services.digest import recipients
from app.services.notifications import enqueue
from app.services.rbac import Grant, load_grants

log = logging.getLogger("seta.weekly")

# Понедельник, 08:00 по месту получателя. Час позже утренней сводки: два письма
# в одну минуту читаются как одно, и второе не читается вовсе.
WEEKLY_AT = time(8, 0)
WEEKLY_WEEKDAY = 0
WEEKLY_WINDOW = timedelta(minutes=180)

# Показатель, у которого нет «прошлой недели»: он считается от сегодняшнего дня.
FORECAST_KEY = "overload_forecast"


@dataclass(slots=True)
class Line:
    """Показатель за неделю и то, каким он был неделей раньше."""

    now: Metric
    before: Metric | None = None

    @property
    def key(self) -> str:
        return self.now.key

    @property
    def moved(self) -> str:
        """Куда сдвинулось число. Только факт, без оценки."""
        if self.before is None or self.now.value is None or self.before.value is None:
            return ""
        if abs(self.now.value - self.before.value) < 0.05:
            return "→"
        return "↑" if self.now.value > self.before.value else "↓"


@dataclass(slots=True)
class Report:
    """Собранный отчёт. Слова сверху сюда не входят — их пишет другой слой."""

    organization_id: int
    since: datetime
    until: datetime
    # С чем сравнивали. Хранится в отчёте, а не подразумевается: «неделя
    # к неделе» — утверждение, которое должно быть видно и проверяемо,
    # иначе сравнение с самим собой выглядит как ровный тренд.
    before_since: datetime | None = None
    before_until: datetime | None = None
    timezone: str = "Asia/Tashkent"
    lines: list[Line] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Нечего показывать: показателей нет или все молчат."""
        return not self.lines or all(line.now.no_data for line in self.lines)


def due_now(now: datetime, timezone_name: str) -> bool:
    """Наступило ли у этого человека время недельного отчёта."""
    local = to_local(now, timezone_name)
    if local.weekday() != WEEKLY_WEEKDAY:
        return False
    start = local.replace(
        hour=WEEKLY_AT.hour, minute=WEEKLY_AT.minute, second=0, microsecond=0
    )
    return start <= local < start + WEEKLY_WINDOW


def week_key(now: datetime, timezone_name: str) -> str:
    """Номер недели по месту получателя — он и делает отчёт однонедельным."""
    local = to_local(now, timezone_name)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


async def build(
    session: AsyncSession,
    *,
    viewer: User,
    grants: dict[str, Grant],
    now: datetime | None = None,
) -> Report:
    """Собирает отчёт: неделя и неделя до неё, теми же функциями показателей."""
    now = now or utcnow()
    report = Report(
        organization_id=viewer.organization_id,
        since=now - timedelta(days=7),
        until=now,
        timezone=viewer.timezone,
    )

    audience = await analytics.audience_for(session, viewer=viewer, grants=grants)
    if audience is None or audience.empty:
        return report

    week = Period(
        since=report.since, until=report.until,
        title_key="metric.period.days", title_args={"days": 7},
    )
    earlier = Period(
        since=now - timedelta(days=14), until=now - timedelta(days=7),
        title_key="metric.period.days", title_args={"days": 7},
    )
    report.before_since, report.before_until = earlier.since, earlier.until

    current = await analytics.all_metrics(
        session, audience=audience, period=week, now=now
    )
    previous = {
        metric.key: metric
        for metric in await analytics.all_metrics(
            session, audience=audience, period=earlier, now=now
        )
    }
    report.lines = [
        Line(
            now=metric,
            # У прогноза прошлого значения нет: он считается от сегодняшнего
            # дня, а не за период, и сравнение с собой недельной давности
            # ничего не значит.
            before=None if metric.key == FORECAST_KEY else previous.get(metric.key),
        )
        for metric in current
    ]
    return report


def render(
    report: Report, locale: str | None = None, *, intro: str | None = None
) -> str:
    """Письмо одним сообщением. Без `intro` выглядит ровно так, как без ИИ."""
    since = to_local(report.since, report.timezone).strftime("%d.%m")
    until = to_local(report.until, report.timezone).strftime("%d.%m")
    lines = [
        f"<b>{t('weekly.title', locale, since=since, until=until)}</b>",
        "",
    ]
    if intro:
        lines += [intro, ""]

    for line in report.lines:
        mark = ""
        if line.moved and line.before is not None:
            mark = (
                f"  {line.moved} "
                f"{t('weekly.was', locale, value=line.before.shown())}"
            )
        lines.append(f"· {esc(line.now.render(locale))}{mark}")
    return "\n".join(lines)


async def send_reports(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    words=None,
) -> int:
    """Один проход. `words` пишет вступление — служба не знает, кто именно.

    Без него отчёт собирается ровно так, как собирался бы без ИИ вовсе:
    таблица показателей остаётся основным содержимым письма.
    """
    now = now or utcnow()
    sent = 0
    for viewer in await recipients(session):
        if not due_now(now, viewer.timezone):
            continue
        state = await feature_service.load(session, viewer.organization_id)
        if not feature_service.is_on(state, "analytics"):
            # Показатели выключены — отчёта о них быть не может.
            continue

        grants = await load_grants(session, viewer)
        report = await build(session, viewer=viewer, grants=grants, now=now)
        if report.empty:
            # Пустой отчёт не рассылается: письмо «данных нет» через месяц
            # перестают открывать вместе с теми, где данные есть.
            continue

        intro = await words(session, report, viewer) if words else ""
        created = await enqueue(
            session,
            user_id=viewer.id,
            organization_id=viewer.organization_id,
            event_key=f"weekly:{viewer.id}:{week_key(now, viewer.timezone)}",
            kind="report.weekly",
            priority=NotificationPriority.NORMAL,
            body=render(report, viewer.locale, intro=intro or None),
            payload={"kind": "weekly"},
            timezone_name=viewer.timezone,
        )
        sent += int(created)
    return sent
