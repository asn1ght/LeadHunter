#!/usr/bin/env bash
# Проверка с целевого сервера ДО деплоя: какие сервисы доступны напрямую, а каким нужен прокси.
# Запуск: bash deploy/check-network.sh
# Код 000 = заблокировано или нет связи → для этого сервиса нужен прокси.
# Любой другой код (200, 302, 401, 404…) = сеть есть, всё в порядке.

check() {
  local name="$1" url="$2" env="$3"
  local code
  code=$(curl -s -o /dev/null -m 15 -w "%{http_code}" "$url")
  if [ "$code" = "000" ]; then
    printf "⛔ %-20s %s  → нужен %s\n" "$name" "$code" "$env"
  else
    printf "✅ %-20s %s\n" "$name" "$code"
  fi
}

check "Telegram"          https://api.telegram.org                TELEGRAM_PROXY
check "Yandex Search API" https://searchapi.api.cloud.yandex.net  YANDEX_PROXY
check "Сайты (ya.ru)"     https://ya.ru                           AUDIT_PROXY
check "DeepSeek"          https://api.deepseek.com                "(прокси не поддерживается)"
check "Чекко"             https://api.checko.ru                   "(прокси не поддерживается)"
echo "Внешний IP сервера: $(curl -s -m 10 https://api.ipify.org)"
