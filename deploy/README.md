# Деплой MAPS на Oracle Cloud

## Что понадобится

- Oracle Cloud VM (Ubuntu 22.04) — уже создан
- SSH-доступ к серверу
- Репозиторий на GitHub (или копирование файлов по SCP)

---

## Шаг 1. Скопировать код на сервер

**Вариант A — через Git (рекомендуется):**
```bash
# На сервере:
git clone https://github.com/ваш-аккаунт/maps.git
cd maps
```

**Вариант B — через SCP с локальной машины:**
```bash
# На локальной машине:
scp -r /home/dan/dev/projects/python/PythonProject ubuntu@<IP_СЕРВЕРА>:~/maps
```

---

## Шаг 2. Запустить установку

```bash
cd ~/maps
sudo bash deploy/setup.sh
```

Скрипт сделает всё сам:
- Установит Python 3.11, PostgreSQL, cloudflared
- Создаст базу данных `maps_db`
- Установит Python-зависимости
- Применит миграции (создаст таблицы)
- Зарегистрирует MAPS как systemd-сервис

**Время выполнения:** 3-5 минут.

---

## Шаг 3. Сменить пароль базы данных

После установки обязательно замените дефолтный пароль:

```bash
# Новый пароль в PostgreSQL:
sudo -u postgres psql -c "ALTER USER maps_user PASSWORD 'ВАШ_НАДЁЖНЫЙ_ПАРОЛЬ';"

# То же самое в .env:
nano ~/maps/.env
# Найдите строку DB_PASSWORD=***REMOVED-DB-PASSWORD*** и замените
```

---

## Шаг 4. Запустить MAPS

```bash
sudo systemctl start maps
sudo systemctl status maps   # Должно быть: Active: active (running)
```

Проверка что работает:
```bash
curl http://localhost:8000/api/status
```

---

## Шаг 5. Открыть Cloudflare Tunnel

```bash
# Разовая сессия (URL показывается в терминале):
bash deploy/tunnel.sh

# Фоновый режим (URL сохраняется в /tmp/maps_tunnel.url):
bash deploy/tunnel.sh --bg
cat /tmp/maps_tunnel.url
```

Получите ссылку вида `https://xxxx.trycloudflare.com`.

Чтобы скачать отчёт с рабочего компа:
```
https://xxxx.trycloudflare.com/api/export/{session_id}
```

---

## Управление сервисом

```bash
sudo systemctl start maps      # Запустить
sudo systemctl stop maps       # Остановить
sudo systemctl restart maps    # Перезапустить (после обновления кода)
sudo systemctl status maps     # Статус

# Логи:
sudo journalctl -u maps -f                    # В реальном времени
sudo journalctl -u maps --since "1 hour ago"  # За последний час
sudo journalctl -u maps -n 50                 # Последние 50 строк
```

---

## Обновление кода

```bash
cd ~/maps
git pull origin main
sudo systemctl restart maps
```

---

## Порты и файрвол Oracle Cloud

Oracle Cloud по умолчанию блокирует входящие соединения. Для Cloudflare Tunnel
открывать порты НЕ нужно — туннель работает через исходящие соединения (outbound).

Если хотите прямой доступ по IP (без туннеля), откройте порт 8000:
```bash
# На сервере (UFW):
sudo ufw allow 8000/tcp

# В Oracle Cloud Console:
# Networking → Virtual Cloud Networks → Security Lists → Add Ingress Rule
# Source: 0.0.0.0/0, Port: 8000
```

---

## Структура файлов деплоя

```
deploy/
├── setup.sh       — установка всего на сервере (запускать один раз)
├── maps.service   — systemd-юнит (автозапуск MAPS)
├── tunnel.sh      — запуск Cloudflare Quick Tunnel
└── README.md      — этот файл
```
