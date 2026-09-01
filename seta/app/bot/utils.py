"""Мелкие помощники для обработчиков.

Данные в callback приходят от клиента и могут быть какими угодно: старая кнопка
из вчерашнего сообщения, пересланное сообщение, подставленное вручную значение.
Разбирать их через голый int() нельзя — обработчик упадёт, а человек увидит
зависшую кнопку без объяснений.
"""


def callback_int(data: str | None, index: int = -1) -> int | None:
    """Достаёт число из callback-данных. Возвращает None, если там не число."""
    if not data:
        return None
    parts = data.split(":")
    try:
        return int(parts[index])
    except (ValueError, IndexError):
        return None


STALE_BUTTON = "Кнопка устарела. Откройте раздел заново."
