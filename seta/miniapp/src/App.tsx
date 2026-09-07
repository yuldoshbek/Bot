/**
 * Состав экранов собирается по ролям и переключателям, а не по вкусу.
 *
 * `/me` отдаёт роли и включённые разделы — тот же список, по которому бот
 * собирает нижнее меню. Поэтому руководитель видит «Мой день», сотрудник — нет,
 * а выключенный администратором раздел встреч исчезает у всех. Спрятать вкладку,
 * оставив экран доступным, было бы маскировкой: данные всё равно берутся
 * из API, и он откажет сам — но человек увидит отказ вместо отсутствия.
 *
 * Первый запрос — `/me`. Он же проверка входа: не прошла подпись — дальше
 * идти незачем, и остальные экраны рисовать не из чего.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { configured, get } from "./api";
import type { Result } from "./api";
import type { Me } from "./types";
import { Day } from "./screens/Day";
import { Decisions } from "./screens/Decisions";
import { Meetings } from "./screens/Meetings";
import { Tasks } from "./screens/Tasks";
import { Trouble } from "./screens/parts";
import { insideTelegram } from "./telegram";

type TabName = "day" | "tasks" | "meetings" | "decisions";

interface Tab {
  name: TabName;
  word: string;
}

/** Кому какие вкладки. Порядок повторяет нижнее меню бота. */
function tabsFor(me: Me): Tab[] {
  const boss = me.roles.includes("EXECUTIVE") || me.roles.includes("ASSISTANT");
  const meetingsOn = me.features.meetings !== false;
  const tabs: Tab[] = [];
  // «Мой день» — экран руководителя и ассистента, как и кнопка в боте.
  if (boss) tabs.push({ name: "day", word: "menu.my_day" });
  tabs.push({ name: "tasks", word: "menu.my_tasks" });
  if (meetingsOn) tabs.push({ name: "meetings", word: "menu.my_meetings" });
  tabs.push({ name: "decisions", word: "menu.decisions" });
  return tabs;
}

export function App() {
  const [me, setMe] = useState<Result<Me> | null>(null);
  const [tab, setTab] = useState<TabName | null>(null);

  const load = useCallback(() => {
    setMe(null);
    void get<Me>("/api/v1/me").then(setMe);
  }, []);

  useEffect(load, [load]);

  const words = me?.ok ? me.data.words : null;
  // Ключа нет в присланном словаре — показываем сам ключ. Так же ведёт себя
  // `t` на сервере: видно, чего не хватает, вместо пустоты на экране.
  const w = useMemo(
    () => (key: string) => words?.[key] ?? key,
    [words],
  );

  // Адрес API не задан при сборке. Отдельное состояние: иначе это выглядело бы
  // как «сервер молчит», и чинили бы сервер.
  if (!configured()) {
    return <div className="state">VITE_API_URL</div>;
  }

  // Открыто не из Telegram: подписанной строки нет, и API не ответит никогда.
  // Показывать «нет связи» здесь — врать.
  if (!insideTelegram()) {
    return <div className="state">{w("app.denied")}</div>;
  }

  if (me === null) return <div className="state">{w("app.loading")}</div>;
  if (!me.ok) return <Trouble trouble={me.trouble} w={w} again={load} />;

  const tabs = tabsFor(me.data);
  const current = tab ?? tabs[0].name;

  return (
    <>
      {current === "day" ? <Day w={w} title={w("menu.my_day")} /> : null}
      {current === "tasks" ? <Tasks w={w} buckets={me.data.buckets} /> : null}
      {current === "meetings" ? <Meetings w={w} title={w("menu.my_meetings")} /> : null}
      {current === "decisions" ? <Decisions w={w} title={w("menu.decisions")} /> : null}

      <nav className="tabs">
        {tabs.map((item) => (
          <button
            key={item.name}
            className="tab"
            aria-current={item.name === current}
            onClick={() => setTab(item.name)}
          >
            {w(item.word)}
          </button>
        ))}
      </nav>
    </>
  );
}
