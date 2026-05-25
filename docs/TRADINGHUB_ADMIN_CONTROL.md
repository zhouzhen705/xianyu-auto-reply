# TradingHub Admin Control

This module exposes an internal control API for TradingHub-Web. It runs as a separate local service and controls the existing Xianyu services instead of embedding them in Next.js.

## Local Services

- `backend-web` on `8089`: real QR login, account APIs, order APIs.
- `websocket` on `8090`: real Xianyu IM connection and auto-reply runtime.
- `scheduler` on `8091`: real scheduled order fetching and redelivery tasks.
- `control_service` on `8092`: internal API called by TradingHub-Web.

## Current Local Progress

Completed and verified locally:

- TradingHub `/admin/xianyu` can reach `control_service`.
- `backend-web` can generate a real Xianyu QR login image.
- `websocket` starts on `8090` and passes `/health`.
- `scheduler` starts on `8091` and passes `/health`.
- `/worker/start` starts or reuses `websocket` and `scheduler`.
- The order bridge polls `xy_orders` and posts RiskManager callbacks to TradingHub.
- A local `TEST-` paid order generated a TradingHub license and wrote `delivery_content`.
- A local `TEST-` refunded order disabled the license and wrote callback metadata.
- TradingHub `typecheck`, `build`, and `test:order-callback` pass.

Pending manual validation:

- Scan the QR code with the real Xianyu mobile app.
- Complete any Xianyu phone or risk-control verification if prompted.
- Confirm the logged-in account appears in `xy_accounts`.
- Confirm `websocket` loads the real account instead of reporting `0 accounts`.
- Send a real Xianyu test message to validate auto-reply.
- Wait for a real order event to validate real order sync, callback, and delivery message sending.

## Start Control Service

```powershell
cd Integrations/Xianyu-Auto-Reply
$env:XIANYU_SERVICE_INTERNAL_TOKEN="<internal-token>"
$env:TRADINGHUB_ORDER_CALLBACK_URL="http://localhost:3000/api/external/order-callback"
$env:EXTERNAL_ORDER_CALLBACK_SECRET="<callback-secret>"
$env:PRODUCT_SLUG="riskmanager"
$env:XIANYU_BACKEND_BASE_URL="http://127.0.0.1:8089"
$env:XIANYU_BACKEND_TOKEN="<backend-web-admin-token>"
$env:MYSQL_HOST="localhost"
$env:MYSQL_PORT="3306"
$env:MYSQL_USER="<mysql-user>"
$env:MYSQL_PASSWORD="<mysql-password>"
$env:MYSQL_DATABASE="xianyu_auto_reply"
$env:REDIS_HOST="localhost"
$env:REDIS_PORT="6379"
py -3.12 -m uvicorn control_service.main:app --host 127.0.0.1 --port 8092
```

Do not commit real token or password values.

## Endpoints

- `GET /health`
- `GET /status`
- `POST /login/start`
- `GET /login/status`
- `POST /worker/start`
- `POST /worker/stop`
- `GET /worker/status`
- `GET /events/recent`
- `GET /messages/recent`
- `GET /config`
- `PATCH /config`
- `POST /dry-run/order-event`
- `POST /logs/clear`

All endpoints except `/health` require:

```text
x-xianyu-internal-token: <XIANYU_SERVICE_INTERNAL_TOKEN>
```

## Real Login And Worker Flow

1. Start `backend-web` first.
2. Start `control_service`.
3. Open TradingHub-Web `/admin/xianyu`.
4. Click "generate QR code" and scan it with the real Xianyu mobile app.
5. Click "start listener".

`/worker/start` starts or reuses the real `websocket` and `scheduler` services, then starts a TradingHub order bridge thread.

Recommended local check:

```powershell
Invoke-RestMethod http://127.0.0.1:8089/health
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8091/health
Invoke-RestMethod http://127.0.0.1:8092/health
```

## TradingHub Order Bridge

The bridge polls local `xy_orders` and maps order statuses to TradingHub callbacks:

- paid-like statuses such as `pending_ship`, `shipped`, `completed` -> `paid`
- refund-like statuses such as `refunded`, `refund_success` -> `refunded`
- cancelled-like statuses before paid -> `cancelled`

Callback success/failure is recorded in `xy_orders.metadata.tradinghub_callback`. Paid success stores the TradingHub delivery message in `xy_orders.delivery_content`.

If `auto_delivery_enabled=true`, paid success also attempts to send `delivery_message` through `backend-web` chat API using the order `account_id`, `chat_id`, and `buyer_id`.

The bridge never uses Supabase service role directly. It only calls:

```text
POST TRADINGHUB_ORDER_CALLBACK_URL
```

with the same HMAC signature contract as TradingHub:

```text
HMAC_SHA256(timestamp + "." + rawBody, EXTERNAL_ORDER_CALLBACK_SECRET)
```

## Dry-run

```powershell
python scripts/xianyu_control_dry_run.py --event paid --external-order-id TEST-XIANYU-PAID
python scripts/xianyu_control_dry_run.py --event refunded --external-order-id TEST-XIANYU-PAID
```

Pass `--send` only when TradingHub local callback env is configured and you intentionally want to post to TradingHub.

## Current Limits

- Only `product_slug=riskmanager` is accepted.
- Options Level Pro is intentionally rejected by the TradingHub callback.
- The service does not access Supabase directly; orders and licenses go through TradingHub callback.
- A real Xianyu login still requires manual QR scan and any phone/risk verification requested by Xianyu.
- Real automatic chat delivery requires `xy_orders.account_id`, `xy_orders.chat_id`, and `xy_orders.buyer_id`.
- Do not run real paid/refunded customer tests unless you intentionally want to operate a real Xianyu transaction.
