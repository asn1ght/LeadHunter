"""Экономия запросов к Search API: мягкие правила по «новизне» фраз.

1. Глубина листания: если фраза в прошлый раз не дала новых доменов — запрашиваем только 1-ю
   страницу; в синхронном режиме листание останавливается на странице без новых доменов.
   Но раз в N прогонов (QUERY_FULL_DEPTH_EVERY: 1-й, N+1-й, 2N+1-й…) фраза идёт на полную глубину,
   чтобы не пропустить новое в глубине выдачи.
2. Статистика: найдено / новых за последний прогон и новых за всё время.
3. Автопауза: K прогонов подряд без новых (QUERY_ZERO_RUNS_TO_PAUSE) → пауза на QUERY_PAUSE_DAYS,
   после чего фраза сама возвращается. Работающих фраз не становится меньше QUERY_MIN_ACTIVE.
   Ручное выключение (is_active=False) — отдельно и бессрочно.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.models import SearchQuery


@dataclass
class Policy:
    full_depth_every: int = 4
    zero_runs_to_pause: int = 2
    pause_days: int = 28
    min_active: int = 3


def is_paused(q: SearchQuery, now: datetime) -> bool:
    return q.paused_until is not None and q.paused_until > now


def is_full_depth_run(q: SearchQuery, policy: Policy) -> bool:
    """Раз в N прогонов (1-й, N+1-й, …) — полная глубина."""
    runs = q.runs_count or 0
    return runs == 0 or (policy.full_depth_every > 0 and runs % policy.full_depth_every == 0)


def pages_for(q: SearchQuery, max_pages: int, policy: Policy) -> int:
    """Сколько страниц запрашивать заранее (для отложенного режима)."""
    if is_full_depth_run(q, policy) or q.last_new is None or q.last_new > 0:
        return max_pages
    return 1


def stop_paging(q: SearchQuery, page_new: int, policy: Policy) -> bool:
    """Синхронный режим: прекратить листание после страницы без новых доменов (кроме полных прогонов)."""
    return page_new == 0 and not is_full_depth_run(q, policy)


def record_run(q: SearchQuery, found: int, new: int, now: datetime) -> None:
    """Записать итоги прогона фразы."""
    q.runs_count = (q.runs_count or 0) + 1
    q.results_count = found
    q.last_new = new
    q.total_new = (q.total_new or 0) + new
    q.zero_streak = (q.zero_streak or 0) + 1 if new == 0 else 0
    q.last_run_at = now


def apply_autopause(queries: list[SearchQuery], now: datetime, policy: Policy) -> list[SearchQuery]:
    """Поставить на паузу фразы с серией пустых прогонов. Возвращает поставленные на паузу.

    Не опускает число работающих (активных и не на паузе) ниже policy.min_active:
    в первую очередь паузятся фразы, которые дольше всех без новых и принесли меньше всего за всё время.
    """
    if policy.zero_runs_to_pause <= 0:
        return []
    working = [q for q in queries if q.is_active and not is_paused(q, now)]
    candidates = [q for q in working if (q.zero_streak or 0) >= policy.zero_runs_to_pause]
    room = max(0, len(working) - policy.min_active)
    candidates.sort(key=lambda q: (-(q.zero_streak or 0), q.total_new or 0, q.id or 0))
    paused = candidates[:room]
    for q in paused:
        q.paused_until = now + timedelta(days=policy.pause_days)
        q.zero_streak = 0  # после паузы снова даём K попыток
    return paused


def resume(q: SearchQuery) -> None:
    """Снять автопаузу вручную."""
    q.paused_until = None
    q.zero_streak = 0
