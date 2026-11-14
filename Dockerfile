# LeadHunter: Telegram-бот + Chromium (Playwright) для скриншотов и PDF-аудита.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Moscow

WORKDIR /app

# Шрифты с кириллицей и эмодзи — иначе в скриншотах и PDF вместо текста будут квадратики.
# libatomic1 нужна Node.js, который Playwright использует для установки браузера (в slim-образе её нет).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata libatomic1 \
       fonts-dejavu-core fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Зависимости отдельным слоем: пересобираются, только когда меняется requirements.lock
COPY requirements.lock .
RUN pip install -r requirements.lock \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

# Код запускается из /app (пакет не устанавливаем: config.py ищет .env, config/ и data/ рядом с кодом)
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY app ./app

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app/data
USER bot

VOLUME ["/app/data"]

# Планировщик раз в минуту обновляет data/heartbeat; отметка старше 3 минут — контейнер нездоров
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["python", "-m", "app.health"]

CMD ["python", "-m", "app.main"]
