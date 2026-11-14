"""Текст отчёта в формате раздела 9."""
from __future__ import annotations

from app.reports.builder import Period, ReportData


def _pct(num: int, den: int) -> str:
    return f"{round(100 * num / den)}%" if den else "—"


def _delta(cur: int, prev: int) -> str:
    d = cur - prev
    return f"▲{d}" if d > 0 else f"▼{-d}" if d < 0 else "="


def format_report(period: Period, data: ReportData, prev: ReportData | None = None) -> str:
    """Отчёт; при `prev` к каждой строке добавляется сравнение с прошлым периодом (▲/▼)."""
    cur = data.as_dict()
    old = prev.as_dict() if prev else None

    def line(title: str, *keys: str) -> str:
        values = " / ".join(str(cur[k]) for k in keys)
        if old is None:
            return f"- {title} — {values}"
        deltas = " / ".join(_delta(cur[k], old[k]) for k in keys)
        return f"- {title} — {values} ({deltas})"

    lines = [
        f"📊 Отчёт {period.title()}",
        "",
        "📍Общий",
        line("Найдено новых лидов", "found_new"),
        line("Отправлено рассылок", "sent"),
        line("Получено ответов в чате", "replied"),
        line("Попросили созвон позже", "callback"),
        "",
        "📍Звонок №1 (знакомство)",
        line("Звонков совершено", "call1_done"),
        line("Согласны на тест", "test_agreed"),
        line("Отказ / Игнор", "call1_refused", "call1_ignored"),
        "",
        "📍Звонок №2 (после теста)",
        line("Назначено звонков", "call2_scheduled"),
        line("Звонков совершено", "call2_done"),
        line("Отказ / Игнор / Перенос", "call2_refused", "call2_ignored", "call2_rescheduled"),
        "",
        f"📍Результат {'дня' if period.kind == 'day' else 'недели' if period.kind == 'week' else 'месяца'}",
        line("Готовы сотрудничать", "agreed"),
        line("Думают / Перезвон", "thinking", "callback"),
        line("Отказ / Игнор", "refused", "ignored"),
        "",
        "📈 Конверсия: "
        f"рассылка → ответ {_pct(data.replied, data.sent)} · "
        f"ответ → звонок {_pct(data.call1_done, data.replied)} · "
        f"звонок → сделка {_pct(data.agreed, data.call1_done)}",
    ]
    if prev is not None:
        lines.append(f"<i>В скобках — сравнение с периодом {period.prev().title().removeprefix('за ')}</i>")
    return "\n".join(lines)
