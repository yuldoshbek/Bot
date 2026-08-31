"""Перечисления системы. Значения хранятся строками — читаемо в базе и в журнале."""
from enum import StrEnum


class RoleCode(StrEnum):
    EXECUTIVE = "EXECUTIVE"        # Руководитель
    ASSISTANT = "ASSISTANT"        # Ассистент
    DEPT_HEAD = "DEPT_HEAD"        # Начальник отдела
    EMPLOYEE = "EMPLOYEE"          # Сотрудник
    ADMIN = "ADMIN"                # Администратор
    AUDITOR = "AUDITOR"            # Аудитор


class UserStatus(StrEnum):
    PENDING = "PENDING"            # Заявка подана, роль не подтверждена
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"        # Доступ приостановлен
    REJECTED = "REJECTED"


class Availability(StrEnum):
    """Индикатор доступности руководителя — он ставит его сам, одной кнопкой."""
    OFFLINE = "OFFLINE"            # Не отмечен: работают только правила календаря
    OPEN = "OPEN"                  # Доступен для приёма прямо сейчас
    BUSY = "BUSY"                  # Занят
    DND = "DND"                    # Не беспокоить: даже срочные запросы придержатся


class TaskStatus(StrEnum):
    NEW = "NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class Priority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MeetingStatus(StrEnum):
    PLANNED = "PLANNED"
    CONFIRMED = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class MeetingVisibility(StrEnum):
    NORMAL = "NORMAL"              # Ассистент видит детали
    PRIVATE = "PRIVATE"            # Только руководитель; для остальных — занятое время


class RequestStatus(StrEnum):
    NEW = "NEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class AbsenceKind(StrEnum):
    VACATION = "VACATION"
    TRIP = "TRIP"
    SICK = "SICK"
    OTHER = "OTHER"


class Scope(StrEnum):
    """Область видимости права."""
    SELF = "SELF"
    DEPARTMENT = "DEPARTMENT"
    SUBORDINATES = "SUBORDINATES"
    ORGANIZATION = "ORGANIZATION"


class TaskEventKind(StrEnum):
    """Что именно произошло с поручением."""
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    STARTED = "STARTED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    RETURNED = "RETURNED"          # возврат на доработку
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    UNBLOCKED = "UNBLOCKED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"
    DUE_CHANGED = "DUE_CHANGED"
    REASSIGNED = "REASSIGNED"
    COMMENTED = "COMMENTED"
    ESCALATED = "ESCALATED"


class ExtensionStatus(StrEnum):
    NEW = "NEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"      # получатель отключил уведомления


class NotificationPriority(StrEnum):
    LOW = "LOW"                    # копится в сводку
    NORMAL = "NORMAL"              # ждёт окончания тихих часов
    CRITICAL = "CRITICAL"          # уходит немедленно, тихие часы не действуют
