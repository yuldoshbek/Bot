"""Язык интерфейса: узбекский основной, русский дополнительный.

Три варианта, а не два языка:

    uz       узбекский латиницей   — основной, эталон формулировок
    uz-Cyrl  узбекский кириллицей  — выводится из латиницы правилом
    ru       русский               — дополнительный, отдельный словарь

**Эталон — узбекская латиница.** Ключ, которого нет в узбекском, не существует
вовсе: проверка на это отдельная. Русский может отставать, и тогда человек
увидит узбекскую строку — это хуже, чем перевод, но лучше, чем пустота или
имя ключа на экране.

**Кириллица не набирается руками.** Она получается из латиницы переводом
письменности (`app.core.translit`). Полторы тысячи строк, набранных дважды,
разошлись бы на первой правке. Где правило даёт неверное написание — короткая
таблица исключений, а не отказ от правила.

**Ключ — не русская строка.** `t("menu.tasks")`, а не `t("Мои поручения")`.
Строка как ключ выглядит удобной ровно до первой правки формулировки: поправил
текст — потерял все переводы.

Отсутствие перевода не роняет ответ: `t()` возвращает узбекский, а если и его
нет — сам ключ. Экран с `menu.tasks` вместо кнопки уродлив, но система при этом
работает, и проверка такое ловит.
"""
import logging
from typing import Any

from app.core.translit import to_cyrillic

log = logging.getLogger("seta.i18n")

# Эталон и запасной вариант для всех остальных.
BASE_LOCALE = "uz"
# Кириллица выводится из эталона, а не хранится отдельно.
DERIVED_LOCALE = "uz-Cyrl"

LOCALES: dict[str, str] = {
    "uz": "Oʻzbekcha (lotin)",
    "uz-Cyrl": "Ўзбекча (кирилл)",
    "ru": "Русский",
}

# Короткая подпись для кнопки выбора языка.
LOCALE_SHORT: dict[str, str] = {
    "uz": "UZ",
    "uz-Cyrl": "ЎЗ",
    "ru": "RU",
}


def normalize(locale: str | None) -> str:
    """Приводит код языка к одному из трёх известных.

    Незнакомый код — не ошибка: у человека в базе может стоять что угодно,
    и падать из-за этого нельзя. Возвращается основной язык.
    """
    if not locale:
        return BASE_LOCALE
    value = locale.strip()
    if value in LOCALES:
        return value
    # Терпим написание вида «uz_CYRL», «UZ-Cyrl», «ru-RU».
    lowered = value.lower().replace("_", "-")
    if lowered.startswith("uz-cyr"):
        return DERIVED_LOCALE
    if lowered.startswith("uz"):
        return "uz"
    if lowered.startswith("ru"):
        return "ru"
    return BASE_LOCALE


class Catalogue:
    """Словари всех языков и правило вывода кириллицы.

    Собирается один раз при импорте: перевод вызывается на каждую строку
    каждого сообщения, и искать по файлам на каждый вызов недопустимо.
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, str]] = {}
        self._misses: set[str] = set()
        self._loaded = False
        # Кириллица вычисляется, а не хранится, и потому запоминается после
        # первого раза. Без этого каждая строка каждого сообщения переводилась
        # бы посимвольно заново — а фильтр кнопок делает это ещё и на каждый
        # входящий апдейт, по разу на пункт меню.
        self._derived: dict[str, str] = {}

    def _ensure(self) -> None:
        """Подгружает словари при первом обращении.

        Иначе перевод зависел бы от порядка импортов: тот, кто взял `t` из
        этого модуля, но не тронул `app.i18n`, получил бы пустой каталог и
        имена ключей на экране. В проверках это не видно — там пакет обычно
        уже импортирован чем-то другим, — а у человека сломалось бы.

        Флаг ставится до импорта: `app.i18n` вызывает `load`, и без этого
        загрузка вызвала бы сама себя.
        """
        if self._loaded:
            return
        self._loaded = True
        import app.i18n  # noqa: F401  (импорт ради побочного действия — загрузки словарей)

    def load(self, locale: str, table: dict[str, str]) -> None:
        self._tables[locale] = table
        # Запомненная кириллица выведена из прежней таблицы и после подмены
        # соответствует уже не тому тексту. Проверки подменяют словарь ровно
        # затем, чтобы посмотреть на поведение, — и увидели бы старое.
        self._derived.clear()

    @property
    def base(self) -> dict[str, str]:
        self._ensure()
        return self._tables.get(BASE_LOCALE, {})

    def keys(self) -> set[str]:
        return set(self.base)

    def table(self, locale: str) -> dict[str, str]:
        self._ensure()
        return self._tables.get(locale, {})

    def raw(self, key: str, locale: str) -> str | None:
        """Строка как есть, без подстановок. None — перевода нет вовсе."""
        locale = normalize(locale)
        self._ensure()

        if locale == DERIVED_LOCALE:
            # Сначала исключение, потом правило: таблица исключений короткая
            # и существует ровно для тех слов, где перевод письменности врёт.
            override = self._tables.get(DERIVED_LOCALE, {}).get(key)
            if override is not None:
                return override
            done = self._derived.get(key)
            if done is not None:
                return done
            source = self.base.get(key)
            if source is None:
                return None
            done = to_cyrillic(source)
            self._derived[key] = done
            return done

        found = self._tables.get(locale, {}).get(key)
        if found is not None:
            return found
        # Русский может отставать от эталона. Узбекская строка на месте
        # непереведённой честнее пустоты: человек хотя бы поймёт, о чём кнопка.
        return self.base.get(key)

    def miss(self, key: str) -> None:
        """Запоминает недостающий ключ и сообщает о нём один раз.

        Один раз, а не на каждое сообщение: пропущенный ключ на главном экране
        иначе залил бы журнал сотнями одинаковых строк за минуту.
        """
        if key in self._misses:
            return
        self._misses.add(key)
        log.warning("нет перевода для ключа %s", key)

    @property
    def misses(self) -> set[str]:
        return set(self._misses)


catalogue = Catalogue()


def t(key: str, locale: str | None = None, /, **params: Any) -> str:
    """Строка интерфейса по ключу.

    Подстановки именованные: `t("task.created", locale, title=...)`. Позиционные
    здесь не годятся — в разных языках слова идут в разном порядке, и «первый
    аргумент» перестаёт быть тем же самым.

    Ошибка подстановки не роняет ответ: человек получит строку с фигурными
    скобками, а не отсутствие сообщения. Молчащий бот хуже кривой строки.
    """
    text = catalogue.raw(key, locale)
    if text is None:
        catalogue.miss(key)
        return key
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        log.warning("не удалось подставить значения в ключ %s", key)
        return text


def available() -> list[tuple[str, str]]:
    """Языки для выбора: код и название на самом языке."""
    return [(code, title) for code, title in LOCALES.items()]
