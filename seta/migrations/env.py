"""Alembic: миграции схемы. Ручные правки боевой базы не допускаются."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Column
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.models import Base  # noqa: F401  - импорт наполняет метаданные

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Индексы по выражению autogenerate не сравнивает — и не должен пытаться.

    Alembic умеет сличать индексы по колонкам, но не по выражениям вида
    `to_tsvector('russian', ...)`: текст выражения из базы и из модели никогда
    не совпадёт посимвольно. Без этого фильтра `alembic check` показывал бы
    разницу на каждом запуске, и сторож, который кричит всегда, перестал бы
    что-либо значить.

    Плата: такой индекс autogenerate не создаст сам, его пишут в миграцию руками.
    Поэтому наличие каждого из них проверяется в smoke-наборе, а не на веру.
    """
    if type_ == "index":
        expressions = getattr(obj, "expressions", None) or []
        if any(not isinstance(e, Column) for e in expressions):
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata,
        compare_type=True, include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
