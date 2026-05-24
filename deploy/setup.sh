#!/bin/bash
# =============================================================================
# deploy/setup.sh — Установка MAPS на Oracle Cloud Ubuntu
#
# Запуск (один раз на сервере):
#   chmod +x deploy/setup.sh
#   sudo ./deploy/setup.sh
#
# Что делает скрипт:
#   1. Устанавливает Python 3.11, PostgreSQL, системные зависимости
#   2. Создаёт базу данных и пользователя PostgreSQL
#   3. Создаёт виртуальное окружение и устанавливает зависимости Python
#   4. Создаёт .env с настройками для production
#   5. Применяет миграции Alembic (создаёт таблицы в БД)
#   6. Устанавливает cloudflared (Cloudflare Tunnel)
#   7. Регистрирует MAPS как systemd-сервис (автозапуск)
#
# После запуска:
#   systemctl start maps        — запустить MAPS
#   systemctl status maps       — проверить статус
#   bash deploy/tunnel.sh       — получить публичную ссылку Cloudflare
# =============================================================================

set -e  # Остановиться при любой ошибке
# set -x  # Раскомментируйте для отладки (показывает каждую команду)

# --- Цвета для вывода ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # Сброс цвета

log()  { echo -e "${GREEN}[MAPS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# =============================================================================
# 0. Проверки
# =============================================================================
log "Проверка прав доступа..."
[[ $EUID -ne 0 ]] && err "Запустите скрипт с sudo: sudo bash deploy/setup.sh"

# Определяем директорию проекта (папка, где лежит этот скрипт -> родитель)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
log "Директория проекта: $PROJECT_DIR"

# Имя пользователя, от которого работает приложение (не root)
APP_USER="${SUDO_USER:-ubuntu}"
log "Пользователь приложения: $APP_USER"

# =============================================================================
# 1. Системные зависимости
# =============================================================================
log "Обновление пакетов..."
apt-get update -qq

log "Установка Python 3.11, PostgreSQL, вспомогательных утилит..."
apt-get install -y -qq \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    postgresql \
    postgresql-contrib \
    libpq-dev \
    curl \
    git \
    build-essential

# =============================================================================
# 2. PostgreSQL
# =============================================================================
log "Настройка PostgreSQL..."

# Запускаем PostgreSQL если не запущен
systemctl start postgresql
systemctl enable postgresql

# Создаём пользователя и базу данных
# -c: выполнить SQL-команду; || true: не падать если уже существует
sudo -u postgres psql -c "CREATE USER maps_user WITH PASSWORD '***REMOVED-DB-PASSWORD***';" 2>/dev/null || warn "Пользователь maps_user уже существует"
sudo -u postgres psql -c "CREATE DATABASE maps_db OWNER maps_user;" 2>/dev/null || warn "База maps_db уже существует"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE maps_db TO maps_user;" 2>/dev/null

log "PostgreSQL настроен: база maps_db, пользователь maps_user"
warn "ВАЖНО: Смените пароль в /etc/postgresql/*/main/pg_hba.conf и в .env!"

# =============================================================================
# 3. Виртуальное окружение Python
# =============================================================================
log "Создание виртуального окружения Python..."
VENV_DIR="$PROJECT_DIR/.venv"

sudo -u "$APP_USER" python3.11 -m venv "$VENV_DIR"

log "Установка зависимостей..."
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -e "$PROJECT_DIR" -q
# FastAPI и uvicorn указаны в requirements.txt, но на всякий случай:
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install \
    fastapi uvicorn[standard] jinja2 python-multipart anthropic -q

log "Python-зависимости установлены"

# =============================================================================
# 4. Файл .env (production-настройки)
# =============================================================================
ENV_FILE="$PROJECT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env уже существует — пропускаем создание"
else
    log "Создание .env для production..."
    cat > "$ENV_FILE" <<EOF
# Production .env для MAPS на Oracle Cloud
# Создан автоматически: $(date)

# --- PostgreSQL ---
DB_HOST=localhost
DB_PORT=5432
DB_NAME=maps_db
DB_USER=maps_user
DB_PASSWORD=***REMOVED-DB-PASSWORD***

# --- Приложение ---
APP_ENV=production
LOG_LEVEL=INFO
SESSION_PREFIX=session

# --- Пути ---
DATA_DIR=$PROJECT_DIR/data
EXPORT_DIR=$PROJECT_DIR/data/exports
EOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"  # Только владелец может читать (пароли!)
    log ".env создан: $ENV_FILE"
fi

# =============================================================================
# 5. Директории для данных
# =============================================================================
log "Создание директорий для данных..."
sudo -u "$APP_USER" mkdir -p "$PROJECT_DIR/data/exports" "$PROJECT_DIR/logs"

# =============================================================================
# 6. Миграции Alembic
# =============================================================================
log "Применение миграций Alembic (создание таблиц в БД)..."
cd "$PROJECT_DIR"
sudo -u "$APP_USER" "$VENV_DIR/bin/alembic" upgrade head

log "Таблицы созданы"

# =============================================================================
# 7. Cloudflared (Cloudflare Tunnel)
# =============================================================================
log "Установка cloudflared..."

ARCH=$(dpkg --print-architecture)
CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"

curl -L "$CF_URL" -o /tmp/cloudflared.deb -s
dpkg -i /tmp/cloudflared.deb
rm /tmp/cloudflared.deb

log "cloudflared установлен: $(cloudflared --version)"

# =============================================================================
# 8. Systemd-сервис для MAPS
# =============================================================================
log "Регистрация MAPS как systemd-сервис..."

# Копируем юнит-файл (он лежит рядом с этим скриптом)
cp "$SCRIPT_DIR/maps.service" /etc/systemd/system/maps.service

# Подставляем реальные пути и пользователя в юнит-файл
sed -i "s|__PROJECT_DIR__|$PROJECT_DIR|g" /etc/systemd/system/maps.service
sed -i "s|__APP_USER__|$APP_USER|g" /etc/systemd/system/maps.service
sed -i "s|__VENV_DIR__|$VENV_DIR|g" /etc/systemd/system/maps.service

systemctl daemon-reload
systemctl enable maps  # Автозапуск при перезагрузке сервера

log "Сервис maps.service зарегистрирован и включён в автозапуск"

# =============================================================================
# Итог
# =============================================================================
echo ""
echo -e "${BLUE}=================================================${NC}"
echo -e "${GREEN}  MAPS успешно установлен!${NC}"
echo -e "${BLUE}=================================================${NC}"
echo ""
echo "  Запустить MAPS:         sudo systemctl start maps"
echo "  Проверить статус:       sudo systemctl status maps"
echo "  Логи в реальном времени: sudo journalctl -u maps -f"
echo ""
echo "  Открыть туннель:        bash $SCRIPT_DIR/tunnel.sh"
echo ""
echo -e "${YELLOW}  Не забудьте сменить пароль БД в .env!${NC}"
echo -e "${BLUE}=================================================${NC}"
