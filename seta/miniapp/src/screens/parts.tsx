/**
 * Общие куски экранов: загрузка, отказ, пусто, карточка.
 *
 * Собраны в одном месте не ради экономии строк, а ради одинаковости: четыре
 * экрана, каждый со своим «загружаем» и своей кнопкой обновления, разъезжаются
 * на первой же правке — и пользователь видит четыре разных приложения.
 */
import type { ReactNode } from "react";

import type { Result, Trouble } from "../api";
import { TROUBLE_WORD } from "../api";

/** Достаёт подпись из словаря, присланного сервером. */
export type Words = (key: string) => string;

export function Loading({ w }: { w: Words }) {
  return <div className="state">{w("app.loading")}</div>;
}

/**
 * Отказ словами и кнопка «обновить».
 *
 * Кнопка появляется не всегда: перезапрашивать имеет смысл сеть, а не
 * закрытый раздел и не отсутствующий доступ. Кнопка, которая ничего
 * не меняет, учит не нажимать кнопок вовсе.
 */
export function Trouble({
  trouble,
  w,
  again,
}: {
  trouble: Trouble;
  w: Words;
  again?: () => void;
}) {
  const retryable = trouble === "offline" || trouble === "failed";
  return (
    <div className="state">
      <div>{w(TROUBLE_WORD[trouble])}</div>
      {retryable && again ? <button onClick={again}>{w("app.retry")}</button> : null}
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="state">{text}</div>;
}

/**
 * Разворачивает результат запроса в экран.
 *
 * Все четыре состояния — ожидание, отказ, пусто, данные — описаны здесь один
 * раз. Экран, забывший про одно из них, показывает белый лист, и человек
 * решает, что сломалось приложение.
 */
export function Screen<T>({
  result,
  w,
  again,
  empty,
  isEmpty,
  children,
}: {
  result: Result<T> | null;
  w: Words;
  again: () => void;
  empty?: string;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (result === null) return <Loading w={w} />;
  if (!result.ok) return <Trouble trouble={result.trouble} w={w} again={again} />;
  if (empty && isEmpty && isEmpty(result.data)) return <Empty text={empty} />;
  return <>{children(result.data)}</>;
}
