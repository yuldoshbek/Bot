"""Страница состояния системы.

Отдаётся по тому же адресу /health, что и данные для монитора: браузер просит
HTML и получает страницу, монитор просит JSON и получает JSON. Один адрес,
который можно и открыть глазами, и подключить к внешней проверке.

Страница рассчитана на человека, который не читает логи: сверху одно слово
о состоянии в целом, ниже — что именно сломалось и что происходило.
"""
from datetime import datetime
from html import escape

from app.core.timeutil import to_local
from app.services.health import Status

_STYLE = """
:root{--bg:#f3f6f5;--card:#fff;--ink:#0f1f1e;--dim:#6c807e;--line:#d4dedc;
--ok:#26714b;--bad:#b23731;--warn:#9c7413;--accent:#0e6b6b}
@media(prefers-color-scheme:dark){:root{--bg:#0b1110;--card:#121a19;--ink:#e6edeb;
--dim:#7e918e;--line:#25322f;--ok:#5cba88;--bad:#e87a72;--warn:#dcb253;--accent:#43b5af}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--dim);font-size:14px;margin:0 0 20px}
.banner{border-radius:8px;padding:16px 18px;margin:0 0 22px;font-weight:600;font-size:18px}
.banner.ok{background:color-mix(in srgb,var(--ok) 12%,var(--card));color:var(--ok);
border:1px solid color-mix(in srgb,var(--ok) 35%,transparent)}
.banner.bad{background:color-mix(in srgb,var(--bad) 12%,var(--card));color:var(--bad);
border:1px solid color-mix(in srgb,var(--bad) 35%,transparent)}
.banner ul{margin:8px 0 0;padding-left:20px;font-weight:400;font-size:15px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
margin:26px 0 10px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .n{font-size:26px;font-weight:700;line-height:1.1}
.card .l{color:var(--dim);font-size:13px;margin-top:4px}
.card.bad .n{color:var(--bad)}
.card.warn .n{color:var(--warn)}
.row{display:flex;justify-content:space-between;gap:12px;padding:11px 16px;
background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:8px;font-size:15px}
.row .s{font-weight:600}
.row .s.ok{color:var(--ok)}
.row .s.bad{color:var(--bad)}
.err{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--bad);
border-radius:0 8px 8px 0;padding:12px 16px;margin-bottom:8px}
.err .h{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--dim)}
.err .k{font-weight:600;color:var(--bad)}
.err .m{margin-top:5px;font-family:ui-monospace,Consolas,monospace;font-size:13px;
word-break:break-word}
.err .c{margin-top:5px;font-size:13px;color:var(--dim)}
.empty{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:18px;color:var(--dim);text-align:center}
footer{margin-top:30px;color:var(--dim);font-size:13px}
code{background:color-mix(in srgb,var(--accent) 10%,transparent);padding:1px 5px;border-radius:3px}
"""


def _fmt(moment: datetime) -> str:
    return to_local(moment).strftime("%d.%m %H:%M:%S")


def render(status: Status, generated_at: datetime) -> str:
    """Собирает страницу состояния. Обновляется сама раз в 30 секунд."""
    banner_class = "ok" if status.healthy else "bad"
    banner_text = "Система работает" if status.healthy else "Есть проблемы"
    problems = ""
    if status.problems:
        items = "".join(f"<li>{escape(p)}</li>" for p in status.problems)
        problems = f"<ul>{items}</ul>"

    services = ""
    for info in status.services.values():
        mark = "работает" if info["ok"] else "молчит"
        services += (
            f'<div class="row"><span>{escape(info["title"])}</span>'
            f'<span class="s {"ok" if info["ok"] else "bad"}">{mark} · '
            f'{escape(info["text"])}</span></div>'
        )
    for name, info in status.checks.items():
        title = {"database": "База данных", "redis": "Redis"}.get(name, name)
        services += (
            f'<div class="row"><span>{title}</span>'
            f'<span class="s {"ok" if info["ok"] else "bad"}">{escape(info["text"])}</span></div>'
        )

    numbers = status.numbers
    cards = [
        ("people", "Сотрудников", ""),
        ("pending_people", "Заявок ждёт", "warn" if numbers.get("pending_people") else ""),
        ("tasks_active", "Поручений в работе", ""),
        ("tasks_overdue", "Просрочено", "bad" if numbers.get("tasks_overdue") else ""),
        ("queue_pending", "Уведомлений в очереди", ""),
        ("queue_lag_seconds", "Отставание очереди, с",
         "bad" if numbers.get("queue_lag_seconds", 0) > 120 else ""),
        ("queue_failed", "Не доставлено", "bad" if numbers.get("queue_failed") else ""),
        ("errors_day", "Ошибок за сутки", "bad" if numbers.get("errors_day") else ""),
    ]
    grid = "".join(
        f'<div class="card {css}"><div class="n">{numbers.get(key, 0)}</div>'
        f'<div class="l">{label}</div></div>'
        for key, label, css in cards
    )

    if status.errors:
        errors = ""
        for item in status.errors:
            context = (
                f'<div class="c">Действие: {escape(str(item["context"]))}</div>'
                if item.get("context") else ""
            )
            errors += (
                '<div class="err">'
                f'<div class="h"><span class="k">{escape(item["kind"])}</span>'
                f'<span>{_fmt(item["occurred_at"])} · {escape(item["source"])}</span></div>'
                f'<div class="m">{escape(item["message"][:400])}</div>'
                f"{context}</div>"
            )
    else:
        errors = '<div class="empty">Ошибок нет — за последнее время ничего не падало.</div>'

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>SETA — состояние</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>Состояние системы</h1>
<p class="sub">Обновлено {_fmt(generated_at)} · страница обновляется сама каждые 30 секунд</p>

<div class="banner {banner_class}">{banner_text}{problems}</div>

<h2>Службы</h2>
{services}

<h2>Показатели</h2>
<div class="grid">{grid}</div>

<h2>Последние ошибки</h2>
{errors}

<footer>
Данные для мониторинга: тот же адрес <code>/health</code> с заголовком
<code>Accept: application/json</code>. При недоступной службе или вставшей
очереди адрес отвечает кодом 503 — внешняя проверка увидит сбой сама.
</footer>
</div></body></html>"""
