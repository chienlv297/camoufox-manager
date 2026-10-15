"""Tests for browser installation state transitions."""

import asyncio
import os
import threading
import time

import pytest

from camoufox_pm import browser_runtime
from camoufox_pm.browser_runtime import BrowserRuntimeManager


@pytest.mark.asyncio
async def test_bundle_install_is_idempotent(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "camoufox.exe").write_bytes(b"bundled browser")
    target = tmp_path / "browser"
    manager = BrowserRuntimeManager(target, bundled)
    monkeypatch.setattr(browser_runtime, "configure_camoufox_runtime", lambda directory: None)
    monkeypatch.setattr(manager, "_is_ready", lambda: (target / "camoufox.exe").exists())

    await manager.initialize()
    await manager._task

    assert manager.snapshot()["status"] == "ready"
    assert (target / "camoufox.exe").exists()
    assert (target / ".0.5_FLAG").exists()
    assert (target / ".cpm-bundle-complete").exists()
    (target / "camoufox.exe").write_bytes(b"existing user browser")
    await manager.ensure_ready()
    manager._install_bundle()
    assert (target / "camoufox.exe").read_bytes() == b"existing user browser"


@pytest.mark.asyncio
async def test_download_failure_is_visible_and_retryable(tmp_path, monkeypatch):
    manager = BrowserRuntimeManager(tmp_path / "browser")
    available = False
    monkeypatch.setattr(browser_runtime, "configure_camoufox_runtime", lambda directory: None)
    monkeypatch.setattr(manager, "_is_ready", lambda: available)

    def fail():
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(manager, "_download", fail)
    with pytest.raises(RuntimeError):
        await manager.ensure_ready()
    assert manager.snapshot()["status"] == "failed"
    assert "network unavailable" in manager.snapshot()["error"]

    def succeed():
        nonlocal available
        available = True

    monkeypatch.setattr(manager, "_download", succeed)
    await manager.retry()
    await manager._task
    assert manager.snapshot()["status"] == "ready"
    assert manager.snapshot()["progress"] == 100
    assert manager.snapshot()["error"] is None


@pytest.mark.asyncio
async def test_source_startup_does_not_download(tmp_path, monkeypatch):
    manager = BrowserRuntimeManager(tmp_path / "browser")
    monkeypatch.setattr(browser_runtime, "configure_camoufox_runtime", lambda directory: None)
    monkeypatch.setattr(manager, "_is_ready", lambda: False)
    await manager.initialize(install_if_missing=False)
    assert manager.snapshot()["status"] == "missing"
    assert manager._task is None


def test_camoufox_cache_modules_use_app_browser_dir(tmp_path, monkeypatch):
    import camoufox.addons as addons
    import camoufox.geolocation as geolocation
    import camoufox.multiversion as multiversion
    import camoufox.pkgman as pkgman

    target = tmp_path / "browser"
    monkeypatch.setattr(browser_runtime, "is_windows_standalone", lambda: True)
    monkeypatch.setenv("CPM_CAMOUFOX_CACHE_DIR", str(target))
    for module, names in (
        (pkgman, ("INSTALL_DIR", "console")),
        (
            multiversion,
            ("INSTALL_DIR", "BROWSERS_DIR", "CONFIG_FILE", "REPO_CACHE_FILE", "COMPAT_FLAG"),
        ),
        (addons, ("INSTALL_DIR", "ADDONS_DIR")),
        (geolocation, ("GEOIP_DIR", "MMDB_DIR", "GEOIP_CONFIG")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))

    browser_runtime.configure_camoufox_runtime(target)

    assert pkgman.INSTALL_DIR == target
    assert multiversion.INSTALL_DIR == target
    assert multiversion.BROWSERS_DIR == target / "browsers"
    assert multiversion.CONFIG_FILE == target / "config.json"
    assert addons.ADDONS_DIR == target / "addons"
    assert geolocation.GEOIP_DIR == target / "geoip"


@pytest.mark.asyncio
async def test_managers_share_one_download_lock(tmp_path, monkeypatch):
    import camoufox.pkgman as pkgman

    target = tmp_path / "browser"
    target.mkdir()
    binary = target / "camoufox.exe"
    downloads = []
    monkeypatch.setattr(browser_runtime, "configure_camoufox_runtime", lambda directory: None)

    class Fetcher:
        def install(self, replace=False):
            assert replace
            downloads.append(True)
            time.sleep(0.2)
            binary.write_bytes(b"browser")

    monkeypatch.setattr(pkgman, "CamoufoxFetcher", Fetcher)
    managers = [BrowserRuntimeManager(target), BrowserRuntimeManager(target)]
    for manager in managers:
        monkeypatch.setattr(manager, "_is_ready", binary.exists)
    await asyncio.gather(*(manager.ensure_ready() for manager in managers))
    assert len(downloads) == 1
    assert all(manager.snapshot()["status"] == "ready" for manager in managers)
    assert not managers[0]._install_lock.exists()


def test_abandoned_lock_can_be_retried(tmp_path, monkeypatch):
    manager = BrowserRuntimeManager(tmp_path / "browser")
    manager._install_lock.write_text("12345", encoding="ascii")
    os.utime(manager._install_lock, (time.time() - 60, time.time() - 60))
    monkeypatch.setattr(browser_runtime, "pid_exists", lambda process_id: False)
    monkeypatch.setattr(manager, "_is_ready", lambda: False)
    lock_fd = manager._acquire_install_lock()
    try:
        assert manager._install_lock.read_text(encoding="ascii") == str(os.getpid())
    finally:
        os.close(lock_fd)
        manager._install_lock.unlink()


def test_readiness_probe_does_not_delete_partial_install(tmp_path, monkeypatch):
    import camoufox.multiversion as multiversion
    import camoufox.pkgman as pkgman

    manager = BrowserRuntimeManager(tmp_path / "browser")
    manager.browser_dir.mkdir()
    partial = manager.browser_dir / "partial-download"
    partial.write_bytes(b"partial")
    monkeypatch.setattr(multiversion, "COMPAT_FLAG", manager.browser_dir / ".0.5_FLAG")

    def destructive_probe(**kwargs):
        pytest.fail("Camoufox's destructive legacy probe must not run on an incomplete install")

    monkeypatch.setattr(pkgman, "camoufox_path", destructive_probe)
    assert manager._is_ready() is False
    assert partial.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_a_blocked_download(tmp_path, monkeypatch):
    manager = BrowserRuntimeManager(tmp_path / "browser")
    started = threading.Event()
    finish = threading.Event()
    stopped = threading.Event()
    workers = []
    monkeypatch.setattr(browser_runtime, "configure_camoufox_runtime", lambda directory: None)
    monkeypatch.setattr(manager, "_is_ready", lambda: False)

    def download():
        workers.append(threading.current_thread())
        started.set()
        finish.wait(5)
        stopped.set()

    monkeypatch.setattr(manager, "_download", download)
    try:
        await manager.initialize()
        for _attempt in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        await asyncio.wait_for(manager.shutdown(), timeout=1)
        assert workers[0].daemon
    finally:
        finish.set()
        assert stopped.wait(2)
