# news_parser

Микросервис сбора новостей с собственной БД и REST API. Первый источник:
<https://fsp-russia.ru/press/news/>. Календарь мероприятий не парсится.
Изображения сохраняются в `file_service`, подготовленные новости можно экспортировать
в `news_service` через его существующие HTTP-эндпоинты.

## Структура

```text
src/app/            API, worker, модели, клиенты сервисов и адаптеры сайтов
migrations/         Миграции Alembic и начальный источник ФСП
tests/              Автономные тесты и синтетические HTML-фикстуры
tests/integration/  Явно запускаемые проверки сайта и работающего file_service
Dockerfile          Образ микросервиса
compose.yml         Парсер, worker, миграции и PostgreSQL парсера
alembic.ini         Настройки миграций
pyproject.toml      Настройки pytest и Ruff
requirements*.txt   Зависимости приложения и тестов
.env.example        Пример конфигурации
```

Как и в соседних сервисах, приложение находится в `src/app`. Репозиторий самодостаточен:
для установки, запуска и автономных тестов папка `dev` не нужна.
В соседнем `../dev` находятся только демонстрационный отчёт, совместный тестовый стенд,
логи и сохранённая тестовая БД. Инструкции для них — в `../dev/README.md`.
Локальные `.env` и `.venv` исключены из Git.

## Локальный запуск

Python 3.12+. Все команды выполняются из `news_parser` (PowerShell):

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
# Только при первом запуске, если .env ещё нет:
Copy-Item .env.example .env
```

В `.env` задайте длинный случайный `API_KEY` и параметры сервисов.
Для локального запуска поддерживается SQLite, для развёртывания — PostgreSQL.
Затем примените миграции и запустите API:

```powershell
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn app.main:app --app-dir src --host 127.0.0.1 --port 8002
```

В отдельном терминале из той же папки:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.venv/Scripts/python -m app.worker
```

`python -m app.worker --once` обрабатывает максимум одно задание и завершает процесс.
API сам таблицы не создаёт. Swagger — <http://127.0.0.1:8002/docs>;
в **Authorize** укажите `API_KEY`. Все `/api/*` требуют заголовок `X-API-Key`.

Миграция добавляет источник ФСП с ID `c9d743f6-c603-4d37-a911-51f98e718c60`.
Пример тела `POST /api/parse-jobs`:

```json
{"source_id":"c9d743f6-c603-4d37-a911-51f98e718c60","limit":5,"max_pages":1}
```

Ответ 202 содержит ID задания. Передача массива `urls` запускает разбор конкретных
статей (до 500 URL); `limit` и `max_pages` ограничивают только поиск по списку.

## Docker / PostgreSQL

Задайте в `.env` `API_KEY` и `POSTGRES_PASSWORD`, затем:

```powershell
docker compose up --build -d
docker compose logs -f worker
```

Пароль PostgreSQL используется в URI: используйте буквы/цифры либо URL-кодирование.
Compose переопределяет `DATABASE_URL`, сохраняет данные в томе `parser-db` и публикует
API только на `127.0.0.1:8002`. БД наружу не публикуется. Код других сервисов не требуется.
Адреса соседних сервисов должны быть доступны из контейнера; для сервиса на хосте можно
использовать `host.docker.internal`. `FILE_PUBLIC_URL` должен быть доступен потребителю API.

## API

| Метод и путь | Назначение |
|---|---|
| `GET /health`, `GET /ready` | Процесс жив / БД, миграции и ключ настроены |
| `GET /api/adapters` | Доступные адаптеры |
| `GET/POST /api/sources` | Получить / добавить источник |
| `PATCH /api/sources/{id}` | Название и активность источника |
| `POST /api/parse-jobs` | Запустить парсинг |
| `GET /api/parse-jobs/{id}` | Статус задания, алиас `/api/jobs/{id}` |
| `GET /api/jobs` | История заданий с пагинацией |
| `POST /api/jobs/{id}/retry` | Повтор частичного или неудачного задания |
| `GET /api/articles` | Список с пагинацией и фильтрами |
| `GET /api/articles/{id}` | Новость с изображениями и результатом экспорта |
| `PATCH /api/articles/{id}` | Редактирование `title` / `body` |
| `POST /api/articles/{id}/images/retry` | Повтор загрузки изображений |
| `GET /api/articles/{id}/export` | JSON для ручной интеграции |
| `POST /api/articles/{id}/export` | Экспортировать черновик, тело `{"tags":[]}` |
| `POST /api/articles/{id}/export/reconcile` | Уточнить результат неоднозначного создания |
| `POST /api/images/{id}/reconcile` | Уточнить результат неоднозначной загрузки |

Списки используют `page` и `size`. Новости можно фильтровать по `source_id`, `q`,
`published_from`, `published_to`. Полные схемы доступны в Swagger.
Текст — `body_format=plain_text`, изображения — отдельным массивом, первое является
обложкой. Исходная дата публикации хранится в `source_published_at`; даты API — UTC.
Ручные правки текста не перезаписываются перепарсингом.

## Обработка и восстановление

Задания хранятся в БД, обработка выполняется отдельным worker.
Статусы: `queued → running → completed / partial / failed`.
`result.article_ids` содержит сохранённые новости, `result.errors` — ошибки отдельных
статей или изображений. При недоступном файловом сервисе текст сохраняется, загрузку
картинок можно повторить позднее. Дубликаты отсеиваются по источнику и каноническому URL.

Поддерживается один worker: PostgreSQL advisory lock или блокировка файла для SQLite.
После перезапуска прерванный парсинг возвращается в очередь. Встроенного расписания нет:
внешний планировщик может вызывать тот же API.

Если ответ на POST потерян и внешний сервис мог сохранить объект, состояние становится
`unknown`. Автоматического повтора нет: оператор проверяет результат и вызывает
`reconcile` с `{"remote_id":"UUID"}` либо `{"confirmed_absent":true}` после подтверждения,
что объект не создан. Затем можно повторить задание. Гарантии exactly-once между
сервисами нет. Файлы автоматически не удаляются; неактуальные изображения помечаются
`active=false`, чтобы не ломать ссылки потребителей.

## Интеграции

**file_service:** `POST /internal/upload` с `X-Internal-Token`, multipart `file` и
`is_public=true`; чтение — `GET /files/{file_id}`. Постоянные бинарные данные находятся
только в файловом сервисе. JPEG/PNG/WebP преобразуются в WebP до 1920×1080;
исходник по умолчанию до 20 МиБ, результат до 5 МиБ. SVG и анимация не принимаются.
Для загрузки нужен явно выданный `FILE_SERVICE_TOKEN`, зарегистрированный в конфигурации
файлового сервиса. Парсер чужие токены не заимствует и его конфигурацию не изменяет.

**news_service:** экспорт отключён по умолчанию. Предпросмотр выдаёт `title`, `body`,
`status=draft`, `tags`, `published_at=null`. Теги — UUID уже существующих тегов CMS.
При разрешённом экспорте используются `POST /api/news` и `PUT /api/news/{id}/cover`.
Готовый `cover_image_id` контракт не принимает: news_service сохраняет собственную копию.
Остальные изображения доступны через API парсера; отдельного API галереи в CMS нет.
Уже экспортированные новости не обновляются и автоматически не публикуются.
Если обложка не загрузилась после создания черновика, повтор использует сохранённый `news_id`.

Режим `APP_MODE=PUBLIC_MVP` в news_service запрещает запись и возвращает 404.
`NEWS_EXPORT_ENABLED=true` этого ограничения не обходит. Для разрешённого экземпляра
настройте административный `NEWS_BEARER_TOKEN` через gateway либо, только в доверенной
внутренней сети, `NEWS_USER_ID` и `NEWS_USER_ROLES`. Сервис доверяет заголовкам идентичности;
публичный доступ к его управлению должен контролировать gateway.

## Источники и ограничения

Загрузчик ограничивает время, объём и частоту запросов, проверяет домен и публичность IP
на каждом редиректе и закрепляет проверенный IP при соединении. Прокси из окружения
не используются. Повторы GET ограничены.
У ФСП обложка берётся из карточки списка. Пустая ссылка «Больше событий» не считается
пагинацией. Полная историческая выгрузка списком не гарантируется; для отдельного старого
URL доступны изображения самой статьи. Изменение HTML-селекторов отражается ошибкой задания.

Для нового сайта реализуйте интерфейс `src/app/adapters/base.py`, задайте разрешённые
домены и методы проверки URL, `discover()` и `parse()`. Зарегистрируйте адаптер в `ADAPTERS`,
добавьте источник через API и синтетические фикстуры с тестами.

## Проверки

```powershell
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check src migrations tests
.venv/Scripts/alembic check
```

Обычный запуск использует отдельную SQLite и HTTP-заглушки; живые проверки пропускаются.
Включить проверку ФСП:

```powershell
$env:RUN_LIVE_TESTS = "1"
.venv/Scripts/python -m pytest tests/integration/test_fsp_live.py -q
```

Проверить изображения завершённого задания и цикл загрузки/чтения/удаления отдельной
новой тестовой картинки в работающем file_service:

```powershell
$env:RUN_FILE_SERVICE_TESTS = "1"
$env:PARSER_TEST_JOB_ID = "UUID-завершённого-задания"
.venv/Scripts/python -m pytest tests/integration/test_file_service_live.py -q
```

Ключи читаются из `.env`; `PARSER_API_URL` при необходимости переопределяет
`http://127.0.0.1:8002`. После проверок удалите флаги из окружения:

```powershell
Remove-Item Env:RUN_LIVE_TESTS -ErrorAction SilentlyContinue
Remove-Item Env:RUN_FILE_SERVICE_TESTS -ErrorAction SilentlyContinue
```
