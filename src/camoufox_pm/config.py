"""Application settings loaded from environment variables (prefix ``CPM_``)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from camoufox_pm.paths import (
    installation_dir,
    is_windows_standalone,
    runtime_paths,
    user_data_dir,
)

_ENV_FILE = str(runtime_paths().config_dir / ".env") if is_windows_standalone() else ".env"


class Settings(BaseSettings):
    """Runtime configuration. Values come from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(env_prefix="CPM_", env_file=_ENV_FILE, extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    api_key: str | None = None
    db_path: str = Field(default_factory=lambda: str(runtime_paths().db_path))
    profiles_dir: str = Field(default_factory=lambda: str(runtime_paths().profiles_dir))
    browser_dir: str = Field(default_factory=lambda: str(runtime_paths().browser_dir))
    secret_key: str | None = None
    webui_dir: str | None = None
    # Lifetime of a login session. Sessions are stored server-side, so shortening
    # this takes effect for existing sessions on their next request.
    session_ttl_hours: int = 168
    # Force the Secure flag on the session cookie. The flag is set automatically
    # when the request itself arrived over HTTPS, but behind a TLS-terminating
    # proxy the app sees plain HTTP — set this there.
    secure_cookies: bool = False
    # How long a profile lease survives without a heartbeat, in seconds. The
    # heartbeat renews every 30s while a browser is open, so this is really the
    # window in which an instance that died without releasing anything keeps
    # its profiles locked to the rest of the fleet.
    lease_ttl: int = 120

    @field_validator("db_path", "profiles_dir", "browser_dir")
    @classmethod
    def _keep_mutable_paths_out_of_installation(cls, value: str) -> str:
        if not is_windows_standalone():
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = user_data_dir() / path
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(installation_dir().resolve())
        except ValueError:
            return str(resolved)
        return str(user_data_dir() / relative)

    @property
    def effective_profiles_dir(self) -> str:
        if "profiles_dir" in self.model_fields_set:
            return self.profiles_dir
        if "db_path" in self.model_fields_set and not is_windows_standalone():
            return str(Path(self.db_path).parent / "profiles")
        return self.profiles_dir

    @field_validator("lease_ttl")
    @classmethod
    def _lease_ttl_outlives_the_heartbeat(cls, value: int) -> int:
        # Two heartbeat intervals. At or below one, a lease expires while the
        # instance holding it is still renewing: the browser keeps running and
        # another instance is free to open the same profile, which is the exact
        # failure the lease exists to prevent. Zero or negative would disable
        # mutual exclusion outright, and silently.
        if value < 60:
            raise ValueError(
                "CPM_LEASE_TTL must be at least 60 seconds (the heartbeat renews every 30s)"
            )
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
