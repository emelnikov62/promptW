# Перенос шаблонов генераций в БД

**Дата:** 2026-06-22
**Статус:** утверждён дизайн, ожидает реализации

## Проблема

Данные шаблонов («тренды») целиком лежат инлайном в `webapp/static/js/app.js`
(объект `TRENDS` + константы `*_SKELETON`/`*_PARAMS`/`*_VIDEO_PROMPT`). Замер: **128 КБ
на 13 шаблонов ≈ ~10 КБ/шаблон** — уже половина бандла app.js (265 КБ).

При цели ~500 шаблонов это даёт ~5 МБ данных в JS-бандле, который грузится в каждый
клиент при каждом бампе `?v=`. Последствия:

- Тяжёлый старт WebApp в Telegram на телефоне (загрузка + парсинг большого объекта).
- Любая правка шаблона требует PR + `gitdeploy` + бамп версии.
- Цена шаблона дублируется в `app.js` (`TRENDS[*].cost`) и `pricing.py`
  (`TEMPLATE_COST`) — риск рассинхрона.

Отдельная проблема — рендер галереи из сотен карточек разом — решается ленивой
загрузкой и не зависит от места хранения данных.

## Цель

Вынести **данные** шаблонов в БД, оставив **логику движка** в коде. Клиент грузит
лёгкий список и подтягивает полное определение шаблона лениво при открытии. Цена —
серверный источник правды. Редактирование — через существующую админку без деплоя;
git-сид досыпает только новые шаблоны.

## Решения (подтверждены владельцем)

- **Управление:** сид-файл в репо (версионируемая база) + редактирование через
  существующую браузерную админку (`webapp/templates/admin.html`).
- **Источник правды при конфликте:** **админка выше**. Сид при старте делает
  `INSERT … ON CONFLICT (id) DO NOTHING` — раз шаблон в БД, деплой его не трогает;
  правки через админку не теряются. Сид = первоначальный бутстрап + добавление новых.
- **Движок остаётся в `app.js`:** `buildTplPrompt`, `renderTplParams`, `tplGenerate`
  не меняют логику — берут `definition` из API вместо инлайн-`TRENDS`.
- **Без хардкод-фолбэка** на клиенте: если список не загрузился — состояние «повторить»
  (иначе теряется смысл выноса из бандла).

## Архитектура

### 1. Таблица `templates`

Тонкие колонки для списка (грузят все клиенты) + JSONB-блобы для тяжёлого.

| Колонка | Тип | Назначение |
|---|---|---|
| `id` | TEXT PRIMARY KEY | слаг (`birthday-photo`) = `tplId` для биллинга |
| `type` | TEXT NOT NULL | `photo` / `video` / `audio` |
| `enabled` | BOOLEAN DEFAULT TRUE | скрыть/показать без удаления |
| `sort_order` | INT DEFAULT 0 | порядок в галерее |
| `category` | TEXT NULL | под будущую группировку/поиск (UI вне scope v1) |
| `cost` | INT NOT NULL | цена в токенах — серверный источник правды |
| `title` | JSONB | `{ru,en,es}` — нужно в списке |
| `preview` | JSONB | `{img, video}` пути в `/static/tpl/` — нужно в списке |
| `definition` | JSONB | всё тяжёлое (см. ниже) |
| `created_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `updated_at` | TIMESTAMPTZ DEFAULT NOW() | |

`definition` JSONB содержит ровно ту форму, что сейчас в записи `TRENDS[id]`, минус
вынесенные колонки:

```
{
  "model": "Seedance 2.0",
  "settings": { "ratio":"9:16","quality":"480p","mode":"fast","duration":10,
                "sound":true,"minPhotos":1,"maxPhotos":1,"refField":"ref-images",
                "needPhoto":true,"hidePrompt":true },
  "desc":     { "ru":"…","en":"…","es":"…" },
  "skeleton": "…{subject}…{hair}…{outfit}…{age}…",   // для параметризованных фото
  "prompt":   "…",                                    // для hidePrompt-видео
  "params":   [ … ]                                   // каталоги пол/возраст/одежда/причёска
}
```

Создаётся идемпотентно в `db/database.py::init_db` (`CREATE TABLE IF NOT EXISTS`),
как остальные таблицы.

### 2. API

**Юзерские** (`api/routes.py`, под общим `auth_middleware`, который уже гейтит `/api/`):

- `GET /api/templates` → массив enabled-шаблонов, **только лёгкие поля**
  (`id, type, cost, sort_order, category, title, preview`), сортировка по `sort_order`.
  Опционально `?type=photo`.
- `GET /api/templates/{id}` → полный объект (лёгкие поля + `definition`), только enabled.
  404 если нет/выключен.

**Админские** (`api/admin_routes.py`, гейт `_require_admin`, запись через `_audit`):

- `GET /api/admin/templates` — список вкл. disabled, все поля, пагинация (limit/offset).
- `GET /api/admin/templates/{id}` — полная строка.
- `POST /api/admin/templates` — создать (валидация `id`, `type`, `cost`, обязательных полей).
- `PUT /api/admin/templates/{id}` — обновить, `_audit` пишет before/after, `updated_at=NOW()`.
- `DELETE /api/admin/templates/{id}` — удалить, `_audit`.

Сериализация JSONB → `json.loads` на чтении (паттерн проекта), `_row`/`_serialize`
для дат/Decimal.

### 3. Клиент (`webapp/static/js/app.js`)

- **Удалить** из бандла `TRENDS` и все `*_SKELETON/*_PARAMS/*_VIDEO_PROMPT` (~128 КБ).
- Состояние: `var templatesList = []; var templateCache = {};`
- Открытие галереи шаблонов: `GET /api/templates` → рендер карточек из лёгкого списка,
  превью с `loading="lazy"`. Состояния loading / empty / error(retry).
- Открытие шаблона (`showTplDetail`): `fetchTemplate(id)` — если в `templateCache` нет,
  `GET /api/templates/{id}` → кэш. Объект имеет **ту же форму**, что сейчас `TRENDS[id]`,
  поэтому `buildTplPrompt` / `renderTplParams` / `tplGenerate` берут его без изменения
  логики (меняется только источник данных).
- Detail-фетч упал → тост, галерея остаётся.

### 4. Цена — серверный источник правды

`pricing.py::compute_cost` сейчас синхронно читает хардкод `TEMPLATE_COST[tpl_id]`.
Меняем источник словаря, **сохраняя синхронную сигнатуру и анти-форж-свойство**:

- При старте грузим `{id: cost}` из таблицы `templates` в модульный кэш
  `TEMPLATE_COST` (вместо хардкода).
- Админские write-эндпоинты (`POST/PUT/DELETE`) обновляют этот кэш (или сервер
  перечитывает строку по `tplId` — один индексированный PK-lookup на генерацию тоже
  допустим). По умолчанию — кэш + обновление на write.
- Клиентский `cost` остаётся только для отображения; генерация с неизвестным `tplId`
  отклоняется (нечем тарифицировать) — поведение как сейчас.

### 5. Сид + миграция

- Файл `db/templates_seed.py` (Python-словарь) — текущие 13 шаблонов как данные,
  перенесённые из `TRENDS` вручную и сверенные.
- В `init_db` после создания таблицы: для каждой записи сида
  `INSERT … ON CONFLICT (id) DO NOTHING`. Выполняется при каждом старте, но вставляет
  только отсутствующие `id` → правки из админки сохраняются.
- Медиа `static/tpl/` не трогаем; сид ссылается на существующие пути.

### 6. Админ-UI (`admin.html` + `admin.js`)

Новый раздел «Шаблоны» в существующем паттерне (`navItems` → `showSection` → `load*`):

- Таблица шаблонов: id, тип, категория, цена, enabled, порядок; пагинация.
- Модалка редактирования: лёгкие поля + JSON-textarea для `definition`/`title`/`preview`
  (v1 — редактирование JSON напрямую; формы под params — follow-up). Сохранение → `PUT`.
- Кнопки: создать, удалить, вкл/выкл.
- v1: превью задаётся путём-строкой; аплоад картинок — follow-up.

## Поток данных

```
Старт сервера → init_db: CREATE TABLE templates; сид INSERT ON CONFLICT DO NOTHING;
                pricing загружает {id:cost} из templates.
Юзер открыл галерею → GET /api/templates (лёгкий список) → карточки (lazy img).
Юзер открыл шаблон  → GET /api/templates/{id} → templateCache → buildTplPrompt/params.
Юзер сгенерил      → POST /api/generate/* (tplId) → compute_cost из кэша БД → charge.
Админ правит        → PUT /api/admin/templates/{id} → _audit + updated_at + обновить кэш цены.
Деплой нового сида  → INSERT ON CONFLICT DO NOTHING (старые в БД не тронуты).
```

## Обработка ошибок

- Клиент: список не загрузился → retry-состояние (без хардкод-фолбэка). Detail упал → тост.
- Сервер: шаблон отсутствует/disabled у юзера → 404. Неизвестный `tplId` при генерации →
  отказ. Админ-валидация: дубль `id` на create → 409; пустой `type`/`cost` → 400.

## Верификация

1. **Паритет промтов:** для всех 13 шаблонов до/после миграции `buildTplPrompt` даёт
   идентичный результат (матрица пол×возраст×одежда×причёска). Сверка офлайн в консоли.
2. **Лёгкость списка:** `GET /api/templates` отдаёт payload без `definition`
   (проверить, что тяжёлые поля не утекают в список).
3. **Ленивый detail:** открытие шаблона делает один GET, повторное — из кэша (без сети).
4. **Цена с сервера:** изменение `cost` в БД → генерация списывает новую сумму без
   деплоя фронта; неизвестный `tplId` отклоняется.
5. **Сид не затирает админку:** изменить шаблон через админку → перезапуск/деплой →
   правка на месте (ON CONFLICT DO NOTHING).
6. **Бандл:** app.js после удаления `TRENDS` ≈ вдвое легче; бамп `?v=`, деплой через PR.

## Вне scope v1 (follow-up)

- Юзерские табы категорий / поиск (схема `category`/`sort_order` готова, UI позже).
- Загрузка превью-картинок через админку (v1 — путь-строка).
- Формы редактирования `params` в админке (v1 — JSON-textarea).
- i18n админки (админка уже RU-only).

## Затрагиваемые файлы

- `db/database.py` — `CREATE TABLE templates` + вызов сида в `init_db`.
- `db/templates_seed.py` — **новый**, данные 13 шаблонов.
- `db/queries.py` — CRUD-запросы шаблонов (list light / get full / upsert / delete).
- `api/routes.py` — `GET /api/templates`, `GET /api/templates/{id}`.
- `api/admin_routes.py` — админский CRUD шаблонов + аудит.
- `pricing.py` — `TEMPLATE_COST` грузится из БД, не хардкод.
- `webapp/static/js/app.js` — удалить `TRENDS`/константы; `fetchTemplate` + ленивый рендер.
- `webapp/templates/admin.html`, `webapp/static/js/admin.js` — раздел «Шаблоны».
- `webapp/templates/index.html` — бамп `?v=` изменённых ассетов.
