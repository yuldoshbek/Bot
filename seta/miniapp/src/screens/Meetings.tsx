/**
 * Встречи на две недели вперёд, сгруппированные по дням.
 *
 * Группировка — единственное, что приложение делает с данными само, и делает
 * оно это по дате, а не по смыслу: сетка календаря без разделителей по дням
 * читается как один длинный список, в котором завтра неотличимо от пятницы.
 *
 * Дата и время показываются в поясе телефона, а не сервера. Это тот же пояс,
 * в котором человек живёт, и подставляет его браузер.
 */
import { useCallback, useEffect, useState } from "react";

import { get } from "../api";
import type { Result } from "../api";
import type { MeetingRow } from "../types";
import { Screen } from "./parts";
import type { Words } from "./parts";

interface List {
  days: number;
  items: MeetingRow[];
}

function dayKey(iso: string): string {
  return new Date(iso).toDateString();
}

function dayTitle(iso: string): string {
  return new Date(iso).toLocaleDateString([], {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
}

function at(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function Meetings({ w, title }: { w: Words; title: string }) {
  const [list, setList] = useState<Result<List> | null>(null);

  const load = useCallback(() => {
    setList(null);
    void get<List>("/api/v1/meetings").then(setList);
  }, []);

  useEffect(load, [load]);

  return (
    <div className="page">
      <div className="title">{title}</div>
      <Screen
        result={list}
        w={w}
        again={load}
        empty={w("app.meetings_none")}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) => {
          let shown = "";
          return data.items.map((meeting) => {
            const key = dayKey(meeting.start_at);
            const first = key !== shown;
            shown = key;
            return (
              <div key={meeting.id}>
                {first ? (
                  <div className="card-line" style={{ margin: "14px 2px 6px" }}>
                    {dayTitle(meeting.start_at)}
                  </div>
                ) : null}
                <div className="card">
                  <div className="card-title">{meeting.title}</div>
                  <div className="card-line">
                    {at(meeting.start_at)} — {at(meeting.end_at)}
                  </div>
                </div>
              </div>
            );
          });
        }}
      </Screen>
    </div>
  );
}
