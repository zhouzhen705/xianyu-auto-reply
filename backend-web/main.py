"""Backend-Web服务启动入口（最小桩，业务逻辑见 _bootstrap.py）"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 将当前目录和项目根目录添加到 Python 路径（必须先于业务导入）
current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(current_dir))
sys.path.insert(0, str(project_root))


def install_safe_qr_debug_probe() -> None:
    enabled = os.getenv("XIANYU_SAFE_QR_DEBUG", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return

    try:
        import httpx
    except Exception:
        return

    logger = logging.getLogger("xianyu.safe_qr_debug")
    sensitive_keys = ("cookie", "token", "secret", "password", "authorization", "session", "url", "href")
    interesting_keys = ("status", "code", "message", "msg", "ret", "result", "action", "state", "success", "error")

    def collect_safe_fields(value: object, prefix: str = "", depth: int = 0) -> dict[str, object]:
        if depth > 4:
            return {}

        fields: dict[str, object] = {}
        if isinstance(value, dict):
            for key, item in value.items():
                text_key = str(key)
                lower_key = text_key.lower()
                is_sensitive_qr_value = (
                    "qr" in lower_key or lower_key in {"codecontent", "lgToken".lower(), "ck"}
                ) and not any(part in lower_key for part in ("status", "result"))
                if any(part in lower_key for part in sensitive_keys) or is_sensitive_qr_value:
                    continue

                next_prefix = f"{prefix}.{text_key}" if prefix else text_key
                if any(part in lower_key for part in interesting_keys) and isinstance(
                    item, (str, int, float, bool, type(None))
                ):
                    fields[next_prefix] = str(item)[:180] if isinstance(item, str) else item

                fields.update(collect_safe_fields(item, next_prefix, depth + 1))
        elif isinstance(value, list):
            for index, item in enumerate(value[:5]):
                fields.update(collect_safe_fields(item, f"{prefix}[{index}]", depth + 1))

        return fields

    def log_qr_response(method: str, url: object, response: object) -> None:
        if "passport.goofish.com/newlogin/qrcode/query.do" not in str(url):
            return

        try:
            payload = response.json()
        except Exception:
            payload = {"body_readable": False}

        summary = collect_safe_fields(payload)
        try:
            status_code = getattr(response, "status_code", None)
        except Exception:
            status_code = None
        logger.warning(
            "SAFE_QR_QUERY_DEBUG method=%s http_status=%s fields=%s",
            method,
            status_code,
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )

    if not getattr(httpx.AsyncClient.request, "_xianyu_safe_qr_debug", False):
        original_async_request = httpx.AsyncClient.request

        async def debug_async_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = await original_async_request(self, method, url, *args, **kwargs)
            log_qr_response(str(method), url, response)
            return response

        debug_async_request._xianyu_safe_qr_debug = True  # type: ignore[attr-defined]
        httpx.AsyncClient.request = debug_async_request  # type: ignore[method-assign]

    if not getattr(httpx.Client.request, "_xianyu_safe_qr_debug", False):
        original_sync_request = httpx.Client.request

        def debug_sync_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = original_sync_request(self, method, url, *args, **kwargs)
            log_qr_response(str(method), url, response)
            return response

        debug_sync_request._xianyu_safe_qr_debug = True  # type: ignore[attr-defined]
        httpx.Client.request = debug_sync_request  # type: ignore[method-assign]


install_safe_qr_debug_probe()

from _bootstrap import app  # noqa: E402


def install_qr_login_probe() -> None:
    """Log sanitized QR poll status fields for server-side login diagnosis."""
    try:
        import inspect
        import json
        import os

        from app.services.qr_login.manager import QRLoginManager  # type: ignore
        from loguru import logger  # type: ignore
    except Exception:
        return

    if os.getenv("XIANYU_QR_DEBUG", "1").strip().lower() in {"0", "false", "no", "off"}:
        return

    original = getattr(QRLoginManager, "_poll_qrcode_status", None)
    if original is None or getattr(original, "_tradinghub_probe", False):
        return

    sensitive_fragments = (
        "cookie",
        "token",
        "secret",
        "password",
        "session",
        "authorization",
        "url",
        "href",
        "redirect",
    )

    def sanitize(value: object, depth: int = 0) -> object:
        if depth > 4:
            return "[depth-limit]"
        if isinstance(value, dict):
            sanitized: dict[str, object] = {}
            for key, item in value.items():
                key_text = str(key)
                lower_key = key_text.lower()
                is_sensitive_qr_value = (
                    "qr" in lower_key or lower_key in {"codecontent", "lgtoken", "ck"}
                ) and not any(fragment in lower_key for fragment in ("status", "result"))
                if any(fragment in lower_key for fragment in sensitive_fragments) or is_sensitive_qr_value:
                    sanitized[key_text] = "[redacted]" if item else item
                else:
                    sanitized[key_text] = sanitize(item, depth + 1)
            return sanitized
        if isinstance(value, list):
            return [sanitize(item, depth + 1) for item in value[:8]]
        if isinstance(value, str):
            return value if len(value) <= 160 else f"{value[:160]}...[truncated]"
        return value

    async def wrapped(self, session):  # type: ignore[no-untyped-def]
        response = await original(self, session)
        try:
            payload = response.json()
        except Exception:
            payload = {"text": response.text[:160]}

        try:
            logger.info(
                "TradingHub QR poll probe: http_status={} payload={}",
                getattr(response, "status_code", None),
                json.dumps(sanitize(payload), ensure_ascii=False),
            )
        except Exception:
            logger.info("TradingHub QR poll probe: failed to serialize payload")

        return response

    wrapped._tradinghub_probe = True  # type: ignore[attr-defined]
    QRLoginManager._poll_qrcode_status = wrapped

    if not inspect.iscoroutinefunction(QRLoginManager._poll_qrcode_status):
        logger.warning("TradingHub QR poll probe installed on a non-async poll function.")
    else:
        logger.info("TradingHub QR poll probe installed.")


install_qr_login_probe()

if __name__ == "__main__":
    from _bootstrap import run_server
    run_server()
