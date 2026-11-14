"""/queries, /niche, /run, /usage — управление поиском (этап 3)."""
from __future__ import annotations

import asyncio
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.ai.client import month_spent, month_start
from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import FlowCb, NicheCb, NavCb, QCb, RunCb, skip_cancel
from app.bot.states import NicheFlow, QueryFlow
from app.config import get_settings
from app.db import repo
from app.db.models import LLMUsage, Niche, SearchOperation, SearchQuery, utcnow
from app.db.session import SessionLocal
from app.discovery.jobs import discovery_job, morning_push
from app.discovery.runner import (
    estimate_cost,
    is_deferred_window,
    is_night,
    month_requests,
    planned_requests,
)

router = Router(name="discovery")
PER_PAGE = 10
_run_lock = asyncio.Lock()

DEFAULT_REGION_MARKERS = ["санкт-петербург", "спб", "петербург", "ленинградская", "ленобласть", "812", "813"]


# ---------------------------------------------------------------- /queries


async def queries_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        niche = await repo.get_active_niche(session)
        if not niche:
            return "Нет активной ниши. Создайте её в /niche.", InlineKeyboardMarkup(inline_keyboard=[])
        qs = await repo.niche_queries(session, niche.id)
    active = sum(q.is_active for q in qs)
    pages = max(1, (len(qs) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages - 1)
    chunk = qs[page * PER_PAGE:(page + 1) * PER_PAGE]
    kb = InlineKeyboardBuilder()
    for q in chunk:
        mark = "✅" if q.is_active else "⛔"
        res = f" · {q.results_count}" if q.last_run_at else ""
        kb.row(
            InlineKeyboardButton(text=f"{mark} {q.text[:40]}{res}", callback_data=QCb(action="toggle", qid=q.id, page=page).pack()),
            InlineKeyboardButton(text="🗑", callback_data=QCb(action="del", qid=q.id, page=page).pack()),
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=QCb(action="page", page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=QCb(action="page", page=page).pack()))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=QCb(action="page", page=page + 1).pack()))
    kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data=QCb(action="add").pack()),
        InlineKeyboardButton(text="🏠 Меню", callback_data=NavCb(action="menu").pack()),
    )
    text = (
        f"🔎 <b>Запросы ниши «{escape(niche.name)}»</b>: активных {active} из {len(qs)}\n"
        "Нажмите на запрос, чтобы включить/выключить. Цифра — результатов в последнем прогоне."
    )
    return text, kb.as_markup()


@router.message(Command("queries"), StateFilter("*"))
async def cmd_queries(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await queries_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(QCb.filter(F.action.in_({"toggle", "del", "page"})), StateFilter("*"))
async def queries_cb(cb: CallbackQuery, callback_data: QCb) -> None:
    if callback_data.action != "page":
        async with SessionLocal() as session:
            q = await session.get(SearchQuery, callback_data.qid)
            if q:
                if callback_data.action == "toggle":
                    q.is_active = not q.is_active
                else:
                    await session.delete(q)
                await session.commit()
    text, kb = await queries_view(callback_data.page)
    if cb.message:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await cb.answer()


@router.callback_query(QCb.filter(F.action == "add"), StateFilter("*"))
async def queries_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(QueryFlow.waiting_text)
    if cb.message:
        await cb.message.answer("➕ Пришлите поисковые запросы — по одному в строке.", reply_markup=skip_cancel(skip=False))
    await cb.answer()


@router.message(QueryFlow.waiting_text, USER_TEXT)
async def queries_add_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    lines = [re.sub(r"\s+", " ", ln).strip()[:255] for ln in (message.text or "").splitlines()]
    lines = [ln for ln in lines if len(ln) >= 3]
    async with SessionLocal() as session:
        niche = await repo.get_active_niche(session)
        if not niche:
            await message.answer("Нет активной ниши.")
            return
        existing = {q.text.lower() for q in await repo.niche_queries(session, niche.id)}
        added = 0
        for ln in lines:
            if ln.lower() not in existing:
                session.add(SearchQuery(niche_id=niche.id, text=ln))
                existing.add(ln.lower())
                added += 1
        await session.commit()
    await message.answer(f"✅ Добавлено запросов: {added}")
    text, kb = await queries_view()
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------- /niche


def slugify(name: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y", "э": "e", "ю": "yu",
        "я": "ya", "ъ": "", "ь": "",
    })
    s = re.sub(r"[^a-z0-9]+", "_", name.lower().translate(table)).strip("_")
    return s[:40] or "niche"


async def niches_view() -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        niches = await repo.list_niches(session)
        active = await repo.get_active_niche(session)
        counts = dict((await session.execute(select(SearchQuery.niche_id, func.count(SearchQuery.id)).group_by(SearchQuery.niche_id))).all())
    kb = InlineKeyboardBuilder()
    for n in niches:
        mark = "✔️ " if active and n.id == active.id else ""
        kb.button(text=f"{mark}{n.name} ({counts.get(n.id, 0)} запр.)", callback_data=NicheCb(action="use", nid=n.id))
    kb.button(text="➕ Новая ниша", callback_data=NicheCb(action="new"))
    kb.adjust(1)
    name = escape(active.name) if active else "—"
    return f"🎯 <b>Ниши</b>. Активная: {name}\nПоиск, /add и /queries работают с активной нишей.", kb.as_markup()


@router.message(Command("niche"), StateFilter("*"))
async def cmd_niche(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await niches_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(NicheCb.filter(F.action == "use"), StateFilter("*"))
async def niche_use(cb: CallbackQuery, callback_data: NicheCb) -> None:
    async with SessionLocal() as session:
        n = await session.get(Niche, callback_data.nid)
        if n:
            await repo.set_setting(session, "ACTIVE_NICHE", n.slug)
            await session.commit()
    text, kb = await niches_view()
    if cb.message:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await cb.answer(f"Активная ниша: {n.name}" if n else "Не найдено")


@router.callback_query(NicheCb.filter(F.action == "new"), StateFilter("*"))
async def niche_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NicheFlow.waiting_name)
    if cb.message:
        await cb.message.answer("🎯 Название новой ниши (например, «Металлообработка»):", reply_markup=skip_cancel(skip=False))
    await cb.answer()


@router.message(NicheFlow.waiting_name, USER_TEXT)
async def niche_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()[:100]
    if len(name) < 2:
        await message.answer("Слишком коротко. Ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(NicheFlow.waiting_keywords)
    await message.answer(
        "🔑 Ключевые слова ниши через запятую — корни слов, по которым сайт считается «нашим»\n"
        "(например: <code>металлообработ, фрезер, токарн, лазерная резка</code>):",
        reply_markup=skip_cancel(skip=False),
    )


@router.message(NicheFlow.waiting_keywords, USER_TEXT)
async def niche_keywords(message: Message, state: FSMContext) -> None:
    kws = [k.strip().lower() for k in re.split(r"[,;\n]", message.text or "") if len(k.strip()) >= 2]
    if not kws:
        await message.answer("Нужно хотя бы одно слово. Ещё раз:")
        return
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as session:
        slug = base = slugify(data["name"])
        i = 2
        while await repo.get_niche_by_slug(session, slug):
            slug, i = f"{base}_{i}", i + 1
        session.add(Niche(slug=slug, name=data["name"], keywords=kws, region_markers=DEFAULT_REGION_MARKERS))
        await repo.set_setting(session, "ACTIVE_NICHE", slug)
        await session.commit()
    await message.answer(f"✅ Ниша «{escape(data['name'])}» создана и сделана активной. Добавьте запросы в /queries.")


# ---------------------------------------------------------------- /run


@router.message(Command("run"), StateFilter("*"))
async def cmd_run(message: Message, state: FSMContext) -> None:
    await state.clear()
    s = get_settings()
    now_local = utcnow().astimezone(s.tz)
    async with SessionLocal() as session:
        niche = await repo.get_active_niche(session)
        if not niche:
            await message.answer("Нет активной ниши. Создайте её в /niche.")
            return
        mode = await repo.get_runtime_setting(session, "YANDEX_SEARCH_MODE")
        planned = await planned_requests(session, niche.id)
        used = await month_requests(session)
        limit = await repo.get_runtime_setting(session, "SEARCH_MONTHLY_LIMIT")
        allow_paid = await repo.get_runtime_setting(session, "ALLOW_PAID_SEARCH")
    if mode == "deferred_v2" and not is_deferred_window(now_local):
        await message.answer("⏳ Режим отложенных запросов: Яндекс принимает их только 00:00–06:00 МСК. "
                             "Ночной прогон запустится сам по расписанию.")
        return
    night = is_night(now_local)
    cost = estimate_cost(planned, mode, night)
    lines = [
        f"🚀 <b>Поиск по нише «{escape(niche.name)}»</b>",
        f"Режим: {mode}, запросов до {planned} (фразы × страницы; кончившаяся выдача не запрашивается)",
        f"Использовано в этом месяце: {used} из {limit}" + ("" if not allow_paid else " (платные разрешены)"),
        f"Оценка стоимости: до {cost:.0f} ₽ по {'ночному' if night else 'дневному'} тарифу",
    ]
    if not night and mode != "deferred_v2":
        lines.append("⚠️ Сейчас вне ночного окна (00:00–07:59 МСК) — запросы по дневному тарифу.")
    if used >= limit and not allow_paid:
        lines.append("⛔ Месячный лимит исчерпан. Поднимите SEARCH_MONTHLY_LIMIT или включите ALLOW_PAID_SEARCH.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=RunCb(action="go"))
    kb.button(text="✖️ Отмена", callback_data=FlowCb(action="cancel"))
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(RunCb.filter(F.action == "go"), StateFilter("*"))
async def run_go(cb: CallbackQuery, bot: Bot) -> None:
    if _run_lock.locked():
        await cb.answer("Поиск уже идёт", show_alert=True)
        return
    await cb.answer("🚀 Запускаю")
    assert cb.message is not None
    await cb.message.edit_text("⏳ Ищу компании… Это может занять несколько минут, пришлю итог.")
    chat_id = cb.message.chat.id

    async def job() -> None:
        async with _run_lock:
            stats = await discovery_job(bot, force=True)
            await bot.send_message(chat_id, "✅ Поиск завершён\n" + stats.summary())
            sent = await morning_push(bot)
            if not sent and stats.new_companies:
                await bot.send_message(chat_id, "Лидов выше порога нет. Все найденные — в /leads.")

    asyncio.create_task(job())


# ---------------------------------------------------------------- /usage


@router.message(Command("usage"), StateFilter("*"))
async def cmd_usage(message: Message, state: FSMContext) -> None:
    await state.clear()
    s = get_settings()
    async with SessionLocal() as session:
        used = await month_requests(session)
        limit = await repo.get_runtime_setting(session, "SEARCH_MONTHLY_LIMIT")
        mode = await repo.get_runtime_setting(session, "YANDEX_SEARCH_MODE")
        pending = (await session.execute(select(func.count(SearchOperation.id)).where(SearchOperation.status == "pending"))).scalar_one()
        spent = await month_spent(session)
        budget = await repo.get_runtime_setting(session, "LLM_MONTHLY_BUDGET_RUB")
        llm_enabled = await repo.get_runtime_setting(session, "LLM_ENABLED")
        rows = (
            await session.execute(
                select(LLMUsage.purpose, func.count(LLMUsage.id), func.sum(LLMUsage.prompt_tokens),
                       func.sum(LLMUsage.completion_tokens), func.sum(LLMUsage.cost_rub))
                .where(LLMUsage.created_at >= month_start()).group_by(LLMUsage.purpose)
            )
        ).all()
    names = {"quality": "анализ сайтов", "letter": "письма"}
    lines = [
        f"📈 <b>Расход за {utcnow().astimezone(s.tz):%m.%Y}</b>",
        "",
        "🔎 Yandex Search API",
        f"- Запросов: {used} из {limit} ({mode})",
        f"- Оценка: ~{estimate_cost(used, mode, night=True):.0f}–{estimate_cost(used, mode, night=False):.0f} ₽",
        f"- Отложенных в ожидании: {pending}",
        "",
        f"🧠 DeepSeek ({s.DEEPSEEK_MODEL}{'' if llm_enabled else ', выключен'})",
        f"- Потрачено: {spent:.2f} ₽ из {budget:.0f} ₽",
    ]
    for purpose, n, pt, ct, cost in rows:
        lines.append(f"- {names.get(purpose, purpose)}: {n} выз., {int(pt or 0) + int(ct or 0)} ток., {float(cost or 0):.2f} ₽")
    if not rows:
        lines.append("- вызовов не было")
    await message.answer("\n".join(lines))
