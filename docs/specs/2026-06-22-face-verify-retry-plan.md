# Face-verify + авто-ретрай — план реализации (статус)

Сопровождает спеку `2026-06-22-face-verify-retry-design.md`. Отмечает, что уже
написано в коде и что осталось на деплой/калибровку.

## Сделано в коде (готово к PR)

- **`face_verify.py`** (new) — InsightFace `buffalo_l`, lazy-singleton, `embed`/`similarity`
  + async `aembed`/`aavailable`, **fail-open** (нет пакета/модели → `available()=False`).
  Модель из `FACE_MODEL_ROOT` (gitignored). Проверено: без `insightface` фича молча off.
- **`requirements.txt`** — `insightface==0.7.3`, `onnxruntime==1.20.1`.
- **`.gitignore`** — `models/`. **`.env.example`** — блок `FACE_*` (фича off по умолчанию).
- **`db/database.py`** — идемпотентная миграция: `face_attempts`, `face_score`,
  `face_accepted`, `face_ref_found`, `face_scores`, `face_threshold` (все nullable).
- **`db/queries.py`** — `set_generation_task_ids`, `record_face_verify`,
  `get_face_verify_stats(period)`, `get_face_verify_by_template(period)`.
- **`api/routes.py`** — `_face_verify_mode` (гейт), `_media_bytes`, `_ref_face_embedding`,
  `_generate_one_image`, `_finalize_image`, `_run_image_generation` (петля best-of:
  эмбеддинг рефа один раз → попытки с тайм-бюджетом → выбор best → удаление непринятых →
  схлопывание task-ids → finalize → запись телеметрии → `face_tip`). `generate_image::_build`
  теперь зовёт `_run_image_generation`. Ошибка ретрай-попытки не теряет лучшую (refund
  только если упала ПЕРВАЯ попытка).
- **`main.py`** — warmup модели на старте, только если фича включена.
- **`api/admin_routes.py`** — `GET /api/admin/face-stats?period=` (под `admin_scope`),
  потери ₽ = `extra_attempts × FACE_VERIFY_RETRY_UNIT_COST`.
- **Админ-фронт** — секция «Сходство лиц» (`admin.html` nav + `admin.js` `loadFace` с
  фильтром периода, KPI, бары попыток/score, таблица по шаблонам; `admin.css` стили).
  Версии бампнуты: `admin.css?v=6`, `admin.js?v=10`.
- **WebApp** — `app.js` показывает `face_tip` мягким тостом; `i18n.js` ключ `faceTipLowSim`
  ru/en/es. Версии: `app.js?v=141`, `i18n.js?v=102`.

Проверки: `py_compile` всех Python-файлов — OK; `node --check` всех JS — OK; fail-open — OK.

## Осталось (деплой / владелец)

1. На VPS: `pip install -r requirements.txt`, затем предзагрузить модель одной командой —
   `set -a && . ./.env && set +a && venv/bin/python tools_face_warmup.py` (скачает `buffalo_l`
   в gitignored `FACE_MODEL_ROOT` и проверит, что модель грузится; exit 0 = ready).
2. Проверить RAM (буфало_l ок — подтверждено).
3. Этап shadow: `.env` `FACE_VERIFY_SHADOW=1`, `FACE_VERIFY=0` → собрать `face_scores`
   без трат на ретраи.
4. По дашборду «Сходство лиц» выставить `FACE_VERIFY_THRESHOLD`, задать
   `FACE_VERIFY_RETRY_UNIT_COST` (₽ за один прогон) для подсчёта потерь.
5. Включить `FACE_VERIFY=1` (ретраи), мониторить retry-rate и потери.

## Порядок PR (каждый деплоится при FACE_VERIFY=0)

- PR-1: `face_verify.py` + requirements + .gitignore + .env.example + миграция + queries.
- PR-2: ядро `api/routes.py` + warmup в `main.py`.
- PR-3: админка (`admin_routes.py` + admin front) + WebApp `face_tip` (app.js/i18n.js).

Лендить через `git worktree`, не смешивать с чужими правками (урок PR #57/#60).
