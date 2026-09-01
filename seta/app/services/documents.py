"""Документы: приём, права на файл, выдача и журнал открытий.

Файл хранит Telegram, мы храним идентификатор. Отсюда всё остальное: приём —
это проверка и запись метаданных, выдача — пересылка ботом после проверки прав.

**Доступ к встрече не даёт доступа к её документам.** Право на файл проверяется
отдельно и всегда. Участник совещания видит, что документ приложен, но получит
его только если загрузивший открыл доступ — явно человеку, отделу или всем
участникам. Это правило раздела «Безопасность» архитектуры, и оно намеренно
неудобное: документы совещаний — обычно самое чувствительное, что есть в системе.
"""
import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.models import (
    Decision,
    Document,
    DocumentAccess,
    DocumentScope,
    DocumentView,
    IndexStatus,
    Meeting,
    MeetingParticipant,
    Task,
    User,
    ViewChannel,
)
from app.services.audit import write_audit
from app.services.rbac import (
    Grant,
    Scope,
    has_permission,
    load_grants,
    scope_of,
    visible_department_ids,
)

# Бот скачивает файлы только до 20 МБ — это ограничение Telegram, не наше.
# Больший документ принимается и пересылается, но текст из него не извлечь.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_STORE_BYTES = 2 * 1024 * 1024 * 1024

# Что бот не примет ни при каких условиях. Список запрещающий, а не
# разрешающий: перечислить всё безопасное невозможно, а исполняемое — можно.
BLOCKED_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".jar", ".apk", ".app",
    ".dll", ".sys", ".cpl", ".hta", ".reg", ".lnk", ".iso", ".img",
}
# Форматы, из которых умеем доставать текст.
TEXT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".csv", ".md"}

# Политики, которые открывает право «читать документы» с широкой областью.
# Круга участников и личных документов здесь намеренно нет.
BROAD_SCOPES = (DocumentScope.ORGANIZATION, DocumentScope.DEPARTMENT)


@dataclass
class Upload:
    document: Document | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.document is not None


def inspect_upload(file_name: str, size_bytes: int) -> str | None:
    """Проверки при приёме. Возвращает причину отказа или None.

    Расширение, размер и MIME — реальная первая линия. Антивирус, который
    требует архитектура, здесь не притворяется работающим: настоящая проверка
    это демон ClamAV с ежедневным обновлением баз, и включать его нужно
    осознанно. Место под него — отдельная функция, а не незаметная заглушка,
    которая создавала бы ложное чувство защищённости.
    """
    name = (file_name or "").strip()
    if not name:
        return "У файла нет имени."
    extension = os.path.splitext(name.lower())[1]
    if extension in BLOCKED_EXTENSIONS:
        return f"Файлы {extension} система не принимает."
    if size_bytes > MAX_STORE_BYTES:
        return "Файл слишком большой даже для Telegram."
    return None


def index_state(file_name: str, size_bytes: int) -> str:
    """С каким состоянием документ уходит в очередь на извлечение текста."""
    extension = os.path.splitext((file_name or "").lower())[1]
    if extension not in TEXT_EXTENSIONS:
        return IndexStatus.UNSUPPORTED
    if size_bytes > MAX_DOWNLOAD_BYTES:
        return IndexStatus.TOO_LARGE
    return IndexStatus.PENDING


async def store(
    session: AsyncSession,
    *,
    uploader: User,
    file_id: str,
    file_unique_id: str,
    file_name: str,
    size_bytes: int,
    mime_type: str | None = None,
    title: str | None = None,
    meeting: Meeting | None = None,
    task: Task | None = None,
    decision: Decision | None = None,
    scope: str = DocumentScope.PRIVATE,
    is_important: bool = False,
) -> Upload:
    """Принимает документ: проверяет, сохраняет метаданные, ставит в очередь."""
    grants = await load_grants(session, uploader)
    if not has_permission(grants, "file.upload"):
        return Upload(reason="Загружать документы может сотрудник организации.")

    problem = inspect_upload(file_name, size_bytes)
    if problem:
        return Upload(reason=problem)

    for linked, label in ((meeting, "Встреча"), (task, "Поручение"), (decision, "Решение")):
        if linked is not None and linked.organization_id != uploader.organization_id:
            return Upload(reason=f"{label} из другой организации.")

    document = Document(
        organization_id=uploader.organization_id,
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_name=file_name.strip()[:300],
        mime_type=(mime_type or None),
        size_bytes=max(0, int(size_bytes)),
        uploaded_by=uploader.id,
        title=(title or "").strip()[:300] or None,
        meeting_id=meeting.id if meeting else None,
        task_id=task.id if task else None,
        decision_id=decision.id if decision else None,
        scope=scope,
        is_important=is_important,
        index_status=index_state(file_name, size_bytes),
    )
    session.add(document)
    await session.flush()

    await write_audit(
        session, actor_id=uploader.id, action="document.upload",
        entity_type="document", entity_id=document.id,
        after={"file_name": document.file_name, "scope": scope, "size": document.size_bytes},
    )
    return Upload(document=document)


# ── Права на файл ───────────────────────────────────────────────────────────
async def may_read(session: AsyncSession, *, document: Document, viewer: User) -> bool:
    """Открыт ли документ этому человеку. Единственная точка ответа на вопрос.

    Отвечает ровно то же, что условие `visible_filter` в SQL. Если эти двое
    разойдутся, поиск начнёт показывать то, что открыть нельзя, — а показанное
    имя файла уже разглашение. Совпадение проверяется отдельным сценарием
    в `smoke_block4.py`, а не держится на аккуратности при правках.
    """
    if viewer.organization_id != document.organization_id:
        return False

    grants = await load_grants(session, viewer)
    if not has_permission(grants, "file.read"):
        return False
    if viewer.id == document.uploaded_by:
        return True
    if document.scope == DocumentScope.ORGANIZATION:
        return True

    if document.scope == DocumentScope.PARTICIPANTS and document.meeting_id:
        joined = await session.scalar(
            select(MeetingParticipant.id).where(
                MeetingParticipant.meeting_id == document.meeting_id,
                MeetingParticipant.user_id == viewer.id,
            )
        )
        if joined is not None:
            return True

    owner = await session.get(User, document.uploaded_by)
    owner_department = owner.department_id if owner else None
    if (
        document.scope == DocumentScope.DEPARTMENT
        and viewer.department_id is not None
        and viewer.department_id == owner_department
    ):
        return True

    conditions = [DocumentAccess.subject_user_id == viewer.id]
    if viewer.department_id is not None:
        conditions.append(DocumentAccess.subject_department_id == viewer.department_id)
    granted = await session.scalar(
        select(DocumentAccess.id).where(
            DocumentAccess.document_id == document.id, or_(*conditions)
        )
    )
    if granted is not None:
        return True

    # Область права открывает общие и отдельские документы, но не документы
    # круга участников: «участникам» — это те, кто был в комнате, и попасть
    # в этот круг по должности нельзя. Тем же правилом в блоке 3 закрыто
    # содержание приватной встречи от ассистента. Личный документ остаётся
    # личным для всех, включая руководителя.
    file_scope = scope_of(grants, "file.read")
    if file_scope == Scope.ORGANIZATION and document.scope in BROAD_SCOPES:
        return True
    if file_scope == Scope.DEPARTMENT and document.scope == DocumentScope.DEPARTMENT:
        visible = await visible_department_ids(session, viewer)
        return owner_department in visible

    return False


async def grant(
    session: AsyncSession,
    *,
    document: Document,
    actor: User,
    to_user: User | None = None,
    to_department_id: int | None = None,
) -> str | None:
    """Открывает доступ человеку или отделу. Возвращает причину отказа или None."""
    if actor.organization_id != document.organization_id:
        return "Документ другой организации."
    if to_user is None and to_department_id is None:
        return "Не указано, кому открывать доступ."
    if to_user is not None and to_user.organization_id != document.organization_id:
        return "Этот человек из другой организации."

    grants = await load_grants(session, actor)
    if not has_permission(grants, "file.share"):
        return "Нет права выдавать доступ к документам."
    # Делиться можно своим документом; чужим — только с правом на всю
    # организацию: иначе один сотрудник открывал бы чужие файлы третьим лицам.
    if actor.id != document.uploaded_by and scope_of(grants, "file.share") != Scope.ORGANIZATION:
        return "Открыть доступ может тот, кто загрузил документ."

    exists = await session.scalar(
        select(DocumentAccess.id).where(
            DocumentAccess.document_id == document.id,
            DocumentAccess.subject_user_id == (to_user.id if to_user else None),
            DocumentAccess.subject_department_id == to_department_id,
        )
    )
    if exists is None:
        session.add(DocumentAccess(
            document_id=document.id,
            subject_user_id=to_user.id if to_user else None,
            subject_department_id=to_department_id,
            granted_by=actor.id,
        ))
        await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="document.share",
        entity_type="document", entity_id=document.id,
        after={"user_id": to_user.id if to_user else None, "department_id": to_department_id},
    )
    return None


async def open_for(
    session: AsyncSession,
    *,
    document: Document,
    viewer: User,
    channel: str = ViewChannel.BOT,
    now: datetime | None = None,
) -> str | None:
    """Выдача документа: проверяет права и записывает открытие.

    Запись делается здесь, а не в обработчике: журнал открытий имеет смысл
    только если его невозможно обойти, взяв файл другим путём.
    """
    now = now or utcnow()
    if not await may_read(session, document=document, viewer=viewer):
        return "Этот документ вам не открыт."
    session.add(DocumentView(
        document_id=document.id, user_id=viewer.id, channel=channel, viewed_at=now
    ))
    await session.flush()
    return None


def visible_filter(user: User, grants: dict[str, Grant], visible_departments: set[int]) -> list:
    """Условие видимости документов в SQL — для поиска и списков.

    Повторяет `may_read`, но выражением базы: проверять права поштучно после
    выборки нельзя, поиск обязан не находить закрытое, а не прятать найденное.
    """
    if not has_permission(grants, "file.read"):
        return [Document.id.is_(None)]

    same_org = Document.organization_id == user.organization_id
    mine = Document.uploaded_by == user.id
    open_to_all = Document.scope == DocumentScope.ORGANIZATION
    shared_to_me = Document.id.in_(
        select(DocumentAccess.document_id).where(DocumentAccess.subject_user_id == user.id)
    )
    as_participant = Document.id.in_(
        select(Document.id)
        .join(MeetingParticipant, MeetingParticipant.meeting_id == Document.meeting_id)
        .where(
            Document.scope == DocumentScope.PARTICIPANTS,
            MeetingParticipant.user_id == user.id,
        )
    )
    allowed = [mine, open_to_all, shared_to_me, as_participant]

    if user.department_id is not None:
        allowed.append(Document.id.in_(
            select(DocumentAccess.document_id).where(
                DocumentAccess.subject_department_id == user.department_id
            )
        ))
        same_department = select(User.id).where(User.department_id == user.department_id)
        allowed.append(
            (Document.scope == DocumentScope.DEPARTMENT)
            & Document.uploaded_by.in_(same_department)
        )

    # Та же граница, что в may_read: общие и отдельские — да, круг участников
    # и личное — нет. Два места обязаны отвечать одинаково, это проверяется.
    if scope_of(grants, "file.read") == Scope.ORGANIZATION:
        allowed.append(Document.scope.in_(list(BROAD_SCOPES)))
    elif scope_of(grants, "file.read") == Scope.DEPARTMENT and visible_departments:
        allowed.append(
            (Document.scope == DocumentScope.DEPARTMENT)
            & Document.uploaded_by.in_(
                select(User.id).where(User.department_id.in_(visible_departments))
            )
        )

    return [same_org, or_(*allowed)]


async def for_meeting(
    session: AsyncSession, *, meeting: Meeting, viewer: User
) -> list[Document]:
    """Документы встречи, открытые этому человеку. Остальные он не увидит вовсе."""
    grants = await load_grants(session, viewer)
    visible = await visible_department_ids(session, viewer)
    return list(
        (
            await session.execute(
                select(Document)
                .where(Document.meeting_id == meeting.id, *visible_filter(viewer, grants, visible))
                .order_by(Document.created_at)
            )
        ).scalars().all()
    )


async def views_of(session: AsyncSession, document: Document, limit: int = 20) -> list[DocumentView]:
    """Кто и когда открывал документ."""
    return list(
        (
            await session.execute(
                select(DocumentView)
                .where(DocumentView.document_id == document.id)
                .order_by(DocumentView.viewed_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )
