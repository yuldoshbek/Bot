"""Служба расшифровки речи: своя, на своём железе, без денег за минуту.

Одна ручка: `POST /transcribe`, на вход файл, на выход текст и разметка
по времени. Больше от неё ничего не требуется, и это осознанно: чем шире
интерфейс, тем труднее заменить модель.

**Почему отдельным процессом.** Расшифровка занимает процессор целиком
и на несколько секунд. Внутри бота это остановило бы все обработчики разом:
у asyncio один поток. Здесь же долгий счёт никому не мешает, служба
перезапускается сама по себе, а полтора гигабайта модели не лежат в образе бота.

**Модель загружается один раз.** Первый запуск скачивает веса, дальше берёт
из тома. Держать модель в памяти между запросами обязательно: загрузка стоит
дольше, чем сама расшифровка.

**Какую модель брать.** Смотрите `stt/README.md`: базовый `whisper` понимает
узбекский плохо, и выбор дообученной модели — главное решение здесь.
"""
import logging
import os
import tempfile

from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s")
log = logging.getLogger("seta.stt")

# Что грузить. Имя из HuggingFace или путь к локальным весам.
MODEL_NAME = os.getenv("STT_MODEL", "small")
# cpu или cuda. На процессоре int8 — единственный разумный выбор: втрое меньше
# памяти и заметно быстрее, а на распознавание речи это влияет мало.
DEVICE = os.getenv("STT_DEVICE", "cpu")
COMPUTE = os.getenv("STT_COMPUTE", "int8")
# Пусто — язык определяется сам. Речь в кабинете смешанная, и навязанный язык
# портит половину записей; ставьте `uz` или `ru`, только если уверены.
LANGUAGE = os.getenv("STT_LANGUAGE", "") or None
THREADS = int(os.getenv("STT_THREADS", "0") or 0)

app = FastAPI(title="SETA STT")
_model: WhisperModel | None = None


def model() -> WhisperModel:
    """Модель в памяти. Загрузка стоит дольше расшифровки — грузим один раз."""
    global _model
    if _model is None:
        log.info("загружаю модель %s (%s, %s)", MODEL_NAME, DEVICE, COMPUTE)
        _model = WhisperModel(
            MODEL_NAME, device=DEVICE, compute_type=COMPUTE,
            cpu_threads=THREADS or 0, download_root="/models",
        )
        log.info("модель загружена")
    return _model


@app.get("/health")
async def health() -> dict:
    """Жива ли служба и какая в ней модель. Модель при этом не грузится:
    проверка состояния не должна сама по себе занимать полтора гигабайта."""
    return {"ok": True, "model": MODEL_NAME, "loaded": _model is not None}


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...), hint: str = Form(default="")
) -> dict:
    """Речь в текст. Разметка по времени возвращается вместе с текстом:
    по паузам вызывающий разбивает расшифровку на абзацы."""
    data = await audio.read()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as handle:
        handle.write(data)
        handle.flush()
        pieces, info = model().transcribe(
            handle.name,
            language=LANGUAGE,
            # Подсказка о лексике: имена и рабочие слова так узнаются лучше.
            initial_prompt=hint or None,
            # Тишина между фразами вырезается — иначе модель придумывает слова
            # в паузах, и это её самая заметная ошибка на диктовке.
            vad_filter=True,
            beam_size=5,
        )
        found = [
            (round(piece.start, 2), round(piece.end, 2), piece.text.strip())
            for piece in pieces
        ]

    return {
        "text": " ".join(text for _, _, text in found).strip(),
        "model": MODEL_NAME,
        "seconds": round(getattr(info, "duration", 0.0) or 0.0, 2),
        "language": getattr(info, "language", "") or "",
        "pieces": found,
    }
