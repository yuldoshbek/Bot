"""Оформление расшифровки: сплошной поток речи — в читаемый текст.

Расшифровщик отдаёт одну длинную строку без заглавных букв и без абзацев;
читать такое невозможно, а именно чтение и есть смысл расшифровки. Разбивку
делает код, а не модель: правила простые, проверяемые и одинаковые для всех
языков, а модель за ту же работу берёт деньги и иногда переписывает слова.

**Абзац начинается там, где человек молчал.** Расшифровщик отдаёт отрезки
со временем, и пауза между ними — единственный настоящий признак смены мысли.
Знаки препинания для этого хуже: в узбекской и русской расшифровке их ставят
неровно, а в диктовке их часто нет вовсе.

**Если отрезков нет, разбиваем по предложениям.** Хуже, но всё же лучше
сплошной простыни: три предложения в абзаце читаются, тридцать — нет.

**Слова не трогаем.** Оформление меняет пробелы, переводы строк и заглавную
букву в начале предложения. Ни одно слово не переписывается: расшифровка —
свидетельство того, что было сказано, и правки в ней потом не отличить
от ошибки распознавания.
"""
import re

# Пауза, после которой начинается новый абзац. Полторы секунды — это заметная
# для собеседника остановка, а не вдох между словами.
PAUSE_SECONDS = 1.5
# Сколько предложений держать в абзаце, когда пауз не видно.
SENTENCES_IN_PARAGRAPH = 3
# Длина, после которой абзац разрывается независимо от всего остального.
MAX_PARAGRAPH_CHARS = 400

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
SPACES = re.compile(r"[ \t]+")
BEFORE_MARK = re.compile(r"\s+([,.!?;:…])")


def tidy(text: str) -> str:
    """Убирает то, что мешает читать: лишние пробелы и пробел перед знаком."""
    text = SPACES.sub(" ", (text or "").strip())
    return BEFORE_MARK.sub(r"\1", text)


def capitalize(text: str) -> str:
    """Заглавная буква в начале. Остальные буквы не трогаем.

    Приводить к нижнему регистру нельзя: расшифровщик пишет имена и названия
    с заглавной, и «ташкент» вместо «Ташкент» — это уже правка текста.
    """
    text = text.strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def sentences(text: str) -> list[str]:
    """Режет на предложения по знакам конца."""
    return [part.strip() for part in SENTENCE_END.split(tidy(text)) if part.strip()]


def by_pauses(pieces: list[tuple[float, float, str]]) -> list[str]:
    """Собирает абзацы по паузам между отрезками речи."""
    blocks: list[list[str]] = []
    previous_end: float | None = None
    for start, end, raw in pieces:
        words = tidy(raw)
        if not words:
            continue
        far = previous_end is not None and start - previous_end >= PAUSE_SECONDS
        long = blocks and len(" ".join(blocks[-1])) >= MAX_PARAGRAPH_CHARS
        if not blocks or far or long:
            blocks.append([words])
        else:
            blocks[-1].append(words)
        previous_end = end
    return [capitalize(" ".join(block)) for block in blocks if block]


def by_sentences(text: str) -> list[str]:
    """Собирает абзацы по предложениям — когда отрезков со временем нет."""
    blocks: list[list[str]] = []
    for part in sentences(text):
        long = blocks and len(" ".join(blocks[-1])) >= MAX_PARAGRAPH_CHARS
        if not blocks or long or len(blocks[-1]) >= SENTENCES_IN_PARAGRAPH:
            blocks.append([capitalize(part)])
        else:
            blocks[-1].append(capitalize(part))
    return [" ".join(block) for block in blocks]


def pretty(text: str, pieces: list[tuple[float, float, str]] | None = None) -> str:
    """Расшифровка, готовая к чтению: абзацы через пустую строку.

    Отрезки со временем предпочтительнее: они знают, где человек молчал.
    Их нет — работает разбивка по предложениям.
    """
    blocks = by_pauses(pieces) if pieces else by_sentences(text)
    if not blocks:
        return capitalize(tidy(text))
    return "\n\n".join(blocks)


def duration(seconds: float | int | None) -> str:
    """Длительность голосового в виде 0:42 или 12:05."""
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}"
