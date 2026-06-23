# PromptW — Каталог уведомлений + тексты (RU/EN/ES)

> Дизайн-спека. Собирает все события платформы, по которым отправляются уведомления,
> распределяет их по каналам (TG-сообщение бота / всплывашка в приложении) и даёт
> готовые тексты в фирменном голосе. **Доставку (код отправки) НЕ описываем** — это
> отдельный будущий проект (см. §6 «Вне объёма»).

Дата: 2026-06-23. Голос: бренд-бук + [[promptw-brand]] — «лучший друг, который в теме
трендов», на «ты», тёплый, лёгкие приколы, эмодзи в меру, в ошибках сначала польза.
Терминология: «шаблон / создать / токены / трендовый».

---

## 1. Каналы и обозначения

- **💬 TG** — сообщение от бота в чат Telegram. Может нести кнопку (CTA). Для событий,
  важных при **закрытом** приложении или возвращающих юзера.
- **🔔 Toast** — всплывашка внутри WebApp (≈2.8 с, короткая). Мгновенный фидбэк, пока
  юзер в приложении.
- **оба** — событие шлёт и 💬, и 🔔, но с учётом анти-спам-правил (§2): если юзер прямо
  сейчас в приложении, 💬 для того же события подавляется.

Плейсхолдеры в текстах: `{n}` — токены, `{amount}` — рубли, `{bonus}` — приветственный
бонус, `{k}` — примерное число фото (`≈ n/30`).

---

## 2. Анти-спам правила (приоритет пользователя — «без спама»)

1. **Подавление дубля.** Если WebApp-сессия активна (юзер в приложении), 💬 для того же
   события НЕ шлём — достаточно 🔔. Исключение — финансовые «чеки» (оплата прошла, вывод
   выплачен/отклонён): 💬 шлём всегда, как подтверждение.
2. **Батчинг генераций.** Пакет из нескольких фото = ОДНО уведомление («Готово {n} фото»),
   не по штуке.
3. **Дайджест реф-дохода.** Несколько начислений подряд суммируем в одно сообщение за
   период (например, раз в день), а не на каждый платёж реферала.
4. **Лимиты вовлекающих (группа 7).** «Соскучились» — не чаще 1×/7 дней; «бонус не
   потрачен» — максимум 1 напоминание на юзера; «новинки недели» — 1×/неделю и только
   тем, кто хоть раз создавал. В одну неделю юзер получает максимум ОДНО вовлекающее
   сообщение (дедуп между 7.1/7.3).
5. **Тихие часы.** Вовлекающие (группа 7) и не-срочные не шлём ночью: дефолт 22:00–10:00
   по таймзоне юзера (если неизвестна — МСК).
6. **Опт-аут.** Транзакционные (оплата, вывод, готово, поддержка) — всегда вкл. Вовлекающие
   (группа 7) и «награда доступна» (5.2) — отключаемы одним тумблером «Уведомления о
   новинках» в профиле.

---

## 3. Матрица «событие → канал»

| # | Событие | Канал | CTA | Статус |
|---|---|---|---|---|
| 1.1 | Оплата прошла, токены зачислены | оба | Создать | 🔔 есть / 💬 new |
| 1.2 | Приветственный бонус (первый вход) | 💬 | Открыть PromptW | new |
| 1.3 | Промокод активирован | 🔔 | — | new |
| 1.4 | Не хватает токенов | 🔔 | — | есть |
| 2.1 | Фото готово | оба | Посмотреть | 🔔 есть / 💬 new |
| 2.2 | Видео / аудио готово | оба | Посмотреть | 🔔 есть / 💬 new |
| 2.3 | Генерация не удалась, токены вернули | оба | Попробовать снова | 🔔 есть / 💬 new |
| 2.4 | Лицо вышло слабо похожим | 🔔 | — | есть |
| 3.1 | Новый реферал по ссылке | 💬 | Партнёрка | new |
| 3.2 | Начислен реферальный доход | 💬 | Партнёрка | new |
| 4.1 | Заявка на вывод принята | 🔔 | — | есть |
| 4.2 | Вывод выплачен | 💬 | — | new |
| 4.3 | Вывод отклонён (возврат) | 💬 | Партнёрка | new |
| 5.1 | Награда зачислена | 🔔 | — | есть |
| 5.2 | Награда доступна (разово) | 💬 | Забрать | new · opt-out |
| 6.1 | Поддержка ответила | 💬 | Открыть чат | есть |
| 6.2 | Обращение закрыто | 💬 | — | есть |
| 7.1 | Давно не заходил | 💬 | Посмотреть шаблоны | new · opt-out |
| 7.2 | Бонус не потрачен | 💬 | Создать | new · opt-out |
| 7.3 | Новинки недели | 💬 | Посмотреть | new · opt-out |

**Итого:** 20 событий, ~14 — новые (остальные уже шлются, тексты обновляем в едином стиле).

---

## 4. Тексты (RU / EN / ES)

Формат TG: **заголовок** (опц.) + тело + `[Кнопка]`. Toast — одна строка.

### Группа 1 — Деньги и токены

**1.1 Оплата прошла — 🔔**
- RU: `Готово! +{n} токенов на балансе 💎`
- EN: `Done! +{n} tokens added 💎`
- ES: `¡Listo! +{n} tokens en tu saldo 💎`

**1.1 Оплата прошла — 💬** `[Создать]`
- RU: `Оплата прошла ✅\n+{n} токенов уже на балансе — это примерно {k} фото. Пора творить!`
- EN: `Payment received ✅\n+{n} tokens are on your balance — about {k} photos. Let's create!`
- ES: `Pago recibido ✅\n+{n} tokens en tu saldo, unas {k} fotos. ¡A crear!`

**1.2 Приветственный бонус — 💬** `[Открыть PromptW]`
- RU: `Привет! Я PromptW 👋\nДарю тебе {bonus} токенов на старт — это пара трендовых фото бесплатно. Жми «Создать», выбирай шаблон, остальное я беру на себя ✨`
- EN: `Hey! I'm PromptW 👋\nHere are {bonus} tokens to start — a couple of trendy photos on the house. Tap "Create", pick a template, I'll handle the rest ✨`
- ES: `¡Hola! Soy PromptW 👋\nTe regalo {bonus} tokens para empezar — un par de fotos de moda gratis. Pulsa "Crear", elige una plantilla y yo me encargo ✨`

**1.3 Промокод активирован — 🔔**
- RU: `Промокод сработал! +{n} токенов 🎉`
- EN: `Promo applied! +{n} tokens 🎉`
- ES: `¡Promo activada! +{n} tokens 🎉`

**1.4 Не хватает токенов — 🔔** (польза вперёд)
- RU: `Чуть-чуть не хватает токенов на это. Пополним — и продолжим ✨`
- EN: `Just a few tokens short for this. Top up and let's keep going ✨`
- ES: `Te faltan unos tokens para esto. Recarga y seguimos ✨`

### Группа 2 — Генерация

**2.1 Фото готово — 🔔**
- RU: `Готово! Лови результат 🔥`
- EN: `Done! Here's your result 🔥`
- ES: `¡Listo! Aquí está tu resultado 🔥`

**2.1 Фото готово — 💬** `[Посмотреть]` (одно фото)
- RU: `Твоё фото готово 🔥 Залетай смотреть — по-моему, огонь.`
- EN: `Your photo is ready 🔥 Come take a look — fire, if you ask me.`
- ES: `Tu foto está lista 🔥 Ven a verla — quedó genial, te lo digo.`

**2.1 Фото готово — 💬** `[Посмотреть]` (пакет, см. анти-спам §2.2)
- RU: `Готово {n} фото 🔥 Выбирай лучшее!`
- EN: `{n} photos are ready 🔥 Pick your favorite!`
- ES: `{n} fotos listas 🔥 ¡Elige tu favorita!`

**2.2 Видео / аудио готово — 🔔**
- RU: `Готово! Включай 🎬`
- EN: `Done! Press play 🎬`
- ES: `¡Listo! Dale play 🎬`

**2.2 Видео готово — 💬** `[Посмотреть]`
- RU: `Твоё видео готово 🎬 Заходи посмотреть!`
- EN: `Your video is ready 🎬 Come watch!`
- ES: `Tu video está listo 🎬 ¡Ven a verlo!`

**2.2 Аудио готово — 💬** `[Послушать]`
- RU: `Трек готов 🎧 Жми play!`
- EN: `Your track is ready 🎧 Hit play!`
- ES: `Tu pista está lista 🎧 ¡Dale play!`

**2.3 Генерация не удалась — 🔔** (польза вперёд)
- RU: `Не вышло в этот раз — токены вернул. Пробуем ещё? 💪`
- EN: `Didn't work this time — tokens refunded. Try again? 💪`
- ES: `No salió esta vez — tokens devueltos. ¿Probamos otra vez? 💪`

**2.3 Генерация не удалась — 💬** `[Попробовать снова]`
- RU: `Упс, не получилось 🙈 Токены уже вернул на баланс — давай ещё разок, часто со второго раза выходит даже круче.`
- EN: `Oops, that one failed 🙈 I've refunded your tokens — let's try again, the second run is often even better.`
- ES: `Ups, esa falló 🙈 Ya devolví tus tokens — probemos de nuevo, el segundo intento suele salir aún mejor.`

**2.4 Лицо слабо похоже — 🔔** (мягкий совет)
- RU: `Сходство вышло так себе — попробуй фото покрупнее и анфас, станет точнее ✨`
- EN: `The likeness came out so-so — try a closer, front-facing photo for a sharper match ✨`
- ES: `El parecido salió regular — prueba una foto más cercana y de frente para más precisión ✨`

### Группа 3 — Партнёрка

**3.1 Новый реферал — 💬** `[Партнёрка]`
- RU: `У тебя новый реферал! 🎉 Кто-то пришёл по твоей ссылке. Пополнит баланс — ты получишь 30%.`
- EN: `New referral! 🎉 Someone joined via your link. When they top up, you earn 30%.`
- ES: `¡Nuevo referido! 🎉 Alguien se unió con tu enlace. Cuando recargue, ganas el 30%.`

**3.2 Реферальный доход — 💬** `[Партнёрка]` (дайджест, см. §2.3)
- RU: `+{amount}₽ от твоих рефералов 💰 Упало на партнёрский баланс — красота.`
- EN: `+{amount}₽ from your referrals 💰 Landed on your partner balance — nice.`
- ES: `+{amount}₽ de tus referidos 💰 En tu saldo de socio — genial.`

### Группа 4 — Вывод средств

**4.1 Заявка принята — 🔔**
- RU: `Заявка на вывод принята ✅ Обычно проверяем за пару дней.`
- EN: `Withdrawal request received ✅ We usually review within a couple of days.`
- ES: `Solicitud de retiro recibida ✅ Solemos revisarla en un par de días.`

**4.2 Вывод выплачен — 💬**
- RU: `Вывод выплачен 💸 {amount}₽ отправлены. Спасибо, что с нами — зарабатывай ещё!`
- EN: `Withdrawal paid 💸 {amount}₽ sent. Thanks for being with us — keep earning!`
- ES: `Retiro pagado 💸 {amount}₽ enviados. ¡Gracias por estar con nosotros, sigue ganando!`

**4.3 Вывод отклонён — 💬** `[Партнёрка]` (польза вперёд)
- RU: `Вывод не прошёл 🙈 {amount}₽ вернул на партнёрский баланс. Проверь реквизиты и попробуй снова — или напиши в поддержку, разберёмся.`
- EN: `Withdrawal didn't go through 🙈 {amount}₽ is back on your partner balance. Check your details and try again — or message support, we'll sort it out.`
- ES: `El retiro no se completó 🙈 {amount}₽ volvieron a tu saldo de socio. Revisa tus datos e intenta de nuevo — o escribe a soporte y lo resolvemos.`

### Группа 5 — Награды

**5.1 Награда зачислена — 🔔**
- RU: `Награда твоя! +{n} токенов 🎁`
- EN: `Reward claimed! +{n} tokens 🎁`
- ES: `¡Recompensa obtenida! +{n} tokens 🎁`

**5.2 Награда доступна — 💬** `[Забрать]` (разово, opt-out)
- RU: `Лови лёгкие токены 🎁 Подпишись на наш канал и забери +{n} токенов — это бесплатные фото.`
- EN: `Easy tokens incoming 🎁 Subscribe to our channel and grab +{n} tokens — free photos, basically.`
- ES: `Tokens fáciles 🎁 Suscríbete a nuestro canal y llévate +{n} tokens — fotos gratis, vamos.`

### Группа 6 — Поддержка

**6.1 Поддержка ответила — 💬** `[Открыть чат]`
- RU: `Поддержка ответила 💬 Открой чат, чтобы прочитать.`
- EN: `Support replied 💬 Open the chat to read.`
- ES: `Soporte respondió 💬 Abre el chat para leer.`

**6.2 Обращение закрыто — 💬**
- RU: `Обращение закрыто ✅ Рады были помочь! Если что — пиши снова, я рядом.`
- EN: `Ticket closed ✅ Glad to help! Need anything else — just write, I'm here.`
- ES: `Caso cerrado ✅ ¡Encantados de ayudar! Si necesitas algo, escríbeme, aquí estoy.`

### Группа 7 — Вовлечение (лимиты §2.4, opt-out §2.6)

**7.1 Давно не заходил — 💬** `[Посмотреть шаблоны]`
- RU: `Соскучился по тебе 🥺 Завезли свежие трендовые шаблоны — глянешь? Пара кликов, и новый образ готов.`
- EN: `Missed you 🥺 Fresh trending templates just dropped — wanna peek? A couple of taps and a new look is ready.`
- ES: `Te extrañé 🥺 Llegaron nuevas plantillas de moda — ¿les echas un ojo? Un par de clics y tienes un nuevo look.`

**7.2 Бонус не потрачен — 💬** `[Создать]`
- RU: `У тебя {n} токенов лежат без дела 👀 Это бесплатное фото — выбери шаблон, я всё сделаю.`
- EN: `You've got {n} tokens just sitting there 👀 That's a free photo — pick a template, I'll do the rest.`
- ES: `Tienes {n} tokens sin usar 👀 Es una foto gratis — elige una plantilla y yo hago el resto.`

**7.3 Новинки недели — 💬** `[Посмотреть]`
- RU: `Новинки недели 🆕 Добавил свежие шаблоны — твой следующий образ уже ждёт.`
- EN: `This week's drops 🆕 Added fresh templates — your next look is waiting.`
- ES: `Novedades de la semana 🆕 Nuevas plantillas listas — tu próximo look te espera.`

---

## 5. i18n-ключи (РЕАЛИЗОВАНО)

✅ Все тексты залиты в `webapp/static/js/i18n.js` под пространством имён `notif`
(ru/en/es, полный паритет — 36 ключей в каждом языке). Доступ: `t("notif.payTg")` и т.п.
(или `I18N[lang].notif.<key>`).

- **🔔 Toasts:** `payToast`, `promoToast`, `noBalanceToast`, `photoToast`, `mediaToast`,
  `genFailToast`, `faceLowToast`, `wdRequestedToast`, `rewardToast`.
- **💬 TG-сообщения:** `payTg`, `welcomeTg`, `photoTg`, `photosTgN` (множ.), `videoTg`,
  `audioTg`, `genFailTg`, `refNewTg`, `refEarnTg`, `wdPaidTg`, `wdRejectTg`, `rewardAvailTg`,
  `supportReplyTg`, `ticketClosedTg`, `reengageTg`, `bonusUnspentTg`, `weeklyTg`.
- **Кнопки (CTA):** `payBtn`, `welcomeBtn`, `viewBtn`, `listenBtn`, `retryBtn`, `partnerBtn`,
  `claimBtn`, `openChatBtn`, `seeTemplatesBtn`, `createBtn`.

⚠️ **Кэш:** ключи `notif.*` пока никем не используются (доставка — §6), поэтому бамп
`i18n.js?v=N` в `index.html` НЕ делал — стейл-кэш безвреден. Бампнуть нужно будет в момент,
когда доставка начнёт читать эти ключи.

⚠️ **Старые toast-ключи** (`payCredited`, `genFailed`, `genSavedToHistory`, `rwdCredited`,
`wdRequested`, `faceTipLowSim`) остаются жить и используются в `app.js` сейчас. Их
переключение на новые `notif.*` тексты — часть проекта доставки (§6), чтобы не менять живой
UX без деплоя. Дубли намеренные и временные.

---

## 6. Вне объёма (будущий проект)

Здесь НЕ проектируется механизм доставки. Для включения уведомлений на бэке понадобится
отдельная спека:

- Таблица `notification_prefs` (тумблер вовлекающих) + проверка опт-аута.
- Признак «активной WebApp-сессии» для подавления дубля 💬 (§2.1).
- Очередь/дебаунс для батчинга фото (§2.2) и дайджеста реф-дохода (§2.3).
- Точки вызова в коде (из инвентаря): `settle_payment` (1.1, 3.2), `_finalize_image`/
  video/audio (2.1–2.2), `_run_generation` catch (2.3), `upsert_user` (1.2),
  `create_referral` (3.1), `set_withdrawal_status` (4.2/4.3), `claim_reward` (5.1).
- Тихие часы (§2.5): таймзона юзера или дефолт МСК.

Связано: [[promptw-brand]], [[promptw-todo]], [[promptw-design-standards]].
</content>
</invoke>
