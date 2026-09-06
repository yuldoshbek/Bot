"""Проверка подписи `initData` — единственный вход в API.

Telegram передаёт Mini App строку `initData`: кто открыл приложение, когда
и подпись. Подпись считается от токена бота, поэтому подделать её, не зная
токена, нельзя.

**Что здесь охраняется.** Не «удобство входа», а тот факт, что весь API верит
одному значению — идентификатору Telegram внутри подписанной строки. Если
проверку обойти, обойдено вообще всё: права, области видимости, границы
организаций. Поэтому разбор написан отдельно от маршрутов и проверяется
отдельно.

**Идентификатор берётся только из подписанной строки.** Ни из тела запроса,
ни из заголовка, ни из параметра. Подменённый `user_id` в теле не должен
менять ничего — на это есть отдельный сценарий.
"""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# Строка живёт сутки. Дольше — потому что Mini App держат открытым весь день
# и переоткрывать его на каждый запрос никто не станет; меньше суток означало бы
# ложные разлогины в середине рабочего дня. Больше — потому что перехваченная
# строка тогда работала бы неделю.
MAX_AGE_SECONDS = 24 * 60 * 60

# Ключ для подписи выводится из токена по правилу Telegram: сначала HMAC
# от постоянной строки, потом им подписываются данные.
_KEY_SALT = b"WebAppData"


class InitDataError(Exception):
    """Строка не прошла проверку. Текст пригоден для журнала, не для человека."""


@dataclass(slots=True)
class Opened:
    """Кто и когда открыл приложение — по подписанным данным, а не по словам."""

    telegram_user_id: int
    username: str | None
    language_code: str | None
    auth_date: int
    query_id: str | None = None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(_KEY_SALT, bot_token.encode(), hashlib.sha256).digest()


def check(init_data: str, bot_token: str, *, now: int | None = None) -> Opened:
    """Разбирает и проверяет `initData`. Бросает `InitDataError`, если что-то не так.

    Порядок проверок важен: сначала подпись, потом срок. Наоборот было бы
    подсказкой — по разнице ответов «просрочено» и «подделано» подбирающий
    узнавал бы, что подпись он уже угадал.
    """
    if not bot_token:
        raise InitDataError("токен бота не задан")
    if not init_data:
        raise InitDataError("пустая строка")

    # `parse_qsl` без `keep_blank_values` выбросил бы пустые поля, а Telegram
    # включает их в подпись — и сверка тогда не сойдётся на ровном месте.
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise InitDataError("строка не разбирается")

    fields = dict(pairs)
    given = fields.pop("hash", None)
    if not given:
        raise InitDataError("нет подписи")

    # Подписывается всё, кроме самой подписи, — по возрастанию имён полей.
    # Порядок задан Telegram и не зависит от того, в каком порядке пришла строка.
    payload = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    expected = hmac.new(
        _secret_key(bot_token), payload.encode(), hashlib.sha256
    ).hexdigest()

    # Сравнение постоянного времени: обычное `==` завершается на первом
    # несовпавшем знаке, и по времени ответа подпись подбирается посимвольно.
    if not hmac.compare_digest(expected, given):
        raise InitDataError("подпись не совпадает")

    raw_date = fields.get("auth_date", "")
    if not raw_date.isdigit():
        raise InitDataError("нет времени подписи")
    auth_date = int(raw_date)

    moment = int(time.time()) if now is None else now
    if moment - auth_date > MAX_AGE_SECONDS:
        raise InitDataError("строка просрочена")
    # Время из будущего — признак подделки или разъехавшихся часов. Небольшой
    # запас нужен: часы клиента и сервера расходятся на секунды всегда.
    if auth_date - moment > 300:
        raise InitDataError("время подписи в будущем")

    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("нет данных пользователя")
    try:
        person = json.loads(raw_user)
    except json.JSONDecodeError as error:
        raise InitDataError("данные пользователя не разбираются") from error
    if not isinstance(person, dict):
        raise InitDataError("данные пользователя не объект")

    telegram_user_id = person.get("id")
    if not isinstance(telegram_user_id, int):
        raise InitDataError("нет идентификатора пользователя")

    return Opened(
        telegram_user_id=telegram_user_id,
        username=person.get("username"),
        language_code=person.get("language_code"),
        auth_date=auth_date,
        query_id=fields.get("query_id"),
    )
