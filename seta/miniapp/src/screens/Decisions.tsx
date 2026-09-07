/**
 * Реестр решений — тот же `registry`, с теми же условиями видимости.
 *
 * Решение не удаляется: оно закрывается или отменяется, и строка остаётся
 * навсегда. Поэтому список открывается на незакрытых — их и надо держать
 * в голове, — а закрытые доступны переключателем.
 */
import { useCallback, useEffect, useState } from "react";

import { get } from "../api";
import type { Result } from "../api";
import type { DecisionRow } from "../types";
import { Screen } from "./parts";
import type { Words } from "./parts";

interface List {
  items: DecisionRow[];
}

export function Decisions({ w, title }: { w: Words; title: string }) {
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [list, setList] = useState<Result<List> | null>(null);

  const load = useCallback(() => {
    setList(null);
    void get<List>(`/api/v1/decisions?only_open=${onlyOpen}`).then(setList);
  }, [onlyOpen]);

  useEffect(load, [load]);

  return (
    <div className="page">
      <div className="title">{title}</div>
      <div className="buckets">
        <button className="bucket" aria-pressed={onlyOpen} onClick={() => setOnlyOpen(true)}>
          {w("decision.registry")}
        </button>
        <button className="bucket" aria-pressed={!onlyOpen} onClick={() => setOnlyOpen(false)}>
          {w("task.list.done")}
        </button>
      </div>
      <Screen
        result={list}
        w={w}
        again={load}
        empty={w("decision.none")}
        isEmpty={(data) => data.items.length === 0}
      >
        {(data) =>
          data.items.map((decision) => (
            <div key={decision.id} className="card">
              <div className="card-title">{decision.title}</div>
              <div className="card-line">{decision.status_title}</div>
            </div>
          ))
        }
      </Screen>
    </div>
  );
}
