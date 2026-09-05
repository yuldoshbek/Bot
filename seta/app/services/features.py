"""Переключатели разделов: администратор выключает функцию, а не ломает её.

Раздел, выключенный переключателем, обязан исчезнуть целиком: кнопка из меню
и обработчик за ней. Спрятать кнопку, оставив обработчик, — это не выключение,
а маскировка: нажатая старая кнопка или подставленный callback всё равно
откроют раздел.

**Отсутствие записи означает «включено».** Таблица хранит только то, что
администратор осознанно выключил. Иначе новая организация не работала бы,
пока кто-нибудь не заполнит справочник, а забытый в нём раздел выглядел бы
поломкой без причины.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeatureFlag, User
from app.services.audit import write_audit

# Что можно выключить. Список короткий намеренно: переключатель на каждую
# кнопку превращает админку во второй исходник, в котором никто не разберётся.
FEATURES: dict[str, str] = {
    "meetings": "Встречи и календарь",
    "documents": "Документы и поиск по ним",
    "templates": "Типовые поручения",
    "analytics": "Показатели на экране руководителя",
    "digest": "Утренняя сводка",
}

# Что человек видит вместо раздела. Не «ошибка», а объяснение: раздел закрыт
# осознанно, и обращаться нужно к администратору, а не в поддержку.
OFF_MESSAGE = "Этот раздел выключен администратором организации."


@dataclass(slots=True)
class Switch:
    """Один переключатель, готовый к показу в админке."""

    code: str
    title: str
    enabled: bool


async def load(session: AsyncSession, organization_id: int) -> dict[str, bool]:
    """Состояние всех переключателей организации — одним запросом.

    Читается на каждый апдейт вместе с правами, поэтому запрос ровно один
    и возвращает только отклонения: остальное включено по умолчанию.
    """
    rows = (
        await session.execute(
            select(FeatureFlag.code, FeatureFlag.enabled).where(
                FeatureFlag.organization_id == organization_id
            )
        )
    ).all()
    state = {code: True for code in FEATURES}
    for code, enabled in rows:
        if code in state:
            state[code] = bool(enabled)
    return state


def is_on(features: dict[str, bool] | None, code: str) -> bool:
    """Включён ли раздел. Неизвестный код считается включённым.

    Пустой словарь — это не «всё выключено», а «состояние не загружено»:
    так бывает в фоновом цикле и в проверках. Ронять работу из-за этого нельзя.
    """
    if not features:
        return True
    return features.get(code, True)


async def switch(
    session: AsyncSession,
    *,
    organization_id: int,
    code: str,
    enabled: bool,
    actor: User,
) -> str | None:
    """Включает или выключает раздел. Возвращает причину отказа или None."""
    if code not in FEATURES:
        return "Неизвестный раздел."
    if actor.organization_id != organization_id:
        return "Это другая организация."

    flag = (
        await session.execute(
            select(FeatureFlag).where(
                FeatureFlag.organization_id == organization_id,
                FeatureFlag.code == code,
            )
        )
    ).scalar_one_or_none()

    was = flag.enabled if flag is not None else True
    if flag is None:
        flag = FeatureFlag(organization_id=organization_id, code=code, enabled=enabled)
        session.add(flag)
    else:
        flag.enabled = enabled
    flag.updated_by = actor.id
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="feature.switch",
        entity_type="feature_flag", entity_id=flag.id,
        before={"code": code, "enabled": was},
        after={"code": code, "enabled": enabled},
    )
    return None


async def switches(session: AsyncSession, organization_id: int) -> list[Switch]:
    """Все переключатели в порядке каталога — для экрана администратора."""
    state = await load(session, organization_id)
    return [Switch(code=code, title=title, enabled=state[code]) for code, title in FEATURES.items()]
