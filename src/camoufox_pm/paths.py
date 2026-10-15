"""Runtime paths for source and frozen desktop builds."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from platformdirs import user_cache_dir
from platformdirs import user_data_dir as platform_user_data_dir

APP_NAME = "CamoufoxProfileManager"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_windows_standalone() -> bool:
    return is_frozen() and sys.platform == "win32"


def installation_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", installation_dir())).resolve()
    return installation_dir()


def user_data_dir() -> Path:
    configured = os.environ.get("CPM_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local_app_data:
        return Path(local_app_data) / APP_NAME
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / APP_NAME
    return Path(platform_user_data_dir(APP_NAME, appauthor=False))


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    profiles_dir: Path
    browser_dir: Path
    logs_dir: Path
    config_dir: Path
    db_path: Path


def runtime_paths() -> RuntimePaths:
    if is_windows_standalone():
        root = user_data_dir()
        data_dir = root / "data"
        profiles_dir = root / "profiles"
        browser_dir = root / "browser"
        logs_dir = root / "logs"
        config_dir = root / "config"
        return RuntimePaths(
            data_dir=data_dir,
            profiles_dir=profiles_dir,
            browser_dir=browser_dir,
            logs_dir=logs_dir,
            config_dir=config_dir,
            db_path=data_dir / "profiles.db",
        )

    data_dir = Path("data")
    return RuntimePaths(
        data_dir=data_dir,
        profiles_dir=data_dir / "profiles",
        browser_dir=Path(user_cache_dir("camoufox")),
        logs_dir=user_data_dir() / "logs",
        config_dir=user_data_dir() / "config",
        db_path=data_dir / "profiles.db",
    )


def ensure_runtime_dirs(paths: RuntimePaths | None = None) -> RuntimePaths:
    resolved = paths or runtime_paths()
    for directory in (
        resolved.data_dir,
        resolved.profiles_dir,
        resolved.browser_dir,
        resolved.logs_dir,
        resolved.config_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return resolved


def bundled_browser_dir() -> Path | None:
    configured = os.environ.get("CPM_BUNDLED_CAMOUFOX_DIR")
    candidate = Path(configured) if configured else resource_dir() / "camoufox-bundle"
    return candidate if candidate.is_dir() else None


def _copy_missing_file(source: str | Path, target: str | Path) -> str:
    if not Path(target).exists():
        return str(shutil.copy2(source, target))
    return str(target)


def _copy_tree_contents(source: Path, target: Path, *, exclude: set[str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in exclude:
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True, copy_function=_copy_missing_file)
        else:
            _copy_missing_file(item, destination)


def _map_legacy_storage_path(value: str, legacy_root: Path, profiles_dir: Path) -> str | None:
    normalized = value.replace("/", "\\")
    prefixes = ("data\\profiles\\", "profiles\\")
    for prefix in prefixes:
        if normalized.lower().startswith(prefix.lower()):
            mapped = profiles_dir.joinpath(*normalized[len(prefix) :].split("\\"))
            if mapped.resolve().is_relative_to(profiles_dir.resolve()):
                return str(mapped)
            return None

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = legacy_root / candidate
    try:
        relative = candidate.resolve().relative_to((legacy_root / "data" / "profiles").resolve())
    except ValueError:
        return None
    return str(profiles_dir / relative)


def _rewrite_legacy_storage_paths(db_path: Path, legacy_root: Path, profiles_dir: Path) -> None:
    if not db_path.exists():
        return
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT id, storage_path FROM profiles").fetchall()
        for profile_id, storage_path in rows:
            if not storage_path:
                continue
            mapped = _map_legacy_storage_path(storage_path, legacy_root, profiles_dir)
            if mapped:
                connection.execute(
                    "UPDATE profiles SET storage_path = ? WHERE id = ?",
                    (mapped, profile_id),
                )
        connection.commit()
    except sqlite3.OperationalError:
        connection.rollback()
    finally:
        connection.close()


def migrate_legacy_data(paths: RuntimePaths | None = None) -> None:
    if not is_windows_standalone():
        return

    resolved = ensure_runtime_dirs(paths)
    legacy_root = installation_dir()
    legacy_config = legacy_root / ".env"
    user_config = resolved.config_dir / ".env"
    if legacy_config.is_file() and not user_config.exists():
        shutil.copy2(legacy_config, user_config)
    if resolved.db_path.exists():
        return

    legacy_data = legacy_root / "data"
    legacy_db = legacy_data / "profiles.db"
    legacy_profiles = legacy_data / "profiles"
    if not legacy_db.exists() and not legacy_profiles.exists():
        return

    try:
        if legacy_data.is_dir():
            _copy_tree_contents(
                legacy_data,
                resolved.data_dir,
                exclude={"profiles", "profiles.db", "profiles.db-wal", "profiles.db-shm"},
            )
        if legacy_profiles.is_dir():
            shutil.copytree(
                legacy_profiles,
                resolved.profiles_dir,
                dirs_exist_ok=True,
                copy_function=_copy_missing_file,
            )
        if legacy_db.exists():
            staging_db = resolved.db_path.with_suffix(".migrating")
            source = sqlite3.connect(f"{legacy_db.as_uri()}?mode=ro", uri=True)
            target = sqlite3.connect(staging_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            _rewrite_legacy_storage_paths(staging_db, legacy_root, resolved.profiles_dir)
            staging_db.replace(resolved.db_path)
        logger.info("Migrated legacy application data to {}", user_data_dir())
    except Exception:
        logger.exception("Legacy application data migration failed")
        raise


def configure_logging(paths: RuntimePaths | None = None) -> None:
    resolved = ensure_runtime_dirs(paths)
    logger.remove()
    logger.add(
        resolved.logs_dir / "app.log",
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )

    class UvicornLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logger.opt(exception=record.exc_info).log(record.levelname, record.getMessage())

    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [UvicornLogHandler()]
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True
