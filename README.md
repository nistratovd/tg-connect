# TG Connect

TG Connect — сервис-маршрутизатор между Telegram Bot API и Битрикс. Он нужен для случаев, когда Битрикс находится в закрытом контуре и не может напрямую работать с Telegram. Сервис принимает запросы от Битрикс в формате, близком к Telegram Bot API, отправляет их в Telegram через `aiogram`, принимает Telegram webhook updates и пересылает их в настроенный endpoint Битрикс.

## Возможности текущей версии

- Telegram-compatible HTTP API вида `/bot{token_or_alias}/{method}`.
- Поддержка базовых Telegram Bot API методов:
  - `sendMessage`;
  - `sendPhoto`;
  - `sendDocument`;
  - `editMessageText`;
  - `deleteMessage`;
  - `answerCallbackQuery`.
- Webhook endpoint Telegram → Битрикс: `/webhooks/telegram/{bot_key}`.
- Административная панель для настройки нескольких ботов.
- Защита запросов от Битрикс:
  - HMAC-подпись;
  - timestamp-защита от replay-атак;
  - allowlist IP;
  - rate limit;
  - ограничение размера тела запроса.
- Шифрование Telegram token/HMAC secret/legacy secret в локальном state-файле.
- Маскирование секретов в событиях и ответах.
- Дедупликация Telegram updates по `update_id` на уровне persistent delivery queue.
- File-backed delivery queue, background-доставка и dead-letter фиксация ошибок доставки.
- Health и metrics endpoints:
  - `/health/live`;
  - `/health/ready`;
  - `/metrics`.

> Важно: текущая очередь доставки хранится в JSON-файле и переживает рестарт одного процесса, но для production с несколькими инстансами ее следует заменить на PostgreSQL/Redis/RabbitMQ-backed backend. Rate limit пока остается in-memory.

---

## Архитектура обмена

```text
Битрикс в закрытом контуре
        |
        | HTTPS + HMAC / IP allowlist
        v
TG Connect
        |
        | aiogram / Telegram Bot API
        v
Telegram
```

Обратное направление:

```text
Telegram webhook update
        |
        v
TG Connect: /webhooks/telegram/{bot_key}
        |
        | HTTP POST JSON
        v
Endpoint Битрикс
```

---

## Требования

- Python 3.11 или выше.
- Доступ сервиса TG Connect к Telegram Bot API.
- Доступ сервиса TG Connect к endpoint Битрикс.
- Telegram bot token от BotFather.
- Секретный ключ `TG_CONNECT_MASTER_KEY` для шифрования токенов и HMAC-секретов.

---

## Быстрый локальный запуск

### 1. Создать и активировать virtualenv

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Установить зависимости

```bash
pip install -e '.[test]'
```

Если тестовые зависимости не нужны:

```bash
pip install -e .
```

### 3. Настроить переменные окружения

Минимальный набор для локального запуска:

```bash
export TG_CONNECT_MASTER_KEY='replace-with-long-random-master-key'
export ADMIN_PASSWORD='change-me'
export ADMIN_SESSION_TOKEN='change-me-session-token'
```

Для генерации ключей можно использовать:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

### 4. Запустить приложение

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

После запуска доступны:

- API: `http://localhost:8000`;
- административная панель: `http://localhost:8000/admin/`;
- healthcheck: `http://localhost:8000/health/live`;
- readiness: `http://localhost:8000/health/ready`;
- metrics: `http://localhost:8000/metrics`.

---

## Публикация и настройка проекта

### Рекомендуемый порядок публикации

1. Подготовить сервер или контейнерную среду, из которой есть исходящий доступ к Telegram API.
2. Установить Python 3.11+.
3. Развернуть код проекта.
4. Установить зависимости.
5. Настроить обязательные переменные окружения.
6. Запустить приложение через `uvicorn`, systemd, supervisor или контейнерный runtime.
7. Настроить reverse proxy с TLS.
8. Создать бота в административной панели.
9. Настроить Telegram webhook на URL TG Connect.
10. Настроить Битрикс на отправку запросов в Telegram-compatible API TG Connect.

### Пример запуска через systemd

Файл `/etc/systemd/system/tg-connect.service`:

```ini
[Unit]
Description=TG Connect
After=network.target

[Service]
WorkingDirectory=/opt/tg-connect
Environment=TG_CONNECT_MASTER_KEY=replace-with-long-random-master-key
Environment=ADMIN_PASSWORD=change-me
Environment=ADMIN_SESSION_TOKEN=change-me-session-token
Environment=ADMIN_STATE_PATH=/var/lib/tg-connect/admin_state.json
ExecStart=/opt/tg-connect/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=tg-connect
Group=tg-connect

[Install]
WantedBy=multi-user.target
```

Применение:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-connect
sudo systemctl start tg-connect
sudo systemctl status tg-connect
```

### Пример reverse proxy через Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name tg-connect.example.com;

    ssl_certificate /etc/letsencrypt/live/tg-connect.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tg-connect.example.com/privkey.pem;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> Если используется `allowed_ips`, учитывайте, что приложение проверяет первый IP из `X-Forwarded-For`. Reverse proxy должен корректно передавать этот заголовок только от доверенной сети.

---

## Переменные окружения

### Основные

| Переменная | Обязательна | Назначение |
|---|---:|---|
| `TG_CONNECT_MASTER_KEY` | Да, если используются секреты в admin state | Master key для шифрования token/secret в state-файле. |
| `TG_CONNECT_MASTER_KEY_FILE` | Нет | Путь к файлу с master key. Используется, если не задан `TG_CONNECT_MASTER_KEY`. |
| `TG_CONNECT_MASTER_KEY_CMD` | Нет | Команда, возвращающая master key. Использовать осторожно. |
| `ADMIN_PASSWORD` | Рекомендуется | Пароль входа в админку. По умолчанию — `admin`, что небезопасно. |
| `ADMIN_SESSION_TOKEN` | Рекомендуется | Значение cookie-сессии админки. Если не задано, используется `ADMIN_PASSWORD`. |
| `ADMIN_STATE_PATH` | Нет | Путь к JSON-файлу состояния админки. По умолчанию `data/admin_state.json`. |

### Конфигурация ботов через env

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_CONFIGS` | JSON-массив полных конфигураций ботов. |
| `TELEGRAM_BOT_ALIASES` | Legacy JSON-объект вида `{"alias":"telegram-token"}`. |
| `BITRIX_BOT_WEBHOOK_URLS` | JSON-объект соответствия `{bot_key: bitrix_webhook_url}` для webhook forwarding. |

Пример `TELEGRAM_BOT_CONFIGS`:

```bash
export TELEGRAM_BOT_CONFIGS='[
  {
    "id": "support",
    "name": "support",
    "telegram_bot_token": "123456:ABCDEF",
    "bitrix_webhook_url": "https://bitrix.internal/tg/webhook",
    "enabled": true,
    "hmac_secret": "bitrix-to-tg-secret",
    "allowed_ips": ["10.0.0.0/8"],
    "rate_limit": 120,
    "timestamp_tolerance_seconds": 300,
    "max_request_body_bytes": 1048576
  }
]'
```

Пример legacy-настроек:

```bash
export TELEGRAM_BOT_ALIASES='{"support":"123456:ABCDEF"}'
export BITRIX_BOT_WEBHOOK_URLS='{"support":"https://bitrix.internal/tg/webhook"}'
```

### Настройки доставки в Битрикс

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `BITRIX_FORWARD_TIMEOUT_SECONDS` | `5` | Timeout HTTP-запроса в Битрикс. |
| `BITRIX_FORWARD_RETRY_ATTEMPTS` | `3` | Количество попыток доставки. |
| `BITRIX_FORWARD_RETRY_BACKOFF_SECONDS` | `0.5` | Базовая задержка между retry. |

---

## Административная часть

### Как получить доступ

1. Откройте в браузере:

   ```text
   https://<host>/admin/
   ```

2. Если сессии нет, сервис перенаправит на:

   ```text
   https://<host>/admin/login
   ```

3. Введите пароль из переменной окружения `ADMIN_PASSWORD`.

4. После успешного входа появится dashboard со списком ботов и последними событиями.

### Важные замечания по безопасности админки

- Обязательно задайте `ADMIN_PASSWORD`; не используйте значение по умолчанию `admin`.
- Обязательно задайте отдельный `ADMIN_SESSION_TOKEN`.
- Закройте `/admin/` на уровне reverse proxy, VPN или allowlist IP.
- Используйте только HTTPS.
- Не публикуйте административную панель в открытый интернет без дополнительной защиты.

### Создание бота через админку

1. Перейдите в `/admin/`.
2. Нажмите **Создать бота**.
3. Заполните поля:
   - **ID бота** — внутренний ключ, например `support`.
   - **Название** — понятное имя, например `Support Bot`.
   - **Telegram bot token** — токен от BotFather.
   - **Endpoint Битрикс** — URL, куда TG Connect будет отправлять Telegram updates.
   - **Секрет webhook** — legacy secret, если используется.
   - **HMAC secret** — секрет для подписи запросов Битрикс → TG Connect.
   - **Разрешенные IP** — список IP/CIDR, например `10.0.0.0/8, 192.168.1.10`.
   - **Rate limit** — лимит запросов в минуту на бота/IP/метод.
   - **Допуск timestamp** — окно валидности подписи в секундах.
   - **Максимальный размер body** — лимит тела запроса.
   - **Бот включен** — должен быть отмечен для активной работы.
4. Нажмите **Сохранить**.

После сохранения token и секреты будут записаны в `ADMIN_STATE_PATH` в зашифрованном виде. Для шифрования должен быть настроен master key.

### Редактирование бота

1. Откройте `/admin/`.
2. Нажмите **Редактировать** рядом с нужным ботом.
3. Измените настройки.
4. Нажмите **Сохранить**.

Если при редактировании в поле token или secret отображается `***`, это означает, что существующее значение будет сохранено без изменений.

### Включение и выключение бота

На dashboard рядом с ботом есть кнопка:

- **Выключить** — отключает бота;
- **Включить** — снова активирует бота.

После изменения статуса реестр ботов перезагружается без полного рестарта сервиса.

### Проверка Telegram и Битрикс

На dashboard есть кнопка **Проверить Telegram и Битрикс**.

Она выполняет:

1. Проверку Telegram token через `getMe`.
2. Проверку доступности endpoint Битрикс через HTTP GET.

Результат отображается сообщением на dashboard.

### Просмотр событий

Dashboard показывает последние входящие и исходящие события:

- направление;
- бот;
- статус;
- ошибка доставки webhook, если она была.

Секреты в событиях маскируются.

### Очередь доставки и dead-letter

В административной панели доступен раздел **Очередь доставки**:

```text
https://<host>/admin/delivery
```

В нем можно увидеть:

- последние события Telegram → Битрикс;
- текущий статус `queued`, `processing`, `delivered` или `dead_letter`;
- количество попыток доставки;
- endpoint Битрикс;
- ошибку последней доставки.

Для `dead_letter` событий доступна кнопка **Повторить доставку**. После нажатия событие возвращается в статус `queued` и доставляется в Битрикс в background-задаче.

---

## Настройка обмена с Битрикс

Обмен состоит из двух направлений:

1. **Битрикс → TG Connect → Telegram** — Битрикс вызывает Telegram-compatible API сервиса.
2. **Telegram → TG Connect → Битрикс** — Telegram отправляет webhook update в TG Connect, а сервис пересылает JSON в endpoint Битрикс.

---

# 1. Битрикс → Telegram

## URL вызова

```text
POST https://<host>/bot{bot_key_or_token}/{method}
```

Где:

- `<host>` — адрес TG Connect;
- `{bot_key_or_token}` — внутренний ID/alias бота или прямой Telegram token;
- `{method}` — поддерживаемый Telegram Bot API метод.

Рекомендуется использовать внутренний `bot_key`, например:

```text
POST https://tg-connect.example.com/botsupport/sendMessage
```

А не прямой token:

```text
POST https://tg-connect.example.com/bot123456:ABCDEF/sendMessage
```

Так безопаснее: token не попадает в URL, логи reverse proxy и историю запросов.

## Пример sendMessage

```bash
curl -X POST 'https://tg-connect.example.com/botsupport/sendMessage' \
  -H 'Content-Type: application/json' \
  -d '{
    "chat_id": 123456789,
    "text": "Сообщение из Битрикс"
  }'
```

Ответ при успехе:

```json
{
  "ok": true,
  "result": {
    "message_id": 42,
    "chat": {
      "id": 123456789
    },
    "text": "Сообщение из Битрикс"
  }
}
```

## Пример editMessageText

```bash
curl -X POST 'https://tg-connect.example.com/botsupport/editMessageText' \
  -H 'Content-Type: application/json' \
  -d '{
    "chat_id": 123456789,
    "message_id": 42,
    "text": "Обновленный текст"
  }'
```

## Пример deleteMessage

```bash
curl -X POST 'https://tg-connect.example.com/botsupport/deleteMessage' \
  -H 'Content-Type: application/json' \
  -d '{
    "chat_id": 123456789,
    "message_id": 42
  }'
```

## Пример answerCallbackQuery

```bash
curl -X POST 'https://tg-connect.example.com/botsupport/answerCallbackQuery' \
  -H 'Content-Type: application/json' \
  -d '{
    "callback_query_id": "1234567890",
    "text": "Принято"
  }'
```

---

## Подпись запросов Битрикс → TG Connect

Если для бота задан `hmac_secret` или legacy `secret`, каждый запрос к `/bot{bot_key}/{method}` должен быть подписан.

Сервис ожидает заголовки:

| Заголовок | Назначение |
|---|---|
| `x-tg-timestamp` | Unix timestamp в секундах. |
| `x-tg-signature` | HMAC-SHA256 подпись. |

Подписываемая строка:

```text
{timestamp}.{raw_body}
```

Алгоритм:

```text
hex(hmac_sha256(secret, timestamp + "." + raw_body))
```

### Пример генерации подписи на Python

```python
import hmac
import json
import time
from hashlib import sha256

secret = "bitrix-to-tg-secret"
payload = {"chat_id": 123456789, "text": "Сообщение из Битрикс"}
body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
timestamp = str(int(time.time()))
signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, sha256).hexdigest()

print(timestamp)
print(signature)
print(body.decode())
```

### Пример signed curl

```bash
BODY='{"chat_id":123456789,"text":"Сообщение из Битрикс"}'
TS=$(date +%s)
SECRET='bitrix-to-tg-secret'
SIG=$(python - <<PY
import hmac, os
from hashlib import sha256
secret = os.environ['SECRET'].encode()
ts = os.environ['TS'].encode()
body = os.environ['BODY'].encode()
print(hmac.new(secret, ts + b'.' + body, sha256).hexdigest())
PY
)

curl -X POST 'https://tg-connect.example.com/botsupport/sendMessage' \
  -H 'Content-Type: application/json' \
  -H "x-tg-timestamp: $TS" \
  -H "x-tg-signature: $SIG" \
  -d "$BODY"
```

> Важно: подпись строится по raw body. Битрикс должен отправить ровно то же тело, по которому была посчитана подпись. Изменение пробелов, порядка сериализации JSON или кодировки после расчета подписи приведет к ошибке `invalid_signature`.

---

# 2. Telegram → Битрикс

## Endpoint TG Connect для Telegram webhook

Для каждого бота Telegram должен отправлять updates в TG Connect:

```text
https://<host>/webhooks/telegram/{bot_key}
```

Пример:

```text
https://tg-connect.example.com/webhooks/telegram/support
```

`bot_key` должен совпадать с ID/alias бота в TG Connect или ключом в `BITRIX_BOT_WEBHOOK_URLS`.

## Настройка webhook в Telegram

Через Telegram Bot API:

```bash
curl -X POST 'https://api.telegram.org/bot123456:ABCDEF/setWebhook' \
  -d 'url=https://tg-connect.example.com/webhooks/telegram/support'
```

Проверка:

```bash
curl 'https://api.telegram.org/bot123456:ABCDEF/getWebhookInfo'
```

Удаление webhook:

```bash
curl -X POST 'https://api.telegram.org/bot123456:ABCDEF/deleteWebhook'
```

## Настройка endpoint Битрикс

Endpoint Битрикс задается одним из способов:

1. В административной панели в поле **Endpoint Битрикс**.
2. Через переменную `BITRIX_BOT_WEBHOOK_URLS`.

Пример env:

```bash
export BITRIX_BOT_WEBHOOK_URLS='{"support":"https://bitrix.internal/local/tg-connect/webhook.php"}'
```

Когда Telegram отправит update на TG Connect, сервис:

1. распарсит payload как Telegram update;
2. проверит `update_id` на дубликат;
3. поставит событие в persistent delivery queue;
4. вернет Telegram успешный прием webhook;
5. в background-задаче отправит JSON в endpoint Битрикс;
6. зафиксирует статус доставки;
7. при ошибке выполнит retry;
8. при итоговой ошибке отметит событие как `dead_letter`;
9. позволит повторить dead-letter доставку из админки.

## Формат payload, который получит Битрикс

TG Connect пересылает в Битрикс JSON, максимально близкий к Telegram webhook update.

Пример входящего сообщения:

```json
{
  "update_id": 100000001,
  "message": {
    "message_id": 10,
    "date": 1710000000,
    "chat": {
      "id": 123456789,
      "type": "private"
    },
    "from": {
      "id": 123456789,
      "is_bot": false,
      "first_name": "User"
    },
    "text": "Привет"
  }
}
```

Пример callback query:

```json
{
  "update_id": 100000002,
  "callback_query": {
    "id": "callback-id",
    "from": {
      "id": 123456789,
      "is_bot": false,
      "first_name": "User"
    },
    "chat_instance": "chat-instance",
    "data": "button_payload"
  }
}
```

## Требования к endpoint Битрикс

Endpoint Битрикс должен:

1. Принимать `POST`.
2. Принимать `Content-Type: application/json`.
3. Быстро возвращать HTTP `2xx` при успешной обработке.
4. Быть доступным с сервера TG Connect.
5. Быть идемпотентным по `update_id`, потому что Telegram и TG Connect могут повторять доставку при ошибках.

Если Битрикс вернет HTTP `4xx` или `5xx`, TG Connect считает доставку неуспешной и применяет retry policy.

---

## Пример обработчика webhook в Битрикс/PHP

```php
<?php
$raw = file_get_contents('php://input');
$update = json_decode($raw, true);

if (!is_array($update)) {
    http_response_code(400);
    echo 'invalid json';
    exit;
}

$updateId = $update['update_id'] ?? null;
$message = $update['message'] ?? null;
$callback = $update['callback_query'] ?? null;

// TODO: проверьте идемпотентность по $updateId в своей БД.

if ($message && isset($message['text'])) {
    $chatId = $message['chat']['id'] ?? null;
    $text = $message['text'];

    // TODO: обработайте сообщение в бизнес-логике Битрикс.
}

if ($callback) {
    $callbackId = $callback['id'] ?? null;
    $data = $callback['data'] ?? null;

    // TODO: обработайте callback button payload.
}

http_response_code(200);
echo json_encode(['ok' => true], JSON_UNESCAPED_UNICODE);
```

---

## Пример отправки сообщения из Битрикс/PHP в Telegram через TG Connect

```php
<?php
function tgConnectRequest(string $baseUrl, string $botKey, string $method, array $payload, ?string $secret = null): array
{
    $body = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    $headers = [
        'Content-Type: application/json',
    ];

    if ($secret !== null) {
        $timestamp = (string) time();
        $signature = hash_hmac('sha256', $timestamp . '.' . $body, $secret);
        $headers[] = 'x-tg-timestamp: ' . $timestamp;
        $headers[] = 'x-tg-signature: ' . $signature;
    }

    $url = rtrim($baseUrl, '/') . '/bot' . rawurlencode($botKey) . '/' . $method;

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => $body,
        CURLOPT_TIMEOUT => 10,
    ]);

    $responseBody = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if ($responseBody === false) {
        $error = curl_error($ch);
        curl_close($ch);
        throw new RuntimeException('TG Connect request failed: ' . $error);
    }

    curl_close($ch);
    $decoded = json_decode($responseBody, true);

    if ($httpCode >= 400 || !is_array($decoded) || empty($decoded['ok'])) {
        throw new RuntimeException('TG Connect returned error: HTTP ' . $httpCode . ' ' . $responseBody);
    }

    return $decoded;
}

$response = tgConnectRequest(
    'https://tg-connect.example.com',
    'support',
    'sendMessage',
    [
        'chat_id' => 123456789,
        'text' => 'Сообщение из Битрикс',
    ],
    'bitrix-to-tg-secret'
);
```

---

## Проверка работоспособности

### Проверить, что сервис жив

```bash
curl 'https://tg-connect.example.com/health/live'
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

### Проверить готовность сервиса

```bash
curl 'https://tg-connect.example.com/health/ready'
```

Ожидаемый ответ:

```json
{"status":"ok","active_bots":1}
```

### Проверить метрики

```bash
curl 'https://tg-connect.example.com/metrics'
```

В ответе должны быть строки вида:

```text
tg_connect_active_bots 1
tg_connect_delivery_queue_items{status="queued"} 0
tg_connect_delivery_queue_items{status="delivered"} 10
tg_connect_delivery_queue_items{status="dead_letter"} 0
tg_connect_idempotency_records 10
```

---

## Тестирование проекта

Установите тестовые зависимости:

```bash
pip install -e '.[test]'
```

Запустите тесты:

```bash
pytest -q
```

Проверка синтаксиса Python-модулей:

```bash
python -m compileall app
```

---

## Безопасная эксплуатация

Минимальные рекомендации:

1. Всегда используйте HTTPS.
2. Не используйте default `ADMIN_PASSWORD=admin`.
3. Не передавайте прямой Telegram token в URL из Битрикс, используйте alias/ID бота.
4. Включайте `hmac_secret` для каждого бота.
5. Ограничивайте доступ по `allowed_ips`.
6. Ограничивайте `/admin/` на reverse proxy или VPN.
7. Храните `TG_CONNECT_MASTER_KEY` вне репозитория.
8. Делайте backup `ADMIN_STATE_PATH`, если используется файловое хранение.
9. Следите за `/metrics` и событиями в админке.
10. Для горизонтального production-масштабирования замените file-backed queue на PostgreSQL/Redis/RabbitMQ backend.

---

## Ограничения текущей версии

- Очередь доставки хранится в JSON-файле и подходит для одного инстанса; для нескольких инстансов нужен PostgreSQL/Redis/RabbitMQ backend.
- Дедупликация `update_id` выполняется через delivery queue; отдельный распределенный idempotency backend еще не подключен.
- Rate limit хранится в памяти процесса.
- Административные настройки по умолчанию хранятся в JSON-файле.
- Telegram Bot API совместимость покрывает только базовые методы.
- Нет полноценной обработки multipart/file upload.
- Нет RBAC и audit log действий администраторов.
- Нет встроенной настройки Telegram webhook из админки.
- Нет long polling runner.
- Нет Dockerfile и docker-compose в текущей версии проекта.

---

## Рекомендуемые следующие доработки

1. PostgreSQL/Redis/RabbitMQ backend для delivery queue при горизонтальном масштабировании.
2. Отдельный worker-процесс для доставки в Битрикс вместо in-process background tasks.
3. Расширенные retry policy с delayed retry, jitter и circuit breaker.
4. Улучшенный admin UI для фильтрации delivery queue/dead-letter и просмотра payload.
5. Расширение Telegram Bot API compatibility.
6. Поддержка файлов и multipart upload.
7. RBAC для админки.
8. Audit log.
9. Dockerfile, docker-compose и deployment docs.
10. CI pipeline с tests/lint/type-check.
