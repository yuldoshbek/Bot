"""Проверка языковой базы: узбекский основной, русский дополнительный.

    docker compose -f docker-compose.dev.yml \
      run --rm --no-deps migrate python scripts/smoke_i18n.py

Проверки здесь не про базу данных, а про тексты, и потому большая часть
не требует подключения — но одна требует: смена языка должна менять ответ
бота, а не только строку в таблице.

**Чего эти проверки боятся.** Не того, что перевод корявый — это вычитает
человек. Того, что система рассыплется молча: ключ, которого нет, покажет
сам себя вместо кнопки; подстановка, потерянная при переводе, съест имя
и срок; две кнопки на разных языках совпадут надписями, и одно нажатие
запустит два обработчика.
"""
import asyncio
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot.keyboards.common import (  # noqa: E402
    MENU_ADMIN, MENU_AVAILABILITY, MENU_CONTROL, MENU_DECISIONS, MENU_HELP,
    MENU_MY_DAY, MENU_MY_MEETINGS, MENU_MY_TASKS, MENU_NEW_TASK, MENU_PROFILE,
    MENU_QUICK_MEETING, MENU_REQUEST_MEETING, MENU_SEARCH, MENU_WHO_IS_OPEN,
    MenuButton, main_menu, texts_for,
)
from app.core.i18n import (  # noqa: E402
    BASE_LOCALE, DERIVED_LOCALE, LOCALES, catalogue, normalize, t,
)
from app.core.translit import to_cyrillic  # noqa: E402
from app.i18n.ru import TABLE as RU  # noqa: E402
from app.i18n.uz import TABLE as UZ  # noqa: E402
from app.i18n.uz_cyrl import OVERRIDES  # noqa: E402
from app.models.enums import RoleCode  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "app"

MENU_KEYS = [
    MENU_MY_DAY, MENU_MY_MEETINGS, MENU_MY_TASKS, MENU_REQUEST_MEETING,
    MENU_NEW_TASK, MENU_QUICK_MEETING, MENU_DECISIONS, MENU_SEARCH,
    MENU_CONTROL, MENU_AVAILABILITY, MENU_WHO_IS_OPEN, MENU_ADMIN,
    MENU_PROFILE, MENU_HELP,
]

passed = 0
failed = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {title}")
    else:
        failed += 1
        print(f"  FAIL {title} {detail}")


def placeholders(text: str) -> set[str]:
    """Имена подстановок в строке: {name}, {count}."""
    return set(re.findall(r"\{(\w+)\}", text))


def used_keys() -> dict[str, set[str]]:
    """Ключи, которые код передаёт в t(), и файлы, где они встретились.

    Разбирается синтаксическое дерево, а не текст: `grep` нашёл бы и `t()`
    внутри строки, и переменную вместо ключа. Вычисляемые ключи (f-строки)
    пропускаются намеренно — проверить их статически нечем.
    """
    found: dict[str, set[str]] = {}
    for path in ROOT.rglob("*.py"):
        if "i18n" in path.parts[-2:]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "t" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, set()).add(path.name)
    return found


def main() -> None:
    print("\n1. Словари согласованы")
    check(set(UZ) == set(RU), "набор ключей совпадает",
          f"лишние в uz: {sorted(set(UZ) - set(RU))[:5]}, в ru: {sorted(set(RU) - set(UZ))[:5]}")
    check(len(UZ) > 300, f"словарь наполнен: {len(UZ)} ключей")
    empty = [k for k, v in list(UZ.items()) + list(RU.items()) if not v.strip()]
    check(not empty, "пустых строк нет", str(empty[:5]))

    # Дубль ключа в словаре Python не ошибка: побеждает последний, первый
    # тихо пропадает. Сверка множеств такого не видит вовсе — множество
    # схлопывает повторы, — а на экране остаётся одно из двух написаний,
    # и не угадать какое. Читается исходник, а не собранный словарь.
    for path, name in ((ROOT.parent / "app/i18n/uz.py", "uz"),
                       (ROOT.parent / "app/i18n/ru.py", "ru"),
                       (ROOT.parent / "app/i18n/uz_cyrl.py", "uz-Cyrl")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen: dict[str, int] = {}
        doubled = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value in seen:
                        doubled.append(key.value)
                    seen[key.value] = 1
        check(not doubled, f"{name}: каждый ключ встречается один раз",
              str(sorted(set(doubled))[:5]))

    print("\n2. Ни один ключ не остался непереведённым")
    # Одинаковый текст в двух языках — почти всегда забытая правка: строку
    # скопировали из русского словаря в узбекский и не тронули. Законны такие
    # совпадения там, где текста нет вовсе: значки, числа, чистая разметка.
    untouched = []
    for key in UZ:
        if UZ[key] != RU[key]:
            continue
        bare = re.sub(r"<[^>]*>|\{[^}]*\}|[\s\d%·—–:.,()\[\]]|[^\w\s]", "", UZ[key])
        if bare.strip():
            untouched.append(key)
    check(not untouched, f"проверено ключей: {len(UZ)}", str(untouched[:5]))

    print("\n3. Подстановки переживают перевод")
    # Потерянное {name} не роняет ответ — оно просто исчезает вместе с именем.
    # Лишнее — наоборот, показывает человеку фигурные скобки. Оба случая
    # выглядят как «бот сломался», и оба видны только на живом сообщении.
    lost = [k for k in UZ if placeholders(UZ[k]) - placeholders(RU[k])]
    extra = [k for k in UZ if placeholders(RU[k]) - placeholders(UZ[k])]
    check(not lost, "русский не потерял подстановок", str(lost[:5]))
    check(not extra, "русский не добавил своих", str(extra[:5]))

    print("\n4. Каждый ключ из кода есть в словаре")
    missing = {key: files for key, files in used_keys().items() if key not in UZ}
    check(not missing, f"проверено ключей в коде: {len(used_keys())}",
          str(sorted(missing.items())[:5]))

    print("\n5. Перевод письменности")
    cases = [
        ("Oʻzbekiston", "Ўзбекистон"), ("yoʻl", "йўл"), ("gʻalaba", "ғалаба"),
        ("shoʻrva", "шўрва"), ("eʼlon", "эълон"), ("chorshanba", "чоршанба"),
        ("yakshanba", "якшанба"), ("huquq", "ҳуқуқ"), ("topshiriq", "топшириқ"),
        ("uchrashuv", "учрашув"), ("qaror", "қарор"), ("boʻlim", "бўлим"),
        ("Yangi", "Янги"), ("ertaga", "эртага"), ("juma", "жума"),
    ]
    wrong = [(a, to_cyrillic(a), b) for a, b in cases if to_cyrillic(a) != b]
    check(not wrong, f"правило переводит верно: {len(cases)} слов", str(wrong[:3]))

    # Апостроф пишут четырьмя разными знаками; для читателя это один знак.
    variants = ["oʻzbek", "o'zbek", "o’zbek", "o`zbek"]
    results = {to_cyrillic(v) for v in variants}
    check(results == {"ўзбек"}, "любое начертание апострофа разбирается", str(results))

    print("\n6. Перевод не трогает то, что не текст")
    sample = "<b>{name}</b> uchun {count} ta topshiriq: <i>shoshilinch</i>"
    got = to_cyrillic(sample)
    check("{name}" in got and "{count}" in got, "имена подстановок целы", got)
    check("<b>" in got and "</b>" in got and "<i>" in got, "разметка цела", got)
    check("топшириқ" in got, "а текст вокруг переведён", got)

    tags = [v for v in UZ.values() if "<" in v]
    broken = [v for v in tags if to_cyrillic(v).count("<") != v.count("<")]
    check(not broken, f"разметка цела во всём словаре: {len(tags)} строк с тегами",
          str(broken[:2]))

    print("\n7. Иностранное слово остаётся собой")
    check(to_cyrillic("Excel va PDF") == "Excel ва PDF",
          "Excel и PDF не переводятся", to_cyrillic("Excel va PDF"))
    # Берётся то, что увидит человек, а не голое правило: исключения на то
    # и заведены, чтобы поправить места, где правило врёт. Проверка правила
    # в обход исключений не проходит никогда — она просто не о том.
    latin_left = []
    for key in UZ:
        bare = re.sub(r"<[^>]*>|\{[^}]*\}", "", t(key, DERIVED_LOCALE))
        for word in re.findall(r"[A-Za-z]+", bare):
            if word not in ("Excel", "PDF"):
                latin_left.append((key, word))
    check(not latin_left, "непереведённой латиницы в кириллице не осталось",
          str(latin_left[:5]))

    print("\n8. Исключения побеждают правило")
    check(t("month.9", DERIVED_LOCALE) == "сентябр",
          "сентябрь пишется по-кирилличному", t("month.9", DERIVED_LOCALE))
    check(to_cyrillic(UZ["month.9"]) != t("month.9", DERIVED_LOCALE),
          "и правило само дало бы другое — исключение работает",
          to_cyrillic(UZ["month.9"]))
    check(all(key in UZ for key in OVERRIDES),
          "каждое исключение относится к существующему ключу",
          str([k for k in OVERRIDES if k not in UZ]))
    check(len(OVERRIDES) < 20,
          f"список исключений короткий: {len(OVERRIDES)}")

    widths = {len(t(f"weekday.short.{i}", loc)) for i in range(7) for loc in LOCALES}
    check(widths == {2}, "сокращения дней недели одной ширины на всех языках",
          str(sorted(widths)))

    print("\n9. Отсутствие перевода не роняет ответ")

    def said(key: str, locale: str | None = None, **params) -> str | None:
        """None означает, что вызов упал.

        Через try, а не напрямую: если `t` начнёт возбуждать исключение,
        прямая проверка оборвала бы весь раздел, и следующие за ней прошли бы
        незамеченными. Однажды так и вышло — проверки после падения выглядели
        пройденными.
        """
        try:
            return t(key, locale, **params)
        except Exception:
            return None

    check(said("такого.ключа.нет") == "такого.ключа.нет",
          "неизвестный ключ возвращает сам себя, а не роняет вызов")
    check(said("menu.tasks.нет", "ru") == "menu.tasks.нет", "и на русском тоже")
    # Строка с подстановкой, вызванная без значений, не должна падать.
    check("{" in (said("start.greeting", "ru") or ""), "подстановка без значений не роняет вызов",
          str(said("start.greeting", "ru")))
    check(said("start.greeting", "ru", name="Иван") == "Здравствуйте, Иван!",
          "а со значением подставляется", str(said("start.greeting", "ru", name="Иван")))
    check(said("start.greeting", "ru", кто="Иван") == RU["start.greeting"],
          "чужое имя подстановки не роняет ответ")

    print("\n10. Русский отстаёт — человек видит узбекский, а не пустоту")
    catalogue.load("ru", {k: v for k, v in RU.items() if k != "menu.search"})
    check(t("menu.search", "ru") == UZ["menu.search"],
          "непереведённый ключ показывает узбекскую строку", t("menu.search", "ru"))
    check(t("menu.search", "ru") != "menu.search", "а не имя ключа")
    catalogue.load("ru", RU)
    check(t("menu.search", "ru") == RU["menu.search"], "словарь восстановлен")

    # Кириллица вычисляется один раз и запоминается. Значит, подмена эталона
    # обязана её сбрасывать — иначе после правки текста человек с кириллицей
    # ещё долго видел бы прежнюю формулировку, а с латиницей уже новую.
    before_cyr = t("menu.search", DERIVED_LOCALE)
    catalogue.load(BASE_LOCALE, {**UZ, "menu.search": "🔎 Boshqa soʻz"})
    check(t(BASE_LOCALE and "menu.search", DERIVED_LOCALE) == "🔎 Бошқа сўз",
          "правка эталона доходит до кириллицы, а не берётся из запомненного",
          t("menu.search", DERIVED_LOCALE))
    catalogue.load(BASE_LOCALE, UZ)
    check(t("menu.search", DERIVED_LOCALE) == before_cyr, "эталон восстановлен",
          t("menu.search", DERIVED_LOCALE))

    print("\n11. Код языка приводится к известному")
    for raw, expect in (
        (None, "uz"), ("", "uz"), ("uz", "uz"), ("uz-Cyrl", "uz-Cyrl"),
        ("uz_CYRL", "uz-Cyrl"), ("UZ-cyrl", "uz-Cyrl"), ("ru", "ru"),
        ("ru-RU", "ru"), ("en", "uz"), ("  uz  ", "uz"), ("мусор", "uz"),
    ):
        check(normalize(raw) == expect, f"«{raw}» → {expect}", normalize(raw))

    print("\n12. Выбор языка принимает только язык")
    # `normalize` возвращает основной язык на что угодно, поэтому проверять
    # им же присланный код бессмысленно: «lang:menu» прошёл бы как «uz».
    for code in ("menu", "", "en", "de", "uz-Latn"):
        check(code not in LOCALES, f"«{code}» не считается языком")
    for code in ("uz", "uz-Cyrl", "ru"):
        check(code in LOCALES, f"«{code}» считается")

    print("\n13. Кнопка меню узнаётся на любом языке")
    for key in MENU_KEYS:
        texts = texts_for(key)
        check(len(texts) >= 2, f"{key}: переводов {len(texts)}", str(texts))

    # Две кнопки с одинаковой надписью — это два обработчика на одно нажатие.
    seen: dict[str, str] = {}
    collisions = []
    for key in MENU_KEYS:
        for text in texts_for(key):
            if text in seen and seen[text] != key:
                collisions.append((text, seen[text], key))
            seen[text] = key
    check(not collisions, "надписи кнопок нигде не совпадают", str(collisions[:3]))

    print("\n14. Меню собирается на выбранном языке")
    for locale, expect_key in (("uz", "menu.profile"), ("ru", "menu.profile"),
                               ("uz-Cyrl", "menu.profile")):
        buttons = {
            button.text
            for row in main_menu({RoleCode.EMPLOYEE}, None, locale).keyboard
            for button in row
        }
        check(t(expect_key, locale) in buttons,
              f"{locale}: кнопка профиля подписана на своём языке",
              str(sorted(buttons)[:3]))

    uz_menu = {b.text for r in main_menu({RoleCode.EMPLOYEE}, None, "uz").keyboard for b in r}
    ru_menu = {b.text for r in main_menu({RoleCode.EMPLOYEE}, None, "ru").keyboard for b in r}
    check(uz_menu != ru_menu, "меню на разных языках действительно разное")
    check(len(uz_menu) == len(ru_menu), "но состоит из тех же кнопок",
          f"{len(uz_menu)} и {len(ru_menu)}")

    print("\n15. Разбор срока понимает оба языка")
    from app.core.dates import MONTHS, WEEKDAYS

    # Каждое название по отдельности: пропущенное в справочнике слово
    # выборочная проверка не заметит — а человек, написавший именно его,
    # получит поручение без срока.
    uz_days = ["dushanba", "seshanba", "chorshanba", "payshanba",
               "juma", "shanba", "yakshanba"]
    missing_days = [d for d in uz_days if d not in WEEKDAYS]
    check(not missing_days, "все семь дней недели по-узбекски разбираются",
          str(missing_days))
    cyr_days = ["душанба", "сешанба", "чоршанба", "пайшанба",
                "жума", "шанба", "якшанба"]
    check(all(d in WEEKDAYS for d in cyr_days), "и кириллицей тоже",
          str([d for d in cyr_days if d not in WEEKDAYS]))
    check([WEEKDAYS.get(d) for d in uz_days] == list(range(7)),
          "и каждый указывает на свой день недели",
          str([WEEKDAYS.get(d) for d in uz_days]))

    uz_months = ["yanvar", "fevral", "mart", "aprel", "may", "iyun",
                 "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
    missing_months = [m for m in uz_months if m not in MONTHS]
    check(not missing_months, "все двенадцать месяцев по-узбекски разбираются",
          str(missing_months))
    check([MONTHS.get(m) for m in uz_months] == list(range(1, 13)),
          "и каждый указывает на свой месяц",
          str([MONTHS.get(m) for m in uz_months]))

    from datetime import datetime, timezone
    from app.core.dates import humanize_due, parse_due

    now = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)  # суббота
    for text, expect_day in (
        ("завтра", 6), ("ertaga", 6), ("эртага", 6),
        ("послезавтра", 7), ("indinga", 7),
        ("до пятницы", 11), ("juma gacha", 11), ("жума гача", 11),
        ("через 3 дня", 8), ("3 kundan keyin", 8),
        ("15 сентября", 15), ("15 sentabr", 15),
    ):
        got = parse_due(text, "Asia/Tashkent", now=now)
        check(got is not None and got.astimezone(TZ_TASHKENT).day == expect_day,
              f"«{text}» → {expect_day} сентября",
              str(got.astimezone(TZ_TASHKENT).date() if got else None))

    print("\n16. Срок называется на языке собеседника")
    # Момент берётся от настоящих часов, а не от подставного `now` выше:
    # `humanize_due` сравнивает срок с сегодняшним днём по реальному времени,
    # и «завтра» от сентября 2026 года было бы «завтра» ровно один день в году.
    # Проверка проходила накануне и падала на следующие сутки.
    from datetime import timedelta as _td

    from app.core.timeutil import utcnow as _utcnow

    tomorrow = _utcnow() + _td(days=1)
    said = {loc: humanize_due(tomorrow, "Asia/Tashkent", loc) for loc in LOCALES}
    check(len(set(said.values())) == 3, "три языка — три разных ответа", str(said))
    check(said["ru"].startswith("Завтра"), "по-русски «Завтра»", said["ru"])
    check(said["uz"].startswith("Ertaga"), "по-узбекски «Ertaga»", said["uz"])
    check(said["uz-Cyrl"].startswith("Эртага"), "кириллицей «Эртага»", said["uz-Cyrl"])


from zoneinfo import ZoneInfo  # noqa: E402

TZ_TASHKENT = ZoneInfo("Asia/Tashkent")


async def with_database() -> None:
    """Смена языка меняет ответ бота, а не только строку в таблице."""
    from sqlalchemy import delete, select

    from app.core.db import session_scope
    from app.models.org import Organization
    from app.models.user import User
    from app.models.enums import UserStatus

    ORG = "ТЕСТ Язык"

    async def cleanup() -> None:
        async with session_scope() as session:
            # Все организации с этим именем, а не первая попавшаяся: проверка
            # заводит её дважды, и уборка «по одному найденному» оставляла
            # вторую в базе — а следующий прогон находил уже её.
            org_ids = list((await session.execute(
                select(Organization.id).where(Organization.name == ORG)
            )).scalars().all())
            if org_ids:
                # Только по organization_id: чужие записи не трогаем никогда.
                from app.models.task import Task as TaskRow

                from app.models.notification import Notification as Letter
                from app.models.task import TaskEvent

                await session.execute(delete(TaskEvent).where(
                    TaskEvent.task_id.in_(
                        select(TaskRow.id).where(TaskRow.organization_id.in_(org_ids))
                    )
                ))
                await session.execute(
                    delete(Letter).where(Letter.organization_id.in_(org_ids))
                )
                await session.execute(
                    delete(TaskRow).where(TaskRow.organization_id.in_(org_ids))
                )
                await session.execute(delete(User).where(User.organization_id.in_(org_ids)))
                await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))

    await cleanup()
    print("\n17. Язык хранится у человека и меняет ответ")
    async with session_scope() as session:
        org = Organization(name=ORG, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()
        person = User(
            organization_id=org.id, telegram_user_id=999_000_111,
            full_name="ТЕСТ Собеседник", status=UserStatus.ACTIVE,
            timezone="Asia/Tashkent",
        )
        session.add(person)
        await session.flush()

        check(person.locale == BASE_LOCALE,
              f"новый сотрудник получает основной язык: {person.locale}")

        before = t("menu.profile", person.locale)
        person.locale = "ru"
        await session.flush()
        after = t("menu.profile", person.locale)
        check(before != after, "смена языка меняет надпись кнопки", f"{before} → {after}")
        check(after == RU["menu.profile"], "и это именно русская надпись", after)

        # Старая кнопка в чате осталась на прежнем языке — она обязана работать.
        # Проверяется сам фильтр, а не набор надписей: между ними стоит `__call__`,
        # и ошибка именно в нём иначе прошла бы незамеченной.
        button = MenuButton(MENU_PROFILE)

        class Pressed:
            def __init__(self, text): self.text = text

        check(await button(Pressed(before)), "фильтр узнаёт старую надпись", before)
        check(await button(Pressed(after)), "и новую тоже", after)
        check(not await button(Pressed("что-то postороннее")),
              "а на чужой текст не срабатывает")

    print("\n18. Карточка поручения говорит на языке смотрящего")
    # Проверка поведения, а не наличия ключей: карточку собирают полтора десятка
    # вызовов, и достаточно одному забыть язык, чтобы русскоязычный человек
    # увидел узбекскую строку посреди русской карточки. Ключи при этом на месте,
    # и проверка словарей такого не заметит.
    from app.bot.handlers.tasks import _render_task
    from app.models.enums import Priority, TaskStatus
    from app.models.task import Task
    from app.services.tasks import priority_title, status_title

    async with session_scope() as session:
        org = Organization(name=ORG, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()
        boss = User(organization_id=org.id, telegram_user_id=999_000_222,
                    full_name="ТЕСТ Каримов", status=UserStatus.ACTIVE,
                    timezone="Asia/Tashkent")
        hand = User(organization_id=org.id, telegram_user_id=999_000_333,
                    full_name="ТЕСТ Юсупов", status=UserStatus.ACTIVE,
                    timezone="Asia/Tashkent")
        session.add_all([boss, hand])
        await session.flush()
        card = Task(
            organization_id=org.id, creator_id=boss.id, assignee_id=hand.id,
            title="ТЕСТ карточка на трёх языках", status=TaskStatus.IN_PROGRESS,
            priority=Priority.CRITICAL,
        )
        session.add(card)
        await session.flush()

        rendered = {
            loc: await _render_task(session, card, boss, loc) for loc in LOCALES
        }
        check(len({v for v in rendered.values()}) == 3,
              "три языка — три разные карточки",
              str({k: v[:40] for k, v in rendered.items()}))

        # Ожидаемое берётся из словаря напрямую, а не через ту же функцию,
        # что собирает карточку. Проверка, зовущая проверяемое, подтверждает
        # только то, что код вызывает сам себя: убери значок из `status_title` —
        # и `status_title(...) in карточка` останется истинным.
        check("🟡" in rendered["ru"], "значок статуса на месте", rendered["ru"][:60])
        check("🔴" in rendered["ru"], "и значок важности тоже", rendered["ru"][:60])
        check(UZ["task.status.in_progress"] in rendered["uz"],
              "узбекская карточка: статус словом из словаря", rendered["uz"][:60])
        check(RU["task.status.in_progress"] in rendered["ru"],
              "русская карточка: статус словом из словаря", rendered["ru"][:60])
        check(RU["priority.critical"] in rendered["ru"],
              "и важность тоже", rendered["ru"][:60])

        # Ни одна подпись поля не должна остаться на чужом языке. Проверяются
        # все подписи разом, а не одно слово: забытый `t` на любой из них
        # выглядит одинаково — русская строка посреди узбекской карточки.
        # Имена людей здесь намеренно не совпадают ни с одним словом словаря:
        # «ТЕСТ Исполнитель» — это одновременно имя и подпись поля, и проверка
        # на утечку подписи срабатывала на имени, а не на ошибке.
        CARD_KEYS = [
            "task.field.status", "task.field.assignee", "task.field.author",
            "task.field.priority", "task.status.in_progress", "priority.critical",
        ]
        ru_leaks = [k for k in CARD_KEYS if RU[k] in rendered["uz"]]
        uz_leaks = [k for k in CARD_KEYS if UZ[k] in rendered["ru"]]
        check(not ru_leaks, "в узбекской карточке нет русских подписей", str(ru_leaks))
        check(not uz_leaks, "а в русской — узбекских", str(uz_leaks))

        # Самая сильная проверка из всех: карточка с латинскими данными,
        # собранная по-узбекски, не должна содержать кириллицы вовсе. Проверка
        # выше ловит только те русские слова, что есть в словаре, — а забытая
        # строка могла быть написана иначе, чем её перевод («Статус» в коде
        # против «Состояние» в словаре), и тогда утечка проходит незамеченной.
        # Все необязательные строки карточки включены разом. Иначе проверка
        # не доходит до половины из них: «на личном контроле» и «проверяет»
        # печатаются только при своих условиях, и забытый там перевод
        # проверка с пустым поручением не увидит.
        latin = Task(
            organization_id=org.id, creator_id=boss.id, assignee_id=hand.id,
            on_behalf_of_id=boss.id,
            title="TEST lotin yozuvidagi kartochka", status=TaskStatus.REVIEW,
            priority=Priority.HIGH,
            description="TEST tavsif matni",
            requires_review=True, reviewer_id=boss.id,
            personal_control=True, rework_count=2, extensions_count=1,
        )
        boss.full_name, hand.full_name = "TEST Karimov", "TEST Yusupov"
        session.add(latin)
        await session.flush()

        only_latin = await _render_task(session, latin, boss, "uz")
        strays = sorted(set(re.findall(r"[А-Яа-яЁё]+", only_latin)))
        check(not strays, "в узбекской карточке не осталось ни одного русского слова",
              str(strays[:6]))

        # Список — тем же способом.
        boss.full_name, hand.full_name = "ТЕСТ Каримов", "ТЕСТ Юсупов"
        await session.flush()

        # Список поручений собирается отдельно от карточки и своим кодом.
        # Забытый там язык карточка не покажет.
        from app.bot.handlers.tasks import _show_bucket

        class Caught:
            """Ловит текст вместо отправки в Telegram."""

            def __init__(self) -> None:
                self.text = ""

            async def answer(self, text, **kwargs):
                self.text = text

            async def edit_text(self, text, **kwargs):
                self.text = text

        listed = {}
        for loc in LOCALES:
            sink = Caught()
            await _show_bucket(sink, session, hand, "active", loc)
            listed[loc] = sink.text
        check(len(set(listed.values())) == 3, "список тоже на трёх языках",
              str({k: v[:40] for k, v in listed.items()}))
        check(UZ["task.list.active"] in listed["uz"],
              "узбекский список подписан по-узбекски", listed["uz"][:60])
        check(RU["task.list.active"] in listed["ru"],
              "русский — по-русски", listed["ru"][:60])
        check(RU["task.status.in_progress"] not in listed["uz"],
              "и статус в узбекском списке не русский", listed["uz"][:80])

    print("\n19. В переведённых модулях не осталось русских строк")
    # Список того, что переведено целиком. Проверка идёт по исходнику: любая
    # русская строка-литерал в этих файлах означает забытый `t()`. Это ловит
    # то, чего не поймает ни проверка ключей (ключ-то на месте), ни проверка
    # экрана (до этой кнопки она может не дойти).
    #
    # Комментарии и docstring не в счёт: документация проекта на русском.
    DONE_MODULES = [
        "app/bot/handlers/tasks.py",
        "app/bot/handlers/meetings.py",
        "app/bot/handlers/documents.py",
        "app/bot/handlers/availability.py",
        "app/bot/handlers/menu.py",
        "app/bot/handlers/start.py",
        "app/bot/handlers/registry.py",
        "app/bot/handlers/voice.py",
        "app/bot/handlers/protocol.py",
        # Сценарий ИИ: промпты лежат отдельно, в `app/ai/prompts.py`, —
        # они указание модели, а не строка интерфейса, и переводу
        # на языки собеседника не подлежат.
        #
        # `app/ai/summary.py` и `app/ai/protocol.py` в списке не значатся
        # намеренно: там собирается сообщение для модели, а не для человека,
        # и русские строки в нём — часть промпта. Человеку эти модули
        # отвечают ключами словаря, и переводит их обработчик.
        "app/ai/voice.py",
        "app/ai/question.py",
        "app/services/questions.py",
        "app/bot/keyboards/common.py",
        "app/bot/middlewares/auth.py",
        "app/services/dashboard.py",
        "app/services/digest.py",
        "app/services/tasks.py",
        "app/services/deadlines.py",
        "app/services/decisions.py",
        "app/services/quotas.py",
        "app/services/availability.py",
        "app/services/attendance.py",
        "app/services/meetings.py",
        "app/services/documents.py",
        "app/services/templates.py",
        "app/services/features.py",
        "app/services/search.py",
        "app/services/briefing.py",
        "app/services/notifications.py",
        "app/bot/handlers/admin.py",
        "app/services/orgadmin.py",
        "app/services/registration.py",
        "app/bot/utils.py",
        "app/core/speech.py",
    ]
    # Что остаётся по-русски намеренно — и почему. Список именно строк,
    # а не файлов: иначе исключение для одной подписи закрыло бы весь файл,
    # и следующая забытая строка прошла бы незамеченной.
    ALLOWED = {
        # Подписи для выгрузок: файл открывают в Excel вне бота, и язык
        # получателя там неизвестен.
        "🔵 Новое", "🔵 Принято", "🟡 В работе", "🟠 На проверке",
        "🟢 Выполнено", "🟠 Заблокировано", "🔴 Просрочено", "⚫ Отменено",
        "Низкий", "Обычный", "🔴 Высокий", "🔴 Критичный",
        "В работе", "Выполнено", "Отменено",
        "Доступен для приёма", "Занят", "Не беспокоить", "Индикатор не выставлен",
        "Полезная", "Нейтральная", "Бесполезная",
        "на неделю", "на месяц",
        # Записи в журнале действий: их читает администратор в разделе аудита,
        # а не участник события.
        "эскалация: ", "файл",
        # Строка не для экрана, а для разбора: срок из шаблона считается тем же
        # разбором, что и набранный руками, и разбор понимает все языки.
        "через ", " дней",
        # Строка журнала работы, а не сообщение человеку.
        "не доставлено пользователю %s: %s",
        # Разбор ввода администратора: принимает оба языка сразу, независимо
        # от настройки. Понимать и говорить — разные вещи.
        r"(?:обед|tushlik|тушлик)\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})",
        r"(?:буфер|bufer|буфер)\s+(\d{1,3})",
        r"(?:подряд|ketma-ket|кетма-кет)\s+(\d{1,2})",
        "мес", "ой", "команд", "хизмат", "сафар", "больн", "касал",
    }

    for module in DONE_MODULES:
        path = ROOT.parent / module
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                text = ast.get_docstring(node, clean=False)
                if text:
                    docstrings.add(text)
        russian = sorted({
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value not in docstrings
            and node.value not in ALLOWED
            and re.search(r"[А-Яа-яЁё]", node.value)
        })
        check(not russian, f"{module.split('/')[-1]}: русских строк нет",
              str([r[:40] for r in russian[:3]]))

    print("\n20. Экран «Мой день» и показатели говорят на языке смотрящего")
    # Экран собирается из показателей, каждый со своим пояснением, и уходит
    # ещё и утренней сводкой. Забытый язык здесь виден сразу всем.
    from app.core.timeutil import utcnow as now_utc
    from app.services import analytics, dashboard

    board = dashboard.Board(day=now_utc(), timezone="Asia/Tashkent")
    board.overdue_total = 3
    board.overdue_by_department = [("Moliya", 2), (None, 1)]
    board.metrics = [
        analytics.Metric(key="calendar_load", value=42.0, unit="metric.unit.percent"),
        analytics.Metric(key="punctuality"),  # молчащий показатель
    ]
    board.metrics[0].say("metric.detail.busy_of", busy=180, total=32400)

    screens = {loc: dashboard.render(board, locale=loc) for loc in LOCALES}
    check(len(set(screens.values())) == 3, "три языка — три разных экрана",
          str({k: v[:30] for k, v in screens.items()}))
    strays = sorted(set(re.findall(r"[А-Яа-яЁё]+", screens["uz"])))
    check(not strays, "в узбекском экране не осталось русских слов", str(strays[:6]))
    check(RU["metric.calendar_load"] in screens["ru"],
          "русский экран называет показатель по-русски", screens["ru"][-90:])
    check(UZ["metric.calendar_load"] in screens["uz"],
          "узбекский — по-узбекски", screens["uz"][-90:])
    check(RU["metric.no_data"] in screens["ru"],
          "молчащий показатель говорит «нет данных» на своём языке", screens["ru"][-60:])
    check(UZ["metric.no_data"] in screens["uz"], "и по-узбекски тоже",
          screens["uz"][-60:])
    check(UZ["dashboard.no_department"] in screens["uz"],
          "строка без отдела подписана на своём языке", screens["uz"][-90:])

    print("\n21. Уведомление приходит на языке получателя, а не отправителя")
    # Самое незаметное место перевода. Уведомление собирает тот, кто совершил
    # действие, а читает совсем другой человек — и языки у них разные. Ошибку
    # такого рода видит только получатель, и пожаловаться ему некому: сообщение
    # выглядит просто «не на том языке», а не сломанным.
    from app.models.notification import Notification
    from app.services import tasks as task_service

    async with session_scope() as session:
        org = Organization(name=ORG, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()
        # Начальник говорит по-русски, исполнитель — по-узбекски.
        chief = User(organization_id=org.id, telegram_user_id=999_000_444,
                     full_name="ТЕСТ Начальник", status=UserStatus.ACTIVE,
                     timezone="Asia/Tashkent", locale="ru")
        worker = User(organization_id=org.id, telegram_user_id=999_000_555,
                      full_name="ТЕСТ Ishchi", status=UserStatus.ACTIVE,
                      timezone="Asia/Tashkent", locale="uz")
        session.add_all([chief, worker])
        await session.flush()

        made = await task_service.create_task(
            session, creator=chief, assignee=worker,
            title="TEST topshiriq", priority=Priority.CRITICAL,
        )
        letter = await session.scalar(
            select(Notification.body).where(
                Notification.user_id == worker.id,
                Notification.event_key == f"task:{made.id}:assigned",
            )
        )
        check(letter is not None, "исполнителю ушло письмо о поручении")
        check(UZ["task.notify.new"] in (letter or ""),
              "письмо на узбекском — языке исполнителя, а не автора",
              (letter or "")[:70])
        check(RU["task.notify.new"] not in (letter or ""),
              "и русского заголовка в нём нет", (letter or "")[:70])
        strays = sorted(set(re.findall(r"[А-Яа-яЁё]+", letter or "")))
        # Имя автора остаётся кириллицей — это данные, а не язык интерфейса.
        strays = [w for w in strays if w not in ("ТЕСТ", "Начальник")]
        check(not strays, "и ни одного русского слова интерфейса", str(strays[:5]))

        # Обратная сторона: автору о принятии приходит по-русски.
        await task_service.accept(session, made, worker)
        back = await session.scalar(
            select(Notification.body).where(
                Notification.user_id == chief.id,
                Notification.event_key == f"task:{made.id}:accepted",
            )
        )
        check(back is not None, "автору ушло письмо о принятии")
        check("принял поручение" in (back or ""),
              "и оно по-русски — на языке автора", (back or "")[:70])

        # А теперь письмо, которое собирает русскоязычный, а читает узбек —
        # и идёт оно через `_notify`, а не через прямую постановку в очередь.
        # Без этого случая подменённый в `_notify` язык выглядел бы верным:
        # предыдущее письмо и так уходило по-русски.
        await task_service.start(session, made, worker)
        await task_service.submit(session, made, worker)
        await task_service.approve(session, made, chief)
        praise = await session.scalar(
            select(Notification.body).where(
                Notification.user_id == worker.id,
                Notification.kind == "task.approved",
            )
        )
        check(praise is not None, "исполнителю ушло письмо о приёмке работы")
        check(UZ["task.notify.approved"] in (praise or ""),
              "и оно по-узбекски, хотя принимал русскоязычный",
              (praise or "")[:70])

    print("\n22. Карточка голосового поручения — на языке говорившего")
    # Черновик собирается из ключей, а не из готовых строк: он переживает
    # перезапуск в хранилище состояния, и язык берётся в момент показа.
    # Ошибка здесь означала бы карточку на чужом языке у того, кто её диктовал.
    from datetime import timedelta

    from app.ai.voice import Draft, render
    from app.core.timeutil import utcnow as now_utc
    from app.models.enums import Priority

    draft = Draft(
        transcript="Karimovga smeta tayyorlash, ertaga",
        title="Smeta tayyorlash",
        assignee_id=1,
        heard_name="Karimov",
        due_at=now_utc() + timedelta(days=1),
        priority=Priority.HIGH,
        notes=["voice.note.assignee_denied"],
    )
    cards = {
        loc: render(draft, loc, assignee_name="Karimov",
                    timezone_name="Asia/Tashkent")
        for loc in LOCALES
    }
    check(len(set(cards.values())) == 3, "три языка — три разные карточки",
          str({k: v[:25] for k, v in cards.items()}))
    check(RU["voice.draft.not_yet"] in cards["ru"],
          "русская карточка честно говорит, что поручения ещё нет",
          cards["ru"][:80])
    check(UZ["voice.draft.not_yet"] in cards["uz"], "и узбекская тоже",
          cards["uz"][:80])
    strays = sorted(set(re.findall(r"[А-Яа-яЁё]+", cards["uz"])))
    check(not strays, "в узбекской карточке не осталось русских слов", str(strays[:6]))
    # Подстановка имени переживает перевод: без неё пояснение теряет смысл.
    for loc, card in cards.items():
        check("Karimov" in card and "{name}" not in card,
              f"имя подставлено в пояснение ({loc})", card[-120:])
    check(to_cyrillic(UZ["voice.draft.not_yet"]) in cards[DERIVED_LOCALE],
          "кириллическая карточка выведена правилом, а не набрана руками",
          cards[DERIVED_LOCALE][:80])

    await cleanup()
    async with session_scope() as session:
        left = await session.scalar(
            select(Organization.id).where(Organization.name == ORG)
        )
    print("\n23. У каждого значения из перечней вопроса есть слово")
    # Строка «понял так» собирает ключи на ходу: `ask.period.{период}`.
    # Разбор дерева такие ключи пропускает — проверить их статически нечем, —
    # поэтому они обходятся здесь, прямо по перечням. Забытое слово показало бы
    # человеку имя ключа: «Понял так: поручения · ask.period.past_month».
    from app.services import questions

    computed = (
        [f"ask.kind.{kind}" for kind in questions.KINDS]
        + [f"ask.period.{period}" for period in questions.PERIODS]
        + [questions.DATE_FIELD[kind] for kind in questions.KINDS]
        + [f"task.status.{value.lower()}" for value in questions.STATUSES["task"].values()]
        + [f"decision.status.{value.lower()}"
           for value in questions.STATUSES["decision"].values()]
        + [f"meeting.status.{value.lower()}"
           for value in questions.STATUSES["meeting"].values()]
        + [f"priority.{value.lower()}" for value in questions.PRIORITIES.values()]
    )
    check(len(computed) > 25, f"перечней набралось: {len(computed)} значений")
    for locale in LOCALES:
        # Ключ, которого нет, `t` возвращает сам собой — по этому и видно.
        missing = sorted(key for key in computed if t(key, locale) == key)
        check(not missing, f"{locale}: слово есть у каждого значения", str(missing[:5]))

    check(left is None, "тестовая организация убрана")


async def run() -> None:
    main()
    await with_database()
    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(run())
