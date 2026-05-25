"""TradingHub external order callback adapter for Xianyu events.

This module is intentionally small and dependency-light so it can be used by
backend-web, scheduler tasks, or a dry-run script without logging in to Xianyu.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Literal


EventType = Literal["paid", "refunded", "cancelled"]


@dataclasses.dataclass(frozen=True)
class XianyuOrderEvent:
    event_type: EventType
    external_order_id: str
    amount: float | int = 0
    currency: str = "CNY"
    quantity: int = 1
    buyer_contact: str | None = None
    buyer: dict[str, Any] | None = None
    paid_at: str | None = None
    refund_reason: str | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


class TradingHubCallbackError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"TradingHub callback failed: status={status_code} payload={payload}")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def build_payload(event: XianyuOrderEvent, product_slug: str | None = None) -> dict[str, Any]:
    slug = product_slug or os.getenv("PRODUCT_SLUG", "riskmanager").strip() or "riskmanager"
    payload: dict[str, Any] = {
        "order_source": "xianyu",
        "event_type": event.event_type,
        "external_order_id": event.external_order_id,
        "product_slug": slug,
        "quantity": event.quantity,
        "amount": event.amount,
        "currency": event.currency,
        "metadata": event.metadata,
    }

    if event.buyer_contact:
        payload["buyer_contact"] = event.buyer_contact
    if event.buyer:
        payload["buyer"] = event.buyer
    if event.paid_at:
        payload["paid_at"] = event.paid_at
    if event.refund_reason:
        payload["refund_reason"] = event.refund_reason

    return payload


def sign_payload(raw_body: str, timestamp_ms: str, secret: str) -> str:
    message = f"{timestamp_ms}.{raw_body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def post_order_callback(
    event: XianyuOrderEvent,
    *,
    callback_url: str | None = None,
    secret: str | None = None,
    product_slug: str | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    url = callback_url or _required_env("TRADINGHUB_ORDER_CALLBACK_URL")
    callback_secret = secret or _required_env("EXTERNAL_ORDER_CALLBACK_SECRET")
    payload = build_payload(event, product_slug=product_slug)
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp_ms = str(int(time.time() * 1000))
    signature = sign_payload(raw_body, timestamp_ms, callback_secret)
    request = urllib.request.Request(
        url,
        data=raw_body.encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-tradinghub-timestamp": timestamp_ms,
            "x-tradinghub-signature": signature,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": "HTTP_ERROR", "message": body}
        raise TradingHubCallbackError(exc.code, payload) from exc
