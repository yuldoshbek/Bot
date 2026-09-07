/**
 * Обёртка над тем, что даёт Telegram.
 *
 * Собственного описания здесь ровно столько, сколько нужно: подписанная строка,
 * цвета темы и кнопка «назад». Библиотеку под это ставить незачем — она
 * добавила бы зависимость ради трёх полей и своего цикла выпуска.
 *
 * Приложение открывают и вне Telegram — из браузера, по ошибке или из
 * любопытства. Тогда подписанной строки нет, и это не сбой: экран честно
 * говорит, что открывать его надо из чата.
 */

type Theme = Record<string, string>;

interface WebApp {
  initData: string;
  themeParams: Theme;
  colorScheme: "light" | "dark";
  ready: () => void;
  expand: () => void;
  BackButton: {
    show: () => void;
    hide: () => void;
    onClick: (handler: () => void) => void;
    offClick: (handler: () => void) => void;
  };
}

function app(): WebApp | null {
  return (window as unknown as { Telegram?: { WebApp?: WebApp } }).Telegram?.WebApp ?? null;
}

/** Подписанная строка. Пусто — приложение открыто не из Telegram. */
export function initData(): string {
  return app()?.initData ?? "";
}

export function insideTelegram(): boolean {
  return initData().length > 0;
}

/**
 * Красит страницу в тему собеседника.
 *
 * Своя палитра выглядела бы чужой ровно у половины: половина людей сидит
 * в тёмной теме. Цвета приходят переменными, поэтому подставляются в CSS
 * как есть, а на случай их отсутствия в стилях стоят запасные значения.
 */
export function applyTheme(): void {
  const instance = app();
  if (!instance) return;
  const root = document.documentElement;
  for (const [name, value] of Object.entries(instance.themeParams ?? {})) {
    root.style.setProperty(`--tg-${name.replace(/_/g, "-")}`, value);
  }
  root.dataset.scheme = instance.colorScheme ?? "light";
}

export function ready(): void {
  const instance = app();
  instance?.ready();
  instance?.expand();
}

/**
 * Кнопка «назад» — та, что рисует Telegram, а не своя внутри страницы.
 * Своя означала бы две кнопки возврата на одном экране, ведущие по-разному.
 */
export function backButton(handler: (() => void) | null): void {
  const instance = app();
  if (!instance) return;
  const back = instance.BackButton;
  if (handler) {
    back.onClick(handler);
    back.show();
    return;
  }
  back.hide();
}
