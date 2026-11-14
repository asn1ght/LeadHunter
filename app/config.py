"""Конфигурация приложения из `.env` (pydantic-settings).

Значения логики (порог, лимиты, веса) могут перекрываться таблицей `settings`
в БД — см. `app.db.repo.get_runtime_setting`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    """Все параметры из `.env`. Имена совпадают с ключами `.env`."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Telegram
    BOT_TOKEN: str = ""
    ADMIN_IDS: str = ""
    TZ: str = "Europe/Moscow"

    # База
    DATABASE_URL: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'leadhunter.db').as_posix()}"

    # Yandex Search API (этап 3)
    YANDEX_SEARCH_API_KEY: str = ""
    YANDEX_FOLDER_ID: str = ""
    YANDEX_SEARCH_REGION: int = 2
    YANDEX_SEARCH_MODE: str = "night_v1"
    YANDEX_PAGES_PER_QUERY: int = 3
    YANDEX_GROUPS_ON_PAGE: int = 10
    SEARCH_MONTHLY_LIMIT: int = 1000  # потолок запросов в месяц без ALLOW_PAID_SEARCH
    ALLOW_PAID_SEARCH: bool = False
    # Экономия запросов (мягкие правила, app.discovery.query_policy)
    QUERY_FULL_DEPTH_EVERY: int = 4  # раз в N прогонов фразы (1-й, 5-й, 9-й…) — все страницы, даже без новых
    QUERY_ZERO_RUNS_TO_PAUSE: int = 2  # столько прогонов подряд без новых доменов → автопауза
    QUERY_PAUSE_DAYS: int = 28  # пауза временная, потом фраза сама возвращается
    QUERY_MIN_ACTIVE: int = 3  # автопауза не опускает число работающих фраз ниже этого

    # DeepSeek (этап 3)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-flash"
    DEEPSEEK_THINKING: bool = False
    LLM_MONTHLY_BUDGET_RUB: float = 1000
    LLM_ENABLED: bool = True
    # Цена в рублях за 1M токенов (DeepSeek flash, пиковый тариф $0.30 / $1.20 при ~100 ₽/$)
    LLM_PRICE_INPUT_PER_1M: float = 30
    LLM_PRICE_OUTPUT_PER_1M: float = 120
    LLM_QUALITY_MIN_TECH: int = 20  # автоматический ИИ-анализ только при score_tech ≥

    # Логика
    SCORE_THRESHOLD: int = 55
    LEADS_DAILY_LIMIT: int = 15  # устарело: заменено планом на неделю
    LEADS_WEEKLY_LIMIT: int = 40  # сколько новых лидов брать в план обзвона на неделю
    NEW_LEADS_PUSH_DOW: str = "mon"  # день плана недели (mon..sun)
    DAILY_QUEUE_REMINDER: bool = True  # в остальные будни — утреннее напоминание об очереди
    AUDIT_CONCURRENCY: int = 5
    DISCOVERY_CRON: str = "0 2 * * 1"
    DAILY_REPORT_TIME: str = "21:00"
    WEEKLY_REPORT_DOW: str = "sun"
    MONTHLY_REPORT_DAY: str = "last"
    NEW_LEADS_PUSH_TIME: str = "09:30"
    TEST_STAGE_LABEL: str = "тест"
    ACTIVE_NICHE: str = "gas"

    # Веса оценки (раздел 7.5)
    SCORE_W_SEV3: int = 15
    SCORE_W_SEV2: int = 8
    SCORE_W_SEV1: int = 3
    SCORE_BONUS_INN: int = 5
    SCORE_BONUS_SEGMENT: int = 5
    SCORE_BONUS_HOT: int = 10

    # Подпись
    OWNER_NAME: str = ""
    OWNER_PHONE: str = ""
    OWNER_TELEGRAM: str = ""
    OWNER_EMAIL: str = ""

    # Чекко (checko.ru): реквизиты, руководитель, финансы по ИНН
    CHEKKO_API_KEY: str = ""
    CHECKO_ENABLED: bool = True
    CHECKO_DAILY_LIMIT: int = 90  # бесплатно 100 запросов/сутки; ООО = 2 запроса, ИП = 1
    CHECKO_REFRESH_DAYS: int = 90  # перезапрашивать сведения не чаще

    # Playwright (этап 4)
    BROWSER_ENABLED: bool = True
    BROWSER_CONCURRENCY: int = 2

    # Прокси по сервисам (этап 5). Пусто = напрямую. Форматы: http://host:port, socks5://user:pass@host:port
    TELEGRAM_PROXY: str = ""  # только Telegram Bot API
    YANDEX_PROXY: str = ""  # только Yandex Search API и operations
    AUDIT_PROXY: str = ""  # загрузка сайтов при аудите (httpx, проверка SSL, Chromium)

    # Бэкапы (этап 5)
    BACKUP_TIME: str = "03:30"  # ежедневный бэкап БД в data/backups/
    BACKUP_KEEP_DAYS: int = 14

    # HTTP
    HTTP_TIMEOUT: float = 20
    USER_AGENT: str = "Mozilla/5.0 (compatible; LeadHunterAudit/1.0)"

    @field_validator("DATABASE_URL")
    @classmethod
    def _abs_sqlite_path(cls, v: str) -> str:
        """Относительный путь sqlite считаем от корня проекта, а не от cwd."""
        prefix = "sqlite+aiosqlite:///"
        if v.startswith(prefix):
            path = v[len(prefix):]
            if path and path != ":memory:" and not Path(path).is_absolute():
                return prefix + (BASE_DIR / path).as_posix()
        return v

    @property
    def admin_ids(self) -> set[int]:
        """Множество Telegram ID владельцев."""
        return {int(x) for x in self.ADMIN_IDS.replace(" ", "").split(",") if x}

    @property
    def tz(self) -> ZoneInfo:
        """Часовой пояс владельца."""
        return ZoneInfo(self.TZ)


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек."""
    return Settings()


def mask_secret(value: str) -> str:
    """Маскировать секрет для логов: оставить 4 последних символа."""
    if not value:
        return "<empty>"
    return "***" + value[-4:] if len(value) > 8 else "***"
