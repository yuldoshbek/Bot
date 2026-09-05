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

    print("\n2. Подстановки переживают перевод")
    # Потерянное {name} не роняет ответ — оно просто исчезает вместе с именем.
    # Лишнее — наоборот, показывает человеку фигурные скобки. Оба случая
    # выглядят как «бот сломался», и оба видны только на живом сообщении.
    lost = [k for k in UZ if placeholders(UZ[k]) - placeholders(RU[k])]
    extra = [k for k in UZ if placeholders(RU[k]) - placeholders(UZ[k])]
    check(not lost, "русский не потерял подстановок", str(lost[:5]))
    check(not extra, "русский не добавил своих", str(extra[:5]))

    print("\n3. Каждый ключ из кода есть в словаре")
    missing = {key: files for key, files in used_keys().items() if key not in UZ}
    check(not missing, f"проверено ключей в коде: {len(used_keys())}",
          str(sorted(missing.items())[:5]))

    print("\n4. Перевод письменности")
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

    print("\n5. Перевод не трогает то, что не текст")
    sample = "<b>{name}</b> uchun {count} ta topshiriq: <i>shoshilinch</i>"
    got = to_cyrillic(sample)
    check("{name}" in got and "{count}" in got, "имена подстановок целы", got)
    check("<b>" in got and "</b>" in got and "<i>" in got, "разметка цела", got)
    check("топшириқ" in got, "а текст вокруг переведён", got)

    tags = [v for v in UZ.values() if "<" in v]
    broken = [v for v in tags if to_cyrillic(v).count("<") != v.count("<")]
    check(not broken, f"разметка цела во всём словаре: {len(tags)} строк с тегами",
          str(broken[:2]))

    print("\n6. Иностранное слово остаётся собой")
    check(to_cyrillic("Excel va PDF") == "Excel ва PDF",
          "Excel и PDF не переводятся", to_cyrillic("Excel va PDF"))
    latin_left = []
    for key, value in UZ.items():
        bare = re.sub(r"<[^>]*>|\{[^}]*\}", "", to_cyrillic(value))
        for word in re.findall(r"[A-Za-z]+", bare):
            if word not in ("Excel", "PDF"):
                latin_left.append((key, word))
    check(not latin_left, "непереведённой латиницы в кириллице не осталось",
          str(latin_left[:5]))

    print("\n7. Исключения побеждают правило")
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

    print("\n8. Отсутствие перевода не роняет ответ")

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

    print("\n9. Русский отстаёт — человек видит узбекский, а не пустоту")
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

    print("\n10. Код языка приводится к известному")
    for raw, expect in (
        (None, "uz"), ("", "uz"), ("uz", "uz"), ("uz-Cyrl", "uz-Cyrl"),
        ("uz_CYRL", "uz-Cyrl"), ("UZ-cyrl", "uz-Cyrl"), ("ru", "ru"),
        ("ru-RU", "ru"), ("en", "uz"), ("  uz  ", "uz"), ("мусор", "uz"),
    ):
        check(normalize(raw) == expect, f"«{raw}» → {expect}", normalize(raw))

    print("\n11. Выбор языка принимает только язык")
    # `normalize` возвращает основной язык на что угодно, поэтому проверять
    # им же присланный код бессмысленно: «lang:menu» прошёл бы как «uz».
    for code in ("menu", "", "en", "de", "uz-Latn"):
        check(code not in LOCALES, f"«{code}» не считается языком")
    for code in ("uz", "uz-Cyrl", "ru"):
        check(code in LOCALES, f"«{code}» считается")

    print("\n12. Кнопка меню узнаётся на любом языке")
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

    print("\n13. Меню собирается на выбранном языке")
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

    print("\n14. Разбор срока понимает оба языка")
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

    print("\n15. Срок называется на языке собеседника")
    tomorrow = now.replace(hour=6) + __import__("datetime").timedelta(days=1)
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
            org_id = await session.scalar(
                select(Organization.id).where(Organization.name == ORG)
            )
            if org_id:
                # Только по organization_id: чужие записи не трогаем никогда.
                await session.execute(delete(User).where(User.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))

    await cleanup()
    print("\n16. Язык хранится у человека и меняет ответ")
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

    await cleanup()
    async with session_scope() as session:
        left = await session.scalar(
            select(Organization.id).where(Organization.name == ORG)
        )
    check(left is None, "тестовая организация убрана")


async def run() -> None:
    main()
    await with_database()
    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(run())
