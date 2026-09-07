"""Вход в API: та же проверка, что и у бота, и тот же контекст.

**Ни одного нового описания прав.** Middleware бота собирает `user`, `grants`,
`features`, `locale` — здесь собирается ровно то же и теми же функциями.
Второе описание прав, «попроще, там же только чтение», рано или поздно
разойдётся с первым, и разойдётся молча: чтение и есть утечка.

Зависимость отдаёт `Caller` — всё, что нужно маршруту, чтобы ничего не решать
самому.
"""
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.initdata import InitDataError, Opened, check
from app.core.config import settings
from app.core.db import get_session
from app.core.i18n import normalize
from app.core.redis import redis
from app.models.enums import RoleCode, UserStatus
from app.models.org import Organization
from app.models.user import User
from app.services.features import load as load_features
from app.services.rbac import Grant, load_grants, user_role_codes
from app.services.registration import get_user_by_telegram_id

log = logging.getLogger("seta.api")

# Предел частоты: столько запросов от одного человека за минуту. Mini App
# открывает несколько экранов подряд и тянет данные пачкой, поэтому предел
# не жёсткий — он против перебора и зациклившегося клиента, а не против работы.
RATE_LIMIT = 120
RATE_WINDOW_SECONDS = 60


@dataclass(slots=True)
class Caller:
    """Тот, кто открыл приложение, и всё, что о нём известно."""

    session: AsyncSession
    user: User
    organization: Organization
    grants: dict[str, Grant]
    roles: set[RoleCode]
    features: dict[str, bool]
    locale: str
    opened: Opened


async def _rate_ok(telegram_user_id: int) -> bool:
    """Считает запросы в окне. Redis недоступен — пропускаем.

    Отказ из-за недоступного Redis превратил бы неполадку счётчика в отказ
    всей системы. Ограничитель — защита от перебора, а не рубеж доступа:
    рубеж выше, в проверке подписи.
    """
    key = f"api:rate:{telegram_user_id}"
    try:
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, RATE_WINDOW_SECONDS)
        return used <= RATE_LIMIT
    except Exception:  # pragma: no cover — сеть до Redis
        log.warning("ограничитель частоты недоступен")
        return True


async def caller(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[Caller]:
    """Проверяет подпись и собирает контекст. Иначе — отказ.

    Строка берётся из заголовка, а не из тела и не из параметра адреса:
    в адресе она попала бы в журналы прокси и истории браузера целиком,
    вместе с подписью.
    """
    if not x_telegram_init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нет initData")

    try:
        opened = check(x_telegram_init_data, settings.bot_token)
    except InitDataError as error:
        # В журнал — причина, наружу — общий отказ. Разница ответов подсказывает
        # подбирающему, насколько он близок.
        log.warning("initData отвергнута: %s", error)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData не принята") from error

    if not await _rate_ok(opened.telegram_user_id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "слишком часто")

    user = await get_user_by_telegram_id(session, opened.telegram_user_id)
    if user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "не зарегистрирован")
    if user.status != UserStatus.ACTIVE:
        # В боте неподтверждённый проходит дальше, чтобы закончить регистрацию.
        # В API ему делать нечего: регистрация идёт в чате, а не в приложении.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "доступ не открыт")

    organization = await session.get(Organization, user.organization_id)
    if organization is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "организация не найдена")

    yield Caller(
        session=session,
        user=user,
        organization=organization,
        grants=await load_grants(session, user),
        roles=await user_role_codes(session, user),
        features=await load_features(session, organization.id),
        locale=normalize(user.locale),
        opened=opened,
    )
