from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

from control_service.state import (
    config_dict,
    clear_activity,
    dry_run_order_event,
    refresh_login_status,
    start_login,
    start_worker,
    status_dict,
    stop_worker,
    state,
    update_config,
)


app = FastAPI(title="Xianyu TradingHub Control Service", version="0.1.0")


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code)

    return JSONResponse(
        {"ok": False, "error": "HTTP_ERROR", "message": str(exc.detail)},
        status_code=exc.status_code,
    )


def require_internal_token(x_xianyu_internal_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("XIANYU_SERVICE_INTERNAL_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail={"ok": False, "error": "INTERNAL_TOKEN_NOT_CONFIGURED"})
    if not x_xianyu_internal_token or not secrets.compare_digest(x_xianyu_internal_token, expected):
        raise HTTPException(status_code=401, detail={"ok": False, "error": "UNAUTHORIZED"})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "xianyu-tradinghub-control"}


@app.get("/status", dependencies=[Depends(require_internal_token)])
def status() -> dict[str, Any]:
    return refresh_login_status()


@app.post("/login/start", dependencies=[Depends(require_internal_token)])
def login_start() -> dict[str, Any]:
    return start_login()


@app.get("/login/status", dependencies=[Depends(require_internal_token)])
def login_status() -> dict[str, Any]:
    refreshed = refresh_login_status()
    return {
        "ok": True,
        "status": state.login_status,
        "qr_content": state.qr_content,
        "qr_image_url": state.qr_image_url,
        "qr_session_id": state.qr_session_id,
        "message": refreshed.get("message", ""),
    }


@app.post("/worker/start", dependencies=[Depends(require_internal_token)])
def worker_start() -> dict[str, Any]:
    return start_worker()


@app.post("/worker/stop", dependencies=[Depends(require_internal_token)])
def worker_stop() -> dict[str, Any]:
    return stop_worker()


@app.get("/worker/status", dependencies=[Depends(require_internal_token)])
def worker_status() -> dict[str, Any]:
    return {"ok": True, "worker": state.worker_status}


@app.get("/events/recent", dependencies=[Depends(require_internal_token)])
def events_recent() -> dict[str, Any]:
    return {"ok": True, "events": state.recent_events}


@app.get("/messages/recent", dependencies=[Depends(require_internal_token)])
def messages_recent() -> dict[str, Any]:
    return {"ok": True, "messages": state.recent_messages}


@app.get("/config", dependencies=[Depends(require_internal_token)])
def config() -> dict[str, Any]:
    return {"ok": True, "config": config_dict()}


@app.patch("/config", dependencies=[Depends(require_internal_token)])
def patch_config(payload: dict[str, Any]) -> dict[str, Any]:
    result = update_config(payload)
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/dry-run/order-event", dependencies=[Depends(require_internal_token)])
def dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    result = dry_run_order_event(payload)
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/logs/clear", dependencies=[Depends(require_internal_token)])
def clear_logs() -> dict[str, Any]:
    return clear_activity()
