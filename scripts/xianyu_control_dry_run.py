from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_service.state import dry_run_order_event, start_login, start_worker, status_dict, stop_worker, update_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run the TradingHub Xianyu control service state flow.")
    parser.add_argument("--event", choices=["paid", "refunded", "cancelled"], default="paid")
    parser.add_argument("--external-order-id", default="TEST-XIANYU-CONTROL-001")
    parser.add_argument("--send", action="store_true", help="Actually post to TRADINGHUB_ORDER_CALLBACK_URL.")
    args = parser.parse_args()

    os.environ.setdefault("PRODUCT_SLUG", "riskmanager")

    start_login()
    start_worker()
    update_config(
        {
            "auto_reply_enabled": True,
            "auto_delivery_enabled": True,
            "refund_callback_enabled": True,
            "product_slug": "riskmanager",
        }
    )
    result = dry_run_order_event(
        {
            "event_type": args.event,
            "external_order_id": args.external_order_id,
            "amount": 299 if args.event == "paid" else 0,
            "currency": "CNY",
            "quantity": 1,
            "buyer": {"id": "dry-run-buyer"},
            "send": args.send,
        }
    )
    stop_worker()
    print(json.dumps({"status": status_dict(), "dry_run": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
