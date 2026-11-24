"""Settings for usaspending_cscrm_mcp (pydantic-settings)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. AUTH_MODE: databricks_passthrough (now) | logingov (later) | dual."""

    model_config = SettingsConfigDict(env_prefix="CSCRM_", extra="ignore")

    auth_mode: str = "databricks_passthrough"
    host: str = "0.0.0.0"
    port: int = 8000
    mcp_path: str = "/mcp"
    app_name: str = "USASpendingCSCRMv2"

    def resolved_port(self, env_port: str | None) -> int:
        if env_port and env_port.isdigit():
            return int(env_port)
        return self.port
