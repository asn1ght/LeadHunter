# LeadHunter

Telegram-бот и мини-CRM для веб-разработчика-фрилансера. Бот сам находит компании с устаревшими или проблемными сайтами, проверяет их, готовит аргументы для холодного звонка и ведёт лиды до сделки.

Первая ниша — газификация в Санкт-Петербурге и Ленинградской области. Ниши и поисковые фразы настраиваются в [config/niches.yaml](config/niches.yaml) и командой `/niche`.

## Что умеет

**Поиск компаний**
- Раз в неделю (по умолчанию ночью в понедельник) ищет сайты через Yandex Search API по фразам активной ниши. Вручную поиск запускается командой `/run`.
- Отсеивает агрегаторы, маркетплейсы, справочники и чёрный список. Сайты на конструкторах и хостингах (tilda.ws, tw1.ru и т. п.) учитываются по полному поддомену.
- Экономит запросы: если фраза перестала приносить новые сайты, глубина поиска по ней уменьшается, а затем она ставится на паузу. Работает месячный лимит запросов.

**Аудит сайта**
- Технические проверки: HTTPS и срок SSL, скорость загрузки, мобильная вёрстка, метатеги, счётчики, устаревшие CMS и jQuery, год в копирайте, формы и кнопки связи.
- Скриншоты для компьютера и телефона через Playwright (Chromium).
- Извлечение контактов: телефоны, email, мессенджеры, адрес, ИНН.
- Слой качества через DeepSeek: опечатки, расхождения в ценах, слабые места и «крючок» для звонка.
- Итоговая оценка 0–100 с расшифровкой «❓ Почему оценка».

**Реквизиты по ИНН (Чекко)**
- Если на сайте найден ИНН, бот подтягивает из [Чекко](https://checko.ru):
  - юрлицо, статус и возраст компании;
  - генерального директора и учредителей;
  - выручку и прибыль за последний год с динамикой;
  - риски: банкротство, налоговые долги, массовость, санкции.
- В карточке есть подсказка, как обратиться к руководителю по имени-отчеству. ИНН можно ввести вручную.

**CRM и работа с лидами**
- Карточка лида: статус, контакты, реквизиты, главное по аудиту и кнопки следующего шага.
- Воронка: рассылка → ответ → звонок №1 → тест → звонок №2 → сделка. Все изменения пишутся в журнал событий.
- Напоминания и перезвоны («пт 14:30», «завтра в 12»), заметки, поиск по базе (`/find` понимает опечатки и транслит).
- Черновики писем и сообщений в мессенджер (DeepSeek), PDF мини-аудит на одну страницу для отправки клиенту.

**План обзвона на неделю**
- В понедельник утром бот берёт лучшие новые лиды (по умолчанию 40) в план недели и присылает сводку.
- «📞 Сегодня» показывает очередь обзвона. Необработанные лиды переносятся на следующий день и неделю, а темп подсказывается («≈8 в день до пятницы»).
- Со вторника по пятницу приходит короткое утреннее напоминание.

**Отчёты**
- Ежедневный, недельный и месячный отчёт по воронке со сравнением с прошлым периодом и конверсиями.

## Команды бота

| Команда | Что делает |
|---|---|
| `/new` | новые лиды по оценке |
| `/today` | очередь обзвона и напоминания на сегодня |
| `/leads [статус]` | все лиды с фильтром |
| `/find <текст>` | поиск по названию, домену, телефону, ИНН, заметкам |
| `/add <url …>` | добавить сайты вручную (или файлом .txt/.csv) |
| `/report [дата]` | отчёт за день, например `/report 13.11` |
| `/week`, `/month` | отчёт за неделю / месяц |
| `/run` | запустить поиск компаний сейчас |
| `/queries` | поисковые фразы ниши и их статистика |
| `/niche` | переключить или создать нишу |
| `/usage` | расход Yandex Search API, DeepSeek и Чекко |
| `/netcheck` | доступ к Telegram, Яндексу, сайтам, DeepSeek и Чекко: через какой прокси и с какого IP |
| `/backup` | прислать свежий бэкап базы файлом |
| `/cancel`, `/help` | отменить ввод / справка |

## Стек

Python 3.12 · aiogram 3 · SQLAlchemy 2 (async) + SQLite · Alembic · APScheduler · httpx · selectolax · Playwright · Jinja2 · DeepSeek (через OpenAI SDK) · Yandex Search API v2 · Чекко API v2.

## Установка для разработки

Нужен Python 3.12 или новее. Как развернуть на сервере — в разделе [Деплой](#деплой).

```bash
git clone https://github.com/asn1ght/LeadHunter.git
cd LeadHunter
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env   # и заполнить ключи
```

Запуск:

```bash
python -m app.main
```

При старте бот сам применяет миграции базы (`data/leadhunter.db`) и запускает планировщик.

## Ключи и настройки

Все параметры лежат в `.env` (образец — [.env.example](.env.example)). Часть настроек логики можно переопределить в базе, они перекрывают `.env`.

| Ключ | Где взять |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) в Telegram |
| `ADMIN_IDS` | ваш Telegram ID (через запятую, если несколько). Бот отвечает только им |
| `YANDEX_SEARCH_API_KEY`, `YANDEX_FOLDER_ID` | Yandex Cloud: сервисный аккаунт с ролью `search-api.webSearch.user`, API-ключ и **ID каталога** (не ID аккаунта). Нужен привязанный платёжный аккаунт |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com). Месячный бюджет — `LLM_MONTHLY_BUDGET_RUB` |
| `CHEKKO_API_KEY` | личный кабинет [checko.ru](https://checko.ru), раздел API. Бесплатно 100 запросов в сутки |

Полезные настройки:
- `SCORE_THRESHOLD`: с какой оценки лид считается горячим.
- `LEADS_WEEKLY_LIMIT`: размер недельного плана.
- `DISCOVERY_CRON`: расписание поиска в формате crontab.
- `SEARCH_MONTHLY_LIMIT`: лимит запросов к Яндексу в месяц.
- `CHECKO_DAILY_LIMIT`: лимит запросов к Чекко в сутки.
- `BROWSER_ENABLED`: включает скриншоты.
- `TELEGRAM_PROXY`, `YANDEX_PROXY`, `AUDIT_PROXY`: прокси по сервисам (см. [Прокси](#прокси)).
- `BACKUP_TIME`, `BACKUP_KEEP_DAYS`: время ежедневного бэкапа и сколько дней хранить копии.

> `.env` и папка `data/` (база, логи, скриншоты) в репозиторий не попадают: там ключи и данные клиентов.

## Деплой

### 1. Выбрать сервер

Из России может быть недоступен Telegram. Из-за рубежа могут быть недоступны Yandex Search API и часть российских сайтов: такие сайты бот посчитает «упавшими». Поэтому у каждого сервиса свой прокси.

- **Рекомендуется:** российский VPS + `TELEGRAM_PROXY` через зарубежный сервер. Яндекс и сайты работают напрямую.
- **Альтернатива:** зарубежный VPS + `YANDEX_PROXY` и `AUDIT_PROXY` через российский сервер.

До установки проверьте с сервера, что доступно напрямую:

```bash
bash deploy/check-network.sh
```

`000` значит, что сервис заблокирован и для него нужен прокси. Любой другой код (200, 302, 401, 404) — всё в порядке.

### 2а. Docker (рекомендуется)

```bash
git clone https://github.com/asn1ght/LeadHunter.git /opt/leadhunter
cd /opt/leadhunter
cp .env.example .env && nano .env               # ключи, ADMIN_IDS, прокси
mkdir -p data && sudo chown -R 1000:1000 data   # в контейнере бот работает от пользователя с uid 1000
docker compose up -d --build
docker compose ps                               # через пару минут статус healthy
docker compose logs -f                          # логи
```

- Образ уже содержит Chromium и шрифты с кириллицей и эмодзи.
- База, бэкапы, логи и скриншоты лежат в `./data` и переживают пересборку.
- Контейнер перезапускается сам после сбоя и перезагрузки сервера.
- Healthcheck проверяет, что планировщик бота жив (`python -m app.health`).

Обновление:

```bash
git pull && docker compose up -d --build
```

Миграции базы применяются при старте автоматически.

### 2б. Без Docker (systemd, Ubuntu 24.04)

В Ubuntu 24.04 Python 3.12 стоит из коробки. В более старых версиях поставьте его отдельно или используйте Docker.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin leadhunter
sudo git clone https://github.com/asn1ght/LeadHunter.git /opt/leadhunter
sudo chown -R leadhunter:leadhunter /opt/leadhunter
cd /opt/leadhunter
sudo -u leadhunter python3.12 -m venv .venv
sudo -u leadhunter .venv/bin/pip install -r requirements.lock
sudo .venv/bin/playwright install-deps chromium            # системные библиотеки (нужен root)
sudo -u leadhunter .venv/bin/playwright install --only-shell chromium
sudo apt install -y fonts-dejavu-core fonts-noto-color-emoji
sudo -u leadhunter cp .env.example .env && sudo -u leadhunter nano .env
sudo cp deploy/leadhunter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now leadhunter
journalctl -u leadhunter -f                                 # логи
```

Обновление: `sudo -u leadhunter git pull && sudo systemctl restart leadhunter`.

### Прокси

Прокси задаются в `.env`. Пустое значение — напрямую.

| Ключ | Что идёт через него |
|---|---|
| `TELEGRAM_PROXY` | только запросы к Telegram Bot API |
| `YANDEX_PROXY` | только Yandex Search API |
| `AUDIT_PROXY` | загрузка сайтов при аудите, проверка SSL, скриншоты в Chromium |

DeepSeek и Чекко всегда работают напрямую: они доступны и из России, и из-за рубежа.

- **Форматы:** `http://host:port`, `socks5://host:port`, `socks5://user:pass@host:port`. В логах и `/netcheck` пароль скрыт.
- **Подходят только SOCKS5 и HTTP.** MTProto-прокси (`t.me/proxy?...`) работают лишь в приложениях Telegram, не для ботов. С такой ссылкой бот не запустится и скажет почему.
- **Бесплатные публичные прокси не используйте:** через Telegram-прокси проходит токен бота.
- **Chromium не умеет SOCKS5 с логином и паролем.** С таким `AUDIT_PROXY` страницы загружаются через прокси, а скриншоты снимаются напрямую (в логе будет предупреждение). SSH-туннель и HTTP-прокси работают полностью.

Где взять прокси:
- **Свой второй сервер нужной страны** — через SSH-туннель, ниже.
- **Купленный индивидуальный SOCKS5-прокси нужной страны.**

После смены прокси перезапустите бота и проверьте командой `/netcheck`: для каждого сервиса она показывает, доступен ли он, через какой прокси и с какого внешнего IP идут запросы.

### Свой прокси через SSH-туннель

Нужен второй сервер в стране, откуда сервис доступен. Туннель поднимает на первом сервере SOCKS5-прокси `127.0.0.1:1080`, трафик выходит со второго.

На **первом** сервере (где бот):

```bash
sudo apt install -y autossh
sudo -u leadhunter ssh-keygen -t ed25519 -N "" -f /home/leadhunter/.ssh/tunnel_ed25519
sudo cat /home/leadhunter/.ssh/tunnel_ed25519.pub          # скопировать
```

На **втором** сервере создайте пользователя без шелла, которому разрешён только туннель:

```bash
sudo useradd --create-home --shell /usr/sbin/nologin tunnel
sudo mkdir -p /home/tunnel/.ssh
echo 'restrict,port-forwarding ssh-ed25519 AAAA...ключ...' | sudo tee /home/tunnel/.ssh/authorized_keys
sudo chown -R tunnel:tunnel /home/tunnel/.ssh
sudo chmod 700 /home/tunnel/.ssh && sudo chmod 600 /home/tunnel/.ssh/authorized_keys
```

Снова на **первом**:

```bash
sudo cp deploy/leadhunter-tunnel.service /etc/systemd/system/
sudo nano /etc/systemd/system/leadhunter-tunnel.service     # заменить PROXY_HOST на адрес второго сервера
sudo systemctl daemon-reload && sudo systemctl enable --now leadhunter-tunnel
curl --socks5-hostname 127.0.0.1:1080 https://api.ipify.org # должен показать IP второго сервера
```

В `.env` укажите `TELEGRAM_PROXY=socks5://127.0.0.1:1080` (или `YANDEX_PROXY` / `AUDIT_PROXY`) и перезапустите бота.

- **Если бот в Docker:** раскомментируйте `network_mode: host` в `docker-compose.yml`, иначе контейнер не увидит `127.0.0.1` хоста.
- **Если бот в systemd:** раскомментируйте в `leadhunter.service` строки `After=`/`Wants=` с туннелем.

autossh сам поднимает туннель после обрыва связи и перезагрузки.

### Бэкапы

- Каждый день в `BACKUP_TIME` (по умолчанию 03:30) бот сохраняет сжатую копию базы в `data/backups/` и удаляет копии старше `BACKUP_KEEP_DAYS` (14 дней). Копия снимается без остановки бота.
- Если ежедневный бэкап не удался, бот напишет об этом.
- `/backup` — сделать копию прямо сейчас и получить её файлом в Telegram. В файле все контакты и заметки, не пересылайте его.

Восстановление:

```bash
docker compose stop           # или: sudo systemctl stop leadhunter
gunzip -c data/backups/leadhunter-20261114-033000.db.gz > data/leadhunter.db
docker compose start          # или: sudo systemctl start leadhunter
```

### Зависимости

Точные версии для сервера зафиксированы в `requirements.lock`. После изменения зависимостей в `pyproject.toml` пересоберите его:

```bash
uv pip compile pyproject.toml --python-platform x86_64-manylinux_2_28 --python-version 3.12 --no-annotate -o requirements.lock
```

## Структура

```
app/
  bot/           хендлеры, клавиатуры, карточки лидов
  crm/           статусы, смена статуса, поиск по базе, план обзвона
  discovery/     Yandex Search API, фильтры, прогоны поиска, экономия запросов
  audit/         загрузка сайта, тех. проверки, извлечение контактов, скриншоты, PDF, оценка
  integrations/  Чекко (реквизиты и финансы по ИНН)
  net.py         прокси по сервисам и /netcheck
  backup.py      бэкапы SQLite
  health.py      healthcheck для Docker
  ai/            клиент DeepSeek и промпты
  reports/       отчёты по журналу событий
  db/            модели и запросы
  templates/     HTML-шаблон PDF-аудита
config/          ниши, чёрный список, хостинги и конструкторы
alembic/         миграции базы
deploy/          systemd-юниты бота и SSH-туннеля, проверка сети перед деплоем
tests/           pytest
```

Подробное техзадание и этапы — в [PLAN.md](PLAN.md).

## Тесты

```bash
python -m pytest -q
```

Тесты не ходят в сеть и не пишут в `data/`. Браузер и Чекко в них подменены заглушками. Тесты с настоящим Chromium помечены маркером `real_browser`.

## Лицензия

[GPL-3.0](LICENSE)
