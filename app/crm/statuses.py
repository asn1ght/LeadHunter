"""Статусы лидов: коды, подписи, эмодзи, рекомендованные действия (раздел 5.2)."""
from __future__ import annotations

from enum import StrEnum

from app.config import get_settings


class Status(StrEnum):
    NEW = "NEW"
    CANCELLED = "CANCELLED"
    SENT = "SENT"
    REPLIED = "REPLIED"
    IGNORED = "IGNORED"
    CALL1_NEEDED = "CALL1_NEEDED"
    CALLBACK = "CALLBACK"
    TEST_AGREED = "TEST_AGREED"
    CALL2_SCHEDULED = "CALL2_SCHEDULED"
    THINKING = "THINKING"
    AGREED = "AGREED"
    REFUSED = "REFUSED"


EMOJI: dict[Status, str] = {
    Status.NEW: "🆕",
    Status.CANCELLED: "❌",
    Status.SENT: "✉️",
    Status.REPLIED: "💬",
    Status.IGNORED: "🙈",
    Status.CALL1_NEEDED: "📞",
    Status.CALLBACK: "⏰",
    Status.TEST_AGREED: "🧪",
    Status.CALL2_SCHEDULED: "📅",
    Status.THINKING: "🤔",
    Status.AGREED: "🤝",
    Status.REFUSED: "🚫",
}

_TEXT: dict[Status, str] = {
    Status.NEW: "Новый",
    Status.CANCELLED: "Отменён",
    Status.SENT: "Отправлено",
    Status.REPLIED: "Ответил",
    Status.IGNORED: "Игнор",
    Status.CALL1_NEEDED: "Нужно созвониться",
    Status.CALLBACK: "Созвониться потом",
    Status.TEST_AGREED: "Согласен на {test}",
    Status.CALL2_SCHEDULED: "Звонок №2 назначен",
    Status.THINKING: "Думает",
    Status.AGREED: "Согласились",
    Status.REFUSED: "Отказ",
}


def label(status: str | Status, with_emoji: bool = True) -> str:
    """Подпись статуса для показа пользователю, например «🆕 Новый»."""
    try:
        st = Status(status)
    except ValueError:
        return str(status)
    text = _TEXT[st].format(test=get_settings().TEST_STAGE_LABEL)
    return f"{EMOJI[st]} {text}" if with_emoji else text


# Исходы звонков: код -> (подпись кнопки, целевой статус или None = без смены статуса)
CALL1_OUTCOMES: dict[str, tuple[str, Status]] = {
    "TEST_AGREED": ("🧪 Тест", Status.TEST_AGREED),
    "THINKING": ("🤔 Думает", Status.THINKING),
    "REFUSED": ("🚫 Отказ", Status.REFUSED),
    "no_answer": ("🙈 Не дозвонился", Status.IGNORED),
}

CALL2_OUTCOMES: dict[str, tuple[str, Status]] = {
    "AGREED": ("🤝 Согласились", Status.AGREED),
    "THINKING": ("🤔 Думает", Status.THINKING),
    "REFUSED": ("🚫 Отказ", Status.REFUSED),
    "no_answer": ("🙈 Не дозвонился", Status.IGNORED),
}


# Рекомендованные кнопки на карточке: (action, подпись).
# action — либо `st_<STATUS>` (прямая смена статуса), либо имя сценария.
RECOMMENDED: dict[Status, list[tuple[str, str]]] = {
    Status.NEW: [
        ("st_SENT", "✉️ Отправил"),
        ("st_CALL1_NEEDED", "📞 Созвониться"),
        ("cancel", "❌ Отменить"),
        ("blacklist", "⛔ В чёрный список"),
    ],
    Status.SENT: [
        ("st_REPLIED", "💬 Ответил"),
        ("st_IGNORED", "🙈 Игнор"),
        ("later", "⏰ Позже"),
    ],
    Status.REPLIED: [("call1", "📞 Звонок №1 проведён"), ("later", "⏰ Позже")],
    Status.CALL1_NEEDED: [("call1", "📞 Звонок №1 проведён"), ("later", "⏰ Позже")],
    Status.CALLBACK: [("call1", "📞 Звонок №1 проведён"), ("later", "⏰ Перенести")],
    Status.IGNORED: [("st_CALL1_NEEDED", "📞 Созвониться"), ("later", "⏰ Позже")],
    Status.TEST_AGREED: [("call2", "📅 Назначить звонок №2")],
    Status.CALL2_SCHEDULED: [("call2done", "✔️ Звонок №2 проведён"), ("call2resched", "⏸ Перенос")],
    Status.THINKING: [
        ("remind", "⏰ Напомнить"),
        ("st_AGREED", "🤝 Согласились"),
        ("st_REFUSED", "🚫 Отказ"),
    ],
    Status.CANCELLED: [],
    Status.AGREED: [],
    Status.REFUSED: [],
}


def parse_status(text: str) -> Status | None:
    """Распознать статус по коду или по слову из подписи («новый», "sent")."""
    t = text.strip().lower()
    if not t:
        return None
    for st in Status:
        if st.value.lower() == t or label(st, with_emoji=False).lower().startswith(t):
            return st
    return None
