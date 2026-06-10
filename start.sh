#!/bin/bash
# =============================================================================
# start.sh — Запуск MAPS одной командой
#
# Использование:
#   bash start.sh          — автоматически выбрать способ запуска
#   bash start.sh docker   — запустить через Docker Compose
#   bash start.sh local    — запустить локально (Python + PostgreSQL)
#   bash start.sh stop     — остановить MAPS
#   bash start.sh status   — проверить статус
#   bash start.sh update   — обновить код и перезапустить
# =============================================================================

set -e

# ── Цвета ─────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
info() { echo -e "${BLUE}  →${NC} $1"; }
warn() { echo -e "${YELLOW}  !${NC} $1"; }
err()  { echo -e "${RED}  ✗${NC} $1"; exit 1; }

# ── Баннер ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   MAPS — Material Allocation & Planning  ║${NC}"
echo -e "${BLUE}║   Версия 3.1.0                           ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

COMMAND="${1:-auto}"

# ── Вспомогательные функции ───────────────────────────────────────────────────

has_docker() {
    command -v docker &>/dev/null && docker info &>/dev/null 2>&1
}

has_python() {
    command -v python3 &>/dev/null || command -v python &>/dev/null
}

get_python() {
    # Ищем python3.11, затем python3, затем python
    for py in python3.11 python3 python; do
        if command -v "$py" &>/dev/null; then
            echo "$py"
            return
        fi
    done
}

show_result() {
    local port="${1:-8000}"
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║          MAPS успешно запущен!           ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Откройте в браузере:"
    echo -e "  ${BLUE}http://localhost:${port}${NC}"
    echo ""
    echo -e "  Логин:  ${YELLOW}admin${NC}"
    echo -e "  Пароль: ${YELLOW}***REMOVED-PASSWORD***${NC}  (сменить в .env: MAPS_PASSWORD=...)"
    echo ""
    echo -e "  Остановить:  ${YELLOW}bash start.sh stop${NC}"
    echo -e "  Логи:        ${YELLOW}bash start.sh logs${NC}"
    echo ""
}

# ── Команда: stop ─────────────────────────────────────────────────────────────
if [[ "$COMMAND" == "stop" ]]; then
    info "Остановка MAPS..."
    if has_docker && [[ -f docker-compose.yml ]]; then
        docker compose down
        ok "MAPS (Docker) остановлен"
    elif [[ -f /tmp/maps_uvicorn.pid ]]; then
        kill "$(cat /tmp/maps_uvicorn.pid)" 2>/dev/null && rm /tmp/maps_uvicorn.pid
        ok "MAPS (локальный) остановлен"
    else
        warn "Запущенный процесс MAPS не найден"
    fi
    exit 0
fi

# ── Команда: status ───────────────────────────────────────────────────────────
if [[ "$COMMAND" == "status" ]]; then
    info "Проверка статуса MAPS..."
    if curl -s http://localhost:8000/api/status &>/dev/null; then
        ok "MAPS работает → http://localhost:8000"
        curl -s http://localhost:8000/api/status | python3 -m json.tool 2>/dev/null || true
    else
        warn "MAPS не отвечает на порту 8000"
    fi
    exit 0
fi

# ── Команда: logs ─────────────────────────────────────────────────────────────
if [[ "$COMMAND" == "logs" ]]; then
    if has_docker && docker compose ps 2>/dev/null | grep -q "maps"; then
        docker compose logs -f maps
    elif command -v journalctl &>/dev/null; then
        sudo journalctl -u maps -f
    elif [[ -f logs/maps.log ]]; then
        tail -f logs/maps.log
    else
        warn "Логи не найдены"
    fi
    exit 0
fi

# ── Команда: update ───────────────────────────────────────────────────────────
if [[ "$COMMAND" == "update" ]]; then
    info "Получение обновлений..."
    git pull origin main
    if has_docker && docker compose ps 2>/dev/null | grep -q "maps"; then
        info "Пересборка Docker-образа..."
        docker compose build maps
        docker compose up -d maps
        docker compose exec maps alembic upgrade head
    elif [[ -d .venv ]]; then
        .venv/bin/pip install -e . -q
        .venv/bin/alembic upgrade head
        # Перезапустить если systemd
        if command -v systemctl &>/dev/null && systemctl is-active maps &>/dev/null; then
            sudo systemctl restart maps
        fi
    fi
    ok "Обновление завершено"
    exit 0
fi

# ── Запуск через Docker ───────────────────────────────────────────────────────
start_docker() {
    info "Запуск через Docker Compose..."

    if ! has_docker; then
        err "Docker не найден. Установите Docker: https://docs.docker.com/engine/install/"
    fi

    # docker-compose.yml работает без .env — все дефолты внутри него
    docker compose up -d --build

    info "Ожидание запуска (до 30 сек)..."
    for i in $(seq 1 30); do
        if curl -s http://localhost:8000/api/status &>/dev/null; then
            ok "MAPS готов (${i} сек)"
            break
        fi
        sleep 1
    done

    show_result 8000
}

# ── Запуск локально (Python) ──────────────────────────────────────────────────
start_local() {
    info "Локальный запуск (Python)..."

    PYTHON=$(get_python)
    [[ -z "$PYTHON" ]] && err "Python 3.11+ не найден. Установите: https://python.org"

    PY_VERSION=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+')
    info "Python: $PY_VERSION"

    # Создать .env из дефолтов если нет
    if [[ ! -f .env ]]; then
        warn ".env не найден — создаю с настройками по умолчанию"
        cp .env.example .env
        ok ".env создан. При необходимости отредактируйте пароль БД."
    fi

    # Создать виртуальное окружение если нет
    if [[ ! -d .venv ]]; then
        info "Создание виртуального окружения..."
        $PYTHON -m venv .venv
    fi

    # Установить зависимости
    info "Установка зависимостей (первый раз занимает 1-2 мин)..."
    .venv/bin/pip install -e . -q

    # Создать директории
    mkdir -p data/exports logs

    # Применить миграции
    info "Применение миграций БД..."
    if ! .venv/bin/alembic upgrade head 2>/dev/null; then
        echo ""
        err "Не удалось подключиться к PostgreSQL.
       Убедитесь что PostgreSQL запущен и настройки в .env верны.
       Текущие настройки: $(grep DB_ .env | tr '\n' ' ')"
    fi

    ok "БД готова"

    # Запустить MAPS в фоне
    info "Запуск MAPS..."
    nohup .venv/bin/uvicorn app.api.main:app \
        --host 0.0.0.0 --port 8000 --log-level info \
        > logs/maps.log 2>&1 &
    echo $! > /tmp/maps_uvicorn.pid

    # Подождать запуска
    for i in $(seq 1 15); do
        if curl -s http://localhost:8000/api/status &>/dev/null; then
            ok "MAPS готов (${i} сек)"
            break
        fi
        sleep 1
    done

    show_result 8000
    info "Логи: tail -f logs/maps.log"
}

# ── Автовыбор способа запуска ─────────────────────────────────────────────────
if [[ "$COMMAND" == "docker" ]]; then
    start_docker
elif [[ "$COMMAND" == "local" ]]; then
    start_local
else
    # auto — пробуем Docker, если нет — локально
    if has_docker; then
        info "Docker обнаружен → используем Docker Compose"
        start_docker
    elif has_python; then
        info "Docker не найден → локальный запуск"
        start_local
    else
        err "Не найден ни Docker, ни Python.
       Установите Docker: https://docs.docker.com/engine/install/
       Или Python 3.11+: https://python.org"
    fi
fi
