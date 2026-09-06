"""Единый справочник моделей: кто что делает и какой моделью.

**Зачем справочник.** Моделей в системе три рода — рутинная текстовая, старшая
текстовая и расшифровка речи, — и каждая упоминается в нескольких местах:
в настройках, в вызове, в журнале, в документации службы расшифровки. Пока
имя модели пишется в каждом сценарии, они расходятся: где-то поправили,
где-то забыли, и объяснить, почему один сценарий стал дороже, уже нечем.

Здесь имя модели выбирается **по виду вызова**, а не пишется руками. Сценарий
говорит, что он делает («сводка», «недельный отчёт»), а какой моделью — решает
это место. Ни один сценарий не знает имён моделей, и это проверяется
по исходнику.

**Список моделей расшифровки общий со службой.** Он лежит в `stt/models.json`
и читается обоими: службой при загрузке весов и ботом — чтобы показать
администратору, что вообще можно выбрать. Двух копий нет намеренно: разошлись
бы они молча, и в журнале стояла бы одна модель, а слушала бы другая.
"""
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("seta.ai.models")

# Общий со службой расшифровки список. Путь от корня проекта, а не от пакета:
# служба живёт рядом с приложением, а не внутри него.
CATALOGUE_PATH = Path(__file__).resolve().parents[2] / "stt" / "models.json"

# Какой вид вызова какой моделью обслуживается. Роль, а не имя: имя меняется
# в настройках, роль — свойство сценария.
KINDS: dict[str, str] = {
    "voice_task": "routine",
    "digest": "routine",
    "protocol": "routine",
    "ask": "routine",
    "weekly_report": "report",
    "voice_transcribe": "voice",
}

ROLES = ("routine", "report", "voice")


@dataclass(frozen=True, slots=True)
class Speech:
    """Модель расшифровки из общего списка."""

    alias: str
    repo: str
    convert: bool
    ram_mb: int
    languages: str
    note: str


def role_of(kind: str) -> str:
    """Какая модель обслуживает этот вид вызова.

    Незнакомый вид — рутинная модель и запись в лог: неизвестный сценарий
    не должен молча уйти на старшую модель и стоить в двадцать раз дороже.
    """
    role = KINDS.get(kind)
    if role is None:
        log.warning("неизвестный вид вызова: %s", kind)
        return "routine"
    return role


def name_for(kind: str) -> str:
    """Имя модели для этого вида вызова. Единственное место, где оно берётся."""
    role = role_of(kind)
    if role == "report":
        return settings.ai_model_report
    if role == "voice":
        return settings.ai_model_voice
    return settings.ai_model_routine


@lru_cache(maxsize=1)
def speech_models() -> dict[str, Speech]:
    """Список моделей расшифровки — общий со службой.

    Файла нет или он испорчен — это не повод падать: расшифровка настраивается
    переменной окружения и работает без справочника. Справочник нужен, чтобы
    показать человеку, из чего выбирать.
    """
    try:
        raw = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        found = raw["models"]
    except (OSError, ValueError, KeyError) as error:
        log.warning("список моделей расшифровки не прочитан: %s", error)
        return {}
    return {
        alias: Speech(
            alias=alias,
            repo=str(item.get("repo", alias)),
            convert=bool(item.get("convert", False)),
            ram_mb=int(item.get("ram_mb", 0) or 0),
            languages=str(item.get("languages", "")),
            note=str(item.get("note", "")),
        )
        for alias, item in found.items()
    }


def for_uzbek() -> list[Speech]:
    """Модели, обученные на узбекской речи.

    Базовый Whisper понимает узбекский плохо, и выбор здесь — не украшение,
    а разница между работающей функцией и мусором на выходе.
    """
    return [item for item in speech_models().values() if "uz" in item.languages]
