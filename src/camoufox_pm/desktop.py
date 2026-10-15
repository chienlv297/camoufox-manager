"""Desktop launcher for the local API and native webview window."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn
from loguru import logger

from camoufox_pm.paths import (
    configure_logging,
    ensure_runtime_dirs,
    is_windows_standalone,
    runtime_paths,
)


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _wait_until_healthy(
    host: str,
    port: int,
    timeout: float = 30.0,
    thread: threading.Thread | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    url_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{url_host}:{port}/health"
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if thread is not None and not thread.is_alive():
            return False
        try:
            with opener.open(Request(url), timeout=1) as response:
                if response.status == 200:
                    body = json.loads(response.read())
                    if isinstance(body, dict) and body.get("status") == "healthy":
                        return True
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.2)
    return False


def _load_app(host: str, port: int):
    os.environ["CPM_HOST"] = host
    os.environ["CPM_PORT"] = str(port)
    from camoufox_pm.config import get_settings

    get_settings.cache_clear()
    from camoufox_pm import main
    from camoufox_pm.main import app

    main.settings.host = host
    main.settings.port = port
    return app


def _start_backend(host: str, port: int) -> tuple[uvicorn.Server, threading.Thread, int]:
    """Start Uvicorn and retry races when selecting an ephemeral port."""
    attempts = 3 if port == 0 else 1
    last_port = port
    for _ in range(attempts):
        actual_port = port or _find_available_port(host)
        last_port = actual_port
        server = uvicorn.Server(
            uvicorn.Config(
                _load_app(host, actual_port),
                host=host,
                port=actual_port,
                log_level="info",
                access_log=False,
                log_config=None,
            )
        )
        thread = threading.Thread(target=server.run, name="camoufox-api", daemon=True)
        thread.start()
        if _wait_until_healthy(host, actual_port, thread=thread) and server.started:
            return server, thread, actual_port
        if not _stop_server(server, thread):
            raise RuntimeError("Backend thread did not stop after startup failure")

    raise RuntimeError(f"Backend did not become healthy on localhost:{last_port}")


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> bool:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        logger.error("Backend thread did not stop within shutdown timeout")
        return False
    return True


def _show_startup_error(message: str) -> None:
    logger.error(message)
    if is_windows_standalone():
        ctypes.windll.user32.MessageBoxW(None, message, "Camoufox Profile Manager", 0x10)


def _check_windows_webview() -> None:
    from webview.platforms import winforms

    if winforms.renderer != "edgechromium":
        raise RuntimeError(
            "Microsoft Edge WebView2 Runtime is required for the desktop interface. "
            "Install or repair WebView2 Runtime, then reopen Camoufox Profile Manager."
        )


def run_desktop(
    host: str = "127.0.0.1", port: int = 8000, title: str = "Camoufox Profile Manager"
) -> None:
    """Start the API, wait for health, show a native window, then shut down cleanly."""
    standalone = is_windows_standalone()
    paths = runtime_paths()
    if standalone:
        paths = ensure_runtime_dirs(paths)
        configure_logging(paths)
        host = "127.0.0.1"

    try:
        import webview
    except ImportError as exc:  # pragma: no cover - depends on optional desktop extra
        message = (
            "Desktop UI dependency is missing. Reinstall Camoufox Profile Manager "
            "or contact support."
        )
        _show_startup_error(message)
        raise SystemExit(message) from exc

    server = None
    thread = None
    try:
        if standalone:
            _check_windows_webview()
        server, thread, actual_port = _start_backend(host, port)

        ui_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
        webview.create_window(
            title,
            f"http://{ui_host}:{actual_port}/",
            width=1280,
            height=800,
        )
        webview.start(
            gui="edgechromium" if os.name == "nt" else None,
            private_mode=not standalone,
            storage_path=(str(paths.config_dir / "webview") if standalone else None),
        )
    except Exception as exc:  # noqa: BLE001
        _show_startup_error(
            "Camoufox Profile Manager could not start.\n\n"
            f"Details: {exc}\n\n"
            f"See the log file at:\n{paths.logs_dir / 'app.log'}"
        )
        raise SystemExit(1) from exc
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
