"""Черновик протокола встречи: предложения по пунктам, подтверждение по одному.

**Протокол опирается на повестку, а не на воображение.** Каждый предложенный
пункт привязан к пункту повестки; элемент, у которого повестки нет, отбрасывается
целиком. Встреча без повестки протокола не даёт — и это честнее, чем протокол,
придуманный моделью. Заодно это единственный способ ответить потом на вопрос
«откуда взялось это решение»: из пункта номер три.

**Основа черновика считается без модели.** Каждый пункт повестки, по которому
решения ещё нет, становится предложением с названием самого пункта. Модель
переформулирует, называет ответственного и срок — и если она молчит, черновик
всё равно есть. Список «по чему мы не записали решения» полезен сам по себе.

**Подтверждение по одному.** Кнопки «принять всё» здесь нет и не будет:
протокол, принятый одним нажатием, — это протокол, который никто не прочитал.
Каждый пункт проходит отдельно, и отклонённый не остаётся нигде.

**Повторное подтверждение не создаёт второй записи.** Состояние пункта хранится
в самом черновике: принятый пункт больше не принимается. Нажать кнопку дважды —
обычное дело, и вторая запись в реестре решений выглядела бы как решение,
которого не принимали.

**Ответственного и срок решает система.** Имя — через тот же поиск, что и в
поручениях; право поручать — через `may_assign_to`; срок — через `parse_due`.
Модель называет фрагмент речи, а не дату.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate
from app.ai.prompts import MEETING_PROTOCOL
from app.core.dates import parse_due
from app.core.text import cut
from app.models import (
    AgendaItem,
    Decision,
    Meeting,
    MeetingAttendance,
    Task,
    User,
)
from app.services import decisions as decision_service
from app.services import tasks as task_service
from app.services.rbac import Grant

log = logging.getLogger("seta.ai.protocol")

PROMPT_VERSION = MEETING_PROTOCOL.version

# Сколько предложений показывать. Протокол длиннее дюжины пунктов не читают,
# а подтверждать их по одному — работа на полчаса.
MAX_ITEMS = 12

KINDS = ("decision", "task")


@dataclass(slots=True)
class Item:
    """Одно предложение. Записи в реестре ему пока не соответствует."""

    kind: str
    title: str
    agenda_item_id: int
    agenda_number: int
    responsible_id: int | None = None
    heard_name: str = ""
    due_at: datetime | None = None
    due_phrase: str = ""
    # new — ждёт решения человека, taken — внесён, dropped — отклонён.
    state: str = "new"
    created_id: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_state(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "agenda_item_id": self.agenda_item_id,
            "agenda_number": self.agenda_number,
            "responsible_id": self.responsible_id,
            "heard_name": self.heard_name,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "due_phrase": self.due_phrase,
            "state": self.state,
            "created_id": self.created_id,
            "notes": list(self.notes),
        }

    @classmethod
    def from_state(cls, data: dict) -> "Item":
        due = data.get("due_at")
        return cls(
            kind=str(data["kind"]),
            title=str(data["title"]),
            agenda_item_id=int(data["agenda_item_id"]),
            agenda_number=int(data["agenda_number"]),
            responsible_id=data.get("responsible_id"),
            heard_name=str(data.get("heard_name", "")),
            due_at=datetime.fromisoformat(due) if due else None,
            due_phrase=str(data.get("due_phrase", "")),
            state=str(data.get("state", "new")),
            created_id=data.get("created_id"),
            notes=[str(note) for note in data.get("notes", [])],
        )


@dataclass(slots=True)
class Draft:
    """Черновик протокола. В реестре ему не соответствует ничего."""

    meeting_id: int
    items: list[Item] = field(default_factory=list)
    call_id: int | None = None

    @property
    def taken(self) -> int:
        return sum(1 for item in self.items if item.state == "taken")

    @property
    def dropped(self) -> int:
        return sum(1 for item in self.items if item.state == "dropped")

    def next_index(self) -> int | None:
        """Первый пункт, по которому человек ещё не решил."""
        for index, item in enumerate(self.items):
            if item.state == "new":
                return index
        return None

    def to_state(self) -> dict:
        return {
            "meeting_id": self.meeting_id,
            "call_id": self.call_id,
            "items": [item.to_state() for item in self.items],
        }

    @classmethod
    def from_state(cls, data: dict | None) -> "Draft | None":
        if not isinstance(data, dict) or not data.get("meeting_id"):
            return None
        try:
            return cls(
                meeting_id=int(data["meeting_id"]),
                call_id=data.get("call_id"),
                items=[Item.from_state(raw) for raw in data.get("items", [])],
            )
        except (KeyError, TypeError, ValueError):
            return None


def payload(raw: str) -> list[dict]:
    """Достаёт список предложений из ответа модели.

    Ответ не по форме — не ошибка: остаётся черновик, посчитанный без модели.
    """
    if not raw:
        return []
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [row for row in items if isinstance(row, dict)][:MAX_ITEMS]


async def build(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    grants: dict[str, Grant],
    now: datetime | None = None,
) -> Draft:
    """Собирает черновик. В реестре при этом не появляется ничего."""
    agenda = await decision_service.agenda_of(session, meeting)
    draft = Draft(meeting_id=meeting.id)
    if not agenda:
        # Встречи без повестки протокола не дают. Придумать его за участников
        # некому: ни коду, ни модели неоткуда знать, о чём говорили.
        return draft

    decided = {
        row[0]
        for row in (
            await session.execute(
                select(Decision.agenda_item_id).where(
                    Decision.meeting_id == meeting.id,
                    Decision.agenda_item_id.is_not(None),
                )
            )
        ).all()
    }
    # Основа считается без модели: пункт повестки без решения — уже предложение.
    numbers: dict[int, AgendaItem] = {}
    for number, point in enumerate(agenda, start=1):
        numbers[number] = point
        if point.id in decided:
            continue
        draft.items.append(
            Item(
                kind="decision",
                title=cut(point.title, 300),
                agenda_item_id=point.id,
                agenda_number=number,
            )
        )
    if not draft.items:
        return draft

    outcome = await gate.ask(
        session,
        organization_id=meeting.organization_id,
        kind="protocol",
        system=MEETING_PROTOCOL.system,
        user=await _ask_text(session, meeting=meeting, agenda=agenda, decided=decided),
        prompt_version=PROMPT_VERSION,
        user_id=actor.id,
        needs_confirmation=True,
    )
    draft.call_id = outcome.call_id
    if not outcome.worked:
        return draft

    await _merge(
        session, draft, payload(outcome.text),
        numbers=numbers, decided=decided, actor=actor, grants=grants, now=now,
    )
    return draft


async def _ask_text(
    session: AsyncSession,
    *,
    meeting: Meeting,
    agenda: list[AgendaItem],
    decided: set[int],
) -> str:
    """Что модель узнаёт о встрече. Только посчитанное, ничего сверх."""
    present = (
        await session.execute(
            select(User.full_name)
            .join(MeetingAttendance, MeetingAttendance.user_id == User.id)
            .where(
                MeetingAttendance.meeting_id == meeting.id,
                MeetingAttendance.present.is_(True),
            )
            .order_by(User.full_name)
        )
    ).scalars().all()
    recorded = (
        await session.execute(
            select(Decision.title).where(Decision.meeting_id == meeting.id)
        )
    ).scalars().all()
    assigned = (
        await session.execute(
            select(Task.title).where(Task.meeting_id == meeting.id)
        )
    ).scalars().all()

    lines = [f"Встреча: {meeting.title}", ""]
    if present:
        lines += ["Присутствовали: " + ", ".join(present), ""]
    lines.append("Повестка:")
    for number, point in enumerate(agenda, start=1):
        mark = "рассмотрен" if point.covered else "не отмечен рассмотренным"
        lines.append(f"  {number}. {point.title} — {mark}")
        if point.note:
            lines.append(f"     заметка: {cut(point.note, 400)}")
        if point.id in decided:
            lines.append("     по этому пункту решение уже записано")
    if recorded:
        lines += ["", "Уже в реестре решений:"] + [f"  — {title}" for title in recorded]
    if assigned:
        lines += ["", "Уже заведены поручения:"] + [f"  — {title}" for title in assigned]
    return "\n".join(lines)


async def _merge(
    session: AsyncSession,
    draft: Draft,
    rows: list[dict],
    *,
    numbers: dict[int, AgendaItem],
    decided: set[int],
    actor: User,
    grants: dict[str, Grant],
    now: datetime | None,
) -> None:
    """Переносит предложения модели в черновик — те, что привязаны к повестке.

    Элемент с чужим номером отбрасывается молча: он не про эту встречу,
    и починить его нечем.
    """
    by_agenda = {item.agenda_item_id: item for item in draft.items}

    for row in rows:
        try:
            number = int(row.get("agenda", 0))
        except (TypeError, ValueError):
            continue
        point = numbers.get(number)
        if point is None:
            continue
        kind = str(row.get("kind", "")).strip().lower()
        if kind not in KINDS:
            continue
        title = str(row.get("title", "")).strip()
        if len(title) < 3:
            continue

        if kind == "decision":
            if point.id in decided:
                # По этому пункту решение уже есть: второе — не протокол,
                # а дубль, и модели об этом было сказано.
                continue
            item = by_agenda.get(point.id)
            if item is None or item.kind != "decision":
                continue
            # Формулировка модели заменяет название пункта повестки:
            # это единственное, что она здесь улучшает.
            item.title = cut(title, 300)
        else:
            if any(
                existing.kind == "task" and existing.agenda_item_id == point.id
                for existing in draft.items
            ):
                continue
            item = Item(
                kind="task",
                title=cut(title, 300),
                agenda_item_id=point.id,
                agenda_number=number,
            )
            draft.items.append(item)

        item.heard_name = str(row.get("responsible", "")).strip()
        item.due_phrase = str(row.get("due", "")).strip()
        await _resolve(session, item, actor=actor, grants=grants, now=now)

    draft.items.sort(key=lambda item: (item.agenda_number, item.kind != "decision"))
    del draft.items[MAX_ITEMS:]


async def _resolve(
    session: AsyncSession,
    item: Item,
    *,
    actor: User,
    grants: dict[str, Grant],
    now: datetime | None,
) -> None:
    """Ответственный и срок — теми же функциями, что и при ручном вводе."""
    item.due_at = parse_due(item.due_phrase, actor.timezone, now=now)
    if item.due_phrase and item.due_at is None:
        item.notes.append("protocol.note.no_due")

    name = item.heard_name.strip()
    if not name:
        return
    found = await task_service.find_assignee(session, actor.organization_id, name)
    if len(found) != 1:
        item.notes.append("protocol.note.responsible_unknown")
        return
    person = found[0]
    if item.kind == "task" and not await task_service.may_assign_to(
        session, actor=actor, grants=grants, assignee=person
    ):
        # Поручение — не просто запись: право поручать проверяет система.
        item.notes.append("protocol.note.cannot_assign")
        return
    item.responsible_id = person.id


async def take(
    session: AsyncSession,
    draft: Draft,
    index: int,
    *,
    meeting: Meeting,
    actor: User,
    grants: dict[str, Grant],
) -> tuple[bool, str]:
    """Вносит один пункт. Возвращает (получилось, причина отказа).

    Единственное место, где предложение становится записью. «Принять всё»
    здесь нет намеренно: протокол, принятый одним нажатием, никто не прочитал.
    """
    if not 0 <= index < len(draft.items):
        return False, "protocol.err.stale"
    item = draft.items[index]
    if item.state != "new":
        # Повторное нажатие. Вторая запись выглядела бы как решение,
        # которого не принимали.
        return False, "protocol.err.already"

    responsible = (
        await session.get(User, item.responsible_id) if item.responsible_id else None
    )
    if responsible is not None and responsible.organization_id != actor.organization_id:
        return False, "protocol.err.other_org"

    if item.kind == "decision":
        point = await session.get(AgendaItem, item.agenda_item_id)
        if point is None or point.meeting_id != meeting.id:
            return False, "protocol.err.stale"
        outcome = await decision_service.create(
            session,
            actor=actor,
            title=item.title,
            meeting=meeting,
            agenda_item=point,
            responsible=responsible,
            due_date=item.due_at,
        )
        if not outcome.ok:
            return False, outcome.reason or "protocol.err.refused"
        item.created_id = outcome.item.id
    else:
        if responsible is None:
            return False, "protocol.err.no_assignee"
        # Право проверяется ещё раз — при записи. Между показом черновика
        # и нажатием кнопки человека могли перевести или уволить.
        if not await task_service.may_assign_to(
            session, actor=actor, grants=grants, assignee=responsible
        ):
            return False, "protocol.err.cannot_assign"
        try:
            task = await task_service.create_task(
                session,
                creator=actor,
                assignee=responsible,
                title=item.title,
                due_at=item.due_at,
                meeting_id=meeting.id,
            )
        except task_service.TaskError as error:
            return False, str(error)
        item.created_id = task.id

    item.state = "taken"
    await gate.mark_confirmed(session, draft.call_id, confirmed=True)
    return True, ""


async def drop(session: AsyncSession, draft: Draft, index: int) -> bool:
    """Отклоняет пункт. Он не остаётся нигде, кроме отметки в черновике."""
    if not 0 <= index < len(draft.items):
        return False
    item = draft.items[index]
    if item.state != "new":
        return False
    item.state = "dropped"
    if draft.next_index() is None and draft.taken == 0:
        # Отклонили всё: в журнале это отказ, а не «подтверждения не было».
        await gate.mark_confirmed(session, draft.call_id, confirmed=False)
    return True
