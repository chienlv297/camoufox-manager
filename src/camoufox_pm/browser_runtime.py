"""Camoufox installation and runtime status management."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger
from platformdirs import user_cache_dir
from psutil import pid_exists

from camoufox_pm.paths import bundled_browser_dir, is_windows_standalone


def configure_camoufox_runtime(browser_dir: Path) -> None:
    """Point Camoufox's package manager at the application's user cache."""
    if (
        not is_windows_standalone()
        and not os.environ.get("CPM_CAMOUFOX_CACHE_DIR")
        and browser_dir.resolve() == Path(user_cache_dir("camoufox")).resolve()
    ):
        return

    browser_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CPM_CAMOUFOX_CACHE_DIR"] = str(browser_dir)
    try:
        import camoufox.pkgman as pkgman
    except ImportError:
        return

    pkgman.INSTALL_DIR = browser_dir
    try:
        from rich.console import Console

        pkgman.console = Console(file=io.StringIO(), quiet=True)
    except ImportError:
        pass
    try:
        import camoufox.multiversion as multiversion

        multiversion.INSTALL_DIR = browser_dir
        multiversion.BROWSERS_DIR = browser_dir / "browsers"
        multiversion.CONFIG_FILE = browser_dir / "config.json"
        multiversion.REPO_CACHE_FILE = browser_dir / "repo_cache.json"
        multiversion.COMPAT_FLAG = browser_dir / ".0.5_FLAG"
    except ImportError:
        pass

    try:
        import camoufox.addons as addons

        addons.INSTALL_DIR = browser_dir
        addons.ADDONS_DIR = browser_dir / "addons"
    except ImportError:
        pass

    try:
        import camoufox.geolocation as geolocation

        geolocation.GEOIP_DIR = browser_dir / "geoip"
        geolocation.MMDB_DIR = geolocation.GEOIP_DIR / "mmdb"
        geolocation.GEOIP_CONFIG = geolocation.GEOIP_DIR / "config.yml"
    except ImportError:
        pass


class BrowserRuntimeManager:
    """Keep one user-scoped Camoufox installation and expose safe status data."""

    def __init__(self, browser_dir: str | Path, bundled_dir: str | Path | None = None) -> None:
        self.browser_dir = Path(browser_dir)
        configured_bundle = bundled_dir or bundled_browser_dir()
        self.bundled_dir = Path(configured_bundle) if configured_bundle else None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state: dict[str, Any] = {
            "status": "missing",
            "progress": None,
            "message": "Camoufox browser is not installed.",
            "error": None,
            "path": str(self.browser_dir),
        }
        self._install_lock = self.browser_dir.with_name(f".{self.browser_dir.name}.cpm.lock")
        self._download_marker = self.browser_dir / ".cpm-download-complete"

    async def initialize(self, install_if_missing: bool = True) -> None:
        self.browser_dir.mkdir(parents=True, exist_ok=True)
        configure_camoufox_runtime(self.browser_dir)
        if self._is_ready():
            self._set_ready()
            return
        if not install_if_missing:
            return
        self._task = asyncio.create_task(self._background_ensure())

    def snapshot(self) -> dict[str, Any]:
        return dict(self._state)

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def retry(self) -> dict[str, Any]:
        if self._task and not self._task.done():
            return self.snapshot()
        self._state.update(
            status="missing",
            progress=None,
            message="Retrying Camoufox browser download.",
            error=None,
        )
        self._task = asyncio.create_task(self._background_ensure())
        return self.snapshot()

    async def _background_ensure(self) -> None:
        try:
            await self.ensure_ready()
        except Exception:
            logger.exception("Background Camoufox setup failed")

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self._is_ready():
                self._set_ready()
                return
            if self.bundled_dir and self.bundled_dir.is_dir():
                self._state.update(
                    status="downloading",
                    progress=None,
                    message="Installing bundled Camoufox browser into the user cache.",
                    error=None,
                )
                try:
                    await self._run_daemon_worker(self._install_bundle)
                except Exception as exc:  # noqa: BLE001
                    self._set_failed(exc)
                    raise RuntimeError(self._state["message"]) from exc
                if self._is_ready():
                    self._set_ready()
                    return

            self._state.update(
                status="downloading",
                progress=None,
                message="Downloading Camoufox browser. This can take a few minutes.",
                error=None,
            )
            try:
                await self._run_daemon_worker(self._download)
            except Exception as exc:  # noqa: BLE001
                self._set_failed(exc)
                raise RuntimeError(self._state["message"]) from exc
            if not self._is_ready():
                error = RuntimeError("Camoufox download completed without a usable browser")
                self._set_failed(error)
                raise error
            self._set_ready()

    async def _run_daemon_worker(self, operation: Any) -> None:
        completed = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        threading.Thread(
            target=run,
            name="camoufox-install",
            daemon=True,
        ).start()
        while not completed.is_set():
            await asyncio.sleep(0.1)
        if errors:
            raise errors[0]

    def _set_ready(self) -> None:
        self._state.update(
            status="ready",
            progress=100,
            message="Camoufox browser is installed and ready.",
            error=None,
        )

    def _set_failed(self, error: BaseException) -> None:
        logger.error("Camoufox browser setup failed: {}", error)
        self._state.update(
            status="failed",
            progress=None,
            message="Camoufox browser setup failed. Retry from Settings.",
            error=str(error),
        )

    def _install_bundle(self) -> None:
        if not self.bundled_dir or not self.bundled_dir.is_dir():
            return
        marker = self.browser_dir / ".cpm-bundle-complete"
        if marker.exists() and self._is_ready():
            return
        lock_fd = self._acquire_install_lock()
        if lock_fd is None:
            return
        try:
            if self._is_ready():
                return
            compatibility_flag = self.browser_dir / ".0.5_FLAG"
            compatibility_flag.unlink(missing_ok=True)
            shutil.copytree(
                self.bundled_dir,
                self.browser_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".0.5_FLAG", ".cpm-*-complete"),
            )
            compatibility_flag.touch()
            configure_camoufox_runtime(self.browser_dir)
            if self._is_ready():
                marker.write_text("bundled\n", encoding="utf-8")
        finally:
            os.close(lock_fd)
            self._install_lock.unlink(missing_ok=True)

    def _download(self) -> None:
        configure_camoufox_runtime(self.browser_dir)
        lock_fd = self._acquire_install_lock()
        if lock_fd is None:
            return
        try:
            if self._is_ready():
                self._download_marker.write_text("ready\n", encoding="utf-8")
                return
            import camoufox.pkgman as pkgman

            runtime = self

            class ProgressFetcher(pkgman.CamoufoxFetcher):
                @staticmethod
                def download_file(file: Any, url: str) -> Any:
                    def progress(downloaded: int, total: int) -> None:
                        runtime._state["progress"] = (
                            round(downloaded * 100 / total) if total else None
                        )

                    return pkgman.webdl(url, buffer=file, progress_callback=progress)

            ProgressFetcher().install(replace=True)
        finally:
            os.close(lock_fd)
            self._install_lock.unlink(missing_ok=True)

        if self._is_ready():
            self._download_marker.write_text("ready\n", encoding="utf-8")

    def _acquire_install_lock(self) -> int | None:
        self._install_lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 900
        while True:
            try:
                lock_fd = os.open(
                    self._install_lock,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(lock_fd, str(os.getpid()).encode("ascii"))
                return lock_fd
            except FileExistsError:
                if self._is_ready():
                    return None
                try:
                    lock_stat = self._install_lock.stat()
                    lock_content = self._install_lock.read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    continue
                try:
                    lock_pid = int(lock_content)
                except ValueError:
                    lock_pid = None
                age = time.time() - lock_stat.st_mtime
                if (lock_pid is None and age > 30) or (
                    lock_pid is not None and not pid_exists(lock_pid) and age > 5
                ):
                    self._install_lock.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for another Camoufox installation"
                    ) from None
                time.sleep(0.25)

    def _is_ready(self) -> bool:
        try:
            import camoufox.multiversion as multiversion
            from camoufox.pkgman import camoufox_path, launch_path

            if not multiversion.COMPAT_FLAG.exists():
                return False
            path = camoufox_path(download_if_missing=False)
            launch_path(path)
            return True
        except Exception:
            return False
