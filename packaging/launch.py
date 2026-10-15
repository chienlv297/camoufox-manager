"""PyInstaller entry point for the desktop app."""

from __future__ import annotations

import ctypes
import os
import sys


def _show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Camoufox Profile Manager", 0x10)


def _attach_parent_console() -> None:
    if not ctypes.windll.kernel32.AttachConsole(-1):
        return
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stdin = open("CONIN$", encoding="utf-8")


_desktop_launch = os.name == "nt" and getattr(sys, "frozen", False) and len(sys.argv) == 1
if _desktop_launch:
    sys.argv.append("--desktop")
elif os.name == "nt" and getattr(sys, "frozen", False):
    _attach_parent_console()

try:
    from camoufox_pm.paths import (
        configure_logging,
        ensure_runtime_dirs,
        is_windows_standalone,
        migrate_legacy_data,
        runtime_paths,
    )

    _paths = ensure_runtime_dirs(runtime_paths())
    if is_windows_standalone():
        configure_logging(_paths)
        migrate_legacy_data(_paths)
    from camoufox_pm.cli import main

    main()
except Exception as exc:
    try:
        from loguru import logger

        logger.exception("Application startup failed")
    except Exception:
        pass
    if _desktop_launch:
        _show_error(
            "Camoufox Profile Manager could not start.\n\n"
            f"Details: {exc}\n\n"
            f"See the log file at:\n{_paths.logs_dir / 'app.log'}"
        )
    elif getattr(sys, "frozen", False):
        print(f"Camoufox Profile Manager startup failed: {exc}", file=sys.stderr)
    raise
