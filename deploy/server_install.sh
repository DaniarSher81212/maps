#!/bin/bash
# =============================================================================
# deploy/server_install.sh — Автоматическая установка MAPS на корпоративный сервер
#
# Поддерживаемые ОС: Ubuntu 20.04+, Debian 11+, RHEL/CentOS/Rocky 8+
# Метод: Docker Compose (изолированно, без вмешательства в систему)
#
# Запуск (от root или через sudo):
#   sudo bash deploy/server_install.sh
#
# Тихая установка с параметрами (для автоматизации):
#   sudo bash deploy/server_install.sh --port 80 --password МойПароль --nginx
#
# Флаги:
#   --port    PORT      Порт для MAPS (по умолчанию: 8000)
#   --password PASS     Пароль входа в систему (по умолчанию: CHANGE_ME)
#   --nginx             Настроить Nginx как reverse proxy на порту 80
#   --silent            Не задавать вопросов, использовать дефолты/флаги
# =============================================================================

set -e

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${BLUE}[→]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗] ОШИБКА:${NC} $1"; exit 1; }
ask()  { echo -e "${BOLD}[?]${NC} $1"; }

# ── Дефолтные значения ────────────────────────────────────────────────────────
MAPS_PORT=8000
MAPS_PASSWORD="CHANGE_ME"
MAPS_USERNAME="admin"
SETUP_NGINX=false
SILENT=false

# ── Парсинг аргументов ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)     MAPS_PORT="$2";     shift 2 ;;
        --password) MAPS_PASSWORD="$2"; shift 2 ;;
        --username) MAPS_USERNAME="$2"; shift 2 ;;
        --nginx)    SETUP_NGINX=true;   shift ;;
        --silent)   SILENT=true;        shift ;;
        *) warn "Неизвестный флаг: $1"; shift ;;
    esac
done

# ── Баннер ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}${BOLD}║   MAPS — Автоматическая установка на сервер  ║${NC}"
echo -e "${BLUE}${BOLD}║   Версия 3.1.0                               ║${NC}"
echo -e "${BLUE}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Проверки ──────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Запустите с правами root: sudo bash deploy/server_install.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_USER="${SUDO_USER:-$(logname 2>/dev/null || echo ubuntu)}"

info "Директория проекта: $PROJECT_DIR"
info "Пользователь системы: $APP_USER"

# ── Определение ОС ────────────────────────────────────────────────────────────
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS_ID="$ID"
        OS_VERSION="$VERSION_ID"
    else
        err "Не удалось определить ОС. Поддерживаются: Ubuntu, Debian, RHEL, CentOS, Rocky."
    fi

    case "$OS_ID" in
        ubuntu|debian)  PKG_MGR="apt-get"; PKG_INSTALL="apt-get install -y -qq" ;;
        rhel|centos|rocky|almalinux|fedora) PKG_MGR="dnf"; PKG_INSTALL="dnf install -y -q" ;;
        *) err "Неподдерживаемая ОС: $OS_ID. Поддерживаются: Ubuntu, Debian, RHEL/CentOS/Rocky." ;;
    esac

    log "ОС: $ID $VERSION_ID (менеджер пакетов: $PKG_MGR)"
}

detect_os

# ── Интерактивный режим ───────────────────────────────────────────────────────
if [[ "$SILENT" != true ]]; then
    echo ""
    echo -e "${BOLD}Настройка установки${NC}"
    echo -e "Нажмите Enter чтобы оставить значение по умолчанию."
    echo ""

    ask "Порт для MAPS [${MAPS_PORT}]:"
    read -r input; [[ -n "$input" ]] && MAPS_PORT="$input"

    ask "Пароль входа в систему [${MAPS_PASSWORD}]:"
    read -r input; [[ -n "$input" ]] && MAPS_PASSWORD="$input"

    ask "Логин входа в систему [${MAPS_USERNAME}]:"
    read -r input; [[ -n "$input" ]] && MAPS_USERNAME="$input"

    ask "Настроить Nginx (доступ через порт 80 вместо ${MAPS_PORT})? [y/N]:"
    read -r input
    [[ "$input" =~ ^[Yy]$ ]] && SETUP_NGINX=true

    echo ""
fi

info "Порт:    $MAPS_PORT"
info "Логин:   $MAPS_USERNAME"
info "Пароль:  $MAPS_PASSWORD"
info "Nginx:   $SETUP_NGINX"
echo ""

# ── Шаг 1: Установка Docker ───────────────────────────────────────────────────
install_docker() {
    if command -v docker &>/dev/null; then
        log "Docker уже установлен: $(docker --version)"
        return
    fi

    info "Установка Docker..."

    if [[ "$PKG_MGR" == "apt-get" ]]; then
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg lsb-release

        # Официальный GPG-ключ Docker
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/${OS_ID}/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        # Репозиторий Docker
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${OS_ID} $(lsb_release -cs) stable" \
            > /etc/apt/sources.list.d/docker.list

        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

    elif [[ "$PKG_MGR" == "dnf" ]]; then
        dnf install -y -q dnf-plugins-core
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    fi

    systemctl start docker
    systemctl enable docker

    # Добавить пользователя в группу docker (чтобы не нужен sudo для docker команд)
    usermod -aG docker "$APP_USER" 2>/dev/null || true

    log "Docker установлен: $(docker --version)"
}

install_docker

# Проверить docker compose (plugin v2 или standalone v1)
if docker compose version &>/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    err "docker compose не найден. Установка Docker могла не завершиться. Проверьте логи выше."
fi
log "Docker Compose: $($DOCKER_COMPOSE version --short 2>/dev/null || echo 'OK')"

# ── Шаг 2: Создание .env ──────────────────────────────────────────────────────
info "Создание конфигурации..."

ENV_FILE="$PROJECT_DIR/.env"

cat > "$ENV_FILE" << EOF
# Конфигурация MAPS — создана server_install.sh $(date '+%Y-%m-%d %H:%M')

# --- PostgreSQL ---
DB_HOST=postgres
DB_PORT=5432
DB_NAME=maps_db
DB_USER=maps_user
DB_PASSWORD=${MAPS_PASSWORD}

# --- Приложение ---
APP_ENV=production
LOG_LEVEL=INFO
SESSION_PREFIX=session
DATA_DIR=/app/data
EXPORT_DIR=/app/data/exports

# --- Вход в дашборд ---
MAPS_USERNAME=${MAPS_USERNAME}
MAPS_PASSWORD=${MAPS_PASSWORD}

# --- Параметры Docker ---
MAPS_PORT=${MAPS_PORT}

# --- AI-функции (опционально) ---
# ANTHROPIC_API_KEY=sk-ant-...
EOF

chmod 600 "$ENV_FILE"
chown "$APP_USER":"$APP_USER" "$ENV_FILE"
log ".env создан: $ENV_FILE"

# ── Шаг 3: Запуск Docker Compose ─────────────────────────────────────────────
info "Сборка и запуск контейнеров MAPS..."
cd "$PROJECT_DIR"

$DOCKER_COMPOSE down --remove-orphans 2>/dev/null || true
$DOCKER_COMPOSE up -d --build

log "Контейнеры запущены"

# ── Шаг 4: Ожидание готовности ────────────────────────────────────────────────
info "Ожидание готовности MAPS (до 120 сек)..."

WAIT=0
until curl -s "http://localhost:${MAPS_PORT}/api/status" &>/dev/null; do
    WAIT=$((WAIT + 2))
    if [[ $WAIT -ge 120 ]]; then
        echo ""
        warn "MAPS не ответил за 120 сек. Проверьте логи:"
        warn "  $DOCKER_COMPOSE logs maps"
        warn "  $DOCKER_COMPOSE logs postgres"
        break
    fi
    printf "."
    sleep 2
done

[[ $WAIT -lt 120 ]] && { echo ""; log "MAPS готов (${WAIT} сек)"; }

# ── Шаг 5: Настройка Nginx (опционально) ─────────────────────────────────────
setup_nginx() {
    info "Установка и настройка Nginx..."

    if [[ "$PKG_MGR" == "apt-get" ]]; then
        apt-get install -y -qq nginx
    else
        dnf install -y -q nginx
    fi

    # Конфиг Nginx → проксирует 80 → localhost:MAPS_PORT
    NGINX_CONF="/etc/nginx/sites-available/maps"
    [[ "$PKG_MGR" == "dnf" ]] && NGINX_CONF="/etc/nginx/conf.d/maps.conf"

    cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name _;

    # Лимит размера загружаемых файлов (Excel)
    client_max_body_size 100M;

    # Таймаут для долгих операций (расчёт распределения)
    proxy_read_timeout 300s;
    proxy_connect_timeout 10s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:${MAPS_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

    # Для Ubuntu/Debian — активировать сайт
    if [[ "$PKG_MGR" == "apt-get" ]]; then
        ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/maps
        rm -f /etc/nginx/sites-enabled/default
    fi

    nginx -t && systemctl restart nginx && systemctl enable nginx
    log "Nginx настроен: порт 80 → MAPS"
}

if [[ "$SETUP_NGINX" == true ]]; then
    setup_nginx
fi

# ── Шаг 6: Systemd-юнит для автозапуска Docker Compose ───────────────────────
info "Настройка автозапуска при перезагрузке сервера..."

cat > /etc/systemd/system/maps-docker.service << EOF
[Unit]
Description=MAPS Docker Compose
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/${DOCKER_COMPOSE// /\/} up -d
ExecStop=/usr/bin/${DOCKER_COMPOSE// /\/} down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

# Корректируем путь к docker compose в юните
COMPOSE_BIN=$(command -v docker-compose 2>/dev/null || echo "")
if [[ -n "$COMPOSE_BIN" ]]; then
    # docker-compose v1 (отдельный бинарник)
    sed -i "s|ExecStart=.*|ExecStart=${COMPOSE_BIN} up -d|" /etc/systemd/system/maps-docker.service
    sed -i "s|ExecStop=.*|ExecStop=${COMPOSE_BIN} down|" /etc/systemd/system/maps-docker.service
else
    # docker compose v2 (плагин)
    DOCKER_BIN=$(command -v docker)
    sed -i "s|ExecStart=.*|ExecStart=${DOCKER_BIN} compose up -d|" /etc/systemd/system/maps-docker.service
    sed -i "s|ExecStop=.*|ExecStop=${DOCKER_BIN} compose down|" /etc/systemd/system/maps-docker.service
fi

systemctl daemon-reload
systemctl enable maps-docker
log "Автозапуск настроен (systemd: maps-docker)"

# ── Финальный отчёт ───────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')

if [[ "$SETUP_NGINX" == true ]]; then
    ACCESS_URL="http://${LOCAL_IP}"
else
    ACCESS_URL="http://${LOCAL_IP}:${MAPS_PORT}"
fi

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║          MAPS успешно установлен!            ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Адрес системы:  ${BLUE}${BOLD}${ACCESS_URL}${NC}"
echo -e "  Логин:          ${YELLOW}${MAPS_USERNAME}${NC}"
echo -e "  Пароль:         ${YELLOW}${MAPS_PASSWORD}${NC}"
echo ""
echo -e "  ${BOLD}Управление:${NC}"
echo -e "  Статус     →  docker compose -f ${PROJECT_DIR}/docker-compose.yml ps"
echo -e "  Логи       →  docker compose -f ${PROJECT_DIR}/docker-compose.yml logs -f maps"
echo -e "  Стоп       →  docker compose -f ${PROJECT_DIR}/docker-compose.yml down"
echo -e "  Обновление →  cd ${PROJECT_DIR} && git pull && docker compose up -d --build"
echo ""
if [[ "$SETUP_NGINX" != true ]]; then
    echo -e "  ${YELLOW}Совет:${NC} если порт ${MAPS_PORT} заблокирован корпоративным файрволом,"
    echo -e "  запустите повторно с флагом --nginx для доступа через стандартный порт 80:"
    echo -e "  ${YELLOW}sudo bash deploy/server_install.sh --nginx --silent${NC}"
    echo ""
fi
echo -e "  ${YELLOW}!${NC} Не забудьте сменить пароль в продакшене: ${PROJECT_DIR}/.env"
echo ""
