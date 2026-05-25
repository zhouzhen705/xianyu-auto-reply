# TradingHub RiskManager Order Callback Adapter

This adapter maps local Xianyu order lifecycle events into TradingHub:

- `paid` -> `POST /api/external/order-callback`, returns `delivery_message`
- `refunded` -> disables the matching RiskManager license
- `cancelled` -> cancels an unfulfilled order only

## Environment

Set these variables in the service that calls the adapter:

```env
TRADINGHUB_ORDER_CALLBACK_URL=https://your-tradinghub-domain/api/external/order-callback
EXTERNAL_ORDER_CALLBACK_SECRET=
PRODUCT_SLUG=riskmanager
```

`EXTERNAL_ORDER_CALLBACK_SECRET` must match TradingHub Web. Do not commit real values.

## Event Mapping

The adapter sends:

```json
{
  "order_source": "xianyu",
  "event_type": "paid",
  "external_order_id": "XY123",
  "product_slug": "riskmanager",
  "quantity": 1,
  "amount": 299,
  "currency": "CNY",
  "buyer_contact": "buyer id or nickname",
  "metadata": {}
}
```

The request is signed with:

```text
HMAC_SHA256(timestamp + "." + rawBody, EXTERNAL_ORDER_CALLBACK_SECRET)
```

Headers:

- `x-tradinghub-timestamp`
- `x-tradinghub-signature`

## Dry Run

Generate payload only:

```powershell
python scripts/tradinghub_order_callback_dry_run.py --event paid --external-order-id TEST-XIANYU-001
```

Send to TradingHub, if local TradingHub is running and env is configured:

```powershell
python scripts/tradinghub_order_callback_dry_run.py --event paid --external-order-id TEST-XIANYU-001 --send
python scripts/tradinghub_order_callback_dry_run.py --event refunded --external-order-id TEST-XIANYU-001 --send
```

This does not log in to Xianyu and does not operate real payments or refunds.
