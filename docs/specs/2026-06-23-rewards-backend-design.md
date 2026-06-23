# PromptW — «Награды»: рабочий бэкенд (дизайн)

> Дата: 2026-06-23. Доводим систему «Награды» от фронт-стаба (localStorage +
> тосты-заглушки) до рабочего бэкенда: серверная проверка подписки на каналы
> (`getChatMember`), идемпотентное начисление токенов, сервер — источник правды.
> Связано: [[promptw-todo]] #Rewards, [[promptw-payments-security-todo]] #4.

## Цель
1. Реальное начисление токенов за награды (а не отметка в localStorage).
2. Серверная проверка **подписки на каналы** через Bot API `getChatMember`.
3. Идемпотентность (одна награда — один раз на `tg_id`), атомарность.
4. Сервер — источник правды о выполненных наградах (переживает смену устройства).
5. Share-задача (+100): `shareToStory` + honor-based начисление (вариант A).

## Не-цели (вне скоупа, отдельные итерации)
- Верификация факта публикации сторис — **технически невозможна** в Telegram
  (нет Bot API для чтения сторис/постов юзера, нет callback после `shareToStory`).
- Реферальная атрибуция share (вариант B) и админ-апрув скриншота (вариант C).
- Защита от subscribe→claim→unsubscribe (награда выдаётся один раз — приемлемо).

## Архитектура

### Данные — `db/database.py` (`_safe_migrations`)
```sql
CREATE TABLE IF NOT EXISTS reward_claims (
  id BIGSERIAL PRIMARY KEY,
  user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
  reward_id  TEXT  NOT NULL,
  amount     INT   NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_tg_id, reward_id)
);
```
`UNIQUE(user_tg_id, reward_id)` = идемпотентность на уровне БД (как
`UNIQUE(promo_id,user_tg_id)` у промо).

### Конфиг наград — сервер (`api/routes.py` или `rewards.py`)
```python
REWARDS = {
  "share":   {"type": "story",        "amount": 100},
  "tg":      {"type": "subscription", "amount": 10, "chat": os.getenv("RWD_TG_CHANNEL", "")},
  "prompts": {"type": "subscription", "amount": 10, "chat": os.getenv("RWD_PROMPTS_CHANNEL", "")},
}
```
- Каналы — из `.env` (не в git): `RWD_TG_CHANNEL`, `RWD_PROMPTS_CHANNEL`
  (формат `@username` или числовой `-100…`).
- Пустой `chat` → награда `configured=false` → карточка скрыта на фронте.
  Включение = вписать `@username` в прод-`.env` + рестарт, **без передеплоя кода**.
- ⚠️ Предпосылка: бот должен быть **админом** канала, иначе `getChatMember`
  вернёт ошибку (тогда — `check_failed`, без начисления).

### Начисление — `db/queries.py`
```python
async def claim_reward(tg_id, reward_id, amount) -> dict
```
В одной транзакции: `INSERT INTO reward_claims … ON CONFLICT DO NOTHING RETURNING id`.
- Вставилось → `UPDATE users SET balance = balance + amount` +
  `INSERT transactions (tx_type='bonus', description='reward:<id>')` →
  `{"credited": amount, "balance": new}`.
- Конфликт (уже есть) → `{"already": True, "balance": cur}`.

`claimed_rewards(tg_id) -> set[str]` — список уже полученных (для `GET /api/rewards`).

### Bot API — проверка подписки
Прямой GET (как `savePreparedInlineMessage`/`Bot(BOT_TOKEN)` уже в routes.py):
`https://api.telegram.org/bot{BOT_TOKEN}/getChatMember?chat_id={chat}&user_id={tg_id}`,
`aiohttp` с bounded timeout. Подписан, если `result.status ∈
{member, administrator, creator}`. `restricted` с `is_member=true` тоже считается
подписанным. `left/kicked` или ошибка API → не начислять.

### Эндпоинты — `api/routes.py`
- **`GET /api/rewards`** (authed) → `[{id, amount, type, configured, claimed,
  channel_link?}]`. `channel_link` для subscription = `https://t.me/<username>`.
- **`POST /api/rewards/claim {reward_id}`** (authed, rate-limit ~10/60s/user):
  - reward_id неизвестен → 400 `bad_reward`.
  - уже получено → `{ok:true, already:true, balance}`.
  - `subscription`: `chat` пуст → 400 `not_configured`; иначе `getChatMember` →
    подписан → `claim_reward` → `{ok, credited, balance}`; не подписан → 400
    `not_subscribed`; ошибка Bot API → 400 `check_failed`.
  - `story`: honor → `claim_reward` сразу → `{ok, credited, balance}`.

### Фронт — `webapp/static/js/app.js` + `index.html`
- `loadRewards()` при открытии страницы (`showPage("rewards")`): `GET /api/rewards`
  → рендер «✓ Получено» по серверному `claimed`, скрытие `configured=false`,
  пульс-точка (`updateRewardsDot`) по серверному состоянию.
- **Подписка**: «Открыть канал» (ссылка с сервера) + «Проверить» →
  `POST /api/rewards/claim` → тост `not_subscribed`/`+10`/`already`.
- **Share**: тап → `tg.shareToStory(mediaUrl, {text, widget_link:{url:refLink,
  name:"PromptW"}})` → затем `claim {reward_id:"share"}` → `+100`.
  (У не-премиум Telegram опустит `widget_link`, бренд на картинке остаётся.)
- На `credited`: `applyBalanceDelta(+amount)`, перерисовка состояния, обновление
  точки. **localStorage-логика (`rwdGetDone`/`rwdMarkDone`) заменяется серверной**
  (источник правды — сервер).
- i18n: добавить ключи статусов (`rwdNotSubscribed`, `rwdCredited`,
  `rwdAlready`, `rwdCheckFail`) ru/en/es.

### Брендовая сторис-картинка
Сделать **с нуля** брендовый кадр 9:16 (≥720×1280) для `shareToStory` →
`webapp/static/img/share-story.jpg` (или `.png`). Тёплый уголь + бренд-градиент
коралл→амбер (дизайн-система Studio), марка «PromptW», монета «W», хэндл
`@promptW_bot` (читаемый, т.к. ссылку-виджет Telegram у не-премиум опустит),
короткий слоган. `mediaUrl = WEBAPP_URL + "/static/img/share-story.jpg"`.

## Поток данных (claim подписки)
1. Юзер жмёт «Проверить» → `POST /api/rewards/claim {reward_id:"tg"}` (с initData).
2. Сервер: reward в конфиге, `chat` настроен, не получено ранее.
3. `getChatMember(@channel, tg_id)` → `member` → подписан.
4. `claim_reward` (атомарно): +10 к балансу, строка transactions, reward_claims.
5. Ответ `{ok, credited:10, balance}` → фронт: тост, `applyBalanceDelta(+10)`,
   карточка → «✓ Получено».

## Обработка ошибок
- Bot API недоступен/таймаут/бот-не-админ → `check_failed` (не начисляем, тост
  «не удалось проверить, попробуй позже»).
- Гонка двойного claim → второй ловит `ON CONFLICT`/возвращает `already` (как
  промо TOCTOU). Без двойного начисления.
- Канал не настроен → карточка скрыта, прямой claim → `not_configured`.

## Тестирование
- Idemпотентность: повторный claim не двоит баланс (live-тест на фейк-id с
  teardown, как реф-аудит).
- `getChatMember`: подписан/не подписан/бот-не-админ — на реальном канале, когда
  появится (до этого — мок/ручная проверка маршрута).
- Share honor: claim один раз, повтор → `already`.
- Сервер-truth: claimed переживает «очистку localStorage».

## Деплой
Ветка → PR → merge → `srv2.sh gitdeploy`. `.env`: добавить (когда каналы готовы)
`RWD_TG_CHANNEL`/`RWD_PROMPTS_CHANNEL` (прод-гейт, ручная правка владельцем).
Бамп `?v` для app.js/i18n.js. `reward_claims` создаётся миграцией при старте.

## Чеклист приёмки
- [ ] `reward_claims` создаётся, UNIQUE работает.
- [ ] `GET /api/rewards` отдаёт configured/claimed корректно.
- [ ] `claim` subscription: начисляет при подписке, отказ без, идемпотентно.
- [ ] `claim` story: honor-начисление один раз.
- [ ] Фронт: серверное состояние, скрытие ненастроенных, share через shareToStory.
- [ ] Брендовая сторис-картинка в `static/img/`.
- [ ] i18n ru/en/es паритет.
- [ ] Live-тест идемпотентности (фейк-id + teardown).
