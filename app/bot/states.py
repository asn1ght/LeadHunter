"""FSM-состояния сценариев."""
from aiogram.fsm.state import State, StatesGroup


class AddFlow(StatesGroup):
    waiting_urls = State()


class FindFlow(StatesGroup):
    waiting_query = State()


class NoteFlow(StatesGroup):
    waiting_text = State()


class CallNoteFlow(StatesGroup):
    """Заметка после звонка (исход уже выбран кнопкой)."""

    waiting_text = State()


class ReminderFlow(StatesGroup):
    """Созвониться потом / Напомнить / Звонок №2 / Перенос звонка №2."""

    choosing_date = State()
    entering_date = State()
    entering_contact = State()
    entering_comment = State()


class ReportFlow(StatesGroup):
    waiting_date = State()


class QueryFlow(StatesGroup):
    waiting_text = State()


class NicheFlow(StatesGroup):
    waiting_name = State()
    waiting_keywords = State()
