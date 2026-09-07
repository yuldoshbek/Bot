/**
 * «Мой день»: что сейчас, что дальше, что требует решения, что просрочено.
 *
 * Ни одно число здесь не считается. Всё приходит посчитанным из `/day` —
 * тем же `Board`, что уходит в бот и в утреннюю сводку. Сложи приложение
 * два числа само — и в чате с приложением разошлись бы цифры, а это худший
 * вид расхождения: обе стороны выглядят правдиво.
 *
 * Показатели приходят уже собранной строкой (`text`): их отрисовка живёт
 * в службе и одинакова для бота, сводки и приложения.
 */
import { useCallback, useEffect, useState } from "react";

import { get } from "../api";
import type { Result } from "../api";
import type { Day as DayData } from "../types";
import { Screen } from "./parts";
import type { Words } from "./parts";

/** Время встречи на языке и в поясе человека — их даёт браузер собеседника. */
function at(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function Day({ w, title }: { w: Words; title: string }) {
  const [day, setDay] = useState<Result<DayData> | null>(null);

  const load = useCallback(() => {
    setDay(null);
    void get<DayData>("/api/v1/day").then(setDay);
  }, []);

  useEffect(load, [load]);

  return (
    <div className="page">
      <div className="title">{title}</div>
      <Screen result={day} w={w} again={load}>
        {(data) => (
          <>
            {data.running.map((meeting) => (
              <div key={meeting.id} className="card">
                <div className="card-title">{meeting.title}</div>
                <div className="card-line">→ {at(meeting.end_at)}</div>
              </div>
            ))}
            {data.ahead.map((meeting) => (
              <div key={meeting.id} className="card">
                <div className="card-title">{meeting.title}</div>
                <div className="card-line">{at(meeting.start_at)}</div>
              </div>
            ))}

            <div className="counts">
              <Count label={w("menu.my_tasks")} value={data.overdue_total} />
              <Count label={w("task.list.review")} value={data.to_review} />
              <Count label={w("menu.decisions")} value={data.stale_decisions} />
              <Count label={w("menu.my_meetings")} value={data.requests_waiting} />
            </div>

            {data.personal_overdue.map((task) => (
              <div key={task.id} className="card">
                <div className="card-title">{task.title}</div>
                <div className="card-line">
                  {task.status_title}
                  {task.due_human ? ` · ${task.due_human}` : ""}
                </div>
              </div>
            ))}

            {data.metrics.map((metric) => (
              <div key={metric.key} className="card">
                <div className="card-title">{metric.title}</div>
                {/* Строка собрана службой: «нет данных» — тоже её слова. */}
                <div className="card-line">{metric.no_data ? w("app.no_data") : metric.text}</div>
              </div>
            ))}
          </>
        )}
      </Screen>
    </div>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div className="count">
      <b>{value}</b>
      <span className="small dim">{label}</span>
    </div>
  );
}
