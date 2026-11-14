"""platform subdomains: домен лида = полный адрес для хостингов/конструкторов

Лиды, записанные до исправления как «tw1.ru», получают домен из сохранённого URL
(«ci697493.tw1.ru»). Список платформ — config/platforms.yaml.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.discovery.filters import load_platforms, normalize_domain

    platforms = load_platforms()
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, domain, url FROM companies")).fetchall()
    taken = {r.domain for r in rows}
    for r in rows:
        if r.domain not in platforms:
            continue
        new = normalize_domain(r.url or "")
        if not new or new == r.domain or new in taken:
            continue
        conn.execute(sa.text("UPDATE companies SET domain = :d WHERE id = :id"), {"d": new, "id": r.id})
        taken.discard(r.domain)
        taken.add(new)


def downgrade() -> None:
    # данные не откатываем: старый вид («tw1.ru») был ошибкой
    pass
