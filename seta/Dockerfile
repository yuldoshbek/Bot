FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PYTHONPATH=/app

WORKDIR /app

# Шрифт с кириллицей нужен для выгрузки в PDF: встроенные в PDF базовые
# шрифты русских букв не содержат, и отчёт вышел бы из знаков вопроса.
RUN apt-get update -qq     && apt-get install -y -qq --no-install-recommends fonts-dejavu-core     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "app.bot.run"]
