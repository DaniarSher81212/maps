#!/bin/bash
# =============================================================================
# deploy/tunnel.sh — Запуск Cloudflare Quick Tunnel
#
# Что такое Cloudflare Quick Tunnel?
#   Бесплатный временный туннель от Cloudflare.
#   Открывает публичный URL вида https://xxxx.trycloudflare.com
#   который проксирует трафик на ваш локальный порт 8000.
#
#   Не нужен домен, не нужна регистрация — работает сразу.
#   URL меняется при каждом перезапуске (это ок для личного использования).
#
# Использование:
#   bash deploy/tunnel.sh           — запуск в foreground (Ctrl+C для остановки)
#   bash deploy/tunnel.sh --bg      — запуск в фоне (URL сохраняется в tunnel.url)
#
# Требования:
#   - cloudflared установлен (делает setup.sh)
#   - MAPS запущен: sudo systemctl start maps
# =============================================================================

PORT=8000
URL_FILE="/tmp/maps_tunnel.url"  # Файл для хранения текущего URL туннеля

# --- Цвета ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Проверяем, что cloudflared установлен
if ! command -v cloudflared &> /dev/null; then
    echo "cloudflared не установлен. Запустите: sudo bash deploy/setup.sh"
    exit 1
fi

# Проверяем, что MAPS отвечает на порту 8000
if ! curl -s "http://localhost:$PORT/api/status" > /dev/null 2>&1; then
    echo -e "${YELLOW}[WARN]${NC} MAPS не отвечает на порту $PORT"
    echo "Запустите: sudo systemctl start maps"
    echo "Или для разработки: maps serve"
    echo ""
    read -p "Продолжить всё равно? (y/N) " -n 1 -r
    echo
    [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
fi

# =============================================================================
# Запуск туннеля
# =============================================================================

# Функция: извлечь URL из вывода cloudflared
extract_url() {
    # cloudflared пишет URL в stderr в формате:
    # ... Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):
    # https://xxxx.trycloudflare.com
    grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | head -1
}

if [[ "$1" == "--bg" ]]; then
    # =========================================================================
    # Фоновый режим: туннель работает в фоне, URL сохраняется в файл
    # =========================================================================
    echo "Запускаю туннель в фоновом режиме..."

    # Запускаем cloudflared и перехватываем stderr для извлечения URL
    cloudflared tunnel --url "http://localhost:$PORT" \
        --no-autoupdate \
        --logfile /tmp/cloudflared.log \
        2>/tmp/cloudflared_stderr.log &

    CF_PID=$!
    echo $CF_PID > /tmp/cloudflared.pid

    # Ждём появления URL (до 30 секунд)
    echo -n "Ожидаю URL туннеля"
    for i in $(seq 1 30); do
        URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cloudflared_stderr.log 2>/dev/null | head -1)
        if [[ -n "$URL" ]]; then
            break
        fi
        echo -n "."
        sleep 1
    done
    echo ""

    if [[ -z "$URL" ]]; then
        echo "Не удалось получить URL. Лог: /tmp/cloudflared_stderr.log"
        exit 1
    fi

    echo "$URL" > "$URL_FILE"

    echo ""
    echo -e "${BLUE}=================================================${NC}"
    echo -e "${GREEN}  Туннель открыт!${NC}"
    echo -e "${BLUE}=================================================${NC}"
    echo ""
    echo -e "  Дашборд:  ${GREEN}$URL${NC}"
    echo -e "  Экспорт:  ${GREEN}$URL/api/export/{session_id}${NC}"
    echo ""
    echo "  URL сохранён: $URL_FILE"
    echo "  PID туннеля:  $CF_PID  (файл: /tmp/cloudflared.pid)"
    echo ""
    echo "  Остановить туннель:"
    echo "    kill \$(cat /tmp/cloudflared.pid)"
    echo -e "${BLUE}=================================================${NC}"

else
    # =========================================================================
    # Foreground режим: туннель работает пока открыт терминал
    # Cloudflared сам выводит URL в консоль — удобно для разовых сессий
    # =========================================================================
    echo ""
    echo -e "${BLUE}=================================================${NC}"
    echo -e "${GREEN}  MAPS Cloudflare Tunnel${NC}"
    echo -e "${BLUE}=================================================${NC}"
    echo ""
    echo "  MAPS дашборд будет доступен по ссылке вида:"
    echo -e "  ${GREEN}https://xxxx.trycloudflare.com${NC}"
    echo ""
    echo "  Ссылка появится через 5-10 секунд после запуска."
    echo "  Ctrl+C — остановить туннель."
    echo -e "${BLUE}=================================================${NC}"
    echo ""

    # --no-autoupdate: не обновлять cloudflared автоматически (стабильнее)
    cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate
fi
