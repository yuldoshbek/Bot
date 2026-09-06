"""Своя расшифровка речи — рядом, но отдельным процессом.

**Зачем своя.** Расшифровка у OpenAI стоит за минуту, а голосовые в рабочем
чате идут потоком: пересланное совещание на сорок минут стоит дороже, чем
весь остальной ИИ за день. Своя служба на своём железе не стоит ничего,
и потолок расхода её не касается.

**Почему отдельным процессом, а не внутри бота.** Расшифровка занимает
процессор целиком и на несколько секунд. Внутри бота это остановило бы все
обработчики разом: у asyncio один поток, и долгий счёт в нём — это зависший
бот для всех. Отдельная служба ещё и перезапускается сама по себе, весит
свои полтора гигабайта модели вне образа бота и при желании уезжает
на другую машину.

**Почему это не ломает слой подмены.** Снаружи это обычный поставщик:
`transcribe` есть, `ask` нет. Смена модели — смена одного контейнера,
бот об этом не знает.

**Смешанный поставщик.** Речь слушает своя служба, текст пишет OpenAI —
это два разных умения и две разных цены. `Mixed` соединяет их в один объект,
чтобы дверь к модели оставалась одна.
"""
import logging
from dataclasses import dataclass, field

import aiohttp

from app.ai.provider import Answer, Heard, Piece, Provider, ProviderError

log = logging.getLogger("seta.ai.local")

# Сколько ждать расшифровку. Минута речи на слабом процессоре считается
# десятками секунд, и обрывать её раньше времени значит платить временем
# человека за ничего.
TIMEOUT_SECONDS = 300


@dataclass
class Local:
    """Расшифровка через свою службу. Текстовой модели здесь нет вовсе."""

    url: str
    name: str = "local"
    free_voice: bool = True
    model_hint: str = ""

    async def ask(
        self, *, system: str, user: str, model: str, max_output: int = 700
    ) -> Answer:
        # Служба расшифровки текстов не пишет. Отказ здесь честнее заглушки:
        # молчаливо пустой ответ выглядел бы как «модель не справилась».
        raise ProviderError("своя служба расшифровки не умеет отвечать текстом")

    async def transcribe(self, audio: bytes, *, model: str, hint: str = "") -> Heard:
        form = aiohttp.FormData()
        form.add_field("audio", audio, filename="voice.ogg",
                       content_type="application/octet-stream")
        if hint:
            form.add_field("hint", hint)

        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(self.url, data=form) as answer:
                    if answer.status != 200:
                        raise ProviderError(
                            f"служба расшифровки ответила {answer.status}"
                        )
                    body = await answer.json()
        except aiohttp.ClientError as error:
            raise ProviderError(f"служба расшифровки недоступна: {error}") from error
        except ValueError as error:
            raise ProviderError(f"служба расшифровки ответила не по форме: {error}") from error

        return Heard(
            text=str(body.get("text", "")).strip(),
            model=str(body.get("model", self.model_hint or "local")),
            seconds=float(body.get("seconds", 0) or 0),
            # Своя служба денег не стоит. Ноль здесь — не заглушка, а факт,
            # и на нём держится решение не проверять для неё бюджет.
            cost_usd=0.0,
            pieces=_pieces(body.get("pieces")),
        )


def _pieces(raw: object) -> list[Piece]:
    """Разметка по времени. Кривой ответ не роняет расшифровку — теряет абзацы."""
    if not isinstance(raw, list):
        return []
    found: list[Piece] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        try:
            found.append((float(item[0]), float(item[1]), str(item[2])))
        except (TypeError, ValueError):
            continue
    return found


@dataclass
class Mixed:
    """Речь слушает один, текст пишет другой. Дверь к модели остаётся одна."""

    voice: Provider
    text: Provider
    name: str = "mixed"
    # Бесплатность берётся у того, кто действительно слушает.
    free_voice: bool = field(default=False)

    def __post_init__(self) -> None:
        self.free_voice = getattr(self.voice, "free_voice", False)
        self.name = f"{self.text.name}+{self.voice.name}"

    async def ask(
        self, *, system: str, user: str, model: str, max_output: int = 700
    ) -> Answer:
        return await self.text.ask(
            system=system, user=user, model=model, max_output=max_output
        )

    async def transcribe(self, audio: bytes, *, model: str, hint: str = "") -> Heard:
        return await self.voice.transcribe(audio, model=model, hint=hint)
