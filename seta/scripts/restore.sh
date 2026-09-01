#!/bin/sh
# Восстановление базы из резервной копии.
#
#   docker compose exec -T postgres sh < scripts/restore.sh          # последняя копия
#   BACKUP_FILE=backups/seta_2026-09-01_0300.sql.gz sh scripts/restore.sh
#
# Скрипт СТИРАЕТ текущее содержимое базы и заменяет его копией.
# Запускать осознанно: он спрашивает подтверждение, если запущен из терминала.
set -eu

DIR="${BACKUP_DIR:-./backups}"
FILE="${BACKUP_FILE:-}"

if [ -z "$FILE" ]; then
    FILE=$(ls -1t "$DIR"/seta_*.sql.gz 2>/dev/null | head -1 || true)
fi

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "Копия не найдена. Проверьте папку $DIR или задайте BACKUP_FILE."
    exit 1
fi

echo "Восстановить базу из копии:"
echo "  $FILE  ($(du -h "$FILE" | cut -f1), $(date -r "$FILE" '+%d.%m.%Y %H:%M'))"
echo
echo "ТЕКУЩИЕ ДАННЫЕ БУДУТ ЗАМЕНЕНЫ."

if [ -t 0 ]; then
    printf 'Продолжить? напишите ДА: '
    read -r answer
    [ "$answer" = "ДА" ] || { echo "Отменено."; exit 1; }
fi

echo "Проверяю целостность копии..."
gzip -t "$FILE"

echo "Останавливаю приложение, чтобы никто не писал во время восстановления..."
docker compose stop bot worker api

echo "Восстанавливаю..."
gunzip -c "$FILE" | docker compose exec -T postgres psql \
    --username="${POSTGRES_USER:-seta}" \
    --dbname="${POSTGRES_DB:-seta}" \
    --set ON_ERROR_STOP=on \
    --quiet

echo "Поднимаю приложение..."
docker compose up -d

echo
echo "Готово. Проверьте: docker compose logs -f bot"
