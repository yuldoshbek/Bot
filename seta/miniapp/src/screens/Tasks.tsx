/**
 * Поручения: разрезы списка и карточка.
 *
 * Разрезы приходят с сервера (`me.buckets`) вместе с названиями — те же
 * шесть, что кнопками в боте. Перечислить их здесь значило бы завести второй
 * список, который однажды разойдётся с первым, и в приложении не окажется
 * разреза, появившегося в боте.
 *
 * Кнопки действий в карточке рисуются по `can`, который считает та же
 * `access_for`, что и в боте. Гадать по ролям приложение не должно: гадание
 * рано или поздно нарисует кнопку, за которой отказ.
 */
import { useCallback, useEffect, useState } from "react";

import { get } from "../api";
import type { Result } from "../api";
import type { TaskCard, TaskRow } from "../types";
import { Screen } from "./parts";
import type { Words } from "./parts";
import { backButton } from "../telegram";

interface List {
  bucket: string;
  items: TaskRow[];
}

export function Tasks({
  w,
  buckets,
}: {
  w: Words;
  buckets: string[];
}) {
  const [bucket, setBucket] = useState(buckets[0] ?? "active");
  const [list, setList] = useState<Result<List> | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  const load = useCallback(() => {
    setList(null);
    void get<List>(`/api/v1/tasks?bucket=${encodeURIComponent(bucket)}`).then(setList);
  }, [bucket]);

  useEffect(load, [load]);

  if (openId !== null) {
    return <Card id={openId} w={w} close={() => setOpenId(null)} />;
  }

  return (
    <div className="page">
      <div className="title">{w("task.list.title")}</div>
      <div className="buckets">
        {buckets.map((name) => (
          <button
            key={name}
            className="bucket"
            aria-pressed={name === bucket}
            onClick={() => setBucket(name)}
          >
            {w(`task.list.${name}`)}
          </button>
        ))}
      </div>
      <Screen
        result={list}
        w={w}
        again={load}
        empty={w("task.list.empty")}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) =>
          data.items.map((task) => (
            <button key={task.id} className="card" onClick={() => setOpenId(task.id)}>
              <div className="card-title">{task.title}</div>
              <div className="card-line">
                {task.status_title}
                {task.due_human ? ` · ${task.due_human}` : ""}
              </div>
            </button>
          ))
        }
      </Screen>
    </div>
  );
}

function Card({ id, w, close }: { id: number; w: Words; close: () => void }) {
  const [card, setCard] = useState<Result<TaskCard> | null>(null);

  const load = useCallback(() => {
    setCard(null);
    void get<TaskCard>(`/api/v1/tasks/${id}`).then(setCard);
  }, [id]);

  useEffect(load, [load]);

  // Возврат — штатной кнопкой Telegram. Своя означала бы две кнопки назад
  // на одном экране, и они вели бы по-разному.
  useEffect(() => {
    backButton(close);
    return () => backButton(null);
  }, [close]);

  return (
    <div className="page">
      <Screen result={card} w={w} again={load}>
        {(task) => (
          <>
            <div className="title">{task.title}</div>
            <div className="card">
              <div className="card-line">{task.status_title}</div>
              <div className="card-line">{task.priority_title}</div>
              {task.due_human ? <div className="card-line">{task.due_human}</div> : null}
            </div>
            {task.description ? <div className="card">{task.description}</div> : null}
            {/*
              Действия остаются в чате. Приложение фазы 8 читает, а не меняет:
              у API нет ни одного маршрута записи, и рисовать кнопку, за которой
              ничего нет, — обман. Что человек вправе сделать, видно по `can`.
            */}
            {Object.values(task.can).some(Boolean) ? (
              <div className="state small">{w("app.in_bot")}</div>
            ) : null}
          </>
        )}
      </Screen>
    </div>
  );
}
