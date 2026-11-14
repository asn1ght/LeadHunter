"""Клавиатуры и callback-данные."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.crm.statuses import CALL1_OUTCOMES, CALL2_OUTCOMES, EMOJI, RECOMMENDED, Status, label
from app.db.models import Company

if TYPE_CHECKING:
    from app.reports.builder import Period

# ---------------------------------------------------------------- главное меню

BTN_NEW = "🔥 Новые"
BTN_TODAY = "📞 Сегодня"
BTN_LEADS = "📋 Лиды"
BTN_ADD = "➕ Добавить"
BTN_REPORT = "📊 Отчёт"
BTN_SETTINGS = "⚙️ Настройки"
MENU_BUTTONS = (BTN_NEW, BTN_TODAY, BTN_LEADS, BTN_ADD, BTN_REPORT, BTN_SETTINGS)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_LEADS)],
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------------------------------------------------------------- callback data


class CardCb(CallbackData, prefix="c"):
    """Действие на карточке лида."""

    action: str
    cid: int
    arg: str = ""


class ListCb(CallbackData, prefix="l"):
    """Список лидов: фильтр по статусу и страница."""

    status: str = ""
    page: int = 0
    filters: int = 0  # 1 — показать выбор фильтра


class RemCb(CallbackData, prefix="r"):
    """Кнопки под сработавшим напоминанием."""

    action: str  # done | h1 | tmrw
    rid: int


class DateCb(CallbackData, prefix="d"):
    """Быстрый выбор даты в сценарии напоминания."""

    choice: str  # t15 | 2h | tm10 | d3 | w1 | manual


class FlowCb(CallbackData, prefix="f"):
    action: str  # skip | cancel


class NavCb(CallbackData, prefix="n"):
    action: str  # menu


# ---------------------------------------------------------------- карточка


def card_keyboard(company: Company, extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    """Рекомендованные кнопки по статусу + постоянный ряд действий."""
    kb = InlineKeyboardBuilder()
    for row in extra_rows or []:
        kb.row(*row)
    rec = RECOMMENDED.get(Status(company.status), [])
    buttons = [InlineKeyboardButton(text=t, callback_data=CardCb(action=a, cid=company.id).pack()) for a, t in rec]
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i : i + 2])
    kb.row(
        InlineKeyboardButton(text="🧠 Глубокий анализ", callback_data=CardCb(action="deep", cid=company.id).pack()),
        InlineKeyboardButton(text="✍️ Письмо", callback_data=CardCb(action="letter", cid=company.id).pack()),
    )
    kb.row(
        InlineKeyboardButton(text="📄 PDF-аудит", callback_data=CardCb(action="pdf", cid=company.id).pack()),
        InlineKeyboardButton(text="🖼 Скрины", callback_data=CardCb(action="shots", cid=company.id).pack()),
    )
    kb.row(
        InlineKeyboardButton(text="📝 Заметка", callback_data=CardCb(action="note", cid=company.id).pack()),
        InlineKeyboardButton(text="🔄 Статус", callback_data=CardCb(action="stmenu", cid=company.id).pack()),
    )
    kb.row(
        InlineKeyboardButton(text="🔗 Сайт", url=company.url if company.url.startswith("http") else f"http://{company.domain}"),
        InlineKeyboardButton(text="❓ Почему оценка", callback_data=CardCb(action="why", cid=company.id).pack()),
    )
    return kb.as_markup()


def back_to_card(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=CardCb(action="back", cid=cid))
    return kb.as_markup()


def status_menu(company: Company) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for st in Status:
        mark = "• " if st.value == company.status else ""
        kb.button(text=mark + label(st), callback_data=CardCb(action="set", cid=company.id, arg=st.value))
    kb.button(text="◀️ Назад", callback_data=CardCb(action="back", cid=company.id))
    kb.adjust(2)
    return kb.as_markup()


def call_outcomes(company: Company, stage: int) -> InlineKeyboardMarkup:
    outcomes = CALL1_OUTCOMES if stage == 1 else CALL2_OUTCOMES
    kb = InlineKeyboardBuilder()
    for code, (text, _) in outcomes.items():
        kb.button(text=text, callback_data=CardCb(action=f"out{stage}", cid=company.id, arg=code))
    kb.button(text="◀️ Назад", callback_data=CardCb(action="back", cid=company.id))
    kb.adjust(2)
    return kb.as_markup()


def reminder_row(rid: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="✅ Сделано", callback_data=RemCb(action="done", rid=rid).pack()),
        InlineKeyboardButton(text="⏰ +1 ч", callback_data=RemCb(action="h1", rid=rid).pack()),
        InlineKeyboardButton(text="⏰ Завтра", callback_data=RemCb(action="tmrw", rid=rid).pack()),
    ]


# ---------------------------------------------------------------- сценарии


def date_choices(now_local: datetime) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if now_local.hour < 14:
        kb.button(text="Сегодня 15:00", callback_data=DateCb(choice="t15"))
    else:
        kb.button(text="Через 2 часа", callback_data=DateCb(choice="2h"))
    kb.button(text="Завтра 10:00", callback_data=DateCb(choice="tm10"))
    kb.button(text="Через 3 дня", callback_data=DateCb(choice="d3"))
    kb.button(text="Через неделю", callback_data=DateCb(choice="w1"))
    kb.button(text="✍️ Указать вручную", callback_data=DateCb(choice="manual"))
    kb.button(text="✖️ Отмена", callback_data=FlowCb(action="cancel"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def skip_cancel(skip: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if skip:
        kb.button(text="⏭ Пропустить", callback_data=FlowCb(action="skip"))
    kb.button(text="✖️ Отмена", callback_data=FlowCb(action="cancel"))
    return kb.as_markup()


# ---------------------------------------------------------------- списки


def lead_button(c: Company) -> InlineKeyboardButton:
    score = f"{c.score}" if c.score is not None else "—"
    name = (c.name or c.domain)[:28]
    emoji = EMOJI.get(Status(c.status), "")
    return InlineKeyboardButton(text=f"{score} · {name} · {emoji}", callback_data=CardCb(action="open", cid=c.id).pack())


def leads_list(companies: list[Company], status: str, page: int, total: int, per_page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in companies:
        kb.row(lead_button(c))
    pages = max(1, (total + per_page - 1) // per_page)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=ListCb(status=status, page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=ListCb(status=status, page=page).pack()))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=ListCb(status=status, page=page + 1).pack()))
    kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="🔎 Фильтр", callback_data=ListCb(status=status, filters=1).pack()),
        InlineKeyboardButton(text="🏠 Меню", callback_data=NavCb(action="menu").pack()),
    )
    return kb.as_markup()


def status_filter(counts: dict[str, int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Все ({sum(counts.values())})", callback_data=ListCb(status=""))
    for st in Status:
        kb.button(text=f"{label(st)} ({counts.get(st.value, 0)})", callback_data=ListCb(status=st.value))
    kb.adjust(1, 2)
    return kb.as_markup()


# ---------------------------------------------------------------- письма


class DraftCb(CallbackData, prefix="dr"):
    """Черновик письма: action = again | short | copy | sent; eid — событие ai_draft."""

    action: str
    eid: int


def letter_channels(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📧 Email", callback_data=CardCb(action="lt", cid=cid, arg="email"))
    kb.button(text="💬 Мессенджер", callback_data=CardCb(action="lt", cid=cid, arg="messenger"))
    kb.button(text="◀️ Назад", callback_data=CardCb(action="back", cid=cid))
    kb.adjust(2, 1)
    return kb.as_markup()


def draft_keyboard(eid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё вариант", callback_data=DraftCb(action="again", eid=eid))
    kb.button(text="✂️ Короче", callback_data=DraftCb(action="short", eid=eid))
    kb.button(text="📋 Текст для копирования", callback_data=DraftCb(action="copy", eid=eid))
    kb.button(text="✉️ Отметить отправленным", callback_data=DraftCb(action="sent", eid=eid))
    kb.adjust(2, 1, 1)
    return kb.as_markup()


# ---------------------------------------------------------------- поиск: запросы и ниши


class QCb(CallbackData, prefix="q"):
    """/queries: action = toggle | del | add | page; qid; page."""

    action: str
    qid: int = 0
    page: int = 0


class NicheCb(CallbackData, prefix="ni"):
    action: str  # use | new
    nid: int = 0


class RunCb(CallbackData, prefix="run"):
    action: str  # go


# ---------------------------------------------------------------- отчёты


class RepCb(CallbackData, prefix="rp"):
    """Отчёт: kind = day | week | month | pick (выбрать день); d = YYYYMMDD."""

    kind: str
    d: str = ""


def report_keyboard(period: Period, today: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    prev, nxt = period.prev(), period.next()
    nav = [InlineKeyboardButton(text="◀️", callback_data=RepCb(kind=prev.kind, d=f"{prev.start:%Y%m%d}").pack())]
    if nxt.start <= today:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=RepCb(kind=nxt.kind, d=f"{nxt.start:%Y%m%d}").pack()))
    kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="📅 Другой день", callback_data=RepCb(kind="pick").pack()),
        InlineKeyboardButton(text="📆 Неделя", callback_data=RepCb(kind="week", d=f"{today:%Y%m%d}").pack()),
        InlineKeyboardButton(text="🗓 Месяц", callback_data=RepCb(kind="month", d=f"{today:%Y%m%d}").pack()),
    )
    return kb.as_markup()


def report_day_choices(today: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, back in (("Сегодня", 0), ("Вчера", 1), ("Позавчера", 2)):
        kb.button(text=title, callback_data=RepCb(kind="day", d=f"{today - timedelta(days=back):%Y%m%d}"))
    kb.button(text="✖️ Отмена", callback_data=FlowCb(action="cancel"))
    kb.adjust(3, 1)
    return kb.as_markup()


def open_buttons(companies: list[Company]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in companies:
        kb.row(lead_button(c))
    kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=NavCb(action="menu").pack()))
    return kb.as_markup()
