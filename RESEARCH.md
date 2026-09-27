# Receipt Scanner — статус реверс-инжиниринга API (2026-08-26)

## ВЕНКОН (`vencon.ua`) — автоимпорт кабинета + публичный поиск

- Кабинет использует телефон/пароль (`POST /ua/auth/sign-in`); при восстановлении
  доступа может потребовать SMS. Пароль не сохраняется: после входа хранится только
  зашифрованная cookie-сессия. `GET /ua/user/profile/orders` автоматически
  импортирует выполненные заказы, а аннулированные возвращает как excluded, чтобы
  ранее сохранённый заказ был удалён при смене статуса.
- В карточке заказа сохраняется исходная ссылка на фискальный PDF, а скачанная
  копия лежит в `backend/data/receipts/` и доступна через обычный endpoint файла
  чека. Для заказа без фискального PDF сохраняются данные выполненного заказа.
- Публичный каталог ищется через `GET https://vencon.ua/ua/search?q=...`; адаптер
  извлекает название, текущую цену, наличие, фото и ссылку без авторизации.
- Текущий остаток бонусов берётся из шапки кабинета (например `Бонуси 324`) при
  каждом сборе; начисленные и списанные бонусы также сохраняются отдельно по заказу.
- Обычная гарантия берётся из карточки товара (`... міс. гарантії від виробника`)
  и сохраняется на чеке и позиции с рассчитанной датой окончания. Раздел
  «Комфортна гарантія» в проверенном кабинете пуст, поэтому отдельного документа
  расширенной гарантии нет.

## Liki.ua (`liki.ua`) — кабинет бронирования, кешбек Fishka, публичный поиск

- Мобильного API нет: `api.liki.ua` на любой путь отвечает `{"message":"Bad
  credentials"}`, приложение LIKI (`com.liki.app`) — тот же кабинет сайта.
  Интеграция построена на собственных AJAX-ручках сайта, сессия — cookie
  `PHPSESSID` (сохраняется зашифрованной, пароль не хранится).
- Логин: `POST /account/login`, тело **только form-urlencoded**
  (`fast=1&login=…&password=…`) плюс заголовок `X-Requested-With: XMLHttpRequest`
  — так шлёт axios самого сайта. JSON сервер не разбирает вообще: поля приходят
  пустыми, и ответ будет «Введені логін або пароль не знайдені» даже с верным
  паролем (проверено на `/account/register`: form отвечает про следующее
  недостающее поле, JSON — про то же самое первое). Логин = e-mail или
  телефон `380XXXXXXXXX`; что именно — зависит от `site_settings.authType`
  (сейчас `"1"` — телефон, 12 цифр). Ответ в конверте `{"result":"success"|"error","errmsg","errcode"}`. После
  нескольких неудачных попыток появляется капча (`captcha_code`, коды ошибок
  206/207) — адаптер её не обходит, а просит войти на сайте вручную.
- Заказы: `GET /shop/orders`, серверный рендер (Smarty `shop/orders.smarty.html`),
  карточки `.lk-order-block` + позиции `.lk-one-order__block`. JSON-эндпоинта для
  истории заказов нет; `/orders/havechanges` только сигнализирует об изменении
  статусов. Проверено на живом кабинете. Особенности разметки:
  - дата печатается словами («21 вересня 2026»), числового формата и времени в
    кабинете нет вообще — время ставится как полдень по Киеву;
  - сумма — `.lk-one-order__total` («Разом: 424.50 ₴»), в свёрнутой шапке она же
    последним числом в `.lk-order-block__head-count`;
  - аптека — `.drugstore-card__pharmacy-title` / `-location`. В
    `.lk-order-block__address-content` в живом кабинете лежит **телефон
    покупателя**, не адрес аптеки — этот класс намеренно не используется;
  - у каждого заказа своя печатная форма:
    `/shop/orderspdf/{orderRID}-{orderPdfKey}.pdf` (ссылка прямо в карточке,
    в `.lk-one-order__foot`).
- Бонусы: собственного кошелька у Liki.ua нет, кешбек начисляется на карту Fishka
  (`myfishka.com`), привязанную к тому же номеру. `POST /loyal/fishka` (постранично
  `POST /loyal/fishka/page=N`; в корне сайта `/fishka` отдаёт 404) отдаёт ленту начислений: `txID`, `txDtAdd`, `orderRID`,
  `orderPdfKey`, `txAmount`, `txStatus`/`statusName`, `txComment`. Адаптер
  привязывает начисление к заказу по `orderRID` и складывает ссылку на PDF заказа
  `/orderspdf/{orderRID}-{orderPdfKey}.pdf`. `get_balance`/`get_loyalty_code`
  сознательно не реализованы: баланс и карта живут в Fishka, а не в этом кабинете.
- Публичный каталог: `GET /shop/search?query=...&filters=1`, карточки
  `.catalog-block`. Цена на карточке — «від N ₴» (минимум по аптекам), наличие —
  строка «в N аптеках». Работает без авторизации, проверено вживую.

Цель: собирать чеки пользователя из мобильных приложений сетевых магазинов через их же API,
от имени самого пользователя (номер телефона + SMS-код, как в оригинальном приложении).

## Общий паттерн авторизации
Все проверенные сети используют **телефон + SMS OTP → JWT/Bearer токен**. Полностью
реверс-инжинирены и реализованы в `backend/app/adapters/*.py`:

- **АТБ** — полный OIDC authorization_code + PKCE флоу через `mobapp.atbmarket.com`:
  `POST /register/login` (шлёт SMS) → `GET /auth/auth` (PKCE challenge) →
  `POST /auth/third_mobile/interaction/{id}/login` (form: `token`+`otp`) →
  `POST /auth/token` (обмен кода на токен). Автоматизировано полностью, без капчи.
  Бонус: `id_token` (тот самый, что идёт как Bearer в eReceipts API) живёт ~360 дней —
  повторный логин практически никогда не понадобится.
- **Fora** — RPC-методы `SendOtpV2`/`ConfirmationOtp_V2` на том же шлюзе
  `api2.mob.fora.ua/api/2.0/exec/FZGlobal/`. Без капчи, автоматизировано полностью,
  **проверено вживую** (реальный SMS-вход + сбор 35 чеков). `accessToken` живёт 24
  часа, `refreshToken` — 180 дней, но отдельного RPC-метода обновления пока не
  найдено — после 24 часов понадобится повторный OTP-логин (без капчи).
  **Важно:** шлюз требует служебные заголовки `deviceTime` (unix ms) и `user-info`
  (JSON-строка с фейковыми, но обязательными полями устройства) — без них ответ
  `errorCode: 232 DEVICE_INFO_NOT_FOUND`. Также `GetOnlineOrderHistory` на свежем
  токене падает с `errorCode: 512` ("дані недоступні"), пока не сделать один
  "прогревающий" вызов `CheckUser` — адаптер делает это автоматически.
- **Fozzy** — мобильный `POST /SendOtp` требует on-device reCAPTCHA, но официальный
  сайт предоставляет OAuth/PKCE-вход (`fozzy_shop_client`, scope `FozzyMobileApi`).
  `FozzyAdapter` открывает эту страницу в headed Chromium/Xvfb по той же схеме, что
  Дніпро-М: сама страница получает Cloudflare Turnstile, отправляет SMS, после кода
  адаптер обменивает authorization code на access+refresh token. Защита не обходится.
  `refresh_token` ротируется; в `Credential.extra.oauth_client` сохраняется тип
  клиента, чтобы обновление шло с правильным `client_id`.
  **Важно:** `loyaltyexpapi.fozzy.ua` требует заголовки `appVersionName`,
  `clientSdkVersion`, `brand`, `x-client-platform` — без них 401, значения не
  валидируются, просто должны присутствовать. **Осторожно с ручным тестированием
  refresh** — `refresh_token` одноразовый и ротируется при каждом использовании;
  ручной вызов через curl "сжигает" тот самый токен, которым пользуется сам
  телефон, и путает, какой access_token сейчас действителен. Проверяйте живой
  токен из свежего mitmproxy-перехвата, а не из старых заметок.
- METRO: анонимный UUID устройства через `customer/signin` (без телефона на этом этапе,
  но для доступа к покупкам нужна регистрация "Shopper" — см. ниже)

Токены/refresh-токены хранятся зашифрованными в `Credential` (backend), у UI аккаунта
есть кнопка "Надіслати код" + поле для кода — полный флоу логина без mitmproxy.

### Картка магазину (QR/штрихкод клієнта)
`BaseAdapter.get_loyalty_code(access_token, extra) -> LoyaltyCode` (adapters/base.py) —
опційний метод, за замовчуванням `NotImplementedYet`. Викликається наживо з UI
(`GET /api/accounts/{id}/loyalty-code`, кнопка "Показати код" на сторінці акаунта
ТА кнопка "Код картки" прямо в таблиці /accounts — відкриває модалку, не кешується
— АТБ видає код з обмеженим часом дії). `display` каже, чим рендерити типово
(`qrcode`/`jsbarcode` npm-пакети) — **QR скрізь, крім Епіцентру, де 1D штрихкод**
(так само в реальних застосунках) — але `LoyaltyCodeDisplay.vue` дає перемикач
QR⇄штрихкод для будь-якого коду (сам код — просто рядок, малює однаково добре в
обох форматах; корисно, якщо касовий сканер не читає один із форматів).

Джерело коду по кожному магазину:
- **АТБ** — окремий хост `wallet-prod.pc-service.ua` (спільний white-label
  loyalty-бекенд, судячи з домену; НЕ за тим самим Cloudflare bot-management, що
  й ereceipt/mobapp, тож із сервера доступний): `GET /api/v1/Client/pin`
  (той самий `id_token`, що й для eReceipts) → `{"pin", "barcode", "expiredIn"}`.
  `barcode` — довгий hex-рядок (не класичний EAN), `expiredIn` — .NET-формат з
  довільною кількістю знаків дробової секунди (та сама проблема, що й у Fozzy —
  `_parse_dt()` з тим самим фіксом). Код живе кілька годин, підходить під live-фетч.
- **Fozzy** — окремого виклику не треба: `access_token` — це JWT з claim'ом
  `"barcode"` напряму (`"0290000000000"`, статичний, не ротується).
- **Fora** — `CheckUser` (FZGlobal RPC) → `{"isDepersonalization","registered",
  "barcode","cardAccount","personId"}`. Той самий виклик, що вже робився як
  "прогрів" перед `GetOnlineOrderHistory` — просто ніколи не дивились у відповідь.
  Підтверджено проти скріншота реального екрана картки: значення `barcode`
  збігається один в один. `GetQRKeys` (публічні EC-ключі `posKey`/`paymentKey`) —
  це виявилось про інше: підпис QR для "Оплата застосунком" (генерується на
  пристрої, працює офлайн, окрема фіча) — не про звичайну картку лояльності.
- **Сільпо** — MCP tool `silpo_get_loyalty_info` (без параметрів) →
  `{"loyalty":{"card":{"barcode","typeName","memberId"},"balance":{...}}}`.
- **Епіцентр К** — `GET /api/v2/user/bonuses` (той самий виклик, що вже
  використовувався для перевірки сесії) → `{"cardNumber","barcode","shortBarcode",
  "cardId","activeBonuses",...}`. Єдиний магазин, де в реальному застосунку це
  штрихкод, не QR.

### Бонуси/кешбек на чеку (накопичення) + баланс (поточний стан) + списання (використання)
Три окремі, але пов'язані штуки:

**Накопичено за чек** — `NormalizedReceipt.bonuses` (adapters/base.py) → `Receipt.bonuses`
у БД, на сторінці чека і сумується в `/api/dashboard` (`total_bonuses`). Джерело:
- **Fora** — `cashbackAmount` вже був у списковому виклику (`GetChequesV2`/
  `GetOnlineOrderHistory`), просто не витягувався раніше.
- **Сільпо** — `accruedBalaBonusesSum` (offline; online — форма непідтверджена,
  бонуси не виводяться).
- **Епіцентр К** — `bonuses.obtained` у деталях offline-замовлення.
- **АТБ, Fozzy** — не знайдено відповідного поля в API чеків (ATB — чистий
  фіскальний чек без прив'язки до бонусів; Fozzy має `sumSaving`/`sumSaved`, але
  це знижка, не нарахування) — `bonuses` лишається `null`.

**Використано на чек** — `NormalizedReceipt.bonuses_used` → `Receipt.bonuses_used`,
сумується в `/api/dashboard` (`total_bonuses_used`). Джерело:
- **Сільпо** — `rewards[]` у деталях offline-замовлення містить різні механіки
  промо; тільки `rewardGroupCodeName == "CN_REWARD_VOUCHERS"` — це власне
  "Використані балобонуси" (перевірено на реальному прикладі), інші типи rewards
  ігноруються.
- **Епіцентр К** — поле `bonusUsed` прямо в `OrderResponse` (є в офіційній Swagger-схемі).
- **АТБ, Fozzy, Fora** — не знайдено.

**Поточний баланс** — `BaseAdapter.get_balance(access_token, extra) -> LoyaltyBalance`
(adapters/base.py), опційний метод. На відміну від `get_loyalty_code` — **кешується**:
`collector.py` викликає його при кожному зборі (до `fetch_receipts`, best-effort,
не валить увесь запуск при помилці — саме тому й *до*, а не після: інакше в АТБ,
де `fetch_receipts` постійно падає на Cloudflare, баланс взагалі ніколи б не
оновлювався), результат лежить у `Account.bonus_balance` +
`bonus_balance_updated_at`, `/api/accounts` віддає його миттєво без live-запиту.
Джерело по магазинах:
- **АТБ** — той самий `wallet-prod.pc-service.ua`: `GET /api/v1/Client/balanceByClientId`
  → `{"balance", "accumulated", "remainderParams",...}` — це "округлення решти"
  програма, не класичні бонуси.
- **Fora** — `GetMenuInfo` (FZGlobal RPC) → `cashBackAmount` (той самий виклик,
  що вже даёт код картки через `CheckUser` — просто інший метод).
- **Сільпо** — `silpo_get_loyalty_info` → `loyalty.balance.total`. **Пастка**:
  перша версія коду читала `data.balance` замість `data.loyalty.balance` (та сама
  помилка вкладеності, якої вдалось уникнути в `get_loyalty_code` для карти) —
  виправлено, перевірено наживо.
- **Епіцентр К** — `/api/v2/user/bonuses` → `total` (+ `notActiveBonuses` як `pending`).
- **Fozzy** — не знайдено (перевірив `personalinfo`, `personaloffercacheback`,
  `personaloffercostscale` — останній має `totalBalanceScale`, але це схоже на
  баланс окремої лімітованої акції "кешбек-скарбничка", не загальний баланс;
  `QR/GetQR` — той самий crypto-key патерн, що й `GetQRKeys` у Fora, не баланс).

---

## АТБ (`com.atb.atbmarket`) — ⚠️ API задокументирован, живой сбор с сервера заблокирован

**Полный OIDC + PKCE флоу авторизации** (SendOTP → interaction/login → auth code →
token exchange) реализован и автоматизирован в `AtbAdapter.start_login/confirm_login`
без капчи (см. ниже общий раздел про Cloudflare).

```
POST https://ereceipt.atbmarket.com/EReceipt_MP/eReceipts/1.0/getReceipts
Authorization: Bearer <JWT, привязан к номеру телефона>
Content-Type: application/json

Body:
{
  "clientId": "+380XXXXXXXXX",
  "checkSortBy": "date",
  "checkOffset": 1,
  "checkLimit": 20
}
```

Ответ: `Data[]` — массив чеков (`receiptStructureVersion: v16`):
- `shop` — магазин (id, номер, адрес, название)
- `rcpt` — фискальные данные (дата, номер чека, фискальный номер РРО, сумма)
- `details[]` — позиции: название, штрихкод (`bar`), кол-во, цена, сумма, скидка, налоговая группа

### ⚠️ Блокер: Cloudflare режет запросы с этого сервера по TLS-отпечатку

И `mobapp.atbmarket.com` (SMS-логин), и **сам** `ereceipt.atbmarket.com/getReceipts`
теперь отвечают Cloudflare-страницей "Just a moment..." (403) на любой запрос с этой
машины — при том что **точно тот же самый Bearer-токен** с телефона в тот же момент
работает нормально (200, реальные данные). Проверено и исключено по очереди:
- не IP (телефон и сервер в одной сети, один и тот же внешний IP)
- не токен (идентичный JWT, с телефона проходит)
- не заголовки (скопированы один в один из перехвата — User-Agent, Accept, и т.д.)
- не куки (реплей боевой `__cf_bm`-куки с телефона тоже не помог)

Значит дело в TLS/JA3-отпечатке клиента — Android/okhttp и curl/httpx устанавливают
TLS-соединение по-разному, и Cloudflare отличает их ещё до HTTP-запроса. Подделка
этого отпечатка (curl-impersonate и т.п.) — уже не reverse engineering API, а обход
антибот-защиты; сознательно не делаем.

**Обходной путь — ручной импорт.** `AtbAdapter.parse_import(payload)` +
`POST /api/accounts/{id}/import` (кнопка "Ручний імпорт" в UI акаунта) принимают сырой
JSON-ответ `getReceipts`, перехваченный через mitmproxy с телефона, и прогоняют его
через ту же нормализацию, что и обычный сбор. Единственное отличие — триггер ручной,
а не по cron-расписанию.

Важно при перехвате больших ответов: mitmdump по умолчанию обрезает длинные тела в
текстовом логе (`(cut off)`) — нужен флаг `--set content_view_lines_cutoff=1000000`
при запуске, иначе список чеков придёт неполным.

### ✅ Пошук по каталогу (не чеки) — окремий хост, Cloudflare не заважає (26.08.2026)

На відміну від `getReceipts`, пошук товарів для "zakaz" (онлайн-замовлення) частини
застосунку йде через **стороннього** провайдера повнотекстового пошуку, зовсім інший
хост:

```
GET https://api.multisearch.io/?query=<текст>&q=<текст>&location=1075&p=1&id=11280&lang=uk&key=63a6d0a760fd2d0562c4061b78e64754
```

Токен акаунта не потрібен взагалі — просто публічний віджет-ключ (`key`) + сайт-id
(`id=11280`, стабільний ідентифікатор АТБ у системі multisearch) + `location`
(id міста **в системі multisearch**, не той самий, що `cityId=395` у
`zakaz.atbmarket.com` — окрема нумерація). Відповідь:

```json
{"query":"молоко","total":91,"results":{"item_groups":[
  {"category":{"id","name","url","count"},
   "items":[{"id","picture","url","is_presence","name","brand","price","currency","label"}]}
]}}
```

**Перевірено вживу**: цей хост теж за Cloudflare (`server: cloudflare` у заголовках
відповіді), але звичайний непідмінений `httpx`-запит з цієї машини повертає 200 з
реальними даними — жодного TLS/JA3-блоку тут нема, на відміну від `getReceipts`. Отже
`AtbAdapter.search_catalog` (backend/app/adapters/atb.py) свідомо використовує голий
`httpx.AsyncClient`, а не спільний `app.core.http.AsyncClient`.

Є й другий, точніший ендпоінт — `POST /api/v3/products/group` на
`api.zakaz.atbmarket.com` (з тілом `{"ids":[...]}`, повертає персональну ціну з картою
лояльності + точний залишок на конкретній крамниці) — але він вимагає повноцінну
авторизовану сесію з `__cf_bm`/`_csrf`/`PHPSESSID` cookies, виданими Cloudflare
bot-management при проходженні його челенджу в реальному застосунку. Відтворювати такі
cookie з сервера без справжнього проходження челенджу — це вже обхід антибот-захисту,
а не reverse engineering API, тож свідомо НЕ реалізовано (той самий принцип, що й
відмова від TLS-impersonation для `getReceipts` вище). `multisearch.io` дає достатньо
(назва, ціна, фото, наявність) для пошуку/порівняння цін без цього.

---

## Fozzy (`ua.fozzy.android`) — ✅ полностью готово

Список чеков по месяцам:
```
GET https://loyaltyexpapi.fozzy.ua/api/1.0/chequeheaders
Authorization: Bearer <JWT>
```
Ответ: `chequeMonthList[]` → `chequeList[]` (chequeId, created, sumReg, sumSaving, скидки,
купоны, магазин, `chequeUrl` — ссылка на публичный чек на receipt.fozzy.elkasa.com.ua)

Детали конкретного чека (товарные позиции):
```
GET https://loyaltyexpapi.fozzy.ua/api/2.0/chequeinfo?filialId=<id>&chequeId=<id>&created=<ISO8601>
Authorization: Bearer <JWT>
```
Ответ: `chequeInfo.chequeLines[]` — товары с полным `lagerInfo` (название, категория, цена,
картинка), `amount`, `sumReg`, `sumDiscount`. `channel="offline"`. Цена за единицу в API не
приходит — считаем `sumReg / amount`.

**Онлайн-заказы (доставка/Click&Collect), `channel="online"`** — отдельный хост и формат,
RPC-шлюз как у Fora:
```
POST https://api.tecom.fozzyshop.ua/api/2.0/exec/TEComGlobal
Authorization: Bearer <тот же JWT>
Body: {"Method": "OrderHeadsList", "data": {"forms": [1, 4], "limit": 50, "offset": 0}}
```
Ответ: `data[]` — список заказов, сразу с id, number, statusName, createdDate, sumActual
(итог), sumOrigin, **sumActualDiscount** (отрицательное число = сумма скидки, готова без
расчётов), deliveryAddress, filialName, paymentTypeName.

Детали (товарные позиции):
```json
{"Method": "GetOrderDetail", "data": {"orderId": "<order.id>"}}
```
Ответ: `data.items[]` — lagerId, name, unit, countActual (реальное кол-во после сборки,
может отличаться от countOrigin/countOrdered), sumActual, price, lineStateId (**1 =
"Видалено"** — товар вычеркнут при сборке, count=0, такие позиции пропускаем).

Важно: у `createdDate`/статусов TEComGlobal дробные секунды произвольной длины
(`.63`, `.2087532`) — `datetime.fromisoformat` в Python 3.10 принимает только 3 или 6
цифр, нужен нормализующий парсер (см. `fozzy.py::_parse_dt`).

### ✅ Пошук по каталогу — REST, не RPC, той самий хост і токен (26.08.2026)

Той самий хост, що й TEComGlobal (`api.tecom.fozzyshop.ua`), але окрема REST-гілка
API (`/api/3.0/...`, не `{"Method": ...}` RPC-шлюз):

```
GET https://api.tecom.fozzyshop.ua/api/3.0/Catalog/products
    ?MerchantId=4&FilialId=51&DisplayOnSite=true&DeliveryType=2
    &CustomFilter=<текст>&PageSize=36&PageNum=1
Authorization: Bearer <той самий токен, що й для чеків>
appVersionName / clientSdkVersion / brand / x-client-platform — ті самі обов'язкові
заголовки, що й у loyaltyexpapi.fozzy.ua (без них 401)
```

`MerchantId=4` — фіксований id бренду "Fozzy" у спільному бекенді Fozzy Group;
`FilialId=51` — своя/найближча крамниця цього акаунта, значення захоплене наживо
(перевикористовуємо, як і `location` в multisearch.io для АТБ). Відповідь:
`items[]` — id, name, unit, price, oldPrice, barcode, mainImage, url (відносний
slug, без домену — канонічного посилання на сторінку товару в відповіді нема,
`product_url` лишається порожнім, так само як у Fora), **quantity**/**quantityFull**
(залишок — `> 0` трактуємо як "є в наявності"), category/categories, nutrition,
techParameters. Полів `brand` на верхньому рівні нема.

Реалізовано в `FozzyAdapter.search_catalog` (backend/app/adapters/fozzy.py).

**Тестування без ризику для сесії телефону**: `refresh_token` в Fozzy одноразовий і
ротується при кожному використанні (див. вище) — щоб перевірити нову реалізацію, не
чіпаючи `ensure_fresh_token`/`adapter.refresh()` (а отже не ризикуючи спалити той
самий `refresh_token`, яким користується телефон), access_token узятий напряму зі
свіжого mitmproxy-перехвату (той, що телефон щойно сам використав для пошуку) і
переданий у `search_catalog` окремим одноразовим скриптом, в обхід БД/колектора.
Живий ендпоінт (`/api/products/live-search`) після цього теж перевірено — на момент
перевірки збережений у БД токен ще не встиг протухнути (>2 хв до `expires_at`), тож
`ensure_fresh_token` обійшовся без рефреша, і `expires_at` у БД лишився незмінним.

---

## Fora (`ua.fora.android`) — ✅ обе ветки готовы

Единый RPC-шлюз, метод передаётся в теле:
```
POST https://api2.mob.fora.ua/api/2.0/exec/FZGlobal/
Authorization: Token <JWT>
Content-Type: application/json
```

**Оффлайн чеки (магазин/касса), `channel="offline"`:**
```json
{"Method": "GetChequesV2", "Data": {}}
```
Ответ: `result.cheques[]` — id, filialName, createDate, amount, cashbackAmount, itemCount,
imageItemUrls, filialId, deliveryTypeCode.

Детали (товарные позиции):
```json
{"Method": "GetChequeDetail", "Data": {"id": "<cheque.id>"}}
```
Ответ: `result.items[]` — lagerId, lagerName, lagerImageUrl, **lagerCountString** (только
строкой типа "1 шт"/"0,536 кг" — числового поля qty нет, парсим regex'ом), priceOut (цена
за единицу уже с учётом скидки), oldPrice (цена до скидки, nullable), discount (%, nullable).
Абсолютную скидку считаем сами: `(oldPrice - priceOut) * qty`.

**Онлайн заказы (доставка), `channel="online"`:**
```json
{"Method": "GetOnlineOrderHistory", "Data": {"from": 0, "take": 20}}
```
Ответ: `result.orders[]` — id, address, timeSlot, dateFrom/dateTo, amount, cashbackAmount,
itemCount, status, basketStatus, deliveryType, paymentStatus. 29 заказов с 2022 года.

Детали (товарные позиции):
```json
{"Method": "GetOnlineOrderDetail", "Data": {"id": "<order.id>"}}
```
Ответ: `result.items[]` — laggerId (опечатка в имени поля у самого Fora, две "g"), name,
slug, lagerImageUrl, **count** (числом, в отличие от оффлайн-чеков), priceOut, oldPrice,
discount, lagerUnit, unit. Бесплатные промо-товары приходят как `priceOut: 0, oldPrice: <цена>`
— скидка в этом случае равна полной цене.

### ✅ Пошук по каталогу — той самий gateway, потрібен токен (26.08.2026)

На відміну від АТБ (окремий хост без авторизації), у Fora пошук товарів — ще один
`Method` на тому самому RPC-шлюзі `api2.mob.fora.ua/api/2.0/exec/FZGlobal/`, з тим самим
`Authorization: Token <JWT>`, що й чеки:

```json
{"Method": "SearchCatalogItems", "Data": {
  "searchText": "хлеб", "from": 1, "to": 20, "sortOrderId": "popular-asc",
  "universalFilters": [], "promoFilters": [], "multiFilters": {}, "rangeFilters": {}
}}
```

Відповідь: `result.items[]` — id, slug, productTitle, imageUrl, price, oldPrice,
discountString, unit (текстовий опис на кшталт "340г"), **calcStoreQuantity**
(залишок у конкретній крамниці цього акаунта — трактуємо `> 0` як "є в наявності"),
brand, categories[], bubbles[] (промо-плашки). Жодного канонічного посилання на
сторінку товару у відповіді немає (ні тут, ні в `GetDetailItemInfo`) — на відміну
від АТБ, `product_url` свідомо лишається порожнім, а не вгадується за `slug`.

Реалізовано в `ForaAdapter.search_catalog` (backend/app/adapters/fora.py).

---

## Сільпо (`com.silpo.online`) — ✅ через офіційний MCP-сервер, реверс-інжиніринг не знадобився

На відміну від усіх інших магазинів, для Сільпо не довелось патчити APK і ловити трафік:
у них є **офіційний MCP-сервер** (`https://mcp.silpo.ua/mcp`), задокументований на
https://ai-factory.silpo.ua/docs/mcp. Санкціонований шлях, без реверс-інжинірингу.
(Спроба перехопити трафік `sf-mobile-api.silpo.ua` через mitmproxy провалилась ще
до цього — Dio/HTTP-клієнт застосунку для основного API ігнорує системний Wi-Fi проксі
Android, на відміну від клієнта для feature-flags `sf-external-api.silpo.ua`, який його
поважає; PCAPdroid (VPN-based, no-root) теж не дав розшифрований трафік за кілька спроб —
все це стало непотрібним, коли знайшовся MCP-сервер.)

### OAuth 2.1 + PKCE (не phone_otp, як інші — окремий `AuthType.oauth_browser`)
Discovery: `GET https://mcp.silpo.ua/.well-known/oauth-authorization-server` →
```json
{"issuer":"https://mcp.silpo.ua","authorization_endpoint":"https://mcp.silpo.ua/authorize",
"token_endpoint":"https://mcp.silpo.ua/token","registration_endpoint":"https://mcp.silpo.ua/register",
"grant_types_supported":["authorization_code","refresh_token"],
"code_challenge_methods_supported":["plain","S256"]}
```
Клієнт зареєстровано один раз через Dynamic Client Registration
(`POST /register`, публічний клієнт, `token_endpoint_auth_method: "none"`),
з `redirect_uri` = `{RS_BASE_URL}/api/oauth/silpo/callback` (цей бекенд сам приймає колбек,
а не якийсь локальний скрипт) — **`client_id` захардкожений в `adapters/silpo.py`**,
якщо `RS_BASE_URL` колись зміниться — потрібна повторна реєстрація з новим `redirect_uri`.

Флоу (веб-версія стандартного authorization_code + PKCE):
1. `GET /api/accounts/{id}/oauth/start` (наш бекенд) — генерує PKCE verifier/challenge +
   `state`, зберігає їх в `app/core/oauth_state.py` (in-memory, TTL 10 хв), повертає
   `{"authorize_url": "https://mcp.silpo.ua/authorize?...&state=..."}`
2. Фронтенд відкриває цей URL у новій вкладці — користувач логіниться на **auth.silpo.ua**
   (той самий "Власний Рахунок": телефон → SMS-код), той самий акаунт, що й у застосунку.
   Технічно тут ДВА вкладених OAuth-флоу: наш клієнт ↔ `mcp.silpo.ua` ↔ `auth.silpo.ua`
   (mcp.silpo.ua сам є клієнтом auth.silpo.ua під `client_id=silpo--mcp-server`) — видно
   в мережевих запитах як два раунди `/oidc/api/v1/Consent`, це нормально, не зациклення.
3. `auth.silpo.ua` редіректить на `mcp.silpo.ua`, той — на наш
   `GET /api/oauth/silpo/callback?code=...&state=...` (`app/api/oauth.py`) — за `state`
   знаходимо account_id + verifier, обмінюємо код на токени, зберігаємо в Credential,
   account → `active`. Показуємо просту HTML-сторінку успіху (вкладку можна закрити).
4. Обмін коду на токен: `POST https://mcp.silpo.ua/token`,
   `grant_type=authorization_code&code=...&redirect_uri=...&client_id=...&code_verifier=...`
   → `{"access_token","token_type":"bearer","expires_in":2592000,"refresh_token"}`.
   **`expires_in` = 30 днів**, є `refresh_token` → підходить для scheduled-збору без браузера
   (`grant_type=refresh_token` для оновлення, так само на `/token`).

### Дані чеків — виключно через MCP tools, не REST
Після `initialize()` MCP-сесії (`mcp` Python SDK 1.29.0 — **не 2.x**, там перейменували
`streamablehttp_client`, див. коментар у `requirements.txt`) доступні 39 tools
(`session.list_tools()`), з них по чеках:

- **`silpo_get_my_offline_orders`** — офлайн-чеки з фізичних магазинів. Схема формально
  вимагає `branchId`/`deliveryType`/`timeslotStart`/`timeslotEnd` (задумано для AI-агента,
  що перевіряє наявність товару в кошику), але **емпірично підтверджено — для звичайного
  читання історії ці поля ніяк не валідуються**, будь-які placeholder-значення повертають
  реальні чеки. `limit` максимум 10 за раз, є `dateStart`/`dateEnd` (ISO, дефолт — 6 місяців
  назад) — для першого повного backfill передаємо `dateStart="2000-01-01T00:00:00Z"` явно.
  Відповідь: `{"orders":[{filId, filialName, cityName, createdAt, sumReg, sumDiscount,
  accruedBalaBonusesSum, receiptUrl, rewards[], products:[{lagerId, name, unit, quantity,
  price, catalogProduct}]}], "meta":{"limit","offset","total"}}`. `price` — ціна за одиницю
  (перевірено: quantity=0.6, price=64.9 для товару вагою в кг). Знижка є лише на рівні
  чека (`sumDiscount`), не по позиціях. `external_id` = останній сегмент `receiptUrl`
  (унікальний per чек, напр. `https://receipt.silpo.elkasa.com.ua/xWjNIV4Vzz8` → `xWjNIV4Vzz8`).
- **`silpo_get_my_online_orders`** — доставка/самовивіз через застосунок/сайт. Робочий,
  підтверджено живим викликом (`{"success":true,"orders":[],"meta":{...}}` — просто в
  тестового акаунта немає жодного онлайн-замовлення), але **точна форма об'єкта замовлення
  не підтверджена реальним прикладом** — нормалізація в `_normalize_online()` (silpo.py)
  побудована на здогадках з конвенцій назв полів і захищена try/except per-order (пропускає
  + логує незнайомі, решту збору це не ламає). Перевірити/поправити, коли з'явиться реальне
  онлайн-замовлення для тестування.

`createdAt` без таймзони (напр. `"2026-08-20T20:21:41"`) — трактуємо як UTC, як і скрізь
в цьому проєкті (див. `_normalize` в atb.py).

### Перевірено наживо (26.08.2026)
Повний цикл через реальний бекенд: `POST /api/accounts` (store_code=silpo) →
`GET /{id}/oauth/start` → логін у браузері (auth.silpo.ua, той самий номер, що в
застосунку) → колбек → `POST /{id}/collect` → **43 офлайн-чеки, всі з позиціями,
цінами, знижками** — акаунт "Сільпо Максим" відпрацював з першого разу.

### ✅ Пошук по каталогу — MCP tool, потребує реального контексту кошика (26.08.2026)

Серед 39 MCP tools є `silpo_find_products_batch` — пошук за назвами (до 30 через
крапку з комою за раз, `products: string[]`), і, окремо, точний пошук за
`externalProductId` (числовий артикул) замість нечіткого збігу по назві. Схема
вимагає ті самі `branchId`/`deliveryType`/`timeslotStart`/`timeslotEnd`, що й
`silpo_get_my_offline_orders` — **але, на відміну від офлайн-чеків, тут ці поля
РЕАЛЬНО впливають на результат**: підставні значення (as `_DUMMY_BRANCH_ID = "0"`,
використовується для читання історії) повертають 0 товарів на будь-який запит,
перевірено емпірично. Тож перед пошуком потрібно взяти справжній, актуальний
контекст кошика цього акаунта:

```
silpo_get_my_shopping_cart {} -> {"shoppingCartId": "<uuid>"}
silpo_get_shopping_cart_by_id {"shoppingCartId": "<uuid>"}
  -> cart.shipments[0].branchId, cart.deliveryType, cart.timeslot.{start,end}
silpo_find_products_batch {branchId, deliveryType, timeslotStart, timeslotEnd,
                            products: ["<текст>"], limit: 20}
```

Відповідь: `queries[].products[]` — id, name, slug, price, oldPrice, **stock**,
**available** (bool), image, weighted, step, **displayRatio** (розмір паковання,
напр. "330г"/"1л" — не плутати з `unit`, якого тут нема), specialPrices,
companyId, branchId, externalProductId. Жодного канонічного посилання на сторінку
товару — `product_url` лишається порожнім, як у Fora/Fozzy.

Нюанс: пошук семантичний/нечіткий, і поводиться нерівномірно — `"молоко"`,
`"вода"`, `"банан"` повертають релевантні збіги, а `"хліб"`/`"хлеб"` (укр./рос.)
чомусь дають 0 результатів на цьому акаунті/філії, хоча хліб у Сільпо точно є;
не досліджено глибше (можливо, залежить від конкретної філії чи індексації) —
`FozzyAdapter`/`ForaAdapter`/`AtbAdapter` таких прогалин не показали.

Реалізовано в `SilpoAdapter.search_catalog` (backend/app/adapters/silpo.py).

---

## Епіцентр К (`ua.epicentrk`) — ✅ офлайн готово, ⚠️ онлайн блокується сервером

Патчинг APK — стандартний (Firebase `@null` meta-data баг, той самий фікс, що й
завжди); `network_security_config.xml` вже дозволяв `certificates src="user"` з
коробки, патчити не довелось. Публічний Swagger доступний і був основним джерелом
(після живого перехоплення підтвердив точні схеми, а не тільки endpoint-и):
`mobile-api.epicentrk.ua/swagger/index.html` → `/swagger/docs/v2/authenticationV2`,
`/swagger/docs/v2/userOrdersV2` (JSON напряму, без авторизації).

### Auth: анонімна сесія → SMS OTP (без капчі)
П'ять кроків, усі на `mobile-api.epicentrk.ua`:
```
1. POST /api/v2/auth/anonymous-token         {"appId": <client uuid4>}
   -> {"accessToken": <anon JWT>, "refreshToken", "expiresIn": 2592000}
2. GET  /api/v2/auth/info/site-user          (Bearer <anon token>, без query)
   -> {"userId": null, "fUserId": <int>, "sessionId": <string>}
3. POST /api/v2/auth/otp-codes/generate?session_id=<sessionId>
        (Bearer <anon token>, X-Fuser-Id: <anon fUserId>)  {"phoneNumber": "380..."}
   -> надсилає SMS
4. POST /api/v2/auth/otp-codes/process?session_id=<sessionId>
        (ті самі headers)  {"phoneNumber": "380...", "code": "1234"}
   -> {"accessToken": <РЕАЛЬНИЙ JWT>, "refreshToken", "expiresIn": 2592000}
5. GET  /api/v2/auth/info/site-user?session_id=<sessionId>  (Bearer <РЕАЛЬНИЙ token>)
   -> {"userId": "<id>", "fUserId": <ІНШИЙ int!>, "sessionId": <той самий>}
```
**Пастка**: `sessionId` не змінюється між анонімною й реальною сесією, а от
`fUserId` — змінюється (це насправді `customer_id` користувача, а не якийсь
device/session id) — крок 5 обов'язковий, без нього всі подальші запити з
"застарілим" анонімним `fUserId` падають на 500 (обгортка над downstream 401).
Перевірено наживо — саме це і було першою причиною провалу collect.

Обов'язкові заголовки на кожен виклик: `Platform: android`, `X-Epicentr-Lang: uk`,
`SessionId: 5040` (статичний app-tracking header, не плутати з auth `sessionId` —
у реальному пристрої не змінювався всю сесію, довільне значення підходить),
`X-API-KEY: p0YLLPSqODXA1fUS90aAaK7bdWETHtRp` (статичний per-app ключ),
`Mobile-Version: 4.11.2762`, `User-Agent: Dalvik/2.1.0 (...)`.

Токен живе 30 днів (`expiresIn: 2592000`), як у Fozzy/Silpo. Публічний Swagger
для authenticationV2 перевірено повністю — **ендпоінта оновлення токена немає
взагалі**, тож `refresh()` свідомо не підтримується (як у atb.py за замовчуванням)
— по закінченню 30 днів акаунт піде в `needs_reauth`, треба заново пройти SMS
(уже автоматизовано, просто повторний вхід).

### Чеки: offline + online, схема з userOrdersV2
```
GET /api/v2/sales/orders/offline?Page=&Limit=&From=&To=&session_id=
  + GET /api/v2/sales/orders/offline/{id}?session_id=        офлайн (у магазині)
GET /api/v2/sales/orders/online?Page=&Limit=&Status=All&From=&To=&session_id=
  + GET /api/v2/sales/orders/online/{id}?session_id=         доставка/самовивіз
```
Список (`OrderHistoryResponse`) — тільки підсумки; деталі (`OrderResponse`) —
повні позиції (`goods[]`: name/quantity/price/linesTotal/measure/article,
`oldLinesTotal`/`oldPrice` для знижки, коли вона є — в живому прикладі знижок не
трапилось, тож перевірено лише по схемі, не по реальних даних). `receipts[]` у
відповіді — посилання на фіскальний чек (типу Сільпо `receiptUrl`), буває
порожнім для offline. `time` заявлений у Swagger як `date-span`, а фактично
приходить як `"HH:MM:SS.ffffff"` — те саме, що час-з-півночі, парситься як
звичайний clock time.

### ✅ Онлайн-замовлення — вирішено: `sessionId` протухає швидше за токен
`GET /api/v2/sales/orders/online` (і `/api/v2/sales/orders/active`) стабільно
повертали `500 {"detail":"...401 (Unauthorized)"}` при виклику з сервера —
навіть з ідентичними заголовками/токеном/`session_id`, тими самими, що в реально
пійманому робочому запиті. `/api/v2/sales/orders/offline` з тим самим токеном —
працювала завжди.

**26.08.2026, знайдено причину**: перевірив заголовки самої відповіді — `content-type:
application/problem+json`, `api-supported-versions` присутній — запит реально
доходить до .NET-бекенду Епіцентру (не Cloudflare WAF, на відміну від АТБ-кейсу).
Далі зловив живий трафік з реального застосунку прямо в момент, коли він успішно
відкрив вкладку "Онлайн замовлення", і відтворив його запит один-в-один — **спрацював**.
Різниця з нашим збереженим (застарілим, з моменту логіну) `session_id` — і більше
нічого. Підтверджено експериментом: узяв наш СТАРИЙ (досі валідний за `exp`) токен,
викликав ним `GET /api/v2/auth/info/site-user` (той самий виклик, що й крок 5 логіну)
без query-параметра `session_id` — отримав НОВИЙ `sessionId`, і з ним `/orders/online`
одразу спрацював (80 замовлень). Тобто пара `session_id`/`fUserId` має власний,
коротший за сам JWT, час життя на боці `/orders/online` та `/orders/active`
конкретно (offline цю пару, вочевидь, взагалі не перевіряє).

**Фікс**: `fetch_receipts()` тепер щоразу перед збором наново викликає
`GET /api/v2/auth/info/site-user` (маючи вже валідний `access_token` — це не
повторний логін, просто той самий "крок 5", повторений) і використовує свіжу пару
`session_id`/`fUserId` для обох каналів. Дешево (один зайвий виклик на весь збір),
тож робиться завжди, а не лише коли онлайн падає.

### Перевірено наживо (26.08.2026)
Повний цикл через реальний бекенд, акаунт "Епіцентр Максим": SMS-логін →
`POST /{id}/collect` → **297 офлайн + 55 онлайн = 352 чеки**, усі з позиціями,
цінами, адресою магазину.

### ⚠️ Баланс бонусів у копійках, не в гривнях
`GET /api/v2/user/bonuses` (використовується і для картки, і для балансу) віддає
`activeBonuses`/`notActiveBonuses`/`total` у **копійках** — на відміну від
взагалі всіх інших грошових полів цього API (ціна товару, ціна чека тощо — усі
в гривнях). Виявлено користувачем: реальний баланс "7.26 активних, 27.73
неактивних", а API віддало `activeBonuses:726, notActiveBonuses:2773` — рівно
у 100 разів більше. `get_balance()` ділить обидва значення на 100; `bonuses`/
`bonuses_used` на самих чеках (`bonuses.obtained`, `bonusUsed`) — уже в
гривнях, не займані.

### ✅ Пошук по каталогу — інший хост, і інший Bearer-токен (26.08.2026)

```
GET https://api.epicentrk.ua/api/v1/search
    ?find=<текст>&lang=ua&page=1&search_size=40&store_id=2&session_id=<sessionId>
Authorization: Bearer <ЦЕ НЕ той самий JWT, що для mobile-api>
X-Fuser-Id: <customerId акаунта>
```

`api.epicentrk.ua` (загальний/веб-шоп API) — окремий хост від `mobile-api.epicentrk.ua`
(чеки/баланс), і, що не очевидно з першого разу, вимагає **інший Bearer-токен**.
Перевірено вживу, включно з повним циклом вихід → SMS-логін заново → пошук:
токен для цього хоста (`m3NQfLUPcPjF2z5izd6B4ZHBfo5pP7XQbpLIdudx`) лишився
**абсолютно тим самим** до і після релогіну — отже це не токен користувача, а
фіксоване, для всього застосунку, значення (та сама роль, що й ключ АТБ у
multisearch.io чи user-info-блоб Fora, просто теж зветься "Bearer" — збило з
пантелику з першого разу: із власним JWT акаунта хост чесно відповідає `403
{"status":"ERR","message":"Bearer Token is not valid"}`, не Cloudflare-блок).
`X-Fuser-Id` (customerId акаунта) і `session_id` — навпаки, персональні, як і
скрізь, з тим самим "протухає швидше за токен" застереженням, що й у
fetch_receipts — тож перед пошуком робиться той самий `GET
/api/v2/auth/info/site-user` (справжнім JWT акаунта) для свіжої пари.
`store_id=2` — обрана фізична крамниця акаунта (див. `PATCH
/api/user/profiles/current` у трафіку застосунку), захоплено наживо.

Відповідь: `data.items[]` — id, productId, name, price, oldPrice, measure,
rating, `properties[]` (звідти береться brand — шукаємо запис з `name:"Бренд"`),
`img.url`, `url` (відносний шлях сторінки товару — **на відміну від Fora/Fozzy,
тут домен точно відомий**, `https://epicentrk.ua<url>`, підтверджено полем
`url` у відповіді-обгортці), `availabilityStatus.code` (100 = "Готовий до
відправки", трактуємо як наявність; інші коди не досліджено — тоді `in_stock =
None`, а не `False`, щоб не стверджувати те, чого не перевірено).

Нюанс: на майже точний збіг запиту з категорією Епіцентр іноді відповідає
**редіректом** замість списку товарів (`type: "redirect...", data: null,
redirect: {"pageType": "listing", "id": "<sectionId>"}`) — підтверджено на
"труба профильная" і "унітаз". Такі відповіді трактуються як (рідкісний) промах,
а не як привід іти по редіректу й тягнути секцію каталогу — це б перетворило
просту функцію пошуку товару на міні-браузер категорій.

Реалізовано в `EpicentrKAdapter.search_catalog` (backend/app/adapters/epicentrk.py).

---

## Дніпро-М (dnipro-m.ua) — ✅ авто-вхід телефон+SMS (headless Playwright, «людський» ввід); ручний імпорт — запасний

**Не мобільний застосунок** — особистий кабінет на сайті (Yii2, PHP-сесія +
cookie, `X-CSRF-Token`), єдиний такий приклад у проєкті. Мобільний
застосунок Дніпро-М ігнорує системний Wi-Fi проксі Android взагалі (жодного
запиту не потрапило в mitmproxy навіть з правильно виставленим
хостом:портом — та сама категорія проблеми, що й Dio-клієнт Сільпо, див.
розділ Сільпо) — тому працювали через сайт у браузері телефону, де системний
проксі поважається чесно.

### Auth-флоу

```
POST /auth/auth/redirect/            — реєструє session_hash (на завантаженні сторінки)
POST /phone-verification/            {"phone":"380XXXXXXXXX","channel":"phone","captcha_token"} -> SMS
     -> {"status":"success","message":"Ми надіслали код у SMS на 380..."}
POST /check-code/                    {"phone","code","remember":true,"source":"default"}
     -> success: {"status":"success","csrf","userId"} + Set-Cookie: PHPSESSID, _identity (14 днів), _csrf-frontend
     -> помилка: {"status":"error","message":"Неправильний код підтвердження"} (або "…надіслано новий код")
```

`captcha_token` — **невидима reCAPTCHA v3**, сторінка мінтить його сама
(`grecaptcha.execute`, токен потрапляє в localStorage `_grecaptcha`). Ми лише
заповнюємо поля й тиснемо кнопку — жодної роботи з капчею.

### ✅ Авто-вхід телефон + SMS (28.08.2026)

`/phone-verification/` проходить reCAPTCHA v3 з Playwright **якщо ввід
«людський»**: (1) заходити через головну (`BASE_URL + "/"`, ~3с пауза) і
відкривати модалку кнопкою `button[aria-label="Вхід/Реєстрація"]`, а не одразу
на `/login/`; (2) друкувати посимвольно (`locator.press(char)` + випадкова
пауза 50-150мс, `_human_type`), не `fill()`. Селектори в модалці — `#phone-auth`
і `#code` (на окремій сторінці `/login/` вони інші). Робочий зразок —
`backend/dnipro.py` (від користувача, sync).

**Важливо:** reCAPTCHA v3 — адаптивний скоринг по IP/оточенню. Після серії
невдалих спроб з одного оточення воно лишається «підозрілим» ще години —
`{"status":"error","message":"Не вдалося підтвердити, що запит виконано
людиною"}` навіть на коректному флоу. Тестувати з чистого стану, не поспіль.
26-27.08 «Playwright палиться» — швидше за все саме цей ефект, не сам факт
автоматизації.

Далі:

- `DniproMAdapter.start_login` / `confirm_login` — звичайний OTP-флоу (кнопка
  «Надіслати код» / поле «Код з SMS» у панелі, як в АТБ/Fora). `start_login`
  піднімає **headless** Chromium (bare, без `channel`), `_open_auth_modal` →
  `_human_type` телефону в `#phone-auth` (локальний `0XXXXXXXXX`, маска робить
  `380…`), «Отримати код», чекає `/phone-verification/`. `confirm_login` тримає
  той самий контекст живим (`auth_session_id` у `Credential.extra`, TTL 10 хв),
  `_human_type` коду в `#code`, потім клік «Продовжити» (форма може
  автосабмітитись), перевіряє `/check-code/` + `/auth/auth/user-info/`, бере
  `storage_state()`.
- Ліміт: сайт ріже кількість SMS — після ~5-6 підряд `{"status":"error",
  "message":"Перевищено ліміт запитів"}` (не captcha, ретраю нема).
- Відкрите питання: чи переживе `storage_state` з логіну перенос у headless
  `_browser_fetch` (той самий чек, що в scripts/dnipro_m_login.py крок 4).
  Не встигли перевірити — reCAPTCHA-скор оточення був зіпсований серією спроб.

### Запасні шляхи

1. **`scripts/dnipro_m_login.py`** — headed-логін на машині з дисплеєм,
   `POST /api/accounts/{id}/token`.
2. **Ручний імпорт** (`parse_import`, нижче).

### ⚠️→✅ Перенесення сесії на бекенд (26.08.2026 — не вийшло; 28.08.2026 — вийшло інакше)

> **UPD 28.08.2026:** нижче — три способи, що НЕ спрацювали (голий реплей
> cookie, обмін коду з бекенда, Playwright-автозаповнення). Четвертий —
> headed-логін руками + `context.storage_state()` — реалізовано в
> `scripts/dnipro_m_login.py`, але **ще не підтверджено живим збором**:
> відкрите питання, чи переживе цей `storage_state` перенос у свіжий
> headless-контекст (сам скрипт це й перевіряє кроком 4 — якщо ні, друкує
> попередження й НЕ зберігає токен). Живу сесію в built-in браузері
> Claude перевірено: `/auth/auth/user-info/`, `/cabinet/order/history/`
> (5 замовлень), `bonuses-info`, warranty-PDF — усі 200. Див. "✅ Робочий
> шлях для живого збору" вище.

На відміну від Fozzy (де досить один раз вставити JWT з mitmproxy-перехвату),
голий реплей **не спрацював жодним із двох способів**, перевірених наживо:

1. **Реплей готових cookie.** Скопійовано весь заголовок `Cookie` +
   `X-CSRF-Token` з активного, щойно виконаного GET-запиту реального браузера
   (перевірено двічі: один раз ~20 хв по тому — можна було списати на TTL
   `session_hash` (`Max-Age=1440`, 24 хв), другий раз — **через 18 секунд**
   після перехвату, той самий результат). Бекенд отримує `403 {"message":
   "Необхідно увійти"}` — і сервер видає **зовсім новий** `PHPSESSID` у
   відповіді, ніби переданого cookie не існувало взагалі.
2. **Сам обмін SMS-коду на сесію, виконаний з бекенда.** Реальний браузер
   виконав `/phone-verification/` (captcha пройдено користувачем, SMS
   надіслано) — бекенд одразу підхопив щойно видані `session_hash`/cookie з
   ЦЬОГО запиту (тобто сесія жодного разу не торкалася `/check-code/` раніше)
   і сам виконав `POST /check-code/` з кодом із SMS. Результат: `400 "Не
   вдалося перевірити передані дані"` (стандартна Yii2 CSRF-помилка) і знову
   **новий** `PHPSESSID` у відповіді.

В обох випадках бекенд і телефон були в одній домашній мережі (той самий
зовнішній IP через NAT) — тобто це не банальна прив'язка по IP. Найімовірніше
— TLS/JA3-відбиток з'єднання або інша прив'язка на рівні транспорту, той
самий клас захисту, що вже блокує `getReceipts` в АТБ через Cloudflare.
Далі підбирати заголовки/відбитки, щоб обійти це, **свідомо не пробували** —
за визначенням проєкту це вже був би обхід антибот-захисту, а не reverse
engineering.

3. **Playwright, що САМ заповнює форму входу — не підійшло.** Спроба зробити
   скрипт за зразком `silpo_login.py` (Playwright сам вписує телефон і тисне
   "Отримати код", людина лише вводить код із SMS): сайт **прямо розпізнав
   автоматизацію** — `{"status":"error","message":"Не вдалося підтвердити, що
   запит виконано людиною. Спробуйте ще раз."}` на `/phone-verification/`.
   Спрацювало саме на Playwright-керований Chromium (типові ознаки —
   `navigator.webdriver`, headless-сигнатури), не на ручний клік. Тюнити далі
   (stealth-плагіни, підміна `navigator.webdriver`) = обхід антибот-захисту,
   свідомо не робимо.

### ⚠️→ виправлено: справа була в «людськості» вводу, не в автоматизації (28.08.2026)

Пункт 3 вище («сайт розпізнав автоматизацію») **виявився неточним**. Реальний
винуватець знайдений відсіюванням наживо (два ідентичні прогони підряд):
**`page.expect_response(...)` навколо кліку «Отримати код» валить reCAPTCHA v3**.
Той самий флоу без обгортки CDP-очікуванням відповіді проходить, з нею —
`{"status":"error","message":"Не вдалося підтвердити, що запит виконано
людиною"}`. Тому `start_login`/`confirm_login` тепер визначають успіх/помилку
через `_await_login_step` (полінг DOM: `#code` з'явилось / `user-info` = auth /
текст помилки в модалці), а не через `expect_response`.

Другорядні фактори, які теж знижують score (усунуті): миттєвий `fill()` замість
`_human_type`, захід одразу на `/login/` замість головна→модалка, `locale`/
`timezone` override у контексті, швидкий retry. `channel="chromium"` дає
«реальніший» фінгерпринт (`window.chrome`, 5 plugins), але для входу він НЕ
використовується — `backend/dnipro.py` (робочий зразок) заходить bare, і цього
досить. `navigator.webdriver` лишається `true` в обох — отже це не вирішальний
сигнал.

**⚠️ Score reCAPTCHA v3 адаптивний і ВИЧЕРПУЄТЬСЯ.** 28.08 перевірено болісно:
ідентичний коректний флоу вранці проходив, після ~12-13 спроб з одного IP за
день став стабільно давати `"Не вдалося підтвердити…"` — навіть standalone,
навіть з ідеальним «людським» вводом. Відновлюється за години (іноді добу).
Наслідок: **не ганяти `start_login` поспіль для дебагу**. Якщо оточення вже
«спалене» — єдине, що працює одразу: вхід через Google у headed-браузері
(`scripts/dnipro_m_login.py`, кнопка Google — оминає `/phone-verification/`
цілком) або ручний імпорт.

### ✅ Робочий шлях — ручний імпорт (`parse_import`, як АТБ)

Користувач сам відкриває "Мої замовлення" в кабінеті сайту (справжня сесія
свого браузера), у DevTools → Network копіює тіло відповіді:

```
GET /cabinet/order/history/    -> {"status":"success","orders_all","orders_left",
                                    "offset","orders":[...]}
```

і вставляє через "Ручний імпорт" в UI акаунта (`POST /api/accounts/{id}/import`).
Перевірено вживу: 5/5 замовлень імпортовано, з позиціями, знижками,
бонусами й прапорцем гарантії.

`orders[]` — order_number (= `external_id`), created_at (ISO, з таймзоною),
creation_provider (`"erp"` = офлайн/самовивіз із салону, `"site"` = онлайн-
замовлення з доставкою), delivery_name/delivery_data.address (адреса
крамниці для самовивозу) або delivery_city_name (місто для доставки),
payment_name, total, **current_purchase_bonuses** (нараховано — тільки для
`site`-замовлень, у `erp` завжди порожній рядок), **bonuses_debiting_amount**
(списано бонусами), **has_warranty_card** (bool — чи є гарантійний талон),
order_items[] — code (= barcode), name, price (за одиницю до знижки),
**discount_cost** (за одиницю ПІСЛЯ знижки — перевірено: price=399,
discount=81, discount_cost=318), itemQuantity.

### ✅ Баланс + персональна знижка

```
GET /cabinet/default/bonuses-info/
-> {"status":"success","data":{"total_bonuses","rate","current_rate",
                                "total_purchase","next_rate","until_next_bonus_rate"}}
```

`total_bonuses` -> `LoyaltyBalance.total`, `rate` (поточний % кешбеку/знижки
за рівнем лояльності) -> новий `LoyaltyBalance.discount_percent` (загальне
поле, додане саме під цей магазин — кешується так само, як `bonus_balance`,
в окрему колонку `Account.discount_percent`). Потребує тієї самої сесії, що
й чеки — наразі так само недоступний з бекенда (див. вище).

### ✅ Гарантійний талон — PDF, дата парситься з тексту (26.08.2026)

```
GET /cabinet/order/get-order-warranty-card/?order_id=<order_number>
-> сирий PDF (не JSON)
```

Живий проксі (`GET /api/receipts/{id}/warranty` через `get_warranty_document`)
потребує тієї самої сесії, що й чеки — тож наразі недоступний з бекенда (див.
вище). Замість цього — ручне завантаження: `POST /api/receipts/{id}/warranty`
(multipart, як фото чека) приймає PDF, завантажений користувачем зі свого
браузера, і парсить його текст (`pypdf`, `PdfReader(...).extract_text()` —
підтверджено, що це не скан-картинка, текстовий шар витягується чисто).

Текст реального талону Дніпро-М (перевірено на живому документі):
```
Ваш інструмент:
- Фен промисловий Dnipro-M GH-200
...
Гарантія діє з 09.06.2026 по 09.06.2029
```
Регексп `Гарант\S*\s+ді[єе]\s+з\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})`
бере другу дату як `Receipt.warranty_expires_at`. Best-effort і не специфічний
для одного магазину технічно (лежить у `api/receipts.py`, не в адаптері) —
просто поки калібрований лише під українську фразу Дніпро-М; для іншого
магазину з іншим формулюванням просто не спрацює, `has_warranty` і сам PDF
однаково збережуться.

`Receipt.warranty_pdf_filename` — файл на диску (`data/warranty/{id}.pdf`,
той самий патерн, що й `photo_filename`) — GET спершу віддає його, і лише
якщо файлу нема, падає на живий проксі через адаптер. `has_warranty`/
`warranty_expires_at` також PATCH-abli вручну (`ReceiptUpdate`) — для чеків
із будь-якої мережі, не лише Дніпро-М.

### ✅ Пошук по каталогу — публічний, авторизація не потрібна взагалі

```
GET /search/drop-down/?q=<текст>
-> {"config",...,"suggests":[...],"products":[{"id","name":{"ru","uk","en"},
    "price_new","price_old","product_photo","url","is_available"}],"categories":[...]}
```

Перевірено вживу без жодних cookie — той самий публічний віджет-патерн, що
й `api.multisearch.io` в АТБ. `url` — повне абсолютне посилання на сторінку
товару (є одразу, на відміну від Fora/Fozzy/Сільпо). Реалізовано в
`DniproMAdapter.search_catalog`.

---

## METRO UA (`com.supersmart.android.metro.ua`) — ⏸ отложено

Приложение — self-checkout ("Scan & Go"), .NET/Xamarin (реверс через IL-дизассемблирование
`ikdasm`, т.к. Play Store недоступен на этом окружении).

Найдено и **подтверждено живым запросом** (сервер вернул осмысленную ошибку о незарегистрированном
пользователе, что подтверждает правильность пути и структуры):
```
POST https://metroua.supersmart.me/services/v4/order/historical/paid
Authorization: Bearer <JWT>
Body: {"id": 0, "customerId": "<device-uuid>"}
```
Возвращает `Shared.OrderSearchResponse` → список `OrderLite`.

Детали конкретного чека:
```
POST https://metroua.supersmart.me/services/v4/lasa/invoice   (генерация)
+ GetInvoiceFileServiceAgent по orderId                        (получение файла чека)
```

**Блокер:** `customerId` должен быть зарегистрирован как "Shopper" — это происходит при
сканировании тележки в реальном магазине при первом входе. После переустановки приложения
customerId сбросился на новый, незарегистрированный UUID.

**План:** пользователь подготовит QR тележки и личной карты в магазине, чтобы пройти
регистрацию Shopper и получить доступ к реальной истории.

---

## Инфраструктура (эта машина, Linux)

- Android SDK: `~/Android/Sdk` (platform-tools, build-tools 33.0.2, emulator — **эмулятор
  оказался нестабилен из-за вложенной виртуализации (nested KVM/AMD SVM), не используем**)
- mitmproxy: `~/mitm-venv` (venv), сертификат CA в `~/.mitmproxy/`
- apktool: `/tmp/apktool.jar` (2.9.3)
- Debug keystore для подписи патченных APK: `~/.android/debug.keystore` (pass: `android`,
  alias: `androiddebugkey`) — все патченные приложения подписаны ИМ, а не оригинальным ключом
  издателя (поэтому оригинал перед установкой патченной версии нужно удалять через `adb uninstall`)
- Реальный телефон (Xiaomi/POCO, `rosemary`) подключён по USB, adb работает благодаря
  постоянному udev-правилу `/etc/udev/rules.d/51-android.rules` (vendor 0e8d — MediaTek)
- mitmproxy запущен в фоне на порту 8080, лог трафика: `/tmp/mitm_capture.log`
- Прокси на телефоне: Wi-Fi → вручную → хост `192.168.1.50`, порт `8080`
  (IP машины в локальной сети может измениться при переподключении к Wi-Fi!)
- CA-сертификат mitmproxy установлен на телефоне как **пользовательский** (не системный,
  root не использовался)

### Метод патчинга APK (для повторения на новых версиях приложений)
1. `adb pull` всех частей приложения (base.apk + все `split_config.*.apk`) с телефона
   (не скачивать APK со сторонних сайтов!)
2. `java -jar apktool.jar d -f -o <dir> base.apk`
3. В `res/xml/network_security_config.xml` добавить/поправить `<base-config>` с
   `<certificates src="user" />` в `<trust-anchors>` (сохраняя существующие домен-specific
   pinning-конфиги, если есть, как у Fora для tile-geo.fora.ua)
4. Если конфига не было — прописать `android:networkSecurityConfig="@xml/network_security_config"`
   в тег `<application>` манифеста
5. `java -jar apktool.jar b --use-aapt2 <dir> -o unsigned.apk` (флаг `--use-aapt2` обязателен —
   без него часто падает с `<item> tag requires a 'drawable' attribute` из-за `@null` в ресурсах;
   если и это не помогает — искать через `grep -rl '"@null"' res --include="*.xml"` и вручную
   менять проблемный `android:drawable="@null"` на `android:drawable="@android:color/transparent"`)
6. `zipalign -f -p 4 unsigned.apk aligned.apk`
7. `apksigner sign --ks ~/.android/debug.keystore ... aligned.apk` — **все части (base + splits)
   подписывать ОДНИМ и тем же ключом**, иначе `adb install-multiple` откажет с mismatch
8. `adb uninstall <package>` (сотрёт локальные данные приложения — пользователь потеряет
   локальный кэш, но не серверные данные, если account завязан на телефон/логин, а не на
   device-id, как выяснилось на примере METRO)
9. `adb install-multiple base_signed.apk split1_signed.apk ...`
10. На MIUI/Xiaomi при первой установке появляется системный диалог подтверждения на самом
    телефоне — установку нужно повторить после того, как пользователь его подтвердит

## Следующие шаги (не начато)
- Спроектировать серверную часть/парсер: единая модель чека, БД, периодический опрос 3-4 API,
  хранение и обновление токенов (включая обработку истечения JWT и повторной OTP-авторизации)
- Поймать детали товарных позиций для онлайн-заказов Fora
- Вернуться к METRO после регистрации Shopper пользователем
