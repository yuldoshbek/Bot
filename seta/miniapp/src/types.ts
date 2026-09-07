/**
 * Что отдаёт API. Описания повторяют маршруты `app/api/v1/routes.py`
 * и ничего к ним не добавляют.
 *
 * Ни одного поля, которого нет в ответе, здесь быть не должно: приложение,
 * рисующее то, чего сервер не присылал, показывает выдумку — и заметно это
 * становится в кабинете, а не в сборке.
 */

export interface Me {
  id: number;
  full_name: string;
  roles: string[];
  roles_title: string;
  timezone: string;
  locale: string;
  locales: { code: string; title: string }[];
  organization: string;
  features: Record<string, boolean>;
  buckets: string[];
  words: Record<string, string>;
}

export interface TaskRow {
  id: number;
  title: string;
  status: string;
  status_title: string;
  priority: string;
  priority_title: string;
  due_at: string | null;
  due_human: string | null;
  requires_review: boolean;
  personal_control: boolean;
  rework_count: number;
}

export interface TaskCard extends TaskRow {
  description: string | null;
  can: Record<string, boolean>;
}

export interface Metric {
  key: string;
  title: string;
  value: number | null;
  detail: string;
  no_data: boolean;
  text: string;
}

export interface Day {
  date: string;
  quiet: boolean;
  running: { id: number; title: string; end_at: string }[];
  ahead: { id: number; title: string; start_at: string }[];
  free_from: string | null;
  requests_waiting: number;
  requests_over_quota: number;
  to_review: number;
  stale_decisions: number;
  overdue_total: number;
  overdue_by_department: { department: string | null; count: number }[];
  overdue_other: number;
  personal_overdue: TaskRow[];
  metrics: Metric[];
}

export interface MeetingRow {
  id: number;
  title: string;
  start_at: string;
  end_at: string;
  status: string;
}

export interface DecisionRow {
  id: number;
  title: string;
  status: string;
  status_title: string;
  due_date: string | null;
  responsible_id: number | null;
}
