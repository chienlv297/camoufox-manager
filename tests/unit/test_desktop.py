"""Desktop startup, health checking, port retries and shutdown."""

import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from camoufox_pm import desktop


def test_find_available_port():
    port = desktop._find_available_port("127.0.0.1")
    assert 0 < port < 65536
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "payload,healthy",
    [
        (b'{"status":"healthy"}', True),
        (b'{"status":"unhealthy"}', False),
        (b"[]", False),
        (b"not-json", False),
    ],
)
def test_wait_checks_health_body(monkeypatch, payload, healthy):
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = payload
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(desktop, "build_opener", lambda *args: opener)
    monkeypatch.setattr(desktop.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: None)
    assert desktop._wait_until_healthy("127.0.0.1", 1234, timeout=1) is healthy


def test_startup_retries_ephemeral_port_race(monkeypatch):
    ports = iter((1234, 2345))
    health = iter((False, True))
    servers = []
    threads = []

    def create_server(config):
        server = SimpleNamespace(started=True, should_exit=False, run=lambda: None)
        servers.append(server)
        return server

    def create_thread(**kwargs):
        thread = MagicMock()
        thread.is_alive.return_value = False
        threads.append(thread)
        return thread

    monkeypatch.setattr(desktop, "_load_app", lambda host, port: object())
    monkeypatch.setattr(desktop, "_find_available_port", lambda host: next(ports))
    monkeypatch.setattr(desktop, "_wait_until_healthy", lambda *args, **kwargs: next(health))
    monkeypatch.setattr(desktop.uvicorn, "Server", create_server)
    monkeypatch.setattr(desktop.threading, "Thread", create_thread)
    server, thread, port = desktop._start_backend("127.0.0.1", 0)
    assert port == 2345
    assert server is servers[1]
    assert servers[0].should_exit
    threads[0].join.assert_called_once_with(timeout=10)


@pytest.mark.parametrize("window_error", [False, True])
def test_webview_closing_or_failing_stops_backend(monkeypatch, tmp_path, window_error):
    server = SimpleNamespace(should_exit=False)
    thread = MagicMock()
    thread.is_alive.return_value = False
    window = MagicMock()
    if window_error:
        window.create_window.side_effect = RuntimeError("UI unavailable")
    paths = SimpleNamespace(config_dir=tmp_path / "config", logs_dir=tmp_path / "logs")
    starts = []

    def start(host, port):
        starts.append((host, port))
        return server, thread, 3456

    monkeypatch.setitem(sys.modules, "webview", window)
    monkeypatch.setattr(desktop, "runtime_paths", lambda: paths)
    monkeypatch.setattr(desktop, "ensure_runtime_dirs", lambda paths: paths)
    monkeypatch.setattr(desktop, "configure_logging", lambda paths: None)
    monkeypatch.setattr(desktop, "is_windows_standalone", lambda: True)
    monkeypatch.setattr(desktop, "_check_windows_webview", lambda: None)
    monkeypatch.setattr(desktop, "_start_backend", start)
    monkeypatch.setattr(desktop, "_show_startup_error", lambda message: None)
    if window_error:
        with pytest.raises(SystemExit):
            desktop.run_desktop(host="0.0.0.0", port=0)
    else:
        desktop.run_desktop(host="0.0.0.0", port=0)
        window.start.assert_called_once()
    assert starts == [("127.0.0.1", 0)]
    assert server.should_exit
    thread.join.assert_called_once_with(timeout=10)
