# Служба расшифровки. Отдельный образ: полтора гигабайта модели незачем
# держать в образе бота, а перезапускать её надо независимо.
FROM python:3.13-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_TELEMETRY=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Веса лежат в томе, а не в образе: смена модели не пересобирает образ.
VOLUME ["/models"]
EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
