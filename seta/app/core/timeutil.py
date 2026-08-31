"""Время. В базе всё в UTC, пользователю показываем его часовой пояс."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime, tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or settings.default_timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def to_utc(dt: datetime, tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or settings.default_timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def fmt_dt(dt: datetime, tz_name: str | None = None) -> str:
    local = to_local(dt, tz_name)
    return local.strftime("%d.%m.%Y %H:%M")


def fmt_time(dt: datetime, tz_name: str | None = None) -> str:
    return to_local(dt, tz_name).strftime("%H:%M")


def in_quiet_hours(dt: datetime, tz_name: str | None = None) -> bool:
    """Тихие часы: обычные уведомления ждут утра, критичные проходят всегда."""
    local = to_local(dt, tz_name).time()
    start = parse_hhmm(settings.quiet_hours_start)
    end = parse_hhmm(settings.quiet_hours_end)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def next_quiet_hours_end(dt: datetime, tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or settings.default_timezone)
    local = to_local(dt, tz_name)
    end = parse_hhmm(settings.quiet_hours_end)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=timezone.utc) if candidate.tzinfo else candidate.replace(tzinfo=tz)
