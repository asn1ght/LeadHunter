# LeadHunter — заметки для Claude

## Git: коммиты и пуши
- Коммиты и пуши делаются **только от лица владельца** (git user `asn1ght`).
- **Не добавлять** в сообщения коммитов строки `Co-Authored-By: Claude ...` и любые другие упоминания соавторства Claude / Anthropic.
- В описаниях pull request тоже не добавлять «Generated with Claude Code» и подобные подписи.
- Репозиторий: https://github.com/asn1ght/LeadHunter.git, ветка `main`.
- `.env` и `data/` (БД, логи) никогда не коммитить — там секреты и персональные данные.

## Проект
- Техзадание и этапы — `PLAN.md`. Работать по этапам, после каждого: тесты, короткий отчёт.
- Python 3.12 в `.venv` (системный Python 3.10 не подходит): `.venv\Scripts\python -m pytest -q`.
- Запуск бота: `.venv\Scripts\python -m app.main` (или ▶ на `app/main.py`).
- Браузер для аудита и PDF (этап 4): после установки зависимостей `.venv\Scripts\python -m playwright install chromium`.
- Тесты не должны писать в `data/`: браузер в тестах подменён заглушкой (`tests/conftest.py`), настоящий Chromium — только с маркером `real_browser` и во временной папке.
- Смена статуса лида — только через `app.crm.service.change_status()`; отчёты считаются по `events`.
- Схема БД меняется только миграциями Alembic (`alembic revision --autogenerate`), для SQLite — batch-режим.
- Тексты бота — на русском, коротко, с эмодзи.
- Деплой (этап 5): `Dockerfile` + `docker-compose.yml`, systemd-юниты в `deploy/`. Зависимости для сервера — `requirements.lock`; после правки `pyproject.toml` пересобрать (команда в README → «Зависимости»).
- Прокси по сервисам: `TELEGRAM_PROXY`, `YANDEX_PROXY`, `AUDIT_PROXY` (`app/net.py`); пароли прокси в логах — только через `mask_proxy()`.
