"""ORM-модели (SQLAlchemy 2.x). Время хранится в UTC.

Типы выбраны переносимые (String, JSON, DateTime), чтобы переезд на Postgres
не требовал переписывания моделей.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время в UTC (aware)."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime, который всегда сохраняет UTC и возвращает aware-datetime в UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime is not allowed, use UTC-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Niche(Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    region_markers: Mapped[list[str]] = mapped_column(JSON, default=list)
    priority_segment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    results_count: Mapped[int] = mapped_column(Integer, default=0)  # доменов в выдаче за последний прогон
    # статистика «новизны» (app.discovery.query_policy)
    runs_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_new: Mapped[int | None] = mapped_column(Integer, nullable=True)  # новых компаний за последний прогон
    total_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    zero_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # прогонов подряд без новых
    paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # автопауза


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id"), nullable=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    url: Mapped[str] = mapped_column(String(1024))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    checko: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # сводка Чекко (реквизиты, финансы)
    checko_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    segment: Mapped[str] = mapped_column(String(16), default="unknown")

    phones: Mapped[list[str]] = mapped_column(JSON, default=list)
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    messengers: Mapped[list[str]] = mapped_column(JSON, default=list)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    region_detected: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(16), default="manual")
    source_query: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serp_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    score_tech: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)

    next_action_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    last_audit_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    niche: Mapped[Niche | None] = relationship(lazy="joined")


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    https: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ssl_days_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    load_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    html_kb: Mapped[float | None] = mapped_column(Float, nullable=True)
    scripts_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    images_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    images_no_alt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    h1: Mapped[list[str]] = mapped_column(JSON, default=list)
    robots_meta: Mapped[str | None] = mapped_column(String(255), nullable=True)
    viewport: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_metrika: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_gtag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cms: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jquery_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    copyright_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oldest_upload_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tel_links: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mail_links: Mapped[int | None] = mapped_column(Integer, nullable=True)
    messengers: Mapped[list[str]] = mapped_column(JSON, default=list)
    forms_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    anchor_only_ctas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mobile_overflow: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    score_factors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    llm_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    screenshot_desktop: Mapped[str | None] = mapped_column(String(512), nullable=True)
    screenshot_mobile: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    text: Mapped[str] = mapped_column(String(1024), default="")
    kind: Mapped[str] = mapped_column(String(16), default="callback")  # callback | call2 | remind
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pre_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(lazy="joined")


class SettingKV(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    purpose: Mapped[str] = mapped_column(String(32))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[float] = mapped_column(Float, default=0)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    api: Mapped[str] = mapped_column(String(32))  # yandex_v1 | yandex_v2
    requests: Mapped[int] = mapped_column(Integer, default=1)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)


class SearchOperation(Base):
    """Отложенный запрос Yandex Search API v2 (результат готов через 5 мин – несколько часов)."""

    __tablename__ = "search_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    op_id: Mapped[str] = mapped_column(String(64), unique=True)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("search_queries.id", ondelete="SET NULL"), nullable=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id", ondelete="SET NULL"), nullable=True)
    query_text: Mapped[str] = mapped_column(String(255))
    page: Mapped[int] = mapped_column(Integer, default=0)
    run_tag: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # один прогон = один тег
    found_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    new_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|done|failed
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BlacklistDomain(Base):
    __tablename__ = "blacklist_domains"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
