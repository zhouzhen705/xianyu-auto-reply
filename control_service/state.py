from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

from common.tradinghub_order_callback import XianyuOrderEvent, build_payload, post_order_callback


LoginStatus = Literal["not_logged_in", "waiting_scan", "logged_in", "expired"]
WorkerStatus = Literal["stopped", "running"]


@dataclasses.dataclass
class ControlConfig:
    auto_reply_enabled: bool = False
    auto_delivery_enabled: bool = False
    refund_callback_enabled: bool = True
    product_slug: str = "riskmanager"


@dataclasses.dataclass
class ControlState:
    login_status: LoginStatus = "not_logged_in"
    worker_status: WorkerStatus = "stopped"
    qr_content: str | None = None
    qr_image_url: str | None = None
    qr_session_id: str | None = None
    config: ControlConfig = dataclasses.field(default_factory=ControlConfig)
    recent_messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    recent_events: list[dict[str, Any]] = dataclasses.field(default_factory=list)


state = ControlState(
    config=ControlConfig(product_slug=os.getenv("PRODUCT_SLUG", "riskmanager").strip() or "riskmanager")
)
worker_stop_event = threading.Event()
worker_thread: threading.Thread | None = None
managed_processes: dict[str, subprocess.Popen[Any]] = {}


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PORTS = {
    "backend_web": int(os.getenv("BACKEND_WEB_PORT", "8089")),
    "websocket": int(os.getenv("WEBSOCKET_PORT", "8090")),
    "scheduler": int(os.getenv("SCHEDULER_PORT", "8091")),
}
PAID_STATUSES = {"paid", "pending_ship", "to_ship", "wait_seller_send_goods", "shipped", "completed", "success"}
REFUNDED_STATUSES = {"refunded", "refund", "refund_success", "refund_completed", "after_sale_success"}
CANCELLED_STATUSES = {"cancelled", "canceled", "closed", "trade_closed", "buyer_cancelled"}


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def service_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for env_path in [
        PROJECT_ROOT.parent.parent / ".env.local",
        PROJECT_ROOT / "backend-web" / ".env",
        PROJECT_ROOT / "websocket" / ".env",
        PROJECT_ROOT / "scheduler" / ".env",
        PROJECT_ROOT / "control_service" / ".env",
    ]:
        values.update({key: value for key, value in load_env_file(env_path).items() if value})
    return values


def merged_env() -> dict[str, str]:
    env = service_env_values()
    env.update({key: value for key, value in os.environ.items() if value})
    return env


def now_ms() -> int:
    return int(time.time() * 1000)


def record_message(message: str, payload: dict[str, Any] | None = None) -> None:
    state.recent_messages.insert(
        0,
        {
            "id": f"msg-{now_ms()}",
            "type": "dry_run_message",
            "message": message,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": payload or {},
        },
    )
    del state.recent_messages[20:]


def record_event(event_type: str, external_order_id: str, payload: dict[str, Any]) -> None:
    state.recent_events.insert(
        0,
        {
            "id": f"evt-{now_ms()}",
            "event_type": event_type,
            "external_order_id": external_order_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": payload,
        },
    )
    del state.recent_events[20:]


def config_dict() -> dict[str, Any]:
    return dataclasses.asdict(state.config)


def status_dict(message: str = "") -> dict[str, Any]:
    service_status = get_service_status()
    state.worker_status = "running" if service_status["websocket"] and service_status["scheduler"] else "stopped"
    account_count = get_xianyu_account_count()
    if (
        isinstance(account_count, int)
        and account_count > 0
        and state.login_status in {"not_logged_in", "expired"}
    ):
        state.login_status = "logged_in"

    return {
        "ok": True,
        "status": state.login_status,
        "login_status": state.login_status,
        "worker": state.worker_status,
        "services": service_status,
        "message": message,
        "qr_content": state.qr_content,
        "qr_image_url": state.qr_image_url,
        "qr_session_id": state.qr_session_id,
        "account_count": account_count,
        "config": config_dict(),
        "recent_messages": state.recent_messages,
        "recent_events": state.recent_events,
    }


def start_login() -> dict[str, Any]:
    state.login_status = "not_logged_in"
    state.qr_session_id = None
    state.qr_image_url = None
    state.qr_content = None

    backend_result = request_backend_qr_login()
    state.login_status = "waiting_scan"

    if backend_result.get("ok"):
        state.qr_session_id = str(backend_result.get("session_id") or "")
        state.qr_image_url = str(backend_result.get("qr_code_url") or "") or None
        state.qr_content = None
        record_message("闲鱼登录二维码已由 backend-web 生成。", {"session_id": state.qr_session_id})
        return status_dict("闲鱼登录二维码已生成。")

    state.qr_session_id = None
    state.qr_image_url = None
    state.qr_content = str(backend_result.get("message") or "Xianyu QR login backend is not connected.")
    record_message("闲鱼登录二维码生成失败。", backend_result)
    return status_dict(state.qr_content)


def refresh_login_status() -> dict[str, Any]:
    if not state.qr_session_id:
        return status_dict()

    backend_result = request_backend_qr_status(state.qr_session_id)
    status = str(backend_result.get("status") or "")

    if status in {"success", "logged_in", "already_processed"}:
        state.login_status = "logged_in"
        sync_successful_qr_login(backend_result)
    elif status in {"expired", "failed", "cancelled", "error"}:
        state.login_status = "expired"
    elif status in {"waiting", "scanned", "processing", "pending", "verification_required"}:
        state.login_status = "waiting_scan"

    return status_dict(str(backend_result.get("message") or ""))


def clear_activity() -> dict[str, Any]:
    state.recent_messages.clear()
    state.recent_events.clear()
    return status_dict("闲鱼消息日志和订单事件已清空。")


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def get_service_status() -> dict[str, bool]:
    return {name: is_port_open(port) for name, port in SERVICE_PORTS.items()}


def local_service_env() -> dict[str, str]:
    env = merged_env()
    defaults = {
        "ENVIRONMENT": "development",
        "LOG_LEVEL": "INFO",
        "MYSQL_HOST": "localhost",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "",
        "MYSQL_DATABASE": "xianyu_auto_reply",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        "REDIS_DB": "0",
        "BACKEND_WEB_SERVICE_URL": f"http://localhost:{SERVICE_PORTS['backend_web']}",
        "WEBSOCKET_SERVICE_URL": f"http://localhost:{SERVICE_PORTS['websocket']}",
        "STATIC_DIR": "static",
        "BROWSER_HEADLESS": "true",
        "WEBSOCKET_PORT": str(SERVICE_PORTS["websocket"]),
        "SCHEDULER_PORT": str(SERVICE_PORTS["scheduler"]),
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    return env


def start_python_service(name: Literal["websocket", "scheduler"], script_dir: str) -> dict[str, Any]:
    port = SERVICE_PORTS[name]
    if is_port_open(port):
        return {"ok": True, "service": name, "already_running": True, "port": port}

    logs_dir = Path(os.getenv("XIANYU_CONTROL_LOG_DIR", str(Path(os.getenv("TEMP", ".")))))
    stdout_path = logs_dir / f"xianyu-{name}-managed.out.log"
    stderr_path = logs_dir / f"xianyu-{name}-managed.err.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(PROJECT_ROOT / script_dir),
        env=local_service_env(),
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    managed_processes[name] = process

    for _ in range(30):
        if is_port_open(port):
            return {"ok": True, "service": name, "pid": process.pid, "port": port}
        if process.poll() is not None:
            return {
                "ok": False,
                "service": name,
                "error": "SERVICE_EXITED",
                "returncode": process.returncode,
                "stderr_log": str(stderr_path),
            }
        time.sleep(0.5)

    return {"ok": False, "service": name, "error": "SERVICE_START_TIMEOUT", "pid": process.pid, "port": port}


def stop_port(port: int) -> None:
    if os.name != "nt":
        return
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique | "
                "ForEach-Object { Stop-Process -Id $_ -Force }"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def db_connect():
    import pymysql
    env = merged_env()

    return pymysql.connect(
        host=env.get("MYSQL_HOST", "localhost"),
        port=int(env.get("MYSQL_PORT", "3306")),
        user=env.get("MYSQL_USER", "root"),
        password=env.get("MYSQL_PASSWORD", ""),
        database=env.get("MYSQL_DATABASE", "xianyu_auto_reply"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def select_pending_order_callbacks(limit: int = 20) -> list[dict[str, Any]]:
    with db_connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                select id, order_no, status, buyer_nick, buyer_id, chat_id, item_id,
                       quantity, amount, currency, account_id, metadata, placed_at
                from xy_orders
                order by updated_at asc
                limit %s
                """,
                (limit,),
            )
            return list(cursor.fetchall())


def update_order_metadata(order_id: int, metadata: dict[str, Any], delivery_message: str | None = None) -> None:
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    with db_connect() as conn:
        with conn.cursor() as cursor:
            if delivery_message:
                cursor.execute(
                    """
                    update xy_orders
                    set metadata=%s, delivery_method='tradinghub', delivery_content=%s
                    where id=%s
                    """,
                    (metadata_json, delivery_message, order_id),
                )
            else:
                cursor.execute("update xy_orders set metadata=%s where id=%s", (metadata_json, order_id))


def event_type_for_order(order: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    status = str(order.get("status") or "").lower()
    sent = metadata.get("tradinghub_callback", {})
    paid_sent = bool(isinstance(sent, dict) and sent.get("paid"))

    if status in PAID_STATUSES and not paid_sent:
        return "paid"
    if status in REFUNDED_STATUSES and state.config.refund_callback_enabled:
        if not (isinstance(sent, dict) and sent.get("refunded")):
            return "refunded"
    if status in CANCELLED_STATUSES:
        if paid_sent and state.config.refund_callback_enabled and not (isinstance(sent, dict) and sent.get("refunded")):
            return "refunded"
        if not paid_sent and not (isinstance(sent, dict) and sent.get("cancelled")):
            return "cancelled"
    return None


def build_event_from_order(order: dict[str, Any], event_type: str) -> XianyuOrderEvent:
    amount = order.get("amount")
    return XianyuOrderEvent(
        event_type=event_type,  # type: ignore[arg-type]
        external_order_id=str(order.get("order_no") or order.get("id")),
        amount=float(amount) if amount is not None else 0,
        currency=str(order.get("currency") or "CNY"),
        quantity=int(order.get("quantity") or 1),
        buyer_contact=str(order.get("buyer_nick") or order.get("buyer_id") or ""),
        buyer={
            "buyer_id": order.get("buyer_id"),
            "buyer_nick": order.get("buyer_nick"),
            "chat_id": order.get("chat_id"),
            "account_id": order.get("account_id"),
        },
        metadata={
            "source": "xianyu_real_worker",
            "xianyu_order_id": order.get("id"),
            "xianyu_status": order.get("status"),
            "item_id": order.get("item_id"),
            "chat_id": order.get("chat_id"),
            "account_id": order.get("account_id"),
            "placed_at": str(order.get("placed_at") or ""),
        },
    )


def mark_callback_result(order: dict[str, Any], event_type: str, callback_result: dict[str, Any]) -> None:
    metadata = parse_metadata(order.get("metadata"))
    callbacks = metadata.get("tradinghub_callback")
    if not isinstance(callbacks, dict):
        callbacks = {}
    callbacks[event_type] = {
        "ok": bool(callback_result.get("ok")),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "order_status": callback_result.get("order_status"),
        "license_status": callback_result.get("license_status"),
        "error": callback_result.get("error"),
    }
    metadata["tradinghub_callback"] = callbacks
    delivery_message = callback_result.get("delivery_message")
    update_order_metadata(
        int(order["id"]),
        metadata,
        delivery_message if isinstance(delivery_message, str) and callback_result.get("ok") else None,
    )


def send_delivery_message_for_order(order: dict[str, Any], message: str) -> dict[str, Any]:
    return send_message_adapter(
        {
            "account_id": order.get("account_id"),
            "chat_id": order.get("chat_id"),
            "buyer_id": order.get("buyer_id"),
            "buyer_nick": order.get("buyer_nick"),
        },
        message,
        dry_run=False,
    )


def process_order_callbacks_once() -> None:
    for order in select_pending_order_callbacks():
        metadata = parse_metadata(order.get("metadata"))
        event_type = event_type_for_order(order, metadata)
        if not event_type:
            continue

        event = build_event_from_order(order, event_type)
        callback_payload = build_payload(event, product_slug=state.config.product_slug)
        record_event(event_type, event.external_order_id, callback_payload)
        callback_result = post_order_callback(event, product_slug=state.config.product_slug)
        mark_callback_result(order, event_type, callback_result)
        record_message("已提交 TradingHub 订单回调。", {"order_no": event.external_order_id, "event_type": event_type, "ok": callback_result.get("ok")})

        delivery_message = callback_result.get("delivery_message")
        if (
            event_type == "paid"
            and callback_result.get("ok")
            and state.config.auto_delivery_enabled
            and isinstance(delivery_message, str)
        ):
            send_result = send_delivery_message_for_order(order, delivery_message)
            record_message("已通过闲鱼 adapter 发送 TradingHub 发货消息。", send_result)


def worker_loop() -> None:
    interval = max(2, int(os.getenv("XIANYU_ORDER_BRIDGE_INTERVAL_SECONDS", "10")))
    while not worker_stop_event.is_set():
        try:
            process_order_callbacks_once()
        except Exception as exc:
            record_message("闲鱼订单桥接失败。", {"error": type(exc).__name__, "message": str(exc)})
        worker_stop_event.wait(interval)


def request_backend_qr_login() -> dict[str, Any]:
    base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")
    timeout_seconds = int(os.getenv("XIANYU_BACKEND_QR_TIMEOUT_SECONDS", "90"))
    url = f"{base_url}/api/v1/qr-login/generate"

    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "content-type": "application/json",
                **backend_auth_header(),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and refresh_backend_token():
                continue
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_payload = {}
            return {
                "ok": False,
                "error": "XIANYU_QR_BACKEND_REJECTED",
                "message": str(error_payload.get("message") or f"QR backend returned HTTP {exc.code}."),
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "error": "XIANYU_QR_BACKEND_UNAVAILABLE",
            "message": f"无法连接闲鱼二维码后端 {url}。请启动 backend-web，或设置 XIANYU_BACKEND_BASE_URL。{exc}",
            }

    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and payload.get("success") is not False:
        qr_code_url = data.get("qr_code_url")
        if isinstance(qr_code_url, str) and qr_code_url.startswith("/"):
            qr_code_url = f"{base_url}{qr_code_url}"
        return {
            "ok": True,
            "session_id": data.get("session_id"),
            "qr_code_url": qr_code_url,
        }

    return {
        "ok": False,
        "error": "XIANYU_QR_BACKEND_REJECTED",
        "message": str(payload.get("message") if isinstance(payload, dict) else "二维码后端拒绝了请求。"),
    }


def request_backend_qr_status(session_id: str) -> dict[str, Any]:
    base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")
    url = f"{base_url}/api/v1/qr-login/status/{session_id}"

    for attempt in range(2):
        request = urllib.request.Request(
            url,
            headers=backend_auth_header(),
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and refresh_backend_token():
                continue
            return {
                "ok": False,
                "status": state.login_status,
            "message": f"无法查询闲鱼二维码状态。HTTP {exc.code}",
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "status": state.login_status,
            "message": f"无法查询闲鱼二维码状态。{exc}",
            }

    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return {
            "ok": payload.get("success") is not False,
            "status": data.get("status"),
            "message": data.get("message") or payload.get("message"),
            "account_info": data.get("account_info"),
        }

    return {
        "ok": payload.get("success") is not False if isinstance(payload, dict) else False,
        "status": payload.get("status") if isinstance(payload, dict) else state.login_status,
        "message": payload.get("message") if isinstance(payload, dict) else "",
    }


def request_backend_qr_cookie(session_id: str) -> dict[str, Any]:
    base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")
    url = f"{base_url}/api/v1/qr-login/cookie/{session_id}"

    for attempt in range(2):
        request = urllib.request.Request(
            url,
            headers=backend_auth_header(),
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and refresh_backend_token():
                continue
            return {"ok": False, "message": f"无法查询闲鱼二维码 Cookie。HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {"ok": False, "message": f"无法查询闲鱼二维码 Cookie。{exc}"}

    data = payload.get("data") if isinstance(payload, dict) else None
    return {
        "ok": payload.get("success") is not False if isinstance(payload, dict) else False,
        "message": payload.get("message") if isinstance(payload, dict) else "",
        "data": data if isinstance(data, dict) else {},
    }


def sync_successful_qr_login(backend_result: dict[str, Any]) -> None:
    if not state.qr_session_id:
        return

    account_info = backend_result.get("account_info") if isinstance(backend_result.get("account_info"), dict) else {}
    before_count = get_xianyu_account_count()
    cookie_result = request_backend_qr_cookie(state.qr_session_id)
    cookie_data = cookie_result.get("data") if isinstance(cookie_result.get("data"), dict) else {}

    cookie_value = extract_cookie_value(cookie_data)
    account_id = extract_account_id(account_info, cookie_data)

    if not cookie_value:
        record_message(
            "闲鱼二维码已扫码成功，但 backend-web 暂未返回可同步的 Cookie。",
            {"account_count": before_count, "has_account_info": bool(account_info), "cookie_available": False},
        )
        return

    create_result = create_backend_account(account_id, cookie_value)
    after_count = get_xianyu_account_count()
    record_message(
        "闲鱼扫码登录结果已同步到账户存储。",
        {
            "ok": bool(create_result.get("ok")),
            "account_count_before": before_count,
            "account_count_after": after_count,
            "created_or_updated": after_count >= before_count,
        },
    )

    if after_count > before_count and state.worker_status == "running":
        restart_listener_services_after_login()


def extract_cookie_value(data: dict[str, Any]) -> str:
    for key in ("cookie", "value", "cookie_string"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    cookies = data.get("cookies")
    if isinstance(cookies, dict):
        return "; ".join(f"{key}={value}" for key, value in cookies.items() if value is not None)
    if isinstance(cookies, list):
        pairs: list[str] = []
        for item in cookies:
            if isinstance(item, dict):
                name = item.get("name")
                value = item.get("value")
                if name and value is not None:
                    pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    return ""


def extract_account_id(account_info: dict[str, Any], data: dict[str, Any]) -> str:
    for source in (account_info, data):
        for key in ("account_id", "id", "unb", "user_id", "nick"):
            value = source.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()

    return f"xianyu-{now_ms()}"


def create_backend_account(account_id: str, cookie_value: str) -> dict[str, Any]:
    base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")
    url = f"{base_url}/api/v1/cookies"
    body = json.dumps({"id": account_id, "value": cookie_value}, ensure_ascii=False).encode("utf-8")

    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "content-type": "application/json",
                **backend_auth_header(),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"ok": payload.get("success") is not False, "message": payload.get("message", "")}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and refresh_backend_token():
                continue
            return {"ok": False, "message": f"backend-web 账号同步失败。HTTP {exc.code}"}
        except Exception as exc:
            return {"ok": False, "message": f"backend-web 账号同步失败。{type(exc).__name__}"}

    return {"ok": False, "message": "backend-web 账号同步失败。"}


def get_xianyu_account_count() -> int | None:
    try:
        import pymysql  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        env = merged_env()
        connection = pymysql.connect(
            host=env.get("MYSQL_HOST", "localhost"),
            port=int(env.get("MYSQL_PORT", "3306")),
            user=env.get("MYSQL_USER", "root"),
            password=env.get("MYSQL_PASSWORD", ""),
            database=env.get("MYSQL_DATABASE", "xianyu_auto_reply"),
            charset="utf8mb4",
            connect_timeout=3,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM xy_accounts")
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        finally:
            connection.close()
    except Exception:
        return None


def get_xianyu_accounts() -> list[dict[str, Any]]:
    try:
        with db_connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    select id, owner_id, account_id, display_name, login_method, status,
                           updated_at, last_login_at, cookie
                    from xy_accounts
                    order by updated_at desc, id desc
                    limit 50
                    """
                )
                rows = list(cursor.fetchall())
    except Exception as exc:
        record_message("闲鱼账号列表读取失败。", {"error": type(exc).__name__})
        return []

    accounts: list[dict[str, Any]] = []
    for row in rows:
        account_id = str(row.get("account_id") or "")
        accounts.append(
            {
                "id": row.get("id"),
                "owner_id": row.get("owner_id"),
                "account_label": mask_account_id(account_id),
                "display_name": row.get("display_name") or "",
                "login_method": row.get("login_method") or "",
                "status": row.get("status") or "",
                "updated_at": stringify_time(row.get("updated_at")),
                "last_login_at": stringify_time(row.get("last_login_at")),
                "has_cookie": bool(row.get("cookie")),
            }
        )
    return accounts


def mask_account_id(value: str) -> str:
    if len(value) <= 6:
        return value or "unknown"
    return f"{value[:3]}***{value[-3:]}"


def stringify_time(value: Any) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def restart_listener_services_after_login() -> None:
    record_message("正在重启闲鱼监听服务，以加载新账号。", {})
    stop_port(SERVICE_PORTS["websocket"])
    stop_port(SERVICE_PORTS["scheduler"])
    start_python_service("websocket", "websocket")
    start_python_service("scheduler", "scheduler")


def backend_auth_header() -> dict[str, str]:
    token = os.getenv("XIANYU_BACKEND_TOKEN", "").strip()
    return {"authorization": f"Bearer {token}"} if token else {}


def refresh_backend_token() -> bool:
    base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")
    username = os.getenv("XIANYU_BACKEND_USERNAME", "admin").strip()
    password = os.getenv("XIANYU_BACKEND_PASSWORD", "admin123").strip()

    if not username or not password:
        return False

    token = login_backend_with_json(base_url, username, password) or login_backend_with_form(base_url, username, password)
    if not token:
        return False

    os.environ["XIANYU_BACKEND_TOKEN"] = token
    persist_control_env_value("XIANYU_BACKEND_TOKEN", token)
    return True


def login_backend_with_json(base_url: str, username: str, password: str) -> str | None:
    request = urllib.request.Request(
        f"{base_url}/api/v1/auth/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    return read_backend_token_response(request)


def login_backend_with_form(base_url: str, username: str, password: str) -> str | None:
    request = urllib.request.Request(
        f"{base_url}/api/v1/auth/token",
        data=urllib.parse.urlencode({"username": username, "password": password}).encode("utf-8"),
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return read_backend_token_response(request)


def read_backend_token_response(request: urllib.request.Request) -> str | None:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    token = payload.get("token") or payload.get("access_token") or data.get("token") or data.get("access_token")
    return str(token) if token else None


def persist_control_env_value(key: str, value: str) -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    updated = False

    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            updated = True
            break

    if not updated:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def start_worker() -> dict[str, Any]:
    global worker_thread

    websocket_result = start_python_service("websocket", "websocket")
    scheduler_result = start_python_service("scheduler", "scheduler")

    worker_stop_event.clear()
    if worker_thread is None or not worker_thread.is_alive():
        worker_thread = threading.Thread(target=worker_loop, name="xianyu-order-bridge", daemon=True)
        worker_thread.start()

    state.worker_status = "running"
    record_message(
        "闲鱼真实监听 worker 已启动。",
        {"websocket": websocket_result, "scheduler": scheduler_result, "order_bridge": True},
    )
    return status_dict("worker 已启动：包含 websocket、scheduler 启动尝试和 TradingHub 订单桥接。")


def stop_worker() -> dict[str, Any]:
    worker_stop_event.set()
    for name, process in list(managed_processes.items()):
        if process.poll() is None:
            process.terminate()
        managed_processes.pop(name, None)
    stop_port(SERVICE_PORTS["websocket"])
    stop_port(SERVICE_PORTS["scheduler"])
    state.worker_status = "stopped"
    record_message("闲鱼真实监听 worker 已停止。")
    return status_dict("worker 已停止。")


def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("auto_reply_enabled", "auto_delivery_enabled", "refund_callback_enabled"):
        if key in payload:
            setattr(state.config, key, bool(payload[key]))

    if "product_slug" in payload and str(payload["product_slug"]) != "riskmanager":
        return {
            "ok": False,
            "error": "PRODUCT_NOT_SUPPORTED",
            "message": "Only product_slug=riskmanager is supported by this control service.",
        }

    state.config.product_slug = "riskmanager"
    record_message("闲鱼自动化配置已更新。", config_dict())
    return {"ok": True, "config": config_dict()}


def send_message_adapter(buyer: dict[str, Any] | None, message: str, dry_run: bool = True) -> dict[str, Any]:
    payload = {
        "buyer": buyer or {},
        "message": message,
        "dry_run": dry_run,
    }
    if not dry_run:
        account_id = str((buyer or {}).get("account_id") or "")
        chat_id = str((buyer or {}).get("chat_id") or "")
        buyer_id = str((buyer or {}).get("buyer_id") or "")
        token = os.getenv("XIANYU_BACKEND_TOKEN", "").strip()
        base_url = os.getenv("XIANYU_BACKEND_BASE_URL", "http://127.0.0.1:8089").strip().rstrip("/")

        if account_id and chat_id and buyer_id and token:
            request = urllib.request.Request(
                f"{base_url}/api/v1/chat-new/send-message/{account_id}",
                data=json.dumps({"cid": chat_id, "toUserId": buyer_id, "text": message}, ensure_ascii=False).encode("utf-8"),
                headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
                record_message("发货消息已发送到闲鱼聊天。", {"account_id": account_id, "chat_id": chat_id, "buyer_id": buyer_id})
                return {"ok": True, "sent": True, "dry_run": False, "response": response_payload}
            except Exception as exc:
                record_message("发货消息发送失败。", {"error": type(exc).__name__, "message": str(exc), **payload})
                return {"ok": False, "sent": False, "dry_run": False, "error": type(exc).__name__, "message": str(exc)}

        record_message("发货消息未发送：缺少账号、聊天字段或 backend token。", payload)
        return {"ok": False, "sent": False, "dry_run": False, "error": "MISSING_CHAT_CONTEXT"}

    record_message("发货消息已生成，当前为 dry-run。", payload)
    return {"ok": True, "sent": False, "dry_run": dry_run, "message": "当前为 dry-run，未真实发送消息。"}


def dry_run_order_event(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = str(payload.get("event_type", "paid"))
    if event_type not in {"paid", "refunded", "cancelled"}:
        return {"ok": False, "error": "INVALID_EVENT_TYPE", "message": "event_type 必须是 paid/refunded/cancelled。"}

    product_slug = str(payload.get("product_slug") or state.config.product_slug or "riskmanager")
    if product_slug != "riskmanager":
        return {
            "ok": False,
            "error": "PRODUCT_NOT_SUPPORTED",
            "message": "Only product_slug=riskmanager is supported.",
        }

    event = XianyuOrderEvent(
        event_type=event_type,  # type: ignore[arg-type]
        external_order_id=str(payload.get("external_order_id") or f"TEST-XIANYU-{now_ms()}"),
        amount=payload.get("amount", 0) if isinstance(payload.get("amount", 0), (int, float)) else 0,
        currency=str(payload.get("currency") or "CNY"),
        quantity=int(payload.get("quantity") or 1),
        buyer_contact=str(payload.get("buyer_contact") or ""),
        buyer=payload.get("buyer") if isinstance(payload.get("buyer"), dict) else {},
        refund_reason=str(payload.get("refund_reason") or "") or None,
        metadata={
            "source": "xianyu_control_dry_run",
            "buyer": payload.get("buyer") or {},
            **(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
        },
    )
    callback_payload = build_payload(event, product_slug=product_slug)
    record_event(event.event_type, event.external_order_id, callback_payload)

    should_send = bool(payload.get("send"))
    result: dict[str, Any] = {
        "ok": True,
        "event_type": event.event_type,
        "payload": callback_payload,
        "callback_result": {
            "ok": True,
            "dry_run": True,
            "message": "TradingHub callback was not called. Pass send=true to post with HMAC.",
        },
        "delivery_message": None,
    }

    if should_send:
        callback_result = post_order_callback(event, product_slug=product_slug)
        result["callback_result"] = callback_result
        if event.event_type == "paid" and isinstance(callback_result, dict):
            delivery_message = callback_result.get("delivery_message")
            if isinstance(delivery_message, str) and state.config.auto_delivery_enabled:
                result["delivery_message"] = delivery_message
                result["send_result"] = send_message_adapter(payload.get("buyer") if isinstance(payload.get("buyer"), dict) else {}, delivery_message)

    return result
