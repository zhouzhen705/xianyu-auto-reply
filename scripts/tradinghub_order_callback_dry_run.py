from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.tradinghub_order_callback import (  # noqa: E402
    XianyuOrderEvent,
    build_payload,
    post_order_callback,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run TradingHub order callback payloads.")
    parser.add_argument("--event", choices=["paid", "refunded", "cancelled"], default="paid")
    parser.add_argument("--external-order-id", default="TEST-XIANYU-DRY-RUN")
    parser.add_argument("--amount", type=float, default=299)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--buyer-contact", default="dry-run-buyer")
    parser.add_argument("--refund-reason", default="dry-run-refund")
    parser.add_argument("--send", action="store_true", help="Actually POST to TRADINGHUB_ORDER_CALLBACK_URL.")
    args = parser.parse_args()

    event = XianyuOrderEvent(
        event_type=args.event,
        external_order_id=args.external_order_id,
        amount=args.amount,
        quantity=args.quantity,
        buyer_contact=args.buyer_contact,
        refund_reason=args.refund_reason if args.event == "refunded" else None,
        metadata={"dry_run": True},
    )

    if args.send:
        result = post_order_callback(event)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(build_payload(event), ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
