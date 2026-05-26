"""WebSocket service entrypoint."""
from __future__ import annotations

import sys
from pathlib import Path

current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(current_dir))
sys.path.insert(0, str(project_root))


def patch_websockets_headers_compat() -> None:
    """Keep compiled websocket code compatible with websockets 14+."""
    try:
        import websockets
        from websockets.asyncio.client import ClientConnection
    except Exception:
        return

    connect = getattr(websockets, "connect", None)
    if connect is None or getattr(connect, "_tradinghub_headers_compat", False):
        return

    def connect_compat(*args, **kwargs):
        if "extra_headers" in kwargs and "additional_headers" not in kwargs:
            kwargs["additional_headers"] = kwargs.pop("extra_headers")
        return connect(*args, **kwargs)

    connect_compat._tradinghub_headers_compat = True
    websockets.connect = connect_compat

    if not hasattr(ClientConnection, "closed"):
        ClientConnection.closed = property(lambda self: str(getattr(self, "state", "")).endswith("CLOSED"))
    if not hasattr(ClientConnection, "open"):
        ClientConnection.open = property(lambda self: str(getattr(self, "state", "")).endswith("OPEN"))


patch_websockets_headers_compat()

from _bootstrap import app  # noqa: E402

if __name__ == "__main__":
    from _bootstrap import run_server

    run_server()
