"""Отладочный CLI без Telegram.

`python -m app.cli audit gazsever.com gaz-proekt-spb.ru` — аудит и карточка в консоль
(в БД ничего не пишется).
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from types import SimpleNamespace

from app.audit.extract import extract
from app.audit.fetcher import fetch_site
from app.audit.scoring import Weights, compute_score
from app.audit.tech_checks import run_checks
from app.bot.cards import render_card, render_why
from app.db.repo import load_niches_config
from app.discovery.filters import normalize_domain


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def audit(url: str) -> None:
    cfg = load_niches_config().get("gas", {})
    fetch = await fetch_site(url)
    ex = extract(
        [(fetch.final_url or url, fetch.html), *fetch.extra_pages] if fetch.html else [],
        cfg.get("region_markers", []),
        cfg.get("keywords", []),
    )
    tech = run_checks(fetch, datetime.now())
    score = compute_score(
        [i.to_dict() for i in tech.issues], Weights(), ex.inn, ex.segment, cfg.get("priority_segment")
    )
    company = SimpleNamespace(
        id=0, domain=normalize_domain(url) or url, url=fetch.final_url or url, name=ex.name, inn=ex.inn,
        segment=ex.segment, phones=ex.phones, emails=ex.emails, messengers=ex.messengers, address=ex.address,
        status="NEW", score=score.score, score_tech=score.score_tech, score_quality=None,
        niche=SimpleNamespace(name=cfg.get("name", "")), contact_person=None, contact_phone=None,
        next_action_at=None, notes=None, tags=[],
    )
    audit_ns = SimpleNamespace(
        cms=tech.metrics.get("cms"), issues=[i.to_dict() for i in tech.issues], error=fetch.error,
        llm_summary=None, score_factors=score.factors,
    )
    print("=" * 60)
    print(f"fetch: status={fetch.status} final={fetch.final_url} ttfb={fetch.ttfb_ms}ms "
          f"load={fetch.load_ms}ms ssl_days={fetch.ssl_days_left} extra={[u for u, _ in fetch.extra_pages]} err={fetch.error}")
    print(_plain(render_card(company, audit_ns, 55)))  # type: ignore[arg-type]
    print("-" * 60)
    print(_plain(render_why(company, audit_ns)))  # type: ignore[arg-type]


async def main(args: list[str]) -> None:
    if len(args) < 2 or args[0] != "audit":
        print(__doc__)
        return
    for url in args[1:]:
        await audit(url)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    asyncio.run(main(sys.argv[1:]))
