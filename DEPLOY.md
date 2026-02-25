# Руководство по деплою — Бот-консультант ООО «Завод ВРК»

Полное руководство: от чистого сервера до работающей системы с автозапуском,
обновлениями и мониторингом.

---

## Содержание

1. [Требования к серверу](#1-требования-к-серверу)
2. [Подготовка сервера](#2-подготовка-сервера)
3. [Установка Docker и Docker Compose](#3-установка-docker-и-docker-compose)
4. [Клонирование проекта с GitHub](#4-клонирование-проекта-с-github)
5. [Архитектура связи: что с чем соединяется](#5-архитектура-связи-что-с-чем-соединяется)
6. [Настройка окружения](#6-настройка-окружения)
7. [Первый запуск](#7-первый-запуск)
8. [Проверка работоспособности](#8-проверка-работоспособности)
9. [Настройка порта: несколько проектов на одном сервере](#9-настройка-порта-несколько-проектов-на-одном-сервере)
10. [Обновление проекта](#10-обновление-проекта)
11. [Автозапуск при перезагрузке сервера](#11-автозапуск-при-перезагрузке-сервера)
12. [Настройка Nginx (reverse proxy + HTTPS)](#12-настройка-nginx-reverse-proxy--https)
13. [Подключение виджета к сайту заказчика](#13-подключение-виджета-к-сайту-заказчика)
14. [Мониторинг и логи](#14-мониторинг-и-логи)
15. [Резервное копирование](#15-резервное-копирование)
16. [Устранение неполадок](#16-устранение-неполадок)
17. [Шпаргалка команд](#17-шпаргалка-команд)

---

## 1. Требования к серверу

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| ОС | Ubuntu 20.04+ / Debian 11+ | Ubuntu 22.04 LTS |
| CPU | 1 ядро | 2 ядра |
| RAM | 1 ГБ | 2 ГБ |
| Диск | 10 ГБ | 20 ГБ SSD |
| Сеть | Открыт порт 80, 443 | Статический IP |

---

## 2. Подготовка сервера

Подключитесь к серверу по SSH:

```bash
ssh root@YOUR_SERVER_IP
```

Обновите систему и установите базовые утилиты:

```bash
apt update && apt upgrade -y
apt install -y git curl wget nano ufw
```

Настройте файрвол:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

Создайте пользователя для деплоя (опционально, но рекомендуется):

```bash
adduser deploy
usermod -aG sudo deploy
su - deploy
```

---

## 3. Установка Docker и Docker Compose

### Установка Docker

```bash
# Удаляем старые версии (если есть)
sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Устанавливаем зависимости
sudo apt install -y ca-certificates curl gnupg lsb-release

# Добавляем GPG-ключ Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Добавляем репозиторий
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Устанавливаем Docker Engine + Compose
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### Проверка установки

```bash
docker --version
docker compose version
```

### Добавление пользователя в группу docker

Чтобы не использовать `sudo` перед каждой командой:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### Включение автозапуска Docker

```bash
sudo systemctl enable docker
sudo systemctl enable containerd
```

---

## 4. Клонирование проекта с GitHub

```bash
# Перейдите в директорию для проектов
cd /opt

# Клонируйте репозиторий
sudo git clone https://github.com/YOUR_USERNAME/vrk_bot_assistant.git
cd vrk_bot_assistant

# Установите владельца (если работаете не от root)
sudo chown -R $USER:$USER /opt/vrk_bot_assistant
```

> Замените `YOUR_USERNAME` на ваш аккаунт GitHub.
>
> Если репозиторий приватный, используйте SSH-ключ или Personal Access Token:
> ```bash
> git clone https://<TOKEN>@github.com/YOUR_USERNAME/vrk_bot_assistant.git
> ```

---

## 5. Архитектура связи: что с чем соединяется

Прежде чем заполнять конфиг, важно понять, как устроена связь между компонентами.

### Схема

```
                        ИНТЕРНЕТ
                           │
    ┌──────────────────────┼──────────────────────┐
    │                      │                      │
    ▼                      ▼                      ▼
┌────────────┐    ┌──────────────┐    ┌───────────────────┐
│ Посетитель │    │   Telegram   │    │  LLM-провайдер    │
│ сайта ВРК  │    │   серверы    │    │ (OpenRouter /     │
│ (браузер)  │    │              │    │  GigaChat / ...)  │
└─────┬──────┘    └──────┬───────┘    └─────────┬─────────┘
      │                  │                      ▲
      │ HTTP             │ Telegram API         │ HTTP
      │ POST /api/chat   │ (long-polling)       │ (запросы к LLM)
      ▼                  ▼                      │
┌─────────────────────────────────────────────────────────┐
│                  ВАШ СЕРВЕР                             │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Docker Compose                                 │    │
│  │                                                 │    │
│  │  ┌─────────────────────┐  Python import         │    │
│  │  │  vrk-backend        │◄──────────────┐        │    │
│  │  │  FastAPI :8000      │               │        │    │
│  │  │  • /api/chat        │  ┌────────────┴──────┐ │    │
│  │  │  • /health          │  │ vrk-telegram-bot  │ │    │
│  │  │  • ChromaDB         │  │ aiogram 3.x       │ │    │
│  │  │  • LLM-фабрика     │  │ (long-polling)    │ │    │
│  │  └─────────────────────┘  └───────────────────┘ │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌──────────┐  Nginx :80/:443 → proxy → :8000          │
│  │  Nginx   │  (HTTPS-терминация)                       │
│  └──────────┘                                           │
└─────────────────────────────────────────────────────────┘
```

### Два способа связи

| Компонент | Как связан с бэкендом | Через сеть? |
|-----------|----------------------|-------------|
| **JS-виджет** (браузер посетителя) | HTTP-запросы на `https://ваш-домен/api/chat` | Да — через интернет |
| **Telegram-бот** (контейнер `vrk-telegram-bot`) | Прямой Python-импорт `from main import process_message` | Нет — внутри Docker |

**JS-виджет** работает в браузере посетителя, поэтому ему нужен **публичный URL бэкенда**.
Этот URL указывается в HTML-коде сайта заказчика в атрибуте `data-api`:

```html
<script src="https://bot.ventreshetki.com/static/widget.js"
        data-api="https://bot.ventreshetki.com"></script>
```

**Telegram-бот** работает в соседнем Docker-контейнере и вызывает бизнес-логику
напрямую через Python-импорт. Ему не нужен URL бэкенда — только токен бота.

### Какие ключи/токены нужны, откуда их взять

| Что | Откуда получить | Куда записать (`.env`) | Кто использует |
|-----|----------------|----------------------|----------------|
| **URL бэкенда** | Это адрес вашего сервера. Если есть домен — `https://bot.ventreshetki.com`. Если нет — `http://IP_СЕРВЕРА:8000` | `API_BASE_URL` + атрибут `data-api` в HTML на сайте заказчика | JS-виджет |
| **Telegram Bot Token** | [@BotFather](https://t.me/BotFather) в Telegram → `/newbot` → скопировать токен | `TELEGRAM_BOT_TOKEN` | Контейнер `vrk-telegram-bot` |
| **LLM API-ключ** | Регистрация у провайдера: [GigaChat](https://developers.sber.ru/portal/products/gigachat), [Yandex GPT](https://cloud.yandex.ru/services/yandexgpt), [OpenRouter](https://openrouter.ai/keys), [OpenAI](https://platform.openai.com/api-keys) | Соответствующая переменная (`OPENROUTER_API_KEY`, `GIGACHAT_CREDENTIALS` и т.д.) | Контейнер `vrk-backend` (через `llm_factory.py`) |

> **Никакого отдельного «API-ключа для бота» создавать не нужно.**
> Бэкенд сам является API-сервером. Виджет шлёт запросы на его публичный URL без авторизации.
> Telegram-бот вообще не ходит по сети к бэкенду.

### Порядок получения ключей (перед заполнением `.env`)

**Шаг А.** Определите публичный адрес сервера:

```bash
# Если домен ещё не настроен — используйте IP
curl ifconfig.me
# Пример: 185.210.45.12 → API_BASE_URL=http://185.210.45.12:8000

# После настройки Nginx + SSL (раздел 10):
# API_BASE_URL=https://bot.ventreshetki.com
```

**Шаг Б.** Создайте Telegram-бота:

1. Откройте [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте `/newbot`
3. Придумайте имя и username (например, `vrk_consultant_bot`)
4. Скопируйте токен вида `7123456789:AAH...`

**Шаг В.** Получите LLM-ключ (самый быстрый вариант — OpenRouter):

1. Зайдите на [openrouter.ai/keys](https://openrouter.ai/keys)
2. Зарегистрируйтесь
3. Создайте ключ → скопируйте `sk-or-v1-...`

---

## 6. Настройка окружения

### Создание `.env`

```bash
cp .env.example .env
nano .env
```

Заполните все три обязательных параметра, полученных в разделе 5:

```env
# ═══════════════════════════════════════════════════════════
# 1. АДРЕС БЭКЕНДА (ваш сервер)
# ═══════════════════════════════════════════════════════════
# До настройки домена/Nginx — используйте IP:
API_BASE_URL=http://185.210.45.12:8000
# После настройки HTTPS (раздел 10) замените на:
# API_BASE_URL=https://bot.ventreshetki.com

# ═══════════════════════════════════════════════════════════
# 2. TELEGRAM BOT (от @BotFather)
# ═══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ═══════════════════════════════════════════════════════════
# 3. LLM-ПРОВАЙДЕР (раскомментируйте ОДИН блок)
# ═══════════════════════════════════════════════════════════
# Приоритет: GigaChat → Yandex GPT → OpenRouter → OpenAI
# Достаточно заполнить ключи ОДНОГО провайдера.

# --- OpenRouter (самый быстрый старт) ---
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
# OPENROUTER_MODEL=openai/gpt-4o-mini

# --- GigaChat (Сбер) ---
# GIGACHAT_CREDENTIALS=ваши_креды_base64
# GIGACHAT_SCOPE=GIGACHAT_API_PERS

# --- Yandex GPT ---
# YANDEX_FOLDER_ID=ваш_folder_id
# YANDEX_API_KEY=ваш_api_key

# --- OpenAI ---
# OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
```

Сохраните: `Ctrl+O`, `Enter`, `Ctrl+X`.

### Проверка `.env`

Убедитесь, что все три ключа заполнены:

```bash
# Должны быть непустые значения (не placeholder'ы)
grep -E "^(API_BASE_URL|TELEGRAM_BOT_TOKEN|OPENROUTER_API_KEY|GIGACHAT_CREDENTIALS|YANDEX_API_KEY|OPENAI_API_KEY)=" .env
```

### Создание директорий для данных

```bash
mkdir -p data logs chroma_db
```

---

## 7. Первый запуск

> **Важно:** выполняйте шаги строго по порядку.
> Сначала парсинг (наполнение базы), затем запуск сервисов.

### Шаг 1: Сборка Docker-образов

```bash
docker compose build
```

Первая сборка займёт 3–5 минут (скачивание базового образа Python + установка зависимостей).

### Шаг 2: Первичный парсинг каталога

Перед запуском бота необходимо наполнить базу знаний товарами:

```bash
docker compose run --rm scraper
```

Дождитесь завершения. Проверьте, что данные появились:

```bash
ls -la data/raw_products.json
# Файл должен быть непустой (> 1 КБ)
```

### Шаг 3: Запуск всех сервисов

```bash
docker compose up -d
```

Флаг `-d` запускает контейнеры в фоновом режиме.

### Шаг 4: Проверка статуса

```bash
docker compose ps
```

Ожидаемый вывод:

```
NAME               STATUS          PORTS
vrk-backend        Up 10 seconds   0.0.0.0:8000->8000/tcp
vrk-telegram-bot   Up 8 seconds
```

Оба контейнера должны быть в статусе `Up`.

---

## 8. Проверка работоспособности

### Health-check API

```bash
curl http://localhost:8000/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "llm_available": true,
  "chroma_documents": 42
}
```

- `llm_available: true` — LLM-провайдер подключён
- `chroma_documents > 0` — база знаний заполнена

### Тест чата

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Нужна наружная решетка", "session_id": "test-1", "source": "web"}'
```

### Тест Telegram-бота

Откройте бота в Telegram, отправьте `/start` — должно появиться приветствие с Inline-кнопкой «Старт».

### Тест веб-виджета

Откройте в браузере:

```
http://YOUR_SERVER_IP:8000/static/widget.html
```

---

## 9. Настройка порта: несколько проектов на одном сервере

Если на сервере уже запущен другой проект (например, тоже на порту 8000), нужно выделить **отдельный порт** для бота ВРК, чтобы трафик к виджету и API шёл именно в этот проект.

### Шаг 1: Проверить, какие порты заняты

```bash
# Список процессов, слушающих порты
sudo ss -tlnp | grep LISTEN
# или
sudo lsof -i -P -n | grep LISTEN
```

Найдите порт, который уже использует другой проект (часто 8000, 8080, 3000). Для ВРК выберите **свободный** порт, например `8001` или `8010`.

### Шаг 2: Пробросить свой порт в docker-compose

Внутри контейнера приложение всегда слушает порт **8000**. Меняем только **внешний** (хост) порт.

Откройте `docker-compose.yml` и измените секцию `ports` у сервиса `backend`:

```yaml
  backend:
    build: .
    container_name: vrk-backend
    restart: unless-stopped
    ports:
      - "8001:8000"   # было 8000:8000 — теперь снаружи 8001, внутри 8000
```

Формат: `"ПОРТ_НА_СЕРВЕРЕ:ПОРТ_В_КОНТЕЙНЕРЕ"`.  
Примеры:
- `8001:8000` — к боту ВРК обращаются по адресу `http://IP:8001`
- `8010:8000` — по адресу `http://IP:8010`

### Шаг 3: Указать этот адрес в .env и в виджете

В `.env`:

```env
# С портом (если не используете Nginx)
API_BASE_URL=http://85.208.86.148:8001

# Или с HTTPS, если Nginx слушает на отдельном порту (например 8443)
# API_BASE_URL=https://85.208.86.148:8443
```

В коде виджета на сайте заказчика в атрибуте `data-api` — **тот же URL**:

```html
<script src="http://85.208.86.148:8001/static/widget.js"
        data-api="http://85.208.86.148:8001"></script>
```

После смены порта перезапустите контейнеры:

```bash
cd /opt/vrk_bot_assistant
docker compose up -d
```

### Шаг 4: Если используете Nginx (несколько проектов на одном IP)

Нужно разделить трафик: один проект — по одному пути или порту, ВРК — по другому.

**Вариант А: отдельный порт в Nginx для ВРК**

В конфиге Nginx создайте отдельный `server` с другим портом:

```nginx
# Файл: /etc/nginx/sites-available/vrk-bot
server {
    listen 8043;   # отдельный порт только для бота ВРК
    server_name 85.208.86.148;

    location / {
        proxy_pass http://127.0.0.1:8001;   # сюда проброшен контейнер ВРК
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Тогда в `.env` и в виджете: `http://85.208.86.148:8043` (или `https://...` если на 8043 настроен SSL).

**Вариант Б: один порт 80/443, разные пути (path)**

Другой проект — по корню `/`, бот ВРК — по префиксу, например `/vrk/`:

```nginx
server {
    listen 80;
    server_name 85.208.86.148;

    # Другой проект — по корню
    location / {
        proxy_pass http://127.0.0.1:8000;   # другой проект
        # ...
    }

    # Бот ВРК — по пути /vrk/
    location /vrk/ {
        proxy_pass http://127.0.0.1:8001/;   # порт контейнера ВРК, слэш важен
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Тогда:
- API бота: `http://85.208.86.148/vrk/api/chat`
- Статика виджета: `http://85.208.86.148/vrk/static/widget.js`

В `.env` и в виджете:

```env
API_BASE_URL=http://85.208.86.148/vrk
```

```html
<script src="http://85.208.86.148/vrk/static/widget.js"
        data-api="http://85.208.86.148/vrk"></script>
```

Бэкенд FastAPI менять не нужно: Nginx при `proxy_pass http://127.0.0.1:8001/;` отрезает префикс `/vrk` и передаёт бэкенду уже пути `/api/chat`, `/static/...` и т.д.

**Вариант В: отдельный поддомен (если есть домен)**

Например другой проект — `example.com`, бот — `bot.example.com`. Тогда два разных `server { server_name ... }` в Nginx и разный `proxy_pass` на разные порты (8000 и 8001).

### Итог

| Ситуация | Что сделать | URL для виджета и API_BASE_URL |
|----------|-------------|--------------------------------|
| Только этот проект на сервере | Оставить `8000:8000` | `http://IP:8000` или `https://IP` (если Nginx на 443) |
| Другой проект уже на 8000 | В docker-compose задать `8001:8000` | `http://IP:8001` |
| Nginx: бот на отдельном порту | Слушать, например, 8043 → proxy на 8001 | `http://IP:8043` |
| Nginx: бот по пути /vrk/ | location /vrk/ → proxy на 8001 | `http://IP/vrk` |

Telegram-бот от порта не зависит: он дергает бэкенд внутри Docker-сети по имени сервиса `backend` и внутреннему порту 8000.

---

## 10. Обновление проекта

### Стандартное обновление (новый код с GitHub)

```bash
cd /opt/vrk_bot_assistant

# 1. Получить последние изменения
git pull origin main

# 2. Пересобрать образы (если изменились зависимости или код)
docker compose build

# 3. Перезапустить сервисы без простоя
docker compose up -d
```

### Быстрое обновление одной командой

Создайте скрипт `/opt/vrk_bot_assistant/update.sh`:

```bash
#!/bin/bash
set -e

echo "=== Обновление VRK Bot ==="
cd /opt/vrk_bot_assistant

echo "→ Получение изменений из GitHub..."
git pull origin main

echo "→ Пересборка Docker-образов..."
docker compose build

echo "→ Перезапуск сервисов..."
docker compose up -d

echo "→ Очистка старых образов..."
docker image prune -f

echo "→ Статус сервисов:"
docker compose ps

echo "=== Обновление завершено ==="
```

Сделайте исполняемым и запускайте:

```bash
chmod +x update.sh
./update.sh
```

### Обновление только бэкенда (без перезапуска Telegram-бота)

```bash
git pull origin main
docker compose build backend
docker compose up -d backend
```

### Обновление только Telegram-бота

```bash
git pull origin main
docker compose build telegram-bot
docker compose up -d telegram-bot
```

### Принудительная пересборка (без кеша)

Если нужно пересобрать образ с нуля (например, обновились зависимости):

```bash
docker compose build --no-cache
docker compose up -d
```

### Обновление базы знаний (ручной парсинг)

```bash
docker compose run --rm scraper
docker compose restart backend
```

---

## 11. Автозапуск при перезагрузке сервера

### Способ 1: Docker restart policy (уже настроен)

В `docker-compose.yml` указано `restart: unless-stopped` — контейнеры автоматически
перезапустятся после перезагрузки сервера, если Docker-демон работает.

Убедитесь, что Docker запускается автоматически:

```bash
sudo systemctl enable docker
```

Проверка:

```bash
sudo systemctl is-enabled docker
# Ответ: enabled
```

### Способ 2: Systemd-сервис (рекомендуется для продакшена)

Создайте systemd unit-файл:

```bash
sudo nano /etc/systemd/system/vrk-bot.service
```

Содержимое:

```ini
[Unit]
Description=VRK Bot Assistant (Docker Compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/vrk_bot_assistant
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose restart
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
```

Активируйте сервис:

```bash
# Перезагрузить конфигурацию systemd
sudo systemctl daemon-reload

# Включить автозапуск
sudo systemctl enable vrk-bot.service

# Запустить сервис
sudo systemctl start vrk-bot.service

# Проверить статус
sudo systemctl status vrk-bot.service
```

Управление через systemctl:

```bash
sudo systemctl start vrk-bot     # Запустить
sudo systemctl stop vrk-bot      # Остановить
sudo systemctl restart vrk-bot   # Перезапустить
sudo systemctl reload vrk-bot    # Мягкий перезапуск
sudo systemctl status vrk-bot    # Статус
```

### Способ 3: Watchdog — перезапуск при падении отдельного контейнера

Создайте скрипт `/opt/vrk_bot_assistant/watchdog.sh`:

```bash
#!/bin/bash
# Проверяет статус контейнеров и перезапускает упавшие

cd /opt/vrk_bot_assistant

CONTAINERS=("vrk-backend" "vrk-telegram-bot")

for CONTAINER in "${CONTAINERS[@]}"; do
    STATUS=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)

    if [ "$STATUS" != "true" ]; then
        echo "$(date) — $CONTAINER не работает. Перезапуск..." >> logs/watchdog.log
        docker compose up -d "$CONTAINER"
    fi
done
```

Сделайте исполняемым:

```bash
chmod +x watchdog.sh
```

Добавьте в cron (проверка каждые 5 минут):

```bash
crontab -e
```

Добавьте строку:

```cron
*/5 * * * * /opt/vrk_bot_assistant/watchdog.sh
```

---

## 12. Настройка Nginx (reverse proxy + HTTPS)

### Установка Nginx

```bash
sudo apt install -y nginx
```

### Конфигурация виртуального хоста

```bash
sudo nano /etc/nginx/sites-available/vrk-bot
```

Содержимое:

```nginx
server {
    listen 80;
    server_name bot.yourdomain.com;

    # Перенаправление на HTTPS (раскомментировать после получения сертификата)
    # return 301 https://$host$request_uri;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Активируйте конфигурацию:

```bash
sudo ln -s /etc/nginx/sites-available/vrk-bot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Получение SSL-сертификата (Let's Encrypt)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d bot.yourdomain.com
```

Certbot автоматически:
- Получит сертификат
- Настроит HTTPS в Nginx
- Добавит автообновление сертификата в cron

Проверка автообновления:

```bash
sudo certbot renew --dry-run
```

---

## 13. Подключение виджета к сайту заказчика

После того как бэкенд запущен и Nginx настроен (разделы 7–11), можно подключать
виджет к сайту.

### Что вставлять в HTML сайта

Добавьте две строки **перед закрывающим тегом `</body>`** на каждой странице,
где нужен чат-бот:

```html
<!-- Виджет чат-бота ООО "Завод ВРК" -->
<link rel="stylesheet" href="https://bot.ventreshetki.com/static/widget.css">
<script src="https://bot.ventreshetki.com/static/widget.js"
        data-api="https://bot.ventreshetki.com"></script>
```

### Что указать в `data-api`

Это **тот же самый адрес**, который вы указали в `API_BASE_URL` в файле `.env`.

| Этап | Значение `data-api` | Пример |
|------|---------------------|--------|
| Локальная разработка | `http://localhost:8000` | Тестирование на своём ПК |
| Сервер без домена | `http://IP_СЕРВЕРА:8000` | `http://185.210.45.12:8000` |
| Продакшен (после Nginx + SSL) | `https://ваш-домен` | `https://bot.ventreshetki.com` |

### Как это работает

1. Браузер посетителя загружает `widget.js` и `widget.css` с вашего сервера
2. Скрипт читает атрибут `data-api` и формирует URL:
   `data-api` + `/api/chat` = `https://bot.ventreshetki.com/api/chat`
3. При отправке сообщения виджет делает `POST /api/chat` с телом:
   ```json
   {
     "message": "текст пользователя",
     "session_id": "web_abc123...",
     "source": "web"
   }
   ```
4. Бэкенд отвечает JSON'ом, виджет рендерит ответ, кнопки и карточки товаров

### Безопасность

После деплоя замените `allow_origins=["*"]` в `main.py` на домен сайта,
чтобы виджет не мог быть использован с чужих сайтов:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ventreshetki.com",
        "https://www.ventreshetki.com",
        "https://xn----ctbjabaraetfwdan0bzal0e5b4cwe.xn--p1ai",
    ],
    ...
)
```

### Проверка

Откройте тестовую страницу в браузере:

```
https://bot.ventreshetki.com/static/widget.html
```

Если виджет открывается и отвечает — значит связь работает,
и те же `<link>` + `<script>` можно вставлять на сайт заказчика.

---

## 14. Мониторинг и логи

### Просмотр логов контейнеров

```bash
# Все сервисы
docker compose logs -f

# Только бэкенд
docker compose logs -f backend

# Только Telegram-бот
docker compose logs -f telegram-bot

# Последние 100 строк
docker compose logs --tail=100 backend
```

### Просмотр логов приложения

```bash
# Полный лог
cat logs/bot.log

# Последние записи в реальном времени
tail -f logs/bot.log

# Поиск ошибок
grep -i "error\|critical" logs/bot.log
```

### Мониторинг ресурсов контейнеров

```bash
# CPU, память, сеть в реальном времени
docker stats

# Информация о конкретном контейнере
docker inspect vrk-backend
```

### Health-check по cron (уведомление при падении)

Создайте `/opt/vrk_bot_assistant/healthcheck.sh`:

```bash
#!/bin/bash
# Проверяет /health endpoint и логирует ошибки

RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)

if [ "$RESPONSE" != "200" ]; then
    echo "$(date) — HEALTH CHECK FAILED (HTTP $RESPONSE)" >> /opt/vrk_bot_assistant/logs/watchdog.log
    cd /opt/vrk_bot_assistant && docker compose restart
    echo "$(date) — Сервисы перезапущены" >> /opt/vrk_bot_assistant/logs/watchdog.log
fi
```

```bash
chmod +x healthcheck.sh
```

Добавьте в cron:

```cron
*/2 * * * * /opt/vrk_bot_assistant/healthcheck.sh
```

---

## 15. Резервное копирование

### Что нужно бекапить

| Файл / директория | Описание | Критичность |
|---|---|---|
| `.env` | Ключи и токены | Критично |
| `data/raw_products.json` | Данные товаров | Восстановимо (парсер) |
| `chroma_db/` | Векторная база | Восстановимо (reindex) |
| `logs/` | Логи | Некритично |

### Скрипт бекапа

Создайте `/opt/vrk_bot_assistant/backup.sh`:

```bash
#!/bin/bash
BACKUP_DIR="/opt/backups/vrk-bot"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PROJECT_DIR="/opt/vrk_bot_assistant"

mkdir -p "$BACKUP_DIR"

tar -czf "$BACKUP_DIR/vrk-backup-$TIMESTAMP.tar.gz" \
    -C "$PROJECT_DIR" \
    .env \
    data/raw_products.json \
    chroma_db/

# Удалить бекапы старше 30 дней
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +30 -delete

echo "$(date) — Бекап создан: vrk-backup-$TIMESTAMP.tar.gz" >> "$PROJECT_DIR/logs/watchdog.log"
```

```bash
chmod +x backup.sh
```

Автоматический бекап раз в сутки (cron):

```cron
0 2 * * * /opt/vrk_bot_assistant/backup.sh
```

### Восстановление из бекапа

```bash
cd /opt/vrk_bot_assistant
tar -xzf /opt/backups/vrk-bot/vrk-backup-TIMESTAMP.tar.gz
docker compose restart
```

---

## 16. Устранение неполадок

### Контейнер не запускается

```bash
# Посмотреть логи ошибки
docker compose logs backend

# Проверить .env
cat .env | grep -v '^#' | grep -v '^$'

# Пересобрать с нуля
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Health-check возвращает `llm_available: false`

Проблема с LLM-провайдером. Проверьте:

```bash
# Наличие ключа
grep -E "GIGACHAT|YANDEX|OPENROUTER|OPENAI" .env

# Логи для деталей ошибки
grep -i "llm\|provider\|gigachat\|openai\|yandex" logs/bot.log
```

### `chroma_documents: 0`

База знаний пуста. Запустите парсер:

```bash
docker compose run --rm scraper
docker compose restart backend
```

### Telegram-бот не отвечает

```bash
# Проверить статус
docker compose ps telegram-bot

# Проверить токен
grep TELEGRAM_BOT_TOKEN .env

# Перезапустить
docker compose restart telegram-bot

# Посмотреть логи
docker compose logs --tail=50 telegram-bot
```

### Контейнеры потребляют много памяти

```bash
# Проверить потребление
docker stats --no-stream

# Очистить неиспользуемые образы/контейнеры
docker system prune -a -f
```

### Виджет на сайте не отправляет сообщения / ошибка CORS

Откройте консоль браузера (F12 → Console). Типичные ошибки:

**`blocked by CORS policy`** — бэкенд не разрешает запросы с домена сайта:

```bash
# Проверьте, что домен сайта указан в allow_origins в main.py
# Или временно верните allow_origins=["*"] для диагностики
```

**`net::ERR_CONNECTION_REFUSED`** — виджет не может достучаться до бэкенда:

```bash
# Проверьте, что data-api указан правильно
# Проверьте, что бэкенд запущен и порт открыт
curl https://bot.ventreshetki.com/health

# Проверьте файрвол
sudo ufw status
```

**`Mixed Content`** — сайт на HTTPS, а `data-api` указан на HTTP:

```
# Решение: настройте SSL (раздел 11) и используйте https:// в data-api
```

### Порт 8000 занят

```bash
# Найти процесс
sudo lsof -i :8000

# Изменить порт в docker-compose.yml (например, 8080:8000)
# и перезапустить
docker compose up -d
```

---

## 17. Шпаргалка команд

### Ежедневные операции

```bash
cd /opt/vrk_bot_assistant

# Статус сервисов
docker compose ps

# Логи в реальном времени
docker compose logs -f

# Перезапуск всех сервисов
docker compose restart

# Перезапуск конкретного сервиса
docker compose restart backend
docker compose restart telegram-bot
```

### Обновление

```bash
# Полное обновление
git pull origin main && docker compose build && docker compose up -d

# Или через скрипт
./update.sh
```

### Парсер

```bash
# Ручной запуск парсинга
docker compose run --rm scraper

# Перезапуск бэкенда для подхвата новых данных
docker compose restart backend
```

### Остановка / удаление

```bash
# Остановить все
docker compose down

# Остановить и удалить volumes
docker compose down -v

# Полная очистка (образы, контейнеры, кеш)
docker compose down --rmi all
docker system prune -a -f
```

### Диагностика

```bash
# Health-check
curl localhost:8000/health

# Ресурсы контейнеров
docker stats

# Список образов
docker images | grep vrk

# Зайти внутрь контейнера
docker exec -it vrk-backend bash

# Проверить сеть
docker network ls
```

### Systemd (если настроен)

```bash
sudo systemctl status vrk-bot
sudo systemctl restart vrk-bot
sudo systemctl stop vrk-bot
sudo journalctl -u vrk-bot -f
```

---

## Рекомендуемый итоговый crontab

```bash
crontab -e
```

```cron
# Watchdog: перезапуск упавших контейнеров (каждые 5 минут)
*/5 * * * * /opt/vrk_bot_assistant/watchdog.sh

# Health-check: проверка API + перезапуск при ошибке (каждые 2 минуты)
*/2 * * * * /opt/vrk_bot_assistant/healthcheck.sh

# Бекап: ежедневно в 02:00
0 2 * * * /opt/vrk_bot_assistant/backup.sh
```
