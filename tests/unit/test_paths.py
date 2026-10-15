"""Tests for source/frozen runtime path handling."""

import sqlite3
import sys
from pathlib import Path

from camoufox_pm.config import Settings
from camoufox_pm.paths import (
    installation_dir,
    migrate_legacy_data,
    resource_dir,
    runtime_paths,
    user_data_dir,
)


def test_source_paths_keep_relative_database_and_profiles(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    paths = runtime_paths()
    assert paths.db_path == Path("data/profiles.db")
    assert paths.profiles_dir == Path("data/profiles")
    assert resource_dir() == installation_dir()


def test_frozen_paths_use_unicode_local_app_data(monkeypatch, tmp_path):
    local_app_data = tmp_path / "Dữ liệu Người dùng"
    executable = (
        tmp_path / "Program Files" / "CamoufoxProfileManager" / "CamoufoxProfileManager.exe"
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(
        sys, "_MEIPASS", str(tmp_path / "Program Files" / "_internal"), raising=False
    )

    paths = runtime_paths()

    assert user_data_dir() == local_app_data / "CamoufoxProfileManager"
    assert paths.db_path == user_data_dir() / "data" / "profiles.db"
    assert paths.profiles_dir == user_data_dir() / "profiles"
    assert paths.browser_dir == user_data_dir() / "browser"
    assert paths.logs_dir == user_data_dir() / "logs"
    assert str(paths.db_path).startswith(str(local_app_data))
    assert "Program Files" not in str(paths.db_path)
    assert resource_dir() == tmp_path / "Program Files" / "_internal"


def test_frozen_settings_redirect_mutable_install_paths(monkeypatch, tmp_path):
    install = tmp_path / "Program Files" / "CamoufoxProfileManager"
    data_root = tmp_path / "Dữ liệu Người dùng" / "CamoufoxProfileManager"
    monkeypatch.setenv("CPM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(install / "CamoufoxProfileManager.exe"))

    settings = Settings(
        db_path=str(install / "data" / "profiles.db"),
        profiles_dir=str(install / "data" / "profiles"),
        browser_dir=str(install / "browser"),
        _env_file=None,
    )

    assert settings.db_path == str(data_root / "data" / "profiles.db")
    assert settings.profiles_dir == str(data_root / "data" / "profiles")
    assert settings.browser_dir == str(data_root / "browser")


def test_frozen_migration_copies_legacy_data_without_deleting_it(monkeypatch, tmp_path):
    install = tmp_path / "Program Files" / "CamoufoxProfileManager"
    legacy_profiles = install / "data" / "profiles" / "profile_abc123"
    legacy_profiles.mkdir(parents=True)
    (legacy_profiles / "cookies.sqlite").write_bytes(b"cookies")
    (legacy_profiles / "logins.json").write_bytes(b"legacy logins")
    legacy_db = install / "data" / "profiles.db"
    connection = sqlite3.connect(legacy_db)
    connection.execute("CREATE TABLE profiles (id TEXT, storage_path TEXT)")
    connection.execute(
        "INSERT INTO profiles VALUES (?, ?)",
        ("abc123", "data/profiles/profile_abc123"),
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("CPM_DATA_ROOT", str(tmp_path / "Local App Data" / "CamoufoxProfileManager"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(install / "CamoufoxProfileManager.exe"))

    paths = runtime_paths()
    existing_profile = paths.profiles_dir / "profile_abc123"
    existing_profile.mkdir(parents=True)
    (existing_profile / "cookies.sqlite").write_bytes(b"newer user data")
    migrate_legacy_data(paths)

    assert (paths.db_path).exists()
    assert (
        paths.profiles_dir / "profile_abc123" / "cookies.sqlite"
    ).read_bytes() == b"newer user data"
    assert (existing_profile / "logins.json").read_bytes() == b"legacy logins"
    assert legacy_db.exists()
    migrated = (
        sqlite3.connect(paths.db_path)
        .execute("SELECT storage_path FROM profiles WHERE id = 'abc123'")
        .fetchone()[0]
    )
    assert migrated == str(paths.profiles_dir / "profile_abc123")


def test_migration_includes_wal_and_config_without_overwriting_user_data(monkeypatch, tmp_path):
    install = tmp_path / "Program Files" / "CamoufoxProfileManager"
    install.mkdir(parents=True)
    legacy_db = install / "data" / "profiles.db"
    legacy_db.parent.mkdir()
    (install / ".env").write_text("CPM_SECRET_KEY=legacy-value\n", encoding="utf-8")
    source = sqlite3.connect(legacy_db)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("PRAGMA wal_autocheckpoint=0")
    source.execute("CREATE TABLE profiles (id TEXT, storage_path TEXT)")
    source.commit()
    source.execute("INSERT INTO profiles VALUES (?, ?)", ("wal-profile", "profiles/profile_wal"))
    source.commit()

    user_root = tmp_path / "Local App Data" / "CamoufoxProfileManager"
    monkeypatch.setenv("CPM_DATA_ROOT", str(user_root))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(install / "CamoufoxProfileManager.exe"))
    try:
        paths = runtime_paths()
        migrate_legacy_data(paths)
        assert sqlite3.connect(paths.db_path).execute(
            "SELECT storage_path FROM profiles WHERE id = 'wal-profile'"
        ).fetchone()[0] == str(paths.profiles_dir / "profile_wal")
        assert (paths.config_dir / ".env").read_text(encoding="utf-8") == (
            "CPM_SECRET_KEY=legacy-value\n"
        )
        (paths.config_dir / ".env").write_text("new user config\n", encoding="utf-8")
        migrate_legacy_data(paths)
        assert (paths.config_dir / ".env").read_text(encoding="utf-8") == "new user config\n"
        assert legacy_db.exists()
        assert (install / ".env").exists()
    finally:
        source.close()
