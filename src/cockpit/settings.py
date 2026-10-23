"""Konfiguracja: zmienna srodowiskowa ma pierwszenstwo nad plikiem (BRO-98)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COCKPIT_", env_file=".env")

    # W produkcji PostgreSQL. SQLite tylko w testach szkieletu.
    database_url: str = "postgresql+asyncpg://cockpit:cockpit@localhost/cockpit"
    runner_offline_after_s: int = 60   # prog z BRO-109, w konfiguracji a nie w kodzie
    host: str = "127.0.0.1"            # faza testowa; publicznie za Caddym (BRO-76)
    port: int = 8765


settings = Settings()
