#!/bin/sh
# Ежедневная резервная копия базы.
#
# Запускается контейнером backup раз в сутки. Копии складываются в ./backups
# и хранятся 14 дней. Журнал аудита — юридическая запись «кто что поручил»,
# её потеря необратима, поэтому копия делается даже на пилоте.
#
# Копию, которую ни разу не восстанавливали, копией считать нельзя:
# раз в месяц прогоняйте scripts/restore.sh на пустой базе.
set -eu

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
DIR=/backups
STAMP=$(date +%Y-%m-%d_%H%M)
FILE="$DIR/seta_${STAMP}.sql.gz"

mkdir -p "$DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') копирую базу ${POSTGRES_DB}..."
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    --host=postgres \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --format=plain \
    --no-owner \
    | gzip -9 > "$FILE.tmp"

# Переименование в конце: недописанный файл никогда не выглядит как готовая копия.
mv "$FILE.tmp" "$FILE"
SIZE=$(du -h "$FILE" | cut -f1)
echo "$(date '+%Y-%m-%d %H:%M:%S') готово: $FILE ($SIZE)"

# Проверка, что копия не пустая: gzip -t читает файл целиком.
gzip -t "$FILE"

find "$DIR" -name 'seta_*.sql.gz' -mtime "+$KEEP_DAYS" -delete
COUNT=$(find "$DIR" -name 'seta_*.sql.gz' | wc -l)
echo "$(date '+%Y-%m-%d %H:%M:%S') копий в хранилище: $COUNT"
