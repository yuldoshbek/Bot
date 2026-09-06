"""Внутренний интерфейс языковой модели — и ни одного обращения к сети здесь.

**Зачем слой.** Бизнес-логика обращается сюда, а не к OpenAI напрямую. Смена
модели или переход на локальную — замена одного класса. Заводить такой слой
«когда появится второй поставщик» поздно: к тому времени вызовы разойдутся
по шести местам, и менять придётся шесть.

Побочная польза важнее заявленной: **все проверки блока идут на подставном
поставщике**. Если хоть один сценарий требует настоящей сети — слой дырявый,
и это видно сразу.

**Стоимость считает поставщик, а не вызывающий.** Цена за тысячу токенов —
свойство модели, и знать её должен тот, кто эту модель зовёт. Иначе при смене
тарифа править придётся в шести местах, и одно забудут.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class Answer:
    """Ответ модели: текст, расход и чем он получен."""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class Heard:
    """Расшифровка речи."""

    text: str
    model: str
    seconds: float = 0.0
    cost_usd: float = 0.0


class ProviderError(Exception):
    """Поставщик не ответил. Текст — для журнала, не для человека."""


class Provider(Protocol):
    """То, что умеет поставщик. Больше от него ничего не требуется.

    Намеренно узкий интерфейс: чем он шире, тем труднее подменить поставщика
    и тем больше поводов протащить в бизнес-логику особенности одного из них.
    """

    name: str

    async def ask(
        self, *, system: str, user: str, model: str, max_output: int = 700
    ) -> Answer:
        """Один вопрос — один ответ. Диалога здесь нет и не нужно."""
        ...

    async def transcribe(self, audio: bytes, *, model: str, hint: str = "") -> Heard:
        """Речь в текст. `hint` — подсказка о языке и лексике."""
        ...


@dataclass
class Fake:
    """Подставной поставщик: отвечает заготовкой, в сеть не ходит.

    Живёт в основном коде, а не в проверках, намеренно. Во-первых, им
    пользуется каждый сценарий блока — значит, это часть системы. Во-вторых,
    он же служит режимом «ИИ выключен, но интерфейс на месте»: разработчику
    не нужен ключ, чтобы поднять систему целиком.
    """

    name: str = "fake"
    answers: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    # Сколько раз обращались — по этому счётчику проверки убеждаются, что при
    # исчерпанном бюджете вызова не было вовсе, а не был и проигнорирован.
    calls: int = 0
    fail: bool = False

    async def ask(
        self, *, system: str, user: str, model: str, max_output: int = 700
    ) -> Answer:
        self.calls += 1
        if self.fail:
            raise ProviderError("подставной поставщик настроен на отказ")
        text = self.answers.pop(0) if self.answers else ""
        return Answer(
            text=text, model=model,
            tokens_in=len(system) + len(user), tokens_out=len(text),
            cost_usd=0.0,
        )

    async def transcribe(self, audio: bytes, *, model: str, hint: str = "") -> Heard:
        self.calls += 1
        if self.fail:
            raise ProviderError("подставной поставщик настроен на отказ")
        text = self.transcripts.pop(0) if self.transcripts else ""
        return Heard(text=text, model=model, seconds=len(audio) / 16000, cost_usd=0.0)
