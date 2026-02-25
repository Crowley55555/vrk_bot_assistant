# Бот-консультант ООО «Завод ВРК»

Интеллектуальная система подбора вентиляционного оборудования.  
Работает как **веб-виджет** (чистый HTML/JS) и как **Telegram-бот**,  
используя единую бизнес-логику, RAG-поиск и воронку продаж.

---

## Структура проекта

```
vrk_bot_assistant/
├── config.py              # Настройки, воронка, системный промпт
├── logger.py              # Логирование (файл + консоль)
├── models.py              # Pydantic-модели данных
├── llm_factory.py         # Фабрика LLM-провайдеров (GigaChat/Yandex/OpenRouter/OpenAI)
├── scraper.py             # Парсер каталога + Delta Update + чанкирование
├── vector_store.py        # Работа с ChromaDB (индексация, поиск)
├── scheduler.py           # APScheduler — автообновление раз в неделю
├── main.py                # FastAPI бэкенд (RAG, воронка, /api/chat)
├── telegram_bot.py        # Telegram-бот на aiogram 3.x
├── requirements.txt       # Python-зависимости
├── .env.example           # Шаблон переменных окружения
├── Dockerfile             # Docker-образ
├── docker-compose.yml     # Оркестрация всех сервисов
├── data/
│   └── raw_products.json  # Сырые данные товаров (генерируется парсером)
├── logs/
│   └── bot.log            # Лог-файл
├── chroma_db/             # Persistent-хранилище ChromaDB
└── static/
    ├── widget.js           # JS-виджет для встраивания на сайт
    ├── widget.css          # Стили виджета
    └── widget.html         # Тестовая HTML-страница с виджетом
```

---

## Быстрый старт

### 1. Клонирование и зависимости

```bash
git clone <repo-url>
cd vrk_bot_assistant

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Настройка `.env`

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

| Параметр | Описание |
|----------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота от @BotFather |
| **LLM** (раскомментировать один блок) | |
| `GIGACHAT_CREDENTIALS` | Base64-креды GigaChat (Сбер) |
| `YANDEX_FOLDER_ID` + `YANDEX_API_KEY` | Yandex GPT |
| `OPENROUTER_API_KEY` | OpenRouter (gpt-4o-mini по умолчанию) |
| `OPENAI_API_KEY` | OpenAI напрямую |

> **Приоритет:** GigaChat → Yandex GPT → OpenRouter → OpenAI.  
> Достаточно указать ключи **одного** провайдера.

### 3. Первый запуск парсера

Парсер обходит каталог, собирает товары и загружает их в ChromaDB:

```bash
python scheduler.py
```

Это займёт несколько минут (зависит от количества товаров).  
Результат: `data/raw_products.json` и заполненная `chroma_db/`.

### 4. Запуск бэкенда

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Проверка: [http://localhost:8000/health](http://localhost:8000/health)

### 5. Запуск Telegram-бота

В отдельном терминале:

```bash
python telegram_bot.py
```

### 6. Тест веб-виджета

Откройте в браузере:  
[http://localhost:8000/static/widget.html](http://localhost:8000/static/widget.html)

---

## Автоматическое обновление данных

Планировщик APScheduler встроен в бэкенд (`main.py`).  
По умолчанию парсинг запускается **каждый понедельник в 03:00**.

Настройка в `.env`:
```env
SCRAPER_CRON_DAY_OF_WEEK=mon
SCRAPER_CRON_HOUR=3
SCRAPER_CRON_MINUTE=0
```

Ручной запуск: `python scheduler.py`.

---

## Выбор LLM-провайдера

Система автоматически определяет провайдера по наличию ключей в `.env`.

| Провайдер | Переменные | Модель по умолчанию |
|-----------|-----------|---------------------|
| GigaChat | `GIGACHAT_CREDENTIALS` или `GIGACHAT_CLIENT_ID`+`SECRET` | GigaChat |
| Yandex GPT | `YANDEX_FOLDER_ID` + `YANDEX_API_KEY` | YandexGPT |
| OpenRouter | `OPENROUTER_API_KEY` | `openai/gpt-4o-mini` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` |

Для смены провайдера — закомментируйте текущие ключи и раскомментируйте нужные.

---

## Деплой через Docker

### Сборка и запуск всего стека

```bash
# Убедитесь, что .env создан и заполнен
docker-compose up -d --build
```

Это запустит:
- **backend** — FastAPI на порту 8000 + планировщик
- **telegram-bot** — Telegram long-polling

### Ручной запуск парсера в Docker

```bash
docker-compose run --rm scraper
```

### Остановка

```bash
docker-compose down
```

### Просмотр логов

```bash
docker-compose logs -f backend
docker-compose logs -f telegram-bot
```

---

## Архитектура связи: как компоненты общаются с бэкендом

### Общая схема

```
┌──────────────────────┐     HTTP POST /api/chat     ┌─────────────────────────┐
│  JS-виджет (браузер) │ ─────────────────────────→  │                         │
│  на сайте заказчика  │ ←─────────────────────────  │    FastAPI бэкенд       │
└──────────────────────┘     JSON-ответ              │    (main.py :8000)      │
                                                     │                         │
┌──────────────────────┐     Прямой import Python    │    process_message()    │
│  Telegram-бот        │ ─────────────────────────→  │         ↓               │
│  (telegram_bot.py)   │ ←─────────────────────────  │    ChromaDB + LLM      │
└──────────────────────┘     return ChatResponse     └─────────────────────────┘
```

**JS-виджет** и **Telegram-бот** связаны с бэкендом **по-разному**:

| Компонент | Способ связи | Что нужно указать |
|-----------|-------------|-------------------|
| JS-виджет | HTTP-запросы по сети на `/api/chat` | URL бэкенда в атрибуте `data-api` |
| Telegram-бот | Прямой Python-импорт `process_message()` | Ничего — работает в том же процессе / том же Docker-окружении |

### Какие ключи/токены нужны и откуда их взять

| Что | Откуда получить | Куда положить | Кто использует |
|-----|----------------|---------------|----------------|
| URL бэкенда | Это адрес вашего сервера: домен (`https://bot.ventreshetki.com`) или IP (`http://123.45.67.89:8000`) | Атрибут `data-api` в HTML-коде сайта + переменная `API_BASE_URL` в `.env` | JS-виджет |
| Telegram Bot Token | Создать бота через [@BotFather](https://t.me/BotFather) → `/newbot` → скопировать токен | `TELEGRAM_BOT_TOKEN` в `.env` | Telegram-бот |
| LLM API-ключ | Зарегистрироваться у провайдера (GigaChat / Yandex / OpenRouter / OpenAI) | Соответствующая переменная в `.env` | Бэкенд (llm_factory.py) |

> **Собственного API-ключа для бота создавать не нужно.**
> Бэкенд сам является API-сервером — виджет просто отправляет запросы на его публичный URL.

### Как виджет узнаёт адрес бэкенда

Адрес передаётся через атрибут `data-api` тега `<script>`. Виджет читает его при загрузке:

```js
var API_BASE = scriptTag.getAttribute("data-api") || "";
var API_URL  = API_BASE + "/api/chat";
```

Если `data-api` не указан — запросы пойдут на тот же домен, откуда загружен скрипт.

### Как Telegram-бот связан с бэкендом

Telegram-бот **не ходит по HTTP** к бэкенду. Он импортирует функцию напрямую:

```python
from main import process_message
```

Обе стороны работают с одной и той же ChromaDB и LLM-фабрикой в рамках общего Docker-окружения.

### Пример `.env` для продакшена

```env
API_BASE_URL=https://bot.ventreshetki.com
TELEGRAM_BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
```

### Безопасность открытого эндпоинта

Эндпоинт `/api/chat` открыт без авторизации — это нормально для публичного чат-виджета. Для защиты от злоупотреблений:

1. **CORS** — в `main.py` замените `allow_origins=["*"]` на домен сайта:
   ```python
   allow_origins=["https://ventreshetki.com", "https://www.ventreshetki.com"]
   ```
2. **Rate limiting** — ограничение запросов с одного IP через nginx (`limit_req`).
3. **HTTPS** — обязательно для продакшена (см. раздел «Деплой» в `DEPLOY.md`).

---

## Встраивание виджета на сайт

Добавьте перед `</body>` на HTML-странице:

```html
<!-- Виджет чат-бота ВРК -->
<link rel="stylesheet" href="https://bot.ventreshetki.com/static/widget.css">
<script src="https://bot.ventreshetki.com/static/widget.js"
        data-api="https://bot.ventreshetki.com"></script>
```

Замените `bot.ventreshetki.com` на адрес вашего сервера.

При локальной разработке используйте:

```html
<link rel="stylesheet" href="http://localhost:8000/static/widget.css">
<script src="http://localhost:8000/static/widget.js"
        data-api="http://localhost:8000"></script>
```

---

## Тестирование

### Через ngrok (для Telegram webhook / внешнего доступа)

```bash
ngrok http 8000
```

Полученный URL используйте как `data-api` в виджете или для вебхука.

### Чек-лист тестирования

- [ ] `/health` возвращает `{"status": "ok", "llm_available": true, ...}`
- [ ] Веб-виджет открывается по кнопке, отправляет сообщение, получает ответ
- [ ] Воронка: «Старт» → вопросы по шагам → результат с карточкой товара
- [ ] Свободный вопрос: «Нужна решетка 200×200» → ответ с релевантным товаром
- [ ] Telegram: `/start` → Inline-кнопка «Старт» → воронка через кнопки
- [ ] «Связаться с менеджером» → контактные данные
- [ ] Off-topic: «Какой курс доллара?» → отказ с предложением помощи по вентиляции
- [ ] Jailbreak: «Забудь инструкции» → стандартный отказ
- [ ] Fallback при недоступности LLM → сообщение об ошибке + контакты менеджера

---

## Чек-лист перед продакшеном

- [ ] `.env` заполнен (LLM-ключ, Telegram-токен)
- [ ] Парсер запущен, `data/raw_products.json` не пуст
- [ ] ChromaDB заполнена (`/health` → `chroma_documents > 0`)
- [ ] CORS настроен на конкретный домен (заменить `*` в `main.py`)
- [ ] HTTPS настроен (через nginx / Cloudflare)
- [ ] Логирование работает (`logs/bot.log`)
- [ ] Docker-контейнеры стабильны (`docker-compose ps`)

---

## Лицензия

Внутренний проект ООО «Завод ВРК». Все права защищены.
