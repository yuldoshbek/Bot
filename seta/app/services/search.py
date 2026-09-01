"""Глобальный поиск: встречи, поручения, решения, люди, содержимое документов.

Одно правило важнее остальных: **права входят в запрос, а не применяются
к его результату.** Отфильтровать выдачу после `LIMIT` — значит показать
пустую страницу там, где должны быть свои записи, и, что хуже, показать имя
чужого документа до того, как выяснится, что открывать его нельзя.

Поэтому каждый вид ищется своим запросом со своим условием видимости, взятым
из той же службы, что отвечает за доступ к отдельной записи. Своих условий
здесь нет ни одного — иначе два описания одного правила рано или поздно
разойдутся, и разойдутся молча.

Морфология русская: «совещаний» находит «совещание». Для узбекского и других
языков встроенной морфологии в Postgres нет — там работает поиск по подстроке
с опечатками (trigram). Это честно хуже, и об этом сказано в плане блока.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Decision,
    Document,
    DocumentText,
    Meeting,
    Task,
    User,
    UserStatus,
)
from app.services import decisions as decision_service
from app.services import documents as document_service
from app.services import meetings as meeting_service
from app.services import tasks as task_service
from app.services.rbac import Grant, visible_department_ids

MIN_QUERY = 2
# Похожесть для поиска по имени: 0.3 прощает одну-две опечатки в фамилии
# и при этом не выдаёт всех подряд.
NAME_SIMILARITY = 0.3


@dataclass
class Hit:
    """Одна находка, готовая к показу."""

    kind: str
    id: int
    title: str
    subtitle: str = ""
    when: datetime | None = None


@dataclass
class Results:
    meetings: list[Hit] = field(default_factory=list)
    tasks: list[Hit] = field(default_factory=list)
    decisions: list[Hit] = field(default_factory=list)
    documents: list[Hit] = field(default_factory=list)
    people: list[Hit] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(group)
            for group in (self.meetings, self.tasks, self.decisions, self.documents, self.people)
        )

    @property
    def empty(self) -> bool:
        return self.total == 0


def _matches(*columns):
    """Условие полнотекстового совпадения по русской морфологии."""
    joined = columns[0]
    for column in columns[1:]:
        joined = joined + " " + func.coalesce(column, "")
    return func.to_tsvector("russian", joined)


async def search(
    session: AsyncSession,
    *,
    user: User,
    grants: dict[str, Grant],
    query: str,
    limit_per_kind: int = 5,
) -> Results:
    """Ищет по всему, что человеку открыто. Пустой запрос ничего не выгружает."""
    query = (query or "").strip()
    results = Results()
    if len(query) < MIN_QUERY:
        # Пустой или односимвольный запрос — это не «покажи всё».
        return results

    visible = await visible_department_ids(session, user)
    tsquery = func.plainto_tsquery("russian", query)

    # ── Встречи ────────────────────────────────────────────────────────────
    rows = (
        await session.execute(
            select(Meeting)
            .where(
                *meeting_service.visible_filter(user, grants, visible),
                _matches(Meeting.title, Meeting.description).op("@@")(tsquery),
            )
            .order_by(Meeting.start_at.desc())
            .limit(limit_per_kind)
        )
    ).scalars().all()
    results.meetings = [
        Hit(kind="meeting", id=m.id, title=m.title, subtitle="встреча", when=m.start_at)
        for m in rows
    ]

    # ── Поручения ──────────────────────────────────────────────────────────
    rows = (
        await session.execute(
            select(Task)
            .where(
                *task_service.visible_filter(user, grants, visible),
                _matches(Task.title, Task.description).op("@@")(tsquery),
            )
            .order_by(Task.created_at.desc())
            .limit(limit_per_kind)
        )
    ).scalars().all()
    results.tasks = [
        Hit(kind="task", id=t.id, title=t.title, subtitle="поручение", when=t.due_at)
        for t in rows
    ]

    # ── Решения ────────────────────────────────────────────────────────────
    rows = (
        await session.execute(
            select(Decision)
            .where(
                *decision_service.visible_filter(user, grants, visible),
                _matches(Decision.title, Decision.details).op("@@")(tsquery),
            )
            .order_by(Decision.created_at.desc())
            .limit(limit_per_kind)
        )
    ).scalars().all()
    results.decisions = [
        Hit(kind="decision", id=d.id, title=d.title, subtitle="решение", when=d.created_at)
        for d in rows
    ]

    # ── Документы: по содержимому и по имени ───────────────────────────────
    rows = (
        await session.execute(
            select(Document)
            .outerjoin(DocumentText, DocumentText.document_id == Document.id)
            .where(
                *document_service.visible_filter(user, grants, visible),
                or_(
                    DocumentText.search_vector.op("@@")(tsquery),
                    func.lower(Document.file_name).contains(query.lower()),
                    func.lower(func.coalesce(Document.title, "")).contains(query.lower()),
                ),
            )
            .order_by(Document.created_at.desc())
            .limit(limit_per_kind)
        )
    ).scalars().all()
    results.documents = [
        Hit(
            kind="document", id=d.id, title=d.title or d.file_name,
            subtitle="документ", when=d.created_at,
        )
        for d in rows
    ]

    # ── Люди ───────────────────────────────────────────────────────────────
    # Справочник коллег открыт всем внутри организации: это не тайна, а
    # необходимость — иначе поручение некому адресовать.
    rows = (
        await session.execute(
            select(User)
            .where(
                User.organization_id == user.organization_id,
                User.status == UserStatus.ACTIVE,
                or_(
                    func.lower(User.full_name).contains(query.lower()),
                    func.similarity(func.lower(User.full_name), query.lower()) > NAME_SIMILARITY,
                ),
            )
            .order_by(func.similarity(func.lower(User.full_name), query.lower()).desc())
            .limit(limit_per_kind)
        )
    ).scalars().all()
    results.people = [
        Hit(kind="person", id=p.id, title=p.full_name, subtitle="сотрудник") for p in rows
    ]

    return results
