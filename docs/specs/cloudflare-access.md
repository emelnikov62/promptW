# ТЗ: доступ к Mini App из Европы/Украины через Cloudflare

## Контекст и цель
Бот `@promptW_bot`, веб-приложение на домене **`promptw.ru`**. Домен указывает напрямую на
российский IP **`45.147.177.237`** (Beget, nginx, валидный Let's Encrypt). Из **Украины и части
Европы** приложение **не открывается**: тамошние провайдеры режут российские IP-подсети. Сервер
при этом жив и доступен из не-RU сетей (проверено: `https://promptw.ru/api/health` отвечает
`{"status":"ok"}` с зарубежной точки).

**Цель:** завести `promptw.ru` за Cloudflare, чтобы пользователи подключались к НЕ-российскому
edge-IP Cloudflare (а тот уже тянет с origin по своей сети). Это снимает страновую блокировку
RU-IP. **При этом доступ из РФ должен сохраниться** (РФ — основная аудитория).

> Ограничение Telegram: у Mini App **один URL для всех** — нельзя дать россиянам один адрес, а
> иностранцам другой на уровне кнопки. Поэтому фронт (CF) общий для всех. Базовый план — пустить
> всех через Cloudflare; если из РФ просядет — см. раздел «План Б: GeoDNS».

---

## Часть 1. Cloudflare (основное)

1. Создать аккаунт на **cloudflare.com**, план **Free**. Add a site → `promptw.ru`.
2. Cloudflare просканит DNS. Проверить запись: `A  promptw.ru → 45.147.177.237`, режим **Proxied**
   (оранжевое облако). Если есть `www` — тоже Proxied.
3. У регистратора домена сменить **NS-серверы** на выданные Cloudflare. Дождаться статуса **Active**.
4. **SSL/TLS → Overview → Full (strict)** (на origin валидный Let's Encrypt — проверено, подойдёт).
5. **SSL/TLS → Edge Certificates → Always Use HTTPS: On.**
6. **⚠️ SSL/TLS → Edge Certificates → Encrypted Client Hello (ECH): OFF.**
   Это главный триггер троттлинга Cloudflare со стороны РКН (эпизод 2024 г.). С выключенным ECH
   Cloudflare в РФ работает стабильно.

## Часть 2. Критично для Telegram и платежей (иначе сломается бот/оплата)
Вебхук Telegram и колбэки платёжек — это server-to-server запросы, JS-капчу Cloudflare они **не
пройдут**.

7. **Security → Bots → Bot Fight Mode = OFF.** Не включать режим «Under Attack».
8. **Security → WAF → Custom rules** → правило **Skip** (Skip → All remaining custom rules + Bot
   Fight Mode) для путей (URI Path начинается с одного из):
   - `/webhook`
   - `/webhook-support`
   - `/api/callback`
   - `/api/pay/yoomoney`
   - `/api/pay/platega`
   - `/api/media-proxy`
   - `/api/health`

## Часть 3. Восстановить реальный IP посетителя (на сервере)
За Cloudflare все запросы приходят с IP Cloudflare. Сервер ключует rate-limit по реальному IP
клиента (последний hop `X-Forwarded-For`, `TRUST_XFF=1`). Без правки лимиты и логи будут видеть
только Cloudflare → ложные блокировки/слепые логи. Cloudflare присылает реальный IP в заголовке
`CF-Connecting-IP`.

9. В nginx-конфиге `/etc/nginx/sites-available/promptw` в блоке `location /` заменить строки
   проксирования IP на использование `CF-Connecting-IP`:
   ```nginx
   proxy_set_header X-Real-IP        $http_cf_connecting_ip;
   proxy_set_header X-Forwarded-For  $http_cf_connecting_ip;
   ```
   (остальные `proxy_set_header` — `Host`, `X-Forwarded-Proto`, `Upgrade`, `Connection` — оставить).
10. Убедиться, что в `.env` стоит **`TRUST_XFF=1`** (тогда сервер берёт реальный IP из заголовка).
11. `nginx -t && systemctl reload nginx`.

## Часть 4. Хардненинг origin (рекомендуется)
Чтобы никто не обходил Cloudflare, стуча напрямую по IP (и нельзя было подделать `CF-Connecting-IP`):

12. Закрыть порты **80/443 на firewall только для подсетей Cloudflare** (актуальные списки:
    https://www.cloudflare.com/ips-v4 и https://www.cloudflare.com/ips-v6), остальным — deny.
    Порт **22 (SSH) не трогать.** Делать **после** того, как CF заработал (иначе отрежешь себя).

## Что НЕ трогать
- Домен Mini App в BotFather остаётся `promptw.ru` — **не менять**.
- `WEBAPP_URL`, `WEBHOOK_URL` в `.env` — **не менять**.

---

## Часть 5. Проверка и приёмка
- [ ] Cloudflare: домен **Active**; `dig promptw.ru` отдаёт IP Cloudflare (не `45.147.177.237`).
- [ ] **Открывается из РФ** (проверить сразу — ты или знакомый): Mini App грузится, бот отвечает.
- [ ] **Открывается из Украины/Европы** (через VPN/знакомого).
- [ ] Тестовый платёж проходит (колбэк доходит), `getWebhookInfo` без ошибок.
- [ ] В логах/админке реальные IP пользователей, а не диапазоны Cloudflare.

## Откат (мгновенный)
Если из РФ станет хуже — в Cloudflare переключить запись `promptw.ru` в **DNS only** (серое облако)
или вернуть NS регистратора на прежние. Трафик снова пойдёт напрямую на `45.147.177.237` за минуты.

---

## План Б: GeoDNS (без компромиссов для РФ)
Если базовый план просадит РФ — отдавать **разные адреса по гео на одном домене**: российские
резолверы → прямой IP `45.147.177.237`, все остальные → Cloudflare. Тогда РФ идёт напрямую (вне
зависимости от Cloudflare/РКН), а заграница — через CF.

Варианты реализации:
- **Cloudflare Load Balancing** с geo-steering (платно, ~$5/мес): пул `RU → origin`, пул
  `default → CF-проксированный origin`, правило по региону.
- **Внешний GeoDNS-провайдer** (ClouDNS, Gcore DNS, AWS Route 53 geolocation): A-запись
  `promptw.ru` с гео-ветвлением RU → `45.147.177.237`, прочее → IP/CNAME Cloudflare.

Минус — сложнее настройка и зависимость от GeoDNS-провайдера. Плюс — РФ-аудитория не зависит от
Cloudflare вообще.

---

## Альтернатива Cloudflare: reverse-proxy на EU-VPS
Если Cloudflare не подходит: маленький VPS в ЕС (Hetzner ~€4/мес), nginx проксирует на
`45.147.177.237`, домен указывает на этот VPS. Даёт европейский вход без Cloudflare, но без
глобальной edge-сети (одна локация) и требует поддержки своего сервера.
