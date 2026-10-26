"""Browser session lifecycle: launch, monitor, and close Camoufox instances.

Extracted from ``profile_manager`` so profile CRUD and browser control have
clear, independently testable responsibilities.

Cleanup is driven primarily by Playwright's ``close``/``disconnected`` events, so
a user closing the browser window is detected and the session is torn down. OS
process polling is only a best-effort fallback: with a persistent context the
resolvable pid is Playwright's driver process, not Firefox, so it is not a
reliable window-close signal on its own.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from loguru import logger

from ..config import get_settings

if TYPE_CHECKING:
    # Type-only: importing StorageManager at runtime would be a cycle, since
    # database.py has no need of this module at all.
    from .database import StorageManager

try:
    from camoufox.async_api import AsyncCamoufox

    CAMOUFOX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without camoufox installed
    AsyncCamoufox = None  # type: ignore[assignment, misc]
    CAMOUFOX_AVAILABLE = False

ExitHandler = Callable[[str], Awaitable[None]]


class BrowserLaunchError(RuntimeError):
    """Raised when a Camoufox browser fails to launch."""


def _maximize_and_focus_window(profile_path: str | None) -> None:
    if os.name != "nt" or not profile_path:
        return
    try:
        import ctypes.wintypes

        expected_path = os.path.normcase(str(Path(profile_path).resolve()))
        browser_pids: set[int] = set()
        for process in psutil.process_iter(["cmdline"]):
            try:
                command = process.info.get("cmdline") or []
                profile_index = command.index("-profile")
                actual_path = os.path.normcase(str(Path(command[profile_index + 1]).resolve()))
                if actual_path == expected_path:
                    browser_pids.add(process.pid)
            except (ValueError, IndexError, OSError, psutil.Error):
                continue
        if not browser_pids:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [callback_type, ctypes.wintypes.LPARAM]
        enum_windows.restype = ctypes.wintypes.BOOL
        get_window_process_id = user32.GetWindowThreadProcessId
        get_window_process_id.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        is_window_visible = user32.IsWindowVisible
        is_window_visible.argtypes = [ctypes.wintypes.HWND]
        show_window = user32.ShowWindow
        show_window.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        bring_window_to_top = user32.BringWindowToTop
        bring_window_to_top.argtypes = [ctypes.wintypes.HWND]
        set_foreground_window = user32.SetForegroundWindow
        set_foreground_window.argtypes = [ctypes.wintypes.HWND]
        set_active_window = user32.SetActiveWindow
        set_active_window.argtypes = [ctypes.wintypes.HWND]
        set_focus = user32.SetFocus
        set_focus.argtypes = [ctypes.wintypes.HWND]
        get_foreground_window = user32.GetForegroundWindow
        get_foreground_window.restype = ctypes.wintypes.HWND
        get_current_thread_id = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentThreadId
        get_current_thread_id.restype = ctypes.wintypes.DWORD
        attach_thread_input = user32.AttachThreadInput
        attach_thread_input.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
        ]
        windows: list[int] = []

        @callback_type
        def collect_window(hwnd: int, _parameter: int) -> bool:
            process_id = ctypes.wintypes.DWORD()
            get_window_process_id(hwnd, ctypes.byref(process_id))
            if process_id.value in browser_pids and is_window_visible(hwnd):
                windows.append(hwnd)
            return True

        enum_windows(collect_window, 0)
        if windows:
            window = windows[-1]
            foreground_window = get_foreground_window()
            foreground_process_id = ctypes.wintypes.DWORD()
            foreground_thread = get_window_process_id(
                foreground_window, ctypes.byref(foreground_process_id)
            )
            target_process_id = ctypes.wintypes.DWORD()
            target_thread = get_window_process_id(window, ctypes.byref(target_process_id))
            current_thread = get_current_thread_id()
            attached_threads: list[int] = []
            for thread_id in {foreground_thread, target_thread}:
                if thread_id and thread_id != current_thread and attach_thread_input(
                    current_thread, thread_id, True
                ):
                    attached_threads.append(thread_id)
            show_window(window, 3)
            bring_window_to_top(window)
            set_foreground_window(window)
            set_active_window(window)
            set_focus(window)
            for thread_id in attached_threads:
                attach_thread_input(current_thread, thread_id, False)
    except Exception as exc:
        logger.debug(f"Could not maximize/focus browser window: {exc}")


def _resolve_process_id(obj: Any) -> int | None:
    """Best-effort resolution of a driver/browser OS process id.

    Playwright does not expose this publicly, so this walks known internal
    attributes and returns ``None`` if none are available. It never fabricates a
    placeholder pid. Note: for a persistent context this is the Playwright driver
    process, used only as a forceful-kill fallback.
    """
    candidates = (
        lambda b: b._browser_process.pid,  # noqa: SLF001
        lambda b: b.browser._impl._connection._transport._proc.pid,  # noqa: SLF001
        lambda b: b._impl._connection._transport._proc.pid,  # noqa: SLF001
    )
    for getter in candidates:
        try:
            pid = getter(obj)
            if pid:
                return int(pid)
        except Exception:  # noqa: BLE001 - private attributes vary across versions
            continue
    return None


class BrowserSession:
    """A single running Camoufox browser tied to a profile."""

    def __init__(
        self,
        profile_id: str,
        camoufox: Any,
        process_id: int | None = None,
        browser: Any | None = None,
        profile_path: str | None = None,
    ):
        self.profile_id = profile_id
        self.camoufox = camoufox  # AsyncCamoufox context manager instance
        self.browser = browser
        self.process_id = process_id
        self.profile_path = profile_path
        self.started_at = datetime.now()
        self.monitor_task: asyncio.Task | None = None
        self.on_exit: ExitHandler | None = None
        self._terminated = False

    async def terminate(self) -> None:
        """Close the browser and stop its monitor task. Safe to call twice."""
        if self._terminated:
            return
        self._terminated = True
        logger.info(f"Terminating browser session for profile {self.profile_id}")

        # The monitor can be the caller here (_monitor -> _handle_exit -> terminate),
        # and a task cannot await itself; cancelling is enough in that case.
        monitor = self.monitor_task
        if monitor and not monitor.done():
            monitor.cancel()
            if asyncio.current_task() is not monitor:
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

        if self.camoufox is not None:
            try:
                await self.camoufox.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error closing browser for {self.profile_id}: {exc}")

        # Best-effort: make sure the driver process is gone.
        if self.process_id:
            try:
                process = psutil.Process(self.process_id)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error killing process {self.process_id}: {exc}")

    async def open_urls(self, urls: list[str]) -> None:
        """Open URLs, reusing the initial blank tab when one exists."""
        if not urls:
            return
        if self.browser is None or not hasattr(self.browser, "new_page"):
            raise BrowserLaunchError(f"Browser context for profile {self.profile_id} is unavailable")
        pages = list(getattr(self.browser, "pages", []))
        page = next((candidate for candidate in pages if candidate.url == "about:blank"), None)
        for index, url in enumerate(urls):
            target = page if index == 0 and page is not None else await self.browser.new_page()
            try:
                await target.goto(url, wait_until="commit")
            except Exception:
                if target not in pages:
                    try:
                        await target.close()
                    except Exception:
                        pass
                raise
            if index == 0:
                page = target
        await self.activate()

    async def activate(self) -> None:
        """Focus existing browser page and maximize its native window."""
        if self.browser is not None:
            pages = list(getattr(self.browser, "pages", []))
            page = next((candidate for candidate in reversed(pages) if candidate.url != "about:blank"), None)
            if page is None and pages:
                page = pages[-1]
            if page is not None:
                try:
                    await page.bring_to_front()
                except Exception as exc:
                    logger.debug(f"Could not focus browser page for {self.profile_id}: {exc}")
        await asyncio.to_thread(_maximize_and_focus_window, self.profile_path)

    def info(self) -> dict[str, Any]:
        """Return a serializable summary of this session."""
        return {
            "profile_id": self.profile_id,
            "process_id": self.process_id,
            "started_at": self.started_at.isoformat(),
        }


class BrowserSessionManager:
    """Track and control the browsers currently running."""

    def __init__(self, storage: "StorageManager | None" = None, holder: str | None = None) -> None:
        self.active_sessions: dict[str, BrowserSession] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}
        # Profile id -> how many launches are currently inside camoufox.start().
        # A count rather than a set, because two concurrent launches of one
        # profile both mark it and the first to leave would otherwise clear the
        # mark while the other is still starting.
        self._starting: dict[str, int] = {}
        # The event loop only holds weak references to tasks, so a teardown
        # suspended inside camoufox.__aexit__ could be garbage-collected and take
        # the primary cleanup path with it. Hold a strong reference until done.
        self._exit_tasks: set[asyncio.Task[None]] = set()
        # Lease bookkeeping. Without a storage and a holder id (unit tests, and
        # anything that only watches processes) every lease call below is a
        # no-op and this class behaves exactly as it did before leases existed.
        self._storage = storage
        self._holder = holder
        self._heartbeat_task: asyncio.Task[None] | None = None

    def start_heartbeat(self, interval: float = 30.0) -> None:
        """Begin renewing this process's leases in the background.

        The heartbeat is the difference between "this profile is running" and
        "this profile was running when someone last crashed": while it beats
        the lease stays alive, and when it stops — SIGKILL, a power cut — the
        lease outlives the browser by at most its TTL and then frees itself.
        """
        if self._storage is None or self._holder is None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    async def stop_heartbeat(self) -> None:
        """Stop the renewal loop, and wait for the beat in flight to finish."""
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
        self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self._renew_leases()
            except Exception as exc:  # noqa: BLE001 - one bad beat must not stop the loop
                logger.warning(f"Lease heartbeat failed: {exc}")

    async def _renew_leases(self) -> None:
        """Renew every lease we hold, and close any browser whose lease is gone."""
        if self._storage is None or self._holder is None or not self.active_sessions:
            return
        profile_ids = list(self.active_sessions)
        # The configured TTL, not a constant: a hardcoded value here would
        # shorten a deliberately long lease on every beat and let it expire
        # under a browser that is still running.
        ttl_seconds = get_settings().lease_ttl
        renewed = await self._storage.renew_lease(profile_ids, self._holder, ttl_seconds)
        if renewed == len(profile_ids):
            return
        # Something was taken from us. The bulk update already renewed whatever
        # survived; ask one at a time only to learn which ones those were.
        for profile_id in profile_ids:
            if await self._storage.renew_lease([profile_id], self._holder, ttl_seconds) > 0:
                continue
            logger.warning(
                f"Lost the lease on profile {profile_id} — another instance may be "
                "driving this identity; closing the browser."
            )
            # No attempt to take it back: whoever holds it now is already
            # running the profile, and racing them is the corruption itself.
            await self.close(profile_id)

    def is_running(self, profile_id: str) -> bool:
        """Return whether a browser is currently tracked for the profile."""
        return profile_id in self.active_sessions

    def is_live(self, profile_id: str) -> bool:
        """Whether a browser is running *or* still starting for the profile.

        ``is_running`` is false for the whole time ``camoufox.start()`` is
        being awaited, so a lease released on that answer can be taken away
        from a browser that is coming up. Lease decisions use this; the
        user-facing "is it open" answers stay on ``is_running``.
        """
        return profile_id in self.active_sessions or profile_id in self._starting

    def list_active(self) -> list[dict[str, Any]]:
        """Return summaries of the active sessions.

        A pure read: it used to drop sessions whose driver process had gone,
        which skipped ``terminate()`` and the exit handler and so leaked the
        Camoufox context. Teardown belongs to the close event and, failing that,
        to :meth:`_monitor`; both route through ``_handle_exit``.
        """
        return [session.info() for session in self.active_sessions.values()]

    async def launch(
        self,
        profile_id: str,
        launch_options: dict[str, Any],
        on_exit: ExitHandler | None = None,
        initial_urls: list[str] | None = None,
    ) -> BrowserSession:
        lock = self._launch_locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            return await self._launch_locked(profile_id, launch_options, on_exit, initial_urls)

    async def _launch_locked(
        self,
        profile_id: str,
        launch_options: dict[str, Any],
        on_exit: ExitHandler | None = None,
        initial_urls: list[str] | None = None,
    ) -> BrowserSession:
        """Launch a Camoufox browser and register a monitored session."""
        if not CAMOUFOX_AVAILABLE:
            raise BrowserLaunchError(
                "Camoufox is not installed. Install it with: pip install 'camoufox[geoip]'"
            )
        if profile_id in self.active_sessions:
            session = self.active_sessions[profile_id]
            await session.activate()
            return session

        # Counted before the first await: from here until the session is
        # registered, is_live() says this profile is coming up, and nothing
        # may hand its lease away.
        self._starting[profile_id] = self._starting.get(profile_id, 0) + 1
        try:
            try:
                camoufox = AsyncCamoufox(**launch_options)
                browser = await camoufox.start()
            except Exception as exc:  # noqa: BLE001
                raise BrowserLaunchError(f"Failed to launch browser: {exc}") from exc

            process_id = _resolve_process_id(browser) or _resolve_process_id(camoufox)
            session = BrowserSession(
                profile_id,
                camoufox,
                process_id,
                browser,
                profile_path=launch_options.get("user_data_dir"),
            )
            session.on_exit = on_exit
            self.active_sessions[profile_id] = session
            try:
                await session.open_urls(initial_urls or [])
                if not initial_urls:
                    await session.activate()
            except Exception:
                self.active_sessions.pop(profile_id, None)
                await session.terminate()
                raise
        finally:
            remaining = self._starting.get(profile_id, 1) - 1
            if remaining > 0:
                self._starting[profile_id] = remaining
            else:
                self._starting.pop(profile_id, None)

        # Primary signal: the browser/context closing (e.g. the user closes the window).
        self._register_close_handler(browser, profile_id)
        # Fallback: poll the driver pid, only if we could resolve one.
        if process_id:
            session.monitor_task = asyncio.create_task(self._monitor(profile_id, process_id))

        logger.info(f"Browser launched for profile {profile_id} (pid={process_id})")
        return session

    async def open_urls(self, profile_id: str, urls: list[str]) -> None:
        """Open URLs in an active profile without starting another browser."""
        session = self.active_sessions.get(profile_id)
        if session is None:
            raise BrowserLaunchError(f"Browser for profile {profile_id} is not running")
        await session.open_urls(urls)

    async def activate(self, profile_id: str) -> bool:
        """Bring an existing profile window forward without opening another page."""
        session = self.active_sessions.get(profile_id)
        if session is None:
            return False
        activate = getattr(session, "activate", None)
        if activate is not None:
            await activate()
        return True

    def _register_close_handler(self, browser: Any, profile_id: str) -> None:
        """Wire Playwright close/disconnect events to session cleanup."""
        loop = asyncio.get_running_loop()

        def _on_close(*_: Any) -> None:
            task = loop.create_task(self._handle_exit(profile_id))
            self._exit_tasks.add(task)
            task.add_done_callback(self._exit_tasks.discard)

        for event in ("close", "disconnected"):
            try:
                browser.on(event, _on_close)
            except Exception:  # noqa: BLE001 - Browser vs BrowserContext expose different events
                continue

    async def _handle_exit(self, profile_id: str) -> None:
        """Tear down a session exactly once and notify the exit handler."""
        session = self.active_sessions.pop(profile_id, None)
        if session is None:
            return
        await session.terminate()
        # The browser is gone, so the lease must not outlive it. release_lease
        # is guarded on the holder id, so a lease already taken over by another
        # instance is left exactly where it is.
        await self._release_lease(profile_id)
        if session.on_exit is not None:
            try:
                await session.on_exit(profile_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"on_exit handler failed for {profile_id}: {exc}")

    async def close(self, profile_id: str) -> bool:
        """Close a single browser. Returns ``False`` if it was not running."""
        session = self.active_sessions.pop(profile_id, None)
        if session is None:
            return False
        await session.terminate()
        return True

    async def close_and_release(self, profile_id: str) -> bool:
        """Close a browser and hand its lease back, if we still hold it.

        Separate from :meth:`close` because one caller must not release: when
        the heartbeat finds a lease gone, the browser still has to be closed,
        and clearing the lease then would clear the *new* holder's.
        """
        closed = await self.close(profile_id)
        if closed:
            await self._release_lease(profile_id)
        return closed

    async def _release_lease(self, profile_id: str) -> None:
        """Best-effort lease release. Teardown must not fail on bookkeeping."""
        if self._storage is None or self._holder is None:
            return
        try:
            await self._storage.release_lease(profile_id, self._holder)
        except Exception as exc:  # noqa: BLE001 - a stuck release must not block cleanup
            logger.warning(f"Could not release the lease on {profile_id}: {exc}")

    async def close_all(self) -> int:
        """Close every active browser and return how many were closed."""
        count = 0
        for profile_id in list(self.active_sessions.keys()):
            if await self.close_and_release(profile_id):
                count += 1
        return count

    async def _monitor(self, profile_id: str, process_id: int) -> None:
        """Fallback watchdog: clean up if the driver process disappears."""
        while psutil.pid_exists(process_id):
            await asyncio.sleep(10)
        await self._handle_exit(profile_id)
