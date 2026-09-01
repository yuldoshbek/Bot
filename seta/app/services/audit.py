"""Запись в журнал аудита. Единственный способ добавить запись в audit_log."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User


async def write_audit(
    session: AsyncSession,
    *,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    on_behalf_of_id: int | None = None,
    source: str = "bot",
) -> AuditLog:
    """Фиксирует действие. Вызывается в той же транзакции, что и само изменение.

    on_behalf_of_id заполняется, когда ассистент действует по делегированию:
    в журнале остаётся и настоящий автор, и тот, от чьего имени шло действие.
    """
    # Организацию берём у автора действия: журнал читается выборкой по времени,
    # без неё администратор одной организации увидел бы действия другой.
    organization_id = None
    if actor_id is not None:
        actor = await session.get(User, actor_id)
        organization_id = actor.organization_id if actor else None

    entry = AuditLog(
        organization_id=organization_id,
        actor_id=actor_id,
        on_behalf_of_id=on_behalf_of_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=before,
        after_json=after,
        reason=reason,
        source=source,
    )
    session.add(entry)
    await session.flush()
    return entry
