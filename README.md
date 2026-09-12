# Receipt Scanner

## Telegram-бот

У розділі **Налаштування → Інтеграції та доступ** задайте токен від BotFather
та увімкніть бота. Користувач надсилає боту `/start`, після чого власник
підтверджує або відхиляє заявку в налаштуваннях. Авторизовані чати можна відкликати.

Бот приймає фото/QR чеків та публічні HTTP(S)-посилання. Фіскальні QR
завантажуються через налаштований токен ДПС; зображення проходять загальний
конвеєр OCR/AI і дедуплікацію. Доступні також команди `/find`, `/spending`,
`/receipts` і `/add`, які використовують ті самі функції, що експонуються MCP.

Один Docker-образ містить production frontend, FastAPI backend, Chromium та
Xvfb. Веб-інтерфейс і API працюють на одному порту `8001`.

## Варианты запуска

Есть три режима:

- `dev` — backend в Docker с hot-reload, frontend локально через Vite;
- `Docker` — готовое production-приложение одним контейнером;
- `Home Assistant` — тот же production-образ как локальный app для Supervisor.

Для локального frontend необходим Node.js `22.18+` или `24.12+`. Проект требует
современный Node.js из-за Vite 8. Проверить версию:

```bash
node --version
```

Node.js `12` не поддерживается. После обновления Node.js зависимости frontend
нужно установить заново:

```bash
cd frontend
rm -rf node_modules
npm ci
```

Например, с `nvm`:

```bash
nvm install 24
nvm use 24
```

## Розробка с hot-reload

Для разработки backend всегда запускается в Docker с Chromium/Xvfb и автоматически
перезапускается при изменении Python-файлов. Frontend запускается локально через
Vite и обновляется в браузере сразу после изменения Vue/TypeScript.

Терминал 1 — backend:

```bash
PORT=18002 ./dev-docker.sh up
```

Терминал 2 — frontend:

```bash
cd frontend
npm ci             # один раз после установки Node.js 22/24
VITE_API_BASE=http://localhost:8001 npm run dev -- --host 0.0.0.0
```

Откройте `http://localhost:5173`. Изменения Vue/TypeScript применяются сразу,
изменения Python автоматически перезапускают backend. Если порт `18002` свободен,
можно использовать стандартный `8001` и не задавать `PORT`/`VITE_API_BASE`.

Остановить dev backend:

```bash
./dev-docker.sh down
```

## Docker production

Собрать и запустить production-версию одним контейнером:

```bash
./local-docker.sh up
```

Откройте `http://localhost:8001`. Если `8001` занят:

```bash
PORT=18001 ./local-docker.sh up
```

Полезные команды:

```bash
./local-docker.sh status
./local-docker.sh logs
./local-docker.sh restart
./local-docker.sh stop
./local-docker.sh down
```

`down` удаляет только контейнер. База, фото, PDF и браузерные профили остаются
в `backend/data`.

## Локальная LLM (Ollama) — опционально

Для классификации категорий товаров и нормализации запросов списка покупок без
облака. Модель — **Qwen2.5-3B-Instruct** (лучшая для UA/RU в своём классе,
работает на Orange Pi 5 Plus).

```bash
./ollama-docker.sh native   # ставит Ollama на хост (~1.5 GB), тюнит systemd, тянет модель
```

Вариант в Docker — `./ollama-docker.sh up`, но образ `ollama/ollama` тянет
CUDA+ROCm и весит ~4.5 GB; на CPU-машине это лишнее, бери `native`.

Затем на странице **Налаштування → AI · моделі**: включить «Локальна модель»,
URL `http://<хост>:11434`, модель `qwen2.5:3b`, `num_ctx` 8192, маршрутизацию
`classify`/`normalize` → «Тільки локально». OCR чеков остаётся на облаке.

Другую модель — `OLLAMA_MODEL=qwen2.5:1.5b ./ollama-docker.sh native`.
Тюнинг под RK3588 (пиннинг на большие ядра A76) — в systemd-оверрайде
`/etc/systemd/system/ollama.service.d/receipt-scanner.conf`.

## Home Assistant Supervised — локальный app

1. Скопируйте каталог проекта на Home Assistant host в
   `/usr/share/hassio/addons/local/receipt_scanner` (або `/addons/receipt_scanner`
   через Samba/SSH add-on).
2. В Home Assistant откройте **Settings → Apps → App store** и нажмите
   **Check for updates / Reload**.
3. Установите локальный app **Receipt Scanner**, запустите его и включите
   показ в боковой панели.
4. Откройте UI через **Open Web UI**. Данные сохраняются в постоянном volume
   Supervisor и включаются в backup app.

По умолчанию app доступен только через защищённый Home Assistant Ingress.
Порт `8001` можно отдельно включить на вкладке Network.
