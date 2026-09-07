/**
 * Единственный способ обратиться к API.
 *
 * **Подпись уходит заголовком на каждом запросе.** Не в адресе: в адресе она
 * попала бы в журналы прокси и в историю браузера целиком, вместе с подписью.
 *
 * **Отказ — это состояние, а не исключение.** Приложение открывают в лифте
 * и в машине; «нет связи» и «доступ не открыт» — обычные ответы, и каждый
 * из них экран обязан показать словами, а не пустотой. Поэтому здесь
 * возвращается результат с причиной, а не бросается ошибка, которую кто-то
 * однажды забудет поймать.
 */
import { initData } from "./telegram";

const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

/** Почему не вышло. Не текст: подписи приходят из общего словаря. */
export type Trouble =
  | "offline"      // сети нет или API не ответил
  | "denied"       // подпись не принята, доступ не открыт
  | "not_found"    // записи нет или она не видна
  | "closed"       // раздел выключен администратором
  | "failed";      // всё остальное

export type Result<T> = { ok: true; data: T } | { ok: false; trouble: Trouble };

export function configured(): boolean {
  return BASE.length > 0;
}

function troubleOf(status: number): Trouble {
  if (status === 401 || status === 403) return "denied";
  if (status === 404) return "closed";
  if (status === 429) return "failed";
  return "failed";
}

export async function get<T>(path: string): Promise<Result<T>> {
  if (!configured()) return { ok: false, trouble: "failed" };

  let answer: Response;
  try {
    answer = await fetch(`${BASE}${path}`, {
      headers: { "X-Telegram-Init-Data": initData() },
    });
  } catch {
    // Сеть, а не отказ сервера: показать надо «нет связи», а не «нет прав».
    return { ok: false, trouble: "offline" };
  }

  if (!answer.ok) return { ok: false, trouble: troubleOf(answer.status) };

  try {
    return { ok: true, data: (await answer.json()) as T };
  } catch {
    return { ok: false, trouble: "failed" };
  }
}

/** Ключ словаря под каждую причину. Текст берётся с сервера, как и всё прочее. */
export const TROUBLE_WORD: Record<Trouble, string> = {
  offline: "app.offline",
  denied: "app.denied",
  not_found: "error.not_found",
  closed: "error.section_closed",
  failed: "error.failed",
};
